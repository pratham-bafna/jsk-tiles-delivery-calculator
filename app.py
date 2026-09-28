import os
import requests

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv


# ============================================================
# LOAD ENVIRONMENT VARIABLES
# ============================================================

load_dotenv()


# ============================================================
# CONFIGURATION
# ============================================================

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

ORIGIN_LATITUDE = float(
    os.getenv("ORIGIN_LATITUDE", "11.3410")
)

ORIGIN_LONGITUDE = float(
    os.getenv("ORIGIN_LONGITUDE", "77.7172")
)

ORIGIN_NAME = os.getenv(
    "ORIGIN_NAME",
    "JSK Tiles World"
)


# ============================================================
# PLACEHOLDER PRICING
#
# IMPORTANT:
# These are NOT real JSK delivery rates.
# Replace them once the actual transporter pricing
# is available.
# ============================================================

PLACEHOLDER_RATE_PER_KG_KM = 0.50


# ============================================================
# FASTAPI APPLICATION
# ============================================================

app = FastAPI(
    title="JSK Tiles World Delivery Calculator",
    description="PIN, distance, weight and delivery charge calculator",
    version="3.0.0"
)


# ============================================================
# REQUEST MODEL
# ============================================================

class DistanceRequest(BaseModel):

    pincode: str = Field(
        ...,
        description="6-digit Indian PIN code",
        examples=["641001"]
    )

    weight_kg: float = Field(
        ...,
        gt=0,
        description="Total shipment weight in kilograms",
        examples=[500]
    )


# ============================================================
# GOOGLE GEOCODING
#
# PIN → latitude / longitude
# ============================================================

def geocode_pincode(pincode: str):

    if not GOOGLE_MAPS_API_KEY:

        raise HTTPException(
            status_code=500,
            detail="GOOGLE_MAPS_API_KEY is not configured"
        )


    url = (
        "https://maps.googleapis.com/"
        "maps/api/geocode/json"
    )


    params = {
        "address": f"{pincode}, India",
        "key": GOOGLE_MAPS_API_KEY
    }


    try:

        response = requests.get(
            url,
            params=params,
            timeout=20
        )

        print()
        print("=" * 60)
        print("GOOGLE GEOCODING API")
        print("=" * 60)
        print("HTTP status:", response.status_code)
        print("Response:", response.text)
        print("=" * 60)
        print()

        response.raise_for_status()

        data = response.json()


    except requests.RequestException as exc:

        raise HTTPException(
            status_code=502,
            detail={
                "message":
                    "Google Geocoding API request failed",

                "error":
                    str(exc),

                "google_response":
                    response.text
                    if "response" in locals()
                    else None
            }
        )


    # --------------------------------------------------------
    # Check Google response
    # --------------------------------------------------------

    status = data.get("status")


    if status != "OK":

        raise HTTPException(
            status_code=404,
            detail={
                "message":
                    "Google could not find this PIN code",

                "pincode":
                    pincode,

                "google_status":
                    status,

                "google_error":
                    data.get("error_message")
            }
        )


    results = data.get("results", [])


    if not results:

        raise HTTPException(
            status_code=404,
            detail={
                "message":
                    "No location found for this PIN code",

                "pincode":
                    pincode
            }
        )


    # --------------------------------------------------------
    # Use first Google result
    # --------------------------------------------------------

    result = results[0]

    geometry = result.get(
        "geometry",
        {}
    )

    location = geometry.get(
        "location"
    )


    if not location:

        raise HTTPException(
            status_code=502,
            detail={
                "message":
                    "Google returned a result without coordinates",

                "google_result":
                    result
            }
        )


    return {

        "formatted_address":
            result.get(
                "formatted_address"
            ),

        "latitude":
            location.get("lat"),

        "longitude":
            location.get("lng"),

        "location_type":
            geometry.get(
                "location_type"
            ),

        "place_id":
            result.get(
                "place_id"
            )
    }


# ============================================================
# GOOGLE ROUTES API
#
# Warehouse coordinates → customer PIN coordinates
# ============================================================

def calculate_google_route(
    destination_lat: float,
    destination_lng: float
):

    if not GOOGLE_MAPS_API_KEY:

        raise HTTPException(
            status_code=500,
            detail="GOOGLE_MAPS_API_KEY is not configured"
        )


    url = (
        "https://routes.googleapis.com/"
        "directions/v2:computeRoutes"
    )


    # --------------------------------------------------------
    # Origin
    # --------------------------------------------------------

    origin = {

        "location": {

            "latLng": {

                "latitude":
                    ORIGIN_LATITUDE,

                "longitude":
                    ORIGIN_LONGITUDE
            }
        }
    }


    # --------------------------------------------------------
    # Destination
    # --------------------------------------------------------

    destination = {

        "location": {

            "latLng": {

                "latitude":
                    destination_lat,

                "longitude":
                    destination_lng
            }
        }
    }


    # --------------------------------------------------------
    # Google Routes request
    # --------------------------------------------------------

    payload = {

        "origin":
            origin,

        "destination":
            destination,

        "travelMode":
            "DRIVE",

        "routingPreference":
            "TRAFFIC_UNAWARE",

        "computeAlternativeRoutes":
            False,

        "languageCode":
            "en-IN",

        "units":
            "METRIC"
    }


    headers = {

        "Content-Type":
            "application/json",

        "X-Goog-Api-Key":
            GOOGLE_MAPS_API_KEY,

        "X-Goog-FieldMask":
            "routes.distanceMeters,routes.duration"
    }


    try:

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=20
        )

        print()
        print("=" * 60)
        print("GOOGLE ROUTES API")
        print("=" * 60)
        print("HTTP status:", response.status_code)
        print("Response:", response.text)
        print("=" * 60)
        print()

        response.raise_for_status()

        data = response.json()


    except requests.RequestException as exc:

        raise HTTPException(
            status_code=502,
            detail={
                "message":
                    "Google Routes API request failed",

                "error":
                    str(exc),

                "google_http_status":
                    response.status_code
                    if "response" in locals()
                    else None,

                "google_response":
                    response.text
                    if "response" in locals()
                    else None
            }
        )


    # --------------------------------------------------------
    # Check routes
    # --------------------------------------------------------

    routes = data.get("routes")


    if not routes:

        raise HTTPException(
            status_code=502,
            detail={
                "message":
                    "Google returned no routes",

                "google_response":
                    data
            }
        )


    route = routes[0]


    # --------------------------------------------------------
    # Get distance
    # --------------------------------------------------------

    distance_meters = route.get(
        "distanceMeters"
    )


    if distance_meters is None:

        raise HTTPException(
            status_code=502,
            detail={
                "message":
                    "Google returned a route but no distance",

                "route":
                    route
            }
        )


    return {

        "distance_km":
            round(
                distance_meters / 1000,
                1
            ),

        "distance_meters":
            distance_meters,

        "duration":
            route.get("duration")
    }


# ============================================================
# PLACEHOLDER DELIVERY CALCULATION
# ============================================================

def calculate_delivery_charge(
    distance_km: float,
    weight_kg: float
):

    # --------------------------------------------------------
    # PLACEHOLDER FORMULA
    #
    # charge = distance × weight × rate
    #
    # Example:
    #
    # 100 km × 500 kg × ₹0.50
    # = ₹25,000
    #
    # THIS IS ONLY A PLACEHOLDER.
    # --------------------------------------------------------

    charge = (
        distance_km
        * weight_kg
        * PLACEHOLDER_RATE_PER_KG_KM
    )


    return {

        "rate_per_kg_km":
            PLACEHOLDER_RATE_PER_KG_KM,

        "calculation":
            (
                f"{distance_km} km × "
                f"{weight_kg} kg × "
                f"₹{PLACEHOLDER_RATE_PER_KG_KM}/kg/km"
            ),

        "delivery_charge":
            round(charge, 2),

        "currency":
            "INR",

        "is_placeholder":
            True
    }


# ============================================================
# HOME
# ============================================================

@app.get("/")
def home():

    return {

        "application":
            "JSK Tiles World Delivery Calculator",

        "version":
            "3.0.0",

        "status":
            "running",

        "origin":
            {
                "name":
                    ORIGIN_NAME,

                "latitude":
                    ORIGIN_LATITUDE,

                "longitude":
                    ORIGIN_LONGITUDE
            },

        "placeholder_pricing":
            {
                "rate_per_kg_km":
                    PLACEHOLDER_RATE_PER_KG_KM,

                "enabled":
                    True
            },

        "endpoints":
            {
                "health":
                    "/health",

                "calculate":
                    "POST /distance",

                "documentation":
                    "/docs"
            }
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
def health():

    return {

        "status":
            "healthy",

        "google_maps_api":
            "configured"
            if GOOGLE_MAPS_API_KEY
            else "not configured",

        "origin":
            {
                "name":
                    ORIGIN_NAME,

                "latitude":
                    ORIGIN_LATITUDE,

                "longitude":
                    ORIGIN_LONGITUDE
            },

        "pricing":
            {
                "rate":
                    PLACEHOLDER_RATE_PER_KG_KM,

                "placeholder":
                    True
            }
    }


# ============================================================
# MAIN CALCULATION ENDPOINT
# ============================================================

@app.post("/distance")
def distance(request: DistanceRequest):

    # --------------------------------------------------------
    # Clean PIN
    # --------------------------------------------------------

    pincode = request.pincode.strip()


    # --------------------------------------------------------
    # Validate PIN
    # --------------------------------------------------------

    if not pincode.isdigit():

        raise HTTPException(
            status_code=400,
            detail="PIN code must contain only numbers"
        )


    if len(pincode) != 6:

        raise HTTPException(
            status_code=400,
            detail="PIN code must contain exactly 6 digits"
        )


    # --------------------------------------------------------
    # Validate weight
    # --------------------------------------------------------

    if request.weight_kg <= 0:

        raise HTTPException(
            status_code=400,
            detail="Weight must be greater than 0 kg"
        )


    # --------------------------------------------------------
    # STEP 1
    #
    # PIN → coordinates
    # --------------------------------------------------------

    location = geocode_pincode(
        pincode
    )


    # --------------------------------------------------------
    # STEP 2
    #
    # Coordinates → road distance
    # --------------------------------------------------------

    route = calculate_google_route(

        destination_lat=
            location["latitude"],

        destination_lng=
            location["longitude"]
    )


    # --------------------------------------------------------
    # STEP 3
    #
    # Distance + weight → delivery charge
    # --------------------------------------------------------

    pricing = calculate_delivery_charge(

        distance_km=
            route["distance_km"],

        weight_kg=
            request.weight_kg
    )


    # --------------------------------------------------------
    # FINAL RESPONSE
    # --------------------------------------------------------

    return {

        "status":
            "success",

        "pincode":
            pincode,

        "shipment":
            {
                "weight_kg":
                    request.weight_kg
            },

        "origin":
            {
                "name":
                    ORIGIN_NAME,

                "latitude":
                    ORIGIN_LATITUDE,

                "longitude":
                    ORIGIN_LONGITUDE
            },

        "destination":
            {
                "formatted_address":
                    location["formatted_address"],

                "latitude":
                    location["latitude"],

                "longitude":
                    location["longitude"],

                "location_type":
                    location["location_type"]
            },

        "distance":
            {
                "kilometers":
                    route["distance_km"],

                "meters":
                    route["distance_meters"],

                "estimated_duration":
                    route["duration"]
            },

        "delivery":
            pricing
    }