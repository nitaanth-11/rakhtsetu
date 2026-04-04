"""
server.py
─────────
RakhtSetu Flask backend.

Endpoints
─────────
GET  /api/banks              → all blood banks + ambulances from CSV
GET  /api/feed               → live request feed (SSE)
POST /api/request            → post a blood request (location encrypted)
GET  /api/request/<id>       → get single request metadata (no raw coords)
POST /api/register           → register a new donor
GET  /api/donor/<id>/donations → get donor's donation history
POST /api/connect            → donor connects; decrypts location, returns A* route + records donation (100-day cooldown)
GET  /api/route/<req_id>     → fetch cached A* route for a connected pair
POST /api/chat/<room>        → post a chat message
GET  /api/chat/<room>        → get chat history
GET  /api/stream/<room>      → SSE stream for chat room

Install deps:
    pip install flask flask-cors cryptography

Run:
    python server.py
"""

import json
import math
import os
import queue
import time
import uuid
from datetime import datetime
from threading import Lock

from cryptography.fernet import Fernet
from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS

from data_loader import (
    astar_route,
    load_ambulances,
    load_blood_banks,
    nearest_ambulances,
    nearest_blood_banks,
)
import pathlib

_FRONTEND_DIR = str(pathlib.Path(__file__).resolve().parent.parent / "frontend")

app = Flask(__name__, static_folder=_FRONTEND_DIR, static_url_path="")
CORS(app)


@app.route("/")
def serve_index():
    return app.send_static_file("register.html")

# ─────────────────────────────────────────────────────────────────
#  Encryption — one symmetric key per server process.
#  In production: store in env / HSM, rotate regularly.
# ─────────────────────────────────────────────────────────────────

_FERNET_KEY = os.environ.get("RAKHTSETU_KEY", Fernet.generate_key().decode())
_fernet = Fernet(_FERNET_KEY.encode() if isinstance(_FERNET_KEY, str) else _FERNET_KEY)


def encrypt_coords(lat: float, lng: float) -> str:
    payload = json.dumps({"lat": lat, "lng": lng}).encode()
    return _fernet.encrypt(payload).decode()


def decrypt_coords(token: str) -> dict:
    raw = _fernet.decrypt(token.encode())
    return json.loads(raw)


# ─────────────────────────────────────────────────────────────────
#  In-memory stores  (replace with Redis / DB in production)
# ─────────────────────────────────────────────────────────────────

_requests:  dict[str, dict] = {}       # req_id → request record
_routes:    dict[str, dict] = {}       # req_id → A* result (post-connect)
_chats:     dict[str, list] = {}       # room_id → [msg, ...]
_donors:    dict[str, dict] = {}       # donor_id → donor profile
_donations: list[dict]      = []       # list of donation records
_chat_lock = Lock()

MIN_DONATION_GAP_DAYS = 100            # minimum days between two donations

# SSE subscriber queues
_feed_subscribers:  list[queue.Queue] = []
_chat_subscribers:  dict[str, list[queue.Queue]] = {}

BLOOD_COMPATIBLE = {
    "O-":  ["O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"],
    "O+":  ["O+", "A+", "B+", "AB+"],
    "A-":  ["A-", "A+", "AB-", "AB+"],
    "A+":  ["A+", "AB+"],
    "B-":  ["B-", "B+", "AB-", "AB+"],
    "B+":  ["B+", "AB+"],
    "AB-": ["AB-", "AB+"],
    "AB+": ["AB+"],
}


# ─────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────

def _push_feed_event(data: dict):
    dead = []
    for q in _feed_subscribers:
        try:
            q.put_nowait(data)
        except queue.Full:
            dead.append(q)
    for d in dead:
        _feed_subscribers.remove(d)


def _push_chat_event(room: str, data: dict):
    if room not in _chat_subscribers:
        return
    dead = []
    for q in _chat_subscribers[room]:
        try:
            q.put_nowait(data)
        except queue.Full:
            dead.append(q)
    for d in dead:
        _chat_subscribers[room].remove(d)


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ─────────────────────────────────────────────────────────────────
#  Static data endpoints
# ─────────────────────────────────────────────────────────────────

@app.route("/api/banks")
def api_banks():
    """Return all blood banks + ambulances for the map."""
    return jsonify({
        "blood_banks": load_blood_banks(),
        "ambulances":  load_ambulances(),
    })


@app.route("/api/ambulances")
def api_ambulances():
    """
    Return nearest ambulances enriched with distance, cost_per_km, rating, total_cost, eta.
    Query params:
        lat, lng (float) — pickup location (required)
        drop_lat, drop_lng (float) — dropoff location (optional, for trip cost)
        n (int, default 5)
    """
    try:
        lat = float(request.args["lat"])
        lng = float(request.args["lng"])
    except (KeyError, ValueError):
        return jsonify({"error": "lat and lng query params required"}), 400

    n = int(request.args.get("n", 5))
    units = nearest_ambulances(lat, lng, n=n)

    # Optional dropoff — compute trip distance & cost
    drop_lat = request.args.get("drop_lat")
    drop_lng = request.args.get("drop_lng")
    has_dropoff = drop_lat and drop_lng

    AVG_SPEED_KMH = 25  # avg ambulance speed in Mumbai traffic

    for u in units:
        # ETA from ambulance base → pickup (minutes)
        eta_min = round((u["distance_km"] / AVG_SPEED_KMH) * 60)
        u["eta_min"] = max(eta_min, 2)  # minimum 2 min

        if has_dropoff:
            from data_loader import haversine
            trip_m = haversine(lat, lng, float(drop_lat), float(drop_lng))
            trip_km = round(trip_m / 1000, 2)
            u["trip_km"] = trip_km
            u["total_cost"] = round(u["cost_per_km"] * trip_km, 2)
            # ETA for full trip: ambulance→pickup + pickup→dropoff
            trip_eta = round((trip_km / AVG_SPEED_KMH) * 60)
            u["trip_eta_min"] = max(trip_eta, 3)

    return jsonify({"ambulances": units})


# ─────────────────────────────────────────────────────────────────
#  Donor registration
# ─────────────────────────────────────────────────────────────────

@app.route("/api/register", methods=["POST"])
def register_donor():
    """
    Body (JSON):
        name, age, blood_type, phone, password, latitude, longitude
    Returns: donor_id + profile.
    """
    body = request.get_json(force=True)

    required = {"name", "blood_type", "phone", "password"}
    if not required.issubset(body):
        return jsonify({"error": "Missing required fields: name, blood_type, phone, password"}), 400

    # Check if phone already registered
    for d in _donors.values():
        if d["phone"] == body["phone"]:
            return jsonify({"error": "Phone number already registered", "donor_id": d["id"]}), 409

    donor_id = str(uuid.uuid4())[:8].upper()
    donor = {
        "id":         donor_id,
        "name":       body["name"],
        "age":        int(body.get("age", 0)),
        "blood_type": body["blood_type"],
        "phone":      body["phone"],
        "password":   body["password"],   # In production: hash this!
        "latitude":   float(body.get("latitude", 0)),
        "longitude":  float(body.get("longitude", 0)),
        "available":  True,
        "registered_at": datetime.utcnow().isoformat() + "Z",
    }
    _donors[donor_id] = donor

    # Return without password
    safe = {k: v for k, v in donor.items() if k != "password"}
    return jsonify({"donor_id": donor_id, "donor": safe, "message": "Registration successful"}), 201


@app.route("/api/donor/<donor_id>/donations")
def get_donor_donations(donor_id):
    """Return donation history for a specific donor."""
    donor_id = donor_id.upper()
    if donor_id not in _donors:
        return jsonify({"error": "Donor not found"}), 404
    history = [d for d in _donations if d["donor_id"] == donor_id]
    history.sort(key=lambda x: x["date"], reverse=True)
    return jsonify({"donations": history, "count": len(history)})


# ─────────────────────────────────────────────────────────────────
#  Blood request lifecycle
# ─────────────────────────────────────────────────────────────────

@app.route("/api/request", methods=["POST"])
def post_request():
    """
    Body (JSON):
        name, blood_type, urgency, lat, lng, address_label (optional)
    """
    body = request.get_json(force=True)

    required = {"name", "blood_type", "urgency", "lat", "lng"}
    if not required.issubset(body):
        return jsonify({"error": "Missing fields"}), 400

    lat  = float(body["lat"])
    lng  = float(body["lng"])

    req_id      = str(uuid.uuid4())[:8].upper()
    encrypted   = encrypt_coords(lat, lng)
    timestamp   = datetime.utcnow().isoformat() + "Z"

    # Nearest support — returned to requester for display
    nearby_banks = nearest_blood_banks(lat, lng, n=3)
    nearby_ambs  = nearest_ambulances(lat, lng,  n=2)

    record = {
        "id":            req_id,
        "name":          body["name"],
        "blood_type":    body["blood_type"],
        "urgency":       body["urgency"],
        "address_label": body.get("address_label", "Undisclosed"),
        "timestamp":     timestamp,
        "status":        "open",           # open | connected | fulfilled
        "encrypted_loc": encrypted,        # only returned to matched donor
        "nearby_banks":  nearby_banks,
        "nearby_ambs":   nearby_ambs,
        "compatible":    BLOOD_COMPATIBLE.get(body["blood_type"], []),
        "room_id":       req_id,           # chat room = same as request id
    }

    _requests[req_id] = record

    # Push sanitised event to live feed (NO raw coords)
    feed_event = {k: v for k, v in record.items()
                  if k not in {"encrypted_loc", "nearby_banks", "nearby_ambs"}}
    _push_feed_event({"event": "new_request", "data": feed_event})

    return jsonify({
        "req_id":        req_id,
        "room_id":       req_id,
        "nearby_banks":  nearby_banks,
        "nearby_ambs":   nearby_ambs,
        "message":       "Request posted. Location encrypted and hidden until donor connects.",
    }), 201


@app.route("/api/request/<req_id>")
def get_request(req_id):
    rec = _requests.get(req_id.upper())
    if not rec:
        return jsonify({"error": "Not found"}), 404
    # Strip encrypted coord from public view
    safe = {k: v for k, v in rec.items() if k != "encrypted_loc"}
    return jsonify(safe)


@app.route("/api/feed")
def api_feed():
    """
    Return paginated snapshot of open requests.
    Query params: page (default 1), per_page (default 20), blood_type (optional filter)
    """
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))
    bt       = request.args.get("blood_type", "").strip()

    all_reqs = [r for r in _requests.values() if r["status"] == "open"]
    if bt:
        all_reqs = [r for r in all_reqs if bt in r["compatible"] or r["blood_type"] == bt]

    all_reqs.sort(key=lambda x: x["timestamp"], reverse=True)

    total  = len(all_reqs)
    start  = (page - 1) * per_page
    items  = all_reqs[start: start + per_page]

    # Strip encrypted coords
    safe_items = [{k: v for k, v in r.items() if k != "encrypted_loc"} for r in items]
    return jsonify({"total": total, "page": page, "items": safe_items})


# ─────────────────────────────────────────────────────────────────
#  SSE – live request feed
# ─────────────────────────────────────────────────────────────────

@app.route("/api/stream/feed")
def stream_feed():
    """Server-Sent Events: push new blood requests to dashboard."""
    q = queue.Queue(maxsize=50)
    _feed_subscribers.append(q)

    def generate():
        yield _sse({"event": "connected", "data": {"msg": "RakhtSetu feed live"}})
        while True:
            try:
                data = q.get(timeout=25)
                yield _sse(data)
            except queue.Empty:
                yield ": heartbeat\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────────
#  Donor connects → decrypt coords → run A*
# ─────────────────────────────────────────────────────────────────

@app.route("/api/connect", methods=["POST"])
def connect_donor():
    """
    Body: { req_id, donor_name, donor_lat, donor_lng, donor_blood_type, donor_id (optional) }
    Returns: decrypted recipient coords + A* route (donor → recipient).
    Also records a donation if donor_id is provided, with 100-day cooldown check.
    """
    body = request.get_json(force=True)
    req_id = body.get("req_id", "").upper()

    rec = _requests.get(req_id)
    if not rec:
        return jsonify({"error": "Request not found"}), 404
    if rec["status"] != "open":
        return jsonify({"error": "Request already fulfilled or connected"}), 409

    # ── 100-day cooldown check ──
    donor_id = body.get("donor_id", "").upper() if body.get("donor_id") else ""
    if donor_id:
        donor_history = [d for d in _donations if d["donor_id"] == donor_id]
        if donor_history:
            donor_history.sort(key=lambda x: x["date"], reverse=True)
            last = datetime.fromisoformat(donor_history[0]["date"].replace("Z", "+00:00"))
            now = datetime.utcnow().replace(tzinfo=last.tzinfo)
            days_since = (now - last).days
            if days_since < MIN_DONATION_GAP_DAYS:
                days_left = MIN_DONATION_GAP_DAYS - days_since
                return jsonify({
                    "error": f"You must wait at least {MIN_DONATION_GAP_DAYS} days between donations. "
                             f"You can donate again in {days_left} day(s).",
                    "days_remaining": days_left,
                }), 429

    donor_lat = float(body["donor_lat"])
    donor_lng = float(body["donor_lng"])

    # Decrypt recipient location
    try:
        recipient_coords = decrypt_coords(rec["encrypted_loc"])
    except Exception:
        return jsonify({"error": "Location decryption failed"}), 500

    r_lat = recipient_coords["lat"]
    r_lng = recipient_coords["lng"]

    # Run A* from donor → recipient
    route = astar_route(donor_lat, donor_lng, r_lat, r_lng)

    # Cache route
    _routes[req_id] = {
        "donor":     {"lat": donor_lat, "lng": donor_lng, "name": body.get("donor_name", "Donor")},
        "recipient": {"lat": r_lat, "lng": r_lng},
        "route":     route,
        "connected_at": datetime.utcnow().isoformat() + "Z",
    }

    # Update request status
    _requests[req_id]["status"] = "connected"

    # ── Record donation naturally ──
    donation_record = {
        "id":          str(uuid.uuid4())[:8].upper(),
        "donor_id":    donor_id,
        "donor_name":  body.get("donor_name", "Donor"),
        "req_id":      req_id,
        "recipient":   rec["name"],
        "blood_type":  rec["blood_type"],
        "location":    rec.get("address_label", "Undisclosed"),
        "date":        datetime.utcnow().isoformat() + "Z",
        "status":      "ok",
        "units":       450,
        "type":        "Whole Blood",
    }
    _donations.append(donation_record)

    # Notify feed
    _push_feed_event({
        "event": "request_connected",
        "data":  {"req_id": req_id, "donor_name": body.get("donor_name", "Donor")},
    })

    # Notify chat room
    _push_chat_event(req_id, {
        "event": "system",
        "msg":   f"✅ {body.get('donor_name','Donor')} has connected. Route revealed.",
        "ts":    datetime.utcnow().isoformat() + "Z",
    })

    return jsonify({
        "req_id":           req_id,
        "recipient_coords": {"lat": r_lat, "lng": r_lng},
        "route":            route,
        "room_id":          req_id,
        "donation":         donation_record,
    })


@app.route("/api/route/<req_id>")
def get_route(req_id):
    """Return cached A* route for an already-connected pair."""
    data = _routes.get(req_id.upper())
    if not data:
        return jsonify({"error": "Route not yet computed or request not connected"}), 404
    return jsonify(data)


# ─────────────────────────────────────────────────────────────────
#  Chat
# ─────────────────────────────────────────────────────────────────

@app.route("/api/chat/<room>", methods=["GET"])
def get_chat(room):
    return jsonify(_chats.get(room, []))


@app.route("/api/chat/<room>", methods=["POST"])
def post_chat(room):
    body = request.get_json(force=True)
    msg = {
        "id":     str(uuid.uuid4())[:6],
        "sender": body.get("sender", "Anonymous"),
        "role":   body.get("role", "donor"),    # "donor" | "recipient"
        "text":   body.get("text", ""),
        "ts":     datetime.utcnow().isoformat() + "Z",
    }
    with _chat_lock:
        _chats.setdefault(room, []).append(msg)

    _push_chat_event(room, {"event": "message", "data": msg})
    return jsonify(msg), 201


@app.route("/api/stream/chat/<room>")
def stream_chat(room):
    """SSE stream for a specific chat room."""
    q = queue.Queue(maxsize=100)
    _chat_subscribers.setdefault(room, []).append(q)

    def generate():
        history = _chats.get(room, [])
        for msg in history:
            yield _sse({"event": "message", "data": msg})
        yield _sse({"event": "connected", "data": {"room": room}})
        while True:
            try:
                data = q.get(timeout=25)
                yield _sse(data)
            except queue.Empty:
                yield ": heartbeat\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────────
#  Seed demo data (dev only)
# ─────────────────────────────────────────────────────────────────

def _seed_demo():
    demo = [
        {"name": "Priya Sharma",   "blood_type": "O-",  "urgency": "Critical",
         "lat": 19.0189, "lng": 72.8441, "address_label": "Dadar, Mumbai"},
        {"name": "Arjun Mehta",    "blood_type": "B+",  "urgency": "Urgent",
         "lat": 19.1026, "lng": 72.8359, "address_label": "Vile Parle, Mumbai"},
        {"name": "Nisha Patel",    "blood_type": "AB-", "urgency": "Normal",
         "lat": 19.0505, "lng": 72.8265, "address_label": "Bandra, Mumbai"},
        {"name": "Rahul Desai",    "blood_type": "A+",  "urgency": "Urgent",
         "lat": 19.0655, "lng": 72.8601, "address_label": "BKC, Mumbai"},
        {"name": "Kavya Nair",     "blood_type": "O+",  "urgency": "Critical",
         "lat": 19.1179, "lng": 72.9102, "address_label": "Powai, Mumbai"},
    ]
    for d in demo:
        enc = encrypt_coords(d["lat"], d["lng"])
        req_id = str(uuid.uuid4())[:8].upper()
        nearby_banks = nearest_blood_banks(d["lat"], d["lng"], n=3)
        nearby_ambs  = nearest_ambulances(d["lat"], d["lng"],  n=2)
        _requests[req_id] = {
            "id":            req_id,
            "name":          d["name"],
            "blood_type":    d["blood_type"],
            "urgency":       d["urgency"],
            "address_label": d["address_label"],
            "timestamp":     datetime.utcnow().isoformat() + "Z",
            "status":        "open",
            "encrypted_loc": enc,
            "nearby_banks":  nearby_banks,
            "nearby_ambs":   nearby_ambs,
            "compatible":    BLOOD_COMPATIBLE.get(d["blood_type"], []),
            "room_id":       req_id,
        }


# ─────────────────────────────────────────────────────────────────
#  Run
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _seed_demo()
    print("🩸 RakhtSetu server running → http://localhost:5000")
    print(f"   Fernet key (save this): {_FERNET_KEY}")
    app.run(debug=True, threaded=True, port=5000)