from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from models.donor import Donor
from models.request import BloodRequest

from data.storage import donors_by_blood, location_grid
from services.geo_service import get_grid_cell
from services.matching_engine import find_matching_donors

app = FastAPI()

# Enable CORS (frontend communication)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Root API

@app.get("/")
def home():
    return {"message": "RakhtSetu API Running"}


# Register Donor

@app.post("/register_donor")
def register_donor(donor: Donor):

    # Store donor by blood type (Hash Map)
    donors_by_blood[donor.blood_type].append(donor)

    # Store donor location (GIS grid)
    cell = get_grid_cell(donor.latitude, donor.longitude)

    if cell not in location_grid:
        location_grid[cell] = []

    location_grid[cell].append(donor)

    return {"status": "Donor Registered"}


# Login API

@app.post("/login")
def login(data: dict):

    phone = data["phone"]
    password = data["password"]

    for blood_group in donors_by_blood.values():
        for donor in blood_group:

            if donor.phone == phone:

                if donor.password == password:
                    return {
                        "status": "success",
                        "donor": donor
                    }

                else:
                    return {
                        "status": "error",
                        "message": "Wrong password"
                    }

    return {
        "status": "error",
        "message": "User not registered"
    }


# Request Blood

@app.post("/request_blood")
def request_blood(request: BloodRequest):

    matches = find_matching_donors(request)

    return {
        "patient": request.patient_name,
        "matches_found": len(matches),
        "nearest_donors": matches
    }