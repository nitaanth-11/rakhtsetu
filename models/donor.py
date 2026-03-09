from pydantic import BaseModel

class Donor(BaseModel):

    name: str
    age: int
    blood_type: str
    phone: str
    password: str
    latitude: float
    longitude: float
    available: bool