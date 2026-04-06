"""
data_loader.py
--------------
Loads blood banks and ambulance/mobile van data from CSV files.
Exposes clean Python dicts for use by server.py.
"""

import csv
import os
import math
from typing import List, Dict, Tuple, Optional
from functools import lru_cache

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BLOOD_BANKS_CSV  = os.path.join(BASE_DIR, "blood_banks.csv")

# Prefer backend/ambulances.csv; fall back to root ambulances_updated.csv
_ambu_candidates = [
    os.path.join(BASE_DIR, "ambulances.csv"),
    os.path.join(BASE_DIR, "..", "ambulances_updated.csv"),
    os.path.join(BASE_DIR, "..", "dataset", "ambulances.csv"),
]
AMBULANCES_CSV = next((p for p in _ambu_candidates if os.path.exists(p)), _ambu_candidates[0])


# ─────────────────────────────────────────────────────────────────
#  CSV Loaders
# ─────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def load_blood_banks() -> List[Dict]:
    """Return list of blood bank dicts from CSV."""
    banks = []
    with open(BLOOD_BANKS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            banks.append({
                "name":    row["name"].strip(),
                "lat":     float(row["lat"]),
                "lng":     float(row["lng"]),
                "address": row["address"].strip(),
                "phone":   row["phone"].strip(),
                "type":    row["type"].strip(),
            })
    return banks


@lru_cache(maxsize=1)
def load_ambulances() -> List[Dict]:
    """Return list of ambulance / mobile-van dicts from CSV."""
    units = []
    with open(AMBULANCES_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("name", "").strip():
                continue  # skip blank trailing rows
            units.append({
                "name":        row["name"].strip(),
                "lat":         float(row["lat"]),
                "lng":         float(row["lng"]),
                "area":        row["area"].strip(),
                "phone":       row["phone"].strip(),
                "type":        row["type"].strip(),      # "ambulance" | "mobile_van"
                "capacity":    int(row["capacity"]),
                "cost_per_km": float(row.get("cost_per_km", 30)),
                "rating":      float(row.get("rating", 4.0)),
            })
    return units


# ─────────────────────────────────────────────────────────────────
#  Haversine distance  (metres)
# ─────────────────────────────────────────────────────────────────

def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres between two (lat, lng) points."""
    R = 6_371_000  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi       = math.radians(lat2 - lat1)
    dlam       = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ─────────────────────────────────────────────────────────────────
#  Nearest-N finders
# ─────────────────────────────────────────────────────────────────

def nearest_blood_banks(lat: float, lng: float, n: int = 5) -> List[Dict]:
    """Return the N closest blood banks to (lat, lng), sorted by distance."""
    banks = load_blood_banks()
    for b in banks:
        b["distance_m"] = haversine(lat, lng, b["lat"], b["lng"])
    return sorted(banks, key=lambda x: x["distance_m"])[:n]


def nearest_ambulances(lat: float, lng: float, n: int = 3,
                       unit_type: Optional[str] = None) -> List[Dict]:
    """
    Return the N closest ambulances / mobile vans enriched with
    distance_km and total_cost (cost_per_km × distance_km).
    Pass unit_type='ambulance' or 'mobile_van' to filter.
    """
    units = load_ambulances()
    if unit_type:
        units = [u for u in units if u["type"] == unit_type]
    for u in units:
        dist_m = haversine(lat, lng, u["lat"], u["lng"])
        dist_km = round(dist_m / 1000, 2)
        u["distance_m"]  = round(dist_m)
        u["distance_km"] = dist_km
        u["total_cost"]  = round(u["cost_per_km"] * dist_km, 2)
    return sorted(units, key=lambda x: x["distance_m"])[:n]


# ─────────────────────────────────────────────────────────────────
#  A* Waypoint Grid  —  shortest path donor → recipient
# ─────────────────────────────────────────────────────────────────

"""
Mumbai road network approximation
──────────────────────────────────
A true A* needs a graph (OSM data).  We approximate with a synthetic
grid of ~200 waypoints seeded from all blood-bank coordinates (already
spread across Mumbai) plus a regular lat/lng grid.  Edge weight = haversine.
This gives realistic approximate routes without requiring OSM / networkx.

For production: replace build_graph() with an OSM-based graph loader.
"""

GRID_STEP = 0.015          # ~1.5 km spacing
LAT_RANGE = (18.89, 19.27)
LNG_RANGE = (72.79, 72.97)


@lru_cache(maxsize=1)
def _build_node_list() -> List[Tuple[float, float]]:
    """Combine blood bank coords with a uniform grid."""
    nodes: List[Tuple[float, float]] = []

    # Grid nodes
    lat = LAT_RANGE[0]
    while lat <= LAT_RANGE[1]:
        lng = LNG_RANGE[0]
        while lng <= LNG_RANGE[1]:
            nodes.append((round(lat, 4), round(lng, 4)))
            lng += GRID_STEP
        lat += GRID_STEP

    # Blood bank nodes
    for b in load_blood_banks():
        nodes.append((b["lat"], b["lng"]))

    # De-duplicate (snap to 4 dp)
    return list({(round(n[0], 4), round(n[1], 4)) for n in nodes})


def _snap(lat: float, lng: float,
          nodes: List[Tuple[float, float]]) -> Tuple[float, float]:
    """Return the nearest node to an arbitrary point."""
    return min(nodes, key=lambda n: haversine(lat, lng, n[0], n[1]))


def astar_route(
    start_lat: float, start_lng: float,
    end_lat:   float, end_lng:   float,
    max_edge_m: float = 3500,         # two nodes are neighbours if ≤ this apart
) -> Dict:
    """
    A* shortest path between two coordinates.

    Returns:
        {
          "waypoints": [(lat, lng), ...],   # ordered path
          "distance_m": float,              # total path length
          "hops": int
        }
    """
    import heapq

    nodes = _build_node_list()
    start = _snap(start_lat, start_lng, nodes)
    goal  = _snap(end_lat,   end_lng,   nodes)

    if start == goal:
        return {
            "waypoints":  [start, goal],
            "distance_m": haversine(start_lat, start_lng, end_lat, end_lng),
            "hops":       1,
        }

    # Build adjacency on-the-fly (memory-efficient for ~200 nodes)
    def neighbours(node):
        return [
            (n, haversine(node[0], node[1], n[0], n[1]))
            for n in nodes
            if 0 < haversine(node[0], node[1], n[0], n[1]) <= max_edge_m
        ]

    def h(node):  # heuristic: straight-line to goal
        return haversine(node[0], node[1], goal[0], goal[1])

    open_heap = []          # (f, g, node, path)
    heapq.heappush(open_heap, (h(start), 0.0, start, [start]))
    visited: Dict[Tuple, float] = {}

    while open_heap:
        f, g, current, path = heapq.heappop(open_heap)

        if current in visited and visited[current] <= g:
            continue
        visited[current] = g

        if current == goal:
            # Prepend true start / append true end
            full_path = [(start_lat, start_lng)] + path[1:] + [(end_lat, end_lng)]
            total_dist = sum(
                haversine(full_path[i][0], full_path[i][1],
                          full_path[i+1][0], full_path[i+1][1])
                for i in range(len(full_path) - 1)
            )
            return {
                "waypoints":  full_path,
                "distance_m": round(total_dist),
                "hops":       len(full_path) - 1,
            }

        for neighbour, cost in neighbours(current):
            new_g = g + cost
            if neighbour not in visited or visited[neighbour] > new_g:
                heapq.heappush(open_heap,
                               (new_g + h(neighbour), new_g,
                                neighbour, path + [neighbour]))

    # Fallback: straight line if no path found
    return {
        "waypoints":  [(start_lat, start_lng), (end_lat, end_lng)],
        "distance_m": round(haversine(start_lat, start_lng, end_lat, end_lng)),
        "hops":       1,
    }


# ─────────────────────────────────────────────────────────────────
#  Quick self-test
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Blood Banks (nearest 3 to Dadar) ===")
    for b in nearest_blood_banks(19.0189, 72.8441, n=3):
        print(f"  {b['name']}  —  {b['distance_m']:.0f} m")

    print("\n=== Ambulances (nearest 2) ===")
    for u in nearest_ambulances(19.0189, 72.8441, n=2):
        print(f"  {u['name']}  ({u['type']})  —  {u['distance_m']:.0f} m")

    print("\n=== A* Route: Dadar → Colaba ===")
    result = astar_route(19.0189, 72.8441, 18.9055, 72.8155)
    print(f"  Distance : {result['distance_m']} m")
    print(f"  Hops     : {result['hops']}")
    print(f"  First 4 waypoints: {result['waypoints'][:4]}")