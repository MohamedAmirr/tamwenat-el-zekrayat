# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Confirmed Bosta API paths and webhook boundary for the shipping MVP."""

BOSTA_API_BASE_URL = "https://app.bosta.co/api/v2"
BOSTA_CREATE_DELIVERY_PATH = "/deliveries?apiVersion=1"
BOSTA_SEARCH_DELIVERIES_PATH = "/deliveries/search"
BOSTA_MASS_AWB_PATH = "/deliveries/mass-awb"
BOSTA_GET_DELIVERY_PATH = "/deliveries/business/{tracking_number}"
BOSTA_UPDATE_DELIVERY_PATH = "/deliveries/business/{tracking_number}"
BOSTA_TERMINATE_DELIVERY_PATH = "/deliveries/business/{tracking_number}/terminate"
BOSTA_CITIES_PATH = "/cities"
BOSTA_PICKUP_LOCATIONS_PATH = "/pickup-locations"
BOSTA_DISTRICTS_PATH = "/cities/{city_id}/districts"
BOSTA_WEBHOOK_PATH = "/api/method/marketplace_integrations.api.bosta_webhooks.delivery_event"

# Bosta documents a different response shape above this count.  The MVP only
# supports the documented base64-PDF response for batches of 50 or fewer.
BOSTA_AWB_MAX_TRACKING_NUMBERS = 50

# Bosta lets merchants configure an arbitrary custom header for dashboard
# webhooks. This is the header this integration asks the merchant to configure.
BOSTA_WEBHOOK_SECRET_HEADER = "X-Bosta-Webhook-Secret"
