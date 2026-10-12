import os
import requests

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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
SQUARESPACE_API_KEY = os.getenv("SQUARESPACE_API_KEY")

ORIGIN_LATITUDE = float(
    os.getenv("ORIGIN_LATITUDE", "11.3410")
)

ORIGIN_LONGITUDE = float(
    os.getenv("ORIGIN_LONGITUDE", "77.7172")
)

ORIGIN_NAME = os.getenv(
    "ORIGIN_NAME",
    "Delivery Warehouse"
)


# ============================================================
# CONFIGURABLE PRICING AND SERVICEABILITY
#
# Defaults preserve the existing test behaviour. The default
# pricing formula is a PLACEHOLDER, not a real transport rate.
# Configure these values in Railway Variables when the business
# supplies verified pricing and delivery-coverage information.
# ============================================================

PRICING_MODE = os.getenv(
    "PRICING_MODE", "placeholder_distance_weight"
).strip().lower()

PLACEHOLDER_RATE_PER_KG_KM = float(
    os.getenv("PLACEHOLDER_RATE_PER_KG_KM", "0.50")
)
BASE_CHARGE_INR = float(os.getenv("BASE_CHARGE_INR", "0"))
RATE_PER_KM_INR = float(os.getenv("RATE_PER_KM_INR", "0"))
RATE_PER_KG_INR = float(os.getenv("RATE_PER_KG_INR", "0"))
RATE_PER_KG_KM_INR = float(os.getenv("RATE_PER_KG_KM_INR", "0"))
MINIMUM_CHARGE_INR = float(os.getenv("MINIMUM_CHARGE_INR", "0"))

# Supported modes:
# - unrestricted: do not block any PIN; coverage is NOT verified
# - allowlist: only PINs in SERVICEABLE_PINCODES are accepted
# - denylist: PINs in BLOCKED_PINCODES are rejected
SERVICEABILITY_MODE = os.getenv(
    "SERVICEABILITY_MODE", "unrestricted"
).strip().lower()


def parse_pincode_set(env_name: str) -> set[str]:
    """Parse comma-separated six-digit PIN codes from an environment variable."""
    raw_value = os.getenv(env_name, "")
    return {
        item.strip()
        for item in raw_value.split(",")
        if item.strip()
    }


SERVICEABLE_PINCODES = parse_pincode_set("SERVICEABLE_PINCODES")
BLOCKED_PINCODES = parse_pincode_set("BLOCKED_PINCODES")
MANUAL_CONFIRMATION_PINCODES = parse_pincode_set(
    "MANUAL_CONFIRMATION_PINCODES"
)


# ============================================================
# FASTAPI APPLICATION
# ============================================================

app = FastAPI(
    title="E-Commerce Delivery Charge Calculator",
    description=(
        "API for calculating delivery charges using "
        "PIN code, driving distance, and shipment weight."
    ),
    version="4.0.0"
)


# ============================================================
# CORS
#
# This allows a web frontend, including an e-commerce
# storefront, to communicate with this API.
#
# For development we allow all origins.
# This should be restricted to the actual website domain
# before production deployment.
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://jsktilesworld.squarespace.com"
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# REQUEST MODEL
# ============================================================

class DeliveryRequest(BaseModel):

    pincode: str = Field(
        ...,
        description="6-digit Indian PIN code",
        examples=["641001"]
    )

    weight_kg: float = Field(
        ...,
        gt=0,
        description="Total shipment weight in kilograms",
        examples=[154]
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
                "message": "Google Geocoding API request failed",
                "error": str(exc),
                "google_response": (
                    response.text
                    if "response" in locals()
                    else None
                )
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
                "message": "Google could not find this PIN code",
                "pincode": pincode,
                "google_status": status,
                "google_error": data.get("error_message")
            }
        )

    results = data.get("results", [])

    if not results:

        raise HTTPException(
            status_code=404,
            detail={
                "message": "No location found for this PIN code",
                "pincode": pincode
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
                "message": (
                    "Google returned a result "
                    "without coordinates"
                ),
                "google_result": result
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
                "message": "Google Routes API request failed",
                "error": str(exc),
                "google_http_status": (
                    response.status_code
                    if "response" in locals()
                    else None
                ),
                "google_response": (
                    response.text
                    if "response" in locals()
                    else None
                )
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
                "message": "Google returned no routes",
                "google_response": data
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
                "message": (
                    "Google returned a route "
                    "but no distance"
                ),
                "route": route
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
# DELIVERY PRICING
#
# This is currently a placeholder.
# The actual pricing model will be added later.
# ============================================================

def calculate_delivery_charge(
    distance_km: float,
    weight_kg: float
):
    """Calculate a charge using the configured pricing mode.

    All non-placeholder modes are configurable frameworks only; do not
    enable them until the business has confirmed the relevant rates.
    """
    if PRICING_MODE == "placeholder_distance_weight":
        raw_charge = (
            distance_km * weight_kg * PLACEHOLDER_RATE_PER_KG_KM
        )
        calculation = (
            f"{distance_km} km × {weight_kg} kg × "
            f"₹{PLACEHOLDER_RATE_PER_KG_KM}/kg/km (PLACEHOLDER)"
        )
        rate_per_kg_km = PLACEHOLDER_RATE_PER_KG_KM
        is_placeholder = True

    elif PRICING_MODE == "flat_rate":
        raw_charge = BASE_CHARGE_INR
        calculation = f"Flat charge: ₹{BASE_CHARGE_INR}"
        rate_per_kg_km = None
        is_placeholder = False

    elif PRICING_MODE == "distance_only":
        raw_charge = BASE_CHARGE_INR + distance_km * RATE_PER_KM_INR
        calculation = (
            f"₹{BASE_CHARGE_INR} base + {distance_km} km × "
            f"₹{RATE_PER_KM_INR}/km"
        )
        rate_per_kg_km = None
        is_placeholder = False

    elif PRICING_MODE == "weight_only":
        raw_charge = BASE_CHARGE_INR + weight_kg * RATE_PER_KG_INR
        calculation = (
            f"₹{BASE_CHARGE_INR} base + {weight_kg} kg × "
            f"₹{RATE_PER_KG_INR}/kg"
        )
        rate_per_kg_km = None
        is_placeholder = False

    elif PRICING_MODE == "hybrid":
        raw_charge = (
            BASE_CHARGE_INR
            + distance_km * RATE_PER_KM_INR
            + weight_kg * RATE_PER_KG_INR
            + distance_km * weight_kg * RATE_PER_KG_KM_INR
        )
        calculation = (
            f"₹{BASE_CHARGE_INR} base + {distance_km} km × "
            f"₹{RATE_PER_KM_INR}/km + {weight_kg} kg × "
            f"₹{RATE_PER_KG_INR}/kg + distance-weight component"
        )
        rate_per_kg_km = RATE_PER_KG_KM_INR
        is_placeholder = False

    else:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Unsupported PRICING_MODE configuration",
                "configured_mode": PRICING_MODE,
                "supported_modes": [
                    "placeholder_distance_weight",
                    "flat_rate",
                    "distance_only",
                    "weight_only",
                    "hybrid",
                ],
            },
        )

    charge = max(raw_charge, MINIMUM_CHARGE_INR)

    if MINIMUM_CHARGE_INR > 0 and charge > raw_charge:
        calculation += f"; minimum charge ₹{MINIMUM_CHARGE_INR} applied"

    return {
        "delivery_charge": round(charge, 2),
        "currency": "INR",
        "is_placeholder": is_placeholder,
        "pricing_mode": PRICING_MODE,
        "rate_per_kg_km": rate_per_kg_km,
        "calculation": calculation,
    }


def check_pincode_serviceability(pincode: str) -> dict:
    """Apply configured PIN rules; unrestricted mode does not verify coverage."""
    if pincode in MANUAL_CONFIRMATION_PINCODES:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MANUAL_DELIVERY_CONFIRMATION_REQUIRED",
                "pincode": pincode,
                "message": (
                    "Delivery to this PIN code requires confirmation. "
                    "Please contact the business before placing the order."
                ),
            },
        )

    if SERVICEABILITY_MODE == "unrestricted":
        return {
            "status": "not_checked",
            "mode": SERVICEABILITY_MODE,
            "message": (
                "Delivery coverage has not been verified. "
                "This mode is for development/testing only."
            ),
        }

    if SERVICEABILITY_MODE == "allowlist":
        if not SERVICEABLE_PINCODES:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Serviceability is configured as allowlist, but "
                    "SERVICEABLE_PINCODES is empty."
                ),
            )
        if pincode not in SERVICEABLE_PINCODES:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "PIN_NOT_SERVICEABLE",
                    "pincode": pincode,
                    "message": "Delivery is not currently available to this PIN code.",
                },
            )
        return {"status": "allowed", "mode": SERVICEABILITY_MODE}

    if SERVICEABILITY_MODE == "denylist":
        if pincode in BLOCKED_PINCODES:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "PIN_NOT_SERVICEABLE",
                    "pincode": pincode,
                    "message": "Delivery is not currently available to this PIN code.",
                },
            )
        return {"status": "not_blocked", "mode": SERVICEABILITY_MODE}

    raise HTTPException(
        status_code=500,
        detail={
            "message": "Unsupported SERVICEABILITY_MODE configuration",
            "configured_mode": SERVICEABILITY_MODE,
            "supported_modes": ["unrestricted", "allowlist", "denylist"],
        },
    )


# ============================================================
# HOME
# ============================================================

@app.get("/")
def home():

    return {

        "application":
            "E-Commerce Delivery Charge Calculator",

        "version":
            "4.0.0",

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

        "pricing": {
            "mode": PRICING_MODE,
            "rate_per_kg_km": PLACEHOLDER_RATE_PER_KG_KM
                if PRICING_MODE == "placeholder_distance_weight" else None,
            "placeholder": PRICING_MODE == "placeholder_distance_weight",
            "minimum_charge_inr": MINIMUM_CHARGE_INR,
        },
        "serviceability": {
            "mode": SERVICEABILITY_MODE,
            "coverage_rules_configured": (
                (SERVICEABILITY_MODE == "allowlist" and bool(SERVICEABLE_PINCODES))
                or (SERVICEABILITY_MODE == "denylist" and bool(BLOCKED_PINCODES))
            ),
            "manual_confirmation_pin_count": len(MANUAL_CONFIRMATION_PINCODES),
        },

        "endpoints":
            {
                "health":
                    "/health",

                "calculate_delivery":
                    "POST /calculate-delivery",

                "calculate_cart_weight":
                    "POST /calculate-cart-weight",

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

        "pricing": {
            "mode": PRICING_MODE,
            "placeholder": PRICING_MODE == "placeholder_distance_weight",
            "minimum_charge_inr": MINIMUM_CHARGE_INR,
        },
        "serviceability": {
            "mode": SERVICEABILITY_MODE,
            "coverage_rules_configured": (
                (SERVICEABILITY_MODE == "allowlist" and bool(SERVICEABLE_PINCODES))
                or (SERVICEABILITY_MODE == "denylist" and bool(BLOCKED_PINCODES))
            ),
        }
    }

# ============================================================
# CART WEIGHT CALCULATION
# ============================================================

@app.post("/calculate-cart-weight")
def calculate_cart_weight(items: list[dict]):

    if not SQUARESPACE_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="SQUARESPACE_API_KEY is not configured"
        )

    total_weight_kg = 0.0
    item_results = []

    headers = {
        "Authorization": f"Bearer {SQUARESPACE_API_KEY}",
        "User-Agent": "E-Commerce Delivery Charge Calculator/1.0",
        "Content-Type": "application/json",
    }

    for item in items:

        product_id = item.get("productId")
        variant_id = item.get("variantId")
        quantity = item.get("quantity", 1)

        if not product_id or not variant_id:
            raise HTTPException(
                status_code=400,
                detail="Each item must contain productId and variantId"
            )

        url = (
            "https://api.squarespace.com/"
            "v2/commerce/products/"
            + product_id
        )

        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=15
            )

            response.raise_for_status()

            product_data = response.json()

        except requests.RequestException as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Failed to retrieve product from Squarespace",
                    "product_id": product_id,
                    "error": str(exc),
                }
            )

        products = product_data.get("products", [])

        if not products:
            raise HTTPException(
                status_code=404,
                detail=f"Product {product_id} was not found"
            )

        product = products[0]

        variants = product.get("variants", [])

        matching_variant = None

        for variant in variants:
            if variant.get("id") == variant_id:
                matching_variant = variant
                break

        if not matching_variant:
            raise HTTPException(
                status_code=404,
                detail={
                    "message": "Variant was not found",
                    "product_id": product_id,
                    "variant_id": variant_id,
                }
            )

        shipping_measurements = matching_variant.get(
            "shippingMeasurements",
            {}
        )

        weight_data = shipping_measurements.get("weight")

        if not weight_data:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "No shipping weight found for variant",
                    "product_id": product_id,
                    "variant_id": variant_id,
                }
            )

        weight = float(weight_data.get("value", 0))
        unit = weight_data.get("unit")

        if unit != "KILOGRAM":
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Unsupported weight unit",
                    "unit": unit,
                    "product_id": product_id,
                    "variant_id": variant_id,
                }
            )

        item_total_weight = weight * quantity

        total_weight_kg += item_total_weight

        item_results.append({
            "product_id": product_id,
            "variant_id": variant_id,
            "product_name": product.get("name"),
            "quantity": quantity,
            "weight_per_unit_kg": weight,
            "total_weight_kg": item_total_weight,
        })

    return {
        "total_weight_kg": round(total_weight_kg, 3),
        "items": item_results,
    }


# ============================================================
# DELIVERY CALCULATION ENDPOINT
#
# POST /calculate-delivery
#
# Example request:
#
# {
#     "pincode": "641001",
#     "weight_kg": 154
# }
#
# ============================================================

@app.post("/calculate-delivery")
def calculate_delivery(request: DeliveryRequest):

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
    # STEP 0: SERVICEABILITY RULES
    # --------------------------------------------------------

    serviceability = check_pincode_serviceability(pincode)

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
    # FINAL E-COMMERCE-FRIENDLY RESPONSE
    # --------------------------------------------------------

    return {

        "success":
            True,

        "pincode":
            pincode,

        "weight_kg":
            request.weight_kg,

        "distance_km":
            route["distance_km"],

        "delivery_charge":
            pricing["delivery_charge"],

        "currency":
            pricing["currency"],

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

        "route":
            {
                "distance_meters":
                    route["distance_meters"],

                "estimated_duration":
                    route["duration"]
            },

        "pricing": {
            "mode": pricing["pricing_mode"],
            "rate_per_kg_km": pricing["rate_per_kg_km"],
            "minimum_charge_inr": MINIMUM_CHARGE_INR,
            "calculation": pricing["calculation"],
            "is_placeholder": pricing["is_placeholder"],
        },

        "serviceability": serviceability
    }