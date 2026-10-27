# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Confirmed Bosta API paths and webhook boundary for the shipping MVP."""

BOSTA_API_BASE_URL = "https://app.bosta.co/api/v2"
# Without ``?apiVersion=1``: that version requires a district (errorCode 777). Without it Bosta
# resolves the district from ``dropOffAddress.city`` (the city code) and ``firstLine``. Not
# documented by Bosta (docs.yaml has only the apiVersion=1 call); verified live on 2026-10-22.
BOSTA_CREATE_DELIVERY_PATH = "/deliveries"
BOSTA_SEARCH_DELIVERIES_PATH = "/deliveries/search"
BOSTA_MASS_AWB_PATH = "/deliveries/mass-awb"
BOSTA_GET_DELIVERY_PATH = "/deliveries/business/{tracking_number}"
BOSTA_UPDATE_DELIVERY_PATH = "/deliveries/business/{tracking_number}"
BOSTA_TERMINATE_DELIVERY_PATH = "/deliveries/business/{tracking_number}/terminate"
BOSTA_CITIES_PATH = "/cities"
BOSTA_PICKUP_LOCATIONS_PATH = "/pickup-locations"
BOSTA_ALL_DISTRICTS_PATH = "/cities/getAllDistricts"
BOSTA_WEBHOOK_PATH = "/api/method/marketplace_integrations.api.bosta_webhooks.delivery_event"

# Bosta documents a different response shape above this count.  The MVP only
# supports the documented base64-PDF response for batches of 50 or fewer.
BOSTA_AWB_MAX_TRACKING_NUMBERS = 50

# Bosta lets merchants configure an arbitrary custom header for dashboard
# webhooks. This is the header this integration asks the merchant to configure.
BOSTA_WEBHOOK_SECRET_HEADER = "X-Bosta-Webhook-Secret"

# Platforms that fulfil their own orders: no Bosta shipment is created for a Sales Order of one of
# them, and the Sales Order form hides every Bosta action for it (#368).  Add Amazon and Instashop
# here when those platforms are built; they do not exist in this app yet.  Mirrored by
# BOSTA_EXCLUDED_PLATFORMS in public/js/sales_order.js (a parity test keeps the two equal).
# Shopify and a Sales Order with no platform keep Bosta.
BOSTA_EXCLUDED_PLATFORMS = ("Noon", "Talabat")
