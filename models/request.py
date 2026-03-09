from pydantic import BaseModel

class BloodRequest(BaseModel):
    patient_name: str
    blood_type: str
    units: int
    latitude: float
    longitude: float
    urgency: str