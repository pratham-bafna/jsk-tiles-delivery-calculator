import os
import uuid
import json
import sqlite3
from datetime import datetime, timezone
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

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM", "JSK Tiles World <onboarding@resend.dev>")
ORDER_NOTIFICATION_EMAIL = os.getenv("ORDER_NOTIFICATION_EMAIL")

# SQLite database stored on the Railway Volume mounted at /data.
ORDERS_DB_PATH = os.getenv("ORDERS_DB_PATH", "/data/orders.db")


def get_orders_db():
    """Open the persistent order database and ensure its schema exists."""
    parent = os.path.dirname(ORDERS_DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    connection = sqlite3.connect(ORDERS_DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_reference TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending review',
            customer_name TEXT NOT NULL,
            customer_email TEXT NOT NULL,
            customer_phone TEXT NOT NULL,
            delivery_address TEXT NOT NULL,
            pincode TEXT NOT NULL,
            items_json TEXT NOT NULL,
            total_weight_kg REAL NOT NULL,
            delivery_distance_km REAL,
            delivery_estimate_inr REAL,
            estimate_only INTEGER NOT NULL DEFAULT 1,
            pricing_mode TEXT,
            destination_text TEXT,
            business_notification_sent INTEGER NOT NULL DEFAULT 0,
            acknowledgment_email_sent INTEGER NOT NULL DEFAULT 0
        )
    """)
    connection.commit()
    return connection


def save_order_record(order: dict) -> None:
    """Persist the order request before attempting any email delivery."""
    with get_orders_db() as connection:
        connection.execute("""
            INSERT INTO orders (
                order_reference, created_at, status, customer_name,
                customer_email, customer_phone, delivery_address, pincode,
                items_json, total_weight_kg, delivery_distance_km,
                delivery_estimate_inr, estimate_only, pricing_mode,
                destination_text, business_notification_sent,
                acknowledgment_email_sent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order["order_reference"], order["created_at"], order["status"],
            order["customer_name"], order["customer_email"], order["customer_phone"],
            order["delivery_address"], order["pincode"], json.dumps(order["items"]),
            order["total_weight_kg"], order["delivery_distance_km"],
            order["delivery_estimate_inr"], 1, order["pricing_mode"],
            order["destination_text"], 0, 0
        ))


def update_order_email_status(order_reference: str, *, business_sent=None, acknowledgment_sent=None) -> None:
    """Record email delivery attempts without changing the order's status."""
    assignments = []
    values = []
    if business_sent is not None:
        assignments.append("business_notification_sent = ?")
        values.append(1 if business_sent else 0)
    if acknowledgment_sent is not None:
        assignments.append("acknowledgment_email_sent = ?")
        values.append(1 if acknowledgment_sent else 0)
    if not assignments:
        return
    values.append(order_reference)
    with get_orders_db() as connection:
        connection.execute(
            f"UPDATE orders SET {', '.join(assignments)} WHERE order_reference = ?",
            values,
        )


def send_email(recipient: str, subject: str, body: str) -> None:
    """Send a plain-text email through Resend's HTTPS API."""
    required_settings = {
        "RESEND_API_KEY": RESEND_API_KEY,
        "EMAIL_FROM": EMAIL_FROM,
    }
    missing = [name for name, value in required_settings.items() if not value]
    if missing:
        raise RuntimeError("Missing email configuration: " + ", ".join(missing))

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": EMAIL_FROM,
            "to": [recipient],
            "subject": subject,
            "text": body,
        },
        timeout=20,
    )
    if not response.ok:
        # Log provider status and response without logging the API key.
        raise RuntimeError(
            f"Resend API returned HTTP {response.status_code}: {response.text[:1000]}"
        )


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


class OrderItem(BaseModel):
    productId: str = Field(..., min_length=1)
    variantId: str = Field(..., min_length=1)
    quantity: int = Field(..., gt=0, le=1000)


class OrderRequest(BaseModel):
    customer_name: str = Field(..., min_length=1, max_length=150)
    customer_email: str = Field(..., min_length=3, max_length=254)
    customer_phone: str = Field(..., min_length=5, max_length=30)
    delivery_address: str = Field(..., min_length=5, max_length=1000)
    pincode: str = Field(..., min_length=6, max_length=6)
    items: list[OrderItem] = Field(..., min_length=1, max_length=100)


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


# ============================================================
# ORDER REQUEST SUBMISSION
# ============================================================

@app.post("/submit-order-request")
def submit_order_request(request: OrderRequest):
    """Validate, persist, and email an order request."""
    customer_name = request.customer_name.strip()
    customer_email = request.customer_email.strip()
    customer_phone = request.customer_phone.strip()
    delivery_address = request.delivery_address.strip()
    pincode = request.pincode.strip()

    if not customer_name or not customer_email or not customer_phone or not delivery_address:
        raise HTTPException(status_code=400, detail="Please complete all required customer and delivery fields.")
    if "@" not in customer_email or "." not in customer_email.rsplit("@", 1)[-1]:
        raise HTTPException(status_code=400, detail="Please provide a valid email address.")
    if not pincode.isdigit() or len(pincode) != 6:
        raise HTTPException(status_code=400, detail="PIN code must contain exactly 6 digits.")

    cart_items = [
        {"productId": item.productId, "variantId": item.variantId, "quantity": item.quantity}
        for item in request.items
    ]
    cart_data = calculate_cart_weight(cart_items)
    total_weight = float(cart_data["total_weight_kg"])
    if total_weight <= 0:
        raise HTTPException(status_code=400, detail="The shipment weight could not be verified.")

    delivery_data = calculate_delivery(DeliveryRequest(pincode=pincode, weight_kg=total_weight))
    order_reference = "JSK-" + uuid.uuid4().hex[:10].upper()

    destination = delivery_data.get("destination", {})
    if isinstance(destination, dict):
        destination_text = destination.get("formatted_address") or "Not available"
    else:
        destination_text = str(destination)
    pricing = delivery_data.get("pricing", {})

    # Save the order before attempting email delivery. Email failures must not
    # discard the request or cause the customer to needlessly resubmit it.
    try:
        save_order_record({
            "order_reference": order_reference,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "Pending review",
            "customer_name": customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
            "delivery_address": delivery_address,
            "pincode": pincode,
            "items": cart_data["items"],
            "total_weight_kg": total_weight,
            "delivery_distance_km": delivery_data.get("distance_km"),
            "delivery_estimate_inr": delivery_data.get("delivery_charge"),
            "pricing_mode": pricing.get("mode", PRICING_MODE),
            "destination_text": destination_text,
        })
    except Exception as exc:
        print(f"Order persistence failed for {order_reference}: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="We couldn't securely save your order request. Please try again or contact JSK Tiles World.",
        )

    lines = [
        "NEW ORDER REQUEST — JSK TILES WORLD",
        f"Order reference: {order_reference}",
        "",
        "CUSTOMER DETAILS",
        f"Name: {customer_name}",
        f"Email: {customer_email}",
        f"Phone: {customer_phone}",
        "",
        "DELIVERY ADDRESS",
        delivery_address,
        f"PIN code: {pincode}",
        "",
        "ORDER ITEMS",
    ]
    for item in cart_data["items"]:
        lines.extend([
            f"- {item.get('product_name') or 'Product'}",
            f"  Quantity: {item['quantity']}",
            f"  Unit weight: {float(item['weight_per_unit_kg']):.2f} kg",
            f"  Item weight: {float(item['total_weight_kg']):.2f} kg",
            f"  Product ID: {item['product_id']}",
            f"  Variant ID: {item['variant_id']}",
        ])
    lines.extend([
        "",
        "DELIVERY ESTIMATE — NOT A FINAL QUOTE",
        f"Total shipment weight: {total_weight:.2f} kg",
        f"Estimated distance: {delivery_data.get('distance_km')} km",
        f"Estimated delivery charge: INR {delivery_data.get('delivery_charge')}",
        f"Destination: {destination_text}",
        f"Pricing mode: {pricing.get('mode', PRICING_MODE)}",
        f"Placeholder pricing: {pricing.get('is_placeholder', True)}",
        "",
        "ACTION REQUIRED",
        "Verify stock, delivery coverage and actual transport costs.",
        "Contact the customer with the confirmed total and payment instructions.",
        "This is an order request, not a confirmed order. No payment has been collected.",
    ])

    business_notification_sent = False
    if not ORDER_NOTIFICATION_EMAIL:
        print(f"Order notification email is not configured for {order_reference}.")
    else:
        try:
            send_email(
                recipient=ORDER_NOTIFICATION_EMAIL,
                subject=f"New order request {order_reference} — PIN {pincode}",
                body="\n".join(lines),
            )
            business_notification_sent = True
            update_order_email_status(order_reference, business_sent=True)
        except Exception as exc:
            print(f"Order notification failed for {order_reference}: {type(exc).__name__}: {exc}")
            update_order_email_status(order_reference, business_sent=False)

    item_lines = []
    for item in cart_data["items"]:
        item_lines.append(f"- {item.get('product_name') or 'Product'} — quantity {item['quantity']}")
        item_lines.append(f"  Weight: {float(item['total_weight_kg']):.2f} kg total")
    acknowledgment_body = "\n".join([
        f"Hello {customer_name},",
        "",
        "Thank you for contacting JSK Tiles World. We have received your order request.",
        "",
        f"Order reference: {order_reference}",
        "",
        "REQUEST SUMMARY",
        *item_lines,
        "",
        f"Delivery PIN code: {pincode}",
        f"Total shipment weight: {total_weight:.2f} kg",
        "",
        "IMPORTANT — THIS IS NOT AN ORDER CONFIRMATION",
        "Your request is pending review. We will check product availability, delivery coverage, and the actual transport charge, then contact you with the final delivery charges and payment instructions.",
        "The delivery amount shown during submission is only an estimate. No payment has been collected, and your order is not confirmed until JSK Tiles World confirms it with you.",
        "",
        "If you have questions, please reply to this email or contact JSK Tiles World.",
        "",
        "Regards,",
        "JSK Tiles World",
    ])

    acknowledgment_sent = False
    try:
        send_email(
            recipient=customer_email,
            subject=f"We received your order request — {order_reference}",
            body=acknowledgment_body,
        )
        acknowledgment_sent = True
        update_order_email_status(order_reference, acknowledgment_sent=True)
    except Exception as exc:
        print(f"Customer acknowledgment email failed for {order_reference}: {type(exc).__name__}: {exc}")
        update_order_email_status(order_reference, acknowledgment_sent=False)

    if business_notification_sent and acknowledgment_sent:
        message = (
            f"Your order request was saved successfully. Reference: {order_reference}. "
            "An acknowledgment email has been sent. This is not an order confirmation; "
            "JSK Tiles World will contact you to confirm availability, final delivery charges and payment instructions."
        )
    elif business_notification_sent:
        message = (
            f"Your order request was saved successfully. Reference: {order_reference}. "
            "JSK Tiles World has been notified, but we couldn't send the acknowledgment email. "
            "Please save your reference. This is not an order confirmation."
        )
    else:
        message = (
            f"Your order request was saved successfully. Reference: {order_reference}, but an email notification could not be sent. "
            "Please contact JSK Tiles World and provide this reference. This is not an order confirmation."
        )

    return {
        "success": True,
        "order_reference": order_reference,
        "status": "Pending review",
        "business_notification_sent": business_notification_sent,
        "acknowledgment_email_sent": acknowledgment_sent,
        "message": message,
        "total_weight_kg": total_weight,
        "delivery_estimate": delivery_data.get("delivery_charge"),
        "estimate_only": True,
    }
