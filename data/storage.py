# Hash map for donors by blood type
donors_by_blood = {
    "O+": [],
    "A+": [],
    "B+": [],
    "AB+": [],
    "O-": [],
    "A-": [],
    "B-": [],
    "AB-": []
}

# Grid based GIS hashing
location_grid = {}

# Request storage
requests = {}

# Blood compatibility graph
blood_compatibility = {
    "O-": ["O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"],
    "O+": ["O+", "A+", "B+", "AB+"],
    "A-": ["A-", "A+", "AB-", "AB+"],
    "A+": ["A+", "AB+"],
    "B-": ["B-", "B+", "AB-", "AB+"],
    "B+": ["B+", "AB+"],
    "AB-": ["AB-", "AB+"],
    "AB+": ["AB+"]
}