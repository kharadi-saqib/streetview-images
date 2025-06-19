import pandas as pd
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter

# Load file
df = pd.read_excel("output_conv_lat_long-Leica-2018-DEC-05_MusafahReSurveyM.xlsx")

# Reverse geocoder setup
geolocator = Nominatim(user_agent="utm_reverse_geocode")
reverse = RateLimiter(geolocator.reverse, min_delay_seconds=1)

# Process first 5 points
locations = []
for idx, row in df.head(5).iterrows():
    lat, lon = row["override_latitude"], row["override_longitude"]
    loc = reverse((lat, lon), language='en')
    if loc:
        addr = loc.raw.get('address', {})
        city = addr.get('city') or addr.get('town') or addr.get('village') or addr.get('hamlet')
        state = addr.get('state')
        country = addr.get('country')
    else:
        city = state = country = None
    locations.append((lat, lon, city, state, country))

# Show results
for lat, lon, city, state, country in locations:
    print(f"{lat:.6f}, {lon:.6f} → {city}, {state}, {country}")

