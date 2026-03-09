import math

def get_grid_cell(lat, lon):

    grid_size = 0.1

    lat_cell = int(lat / grid_size)
    lon_cell = int(lon / grid_size)

    return f"{lat_cell}_{lon_cell}"


def calculate_distance(lat1, lon1, lat2, lon2):

    R = 6371

    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)

    a = (
        math.sin(d_lat/2)**2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon/2)**2
    )

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    return R * c