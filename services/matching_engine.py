from data.storage import donors_by_blood, blood_compatibility
from services.geo_service import calculate_distance

def find_matching_donors(blood_type, req_lat, req_lon):

    compatible_types = blood_compatibility[blood_type]

    matches = []

    for bt in compatible_types:

        donors = donors_by_blood.get(bt, [])

        for donor in donors:

            if donor.available:

                distance = calculate_distance(
                    req_lat,
                    req_lon,
                    donor.latitude,
                    donor.longitude
                )

                matches.append({
                    "name": donor.name,
                    "phone": donor.phone,
                    "blood_type": donor.blood_type,
                    "distance_km": round(distance,2)
                })

    matches.sort(key=lambda x: x["distance_km"])

    return matches