# JSK Tiles World — Delivery Calculator

A Python/FastAPI-based delivery calculation service for JSK Tiles World.

The service accepts a customer's Indian PIN code and shipment weight, determines the approximate destination coordinates using Google Geocoding API, calculates the driving distance from the JSK Tiles World warehouse using Google Routes API, and currently applies a placeholder delivery-pricing formula.

> **Status:** Prototype / Development  
> **Important:** The delivery pricing currently uses placeholder rates and must be replaced with the actual transportation pricing before production use.

---

## Features

- Indian 6-digit PIN code validation
- PIN code → geographic coordinates using Google Geocoding API
- Driving distance calculation using Google Routes API
- Warehouse location configurable through environment variables
- Shipment weight input in kilograms
- Placeholder delivery-charge calculation
- FastAPI REST API
- Interactive Swagger API documentation
- Environment-based configuration
- API key kept outside the source code

---

## Architecture

```text
Customer
   │
   │ PIN + Shipment Weight
   ▼
┌──────────────────────────┐
│      FastAPI Backend     │
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│ Google Geocoding API     │
│                          │
│ PIN → Latitude/Longitude │
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│ Google Routes API        │
│                          │
│ Warehouse → Destination  │
│        ↓                 │
│   Driving Distance       │
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│    Pricing Engine        │
│                          │
│ Distance + Weight        │
└────────────┬─────────────┘
             │
             ▼
       Delivery Charge