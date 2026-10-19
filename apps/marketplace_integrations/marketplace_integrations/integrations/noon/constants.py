# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Countries supported by the checked-in Noon pricing contract for #144."""

# Country set: docs/noon-integration/noon_api_full.md and the official
# Set and Update Prices guide. Currency pairing approved for #144 on
# 2026-09-29; never infer a currency for another country code.
COUNTRY_CURRENCY = {"ae": "AED", "sa": "SAR", "eg": "EGP"}
PRICING_CHUNK_SIZE = 100
STOCK_CHUNK_SIZE = 100

# ListFbpiOrders documents up to 50 orders per page
# (docs/noon-integration/noon_api_full.md, #145's facts table).
ORDERS_PAGE_SIZE = 50

# #145 decision 18: every hourly poll re-lists the last 7 days of orders
# (not just since the previous run) because ListFbpiOrders only filters on
# order creation time, so it would otherwise never surface a later item
# status change (mp_status/integration_status) on an already-seen order.
ORDER_IMPORT_LOOKBACK_DAYS = 7

# #146 FBPI shipment fulfillment paths - docs/noon-integration/noon_api_full.md
# (locked plan's "Noon API facts used" table). All go through
# integrations/noon/client.py::NoonClient.request() unchanged.
SHIPMENT_CREATE_PATH = "/fbpi/v1/shipment/create"
SHIPMENT_GET_PATH = "/fbpi/v1/shipment/get"
SHIPMENT_CANCEL_PATH = "/fbpi/v1/shipment/cancel"
SHIPMENT_COURIER_AWBS_ADD_PATH = "/fbpi/v1/shipment/courier-awbs/add"
NOON_LOGISTICS_AWBS_GET_PATH = "/fbpi/v1/shipment/noon-logistics-awbs/get"
FBPI_ORDER_UPDATE_PATH = "/fbpi/v1/fbpi-order/update"
SHIPMENT_INVOICE_UPLOAD_PATH = "/fbpi/v1/shipment/invoice/upload"
FBPI_ORDER_INVOICE_UPLOAD_PATH = "/fbpi/v1/fbpi-order/invoice/upload"

# Decision 19: UploadShipmentInvoice and UploadFbpiOrderInvoice document a 5 MiB DECODED limit.
SHIPMENT_INVOICE_MAX_BYTES = 5 * 1024 * 1024

# #163 FBPI event-notification webhook receiver (api/noon_webhooks.py).
# Envelope/auth facts sourced from Noon's public Event Notifications page
# (internet-sourced, not a checked-in API spec file - see
# docs/noon-integration/event-notifications.md for the user-approved
# source note) - there is no HMAC/signature scheme, only a static key.
# X-Api-Key is the header Noon's dashboard sends; the key value is generated
# by ERPNext (Noon Connection.webhook_secret) and pasted into Noon.
NOON_WEBHOOK_SECRET_HEADER = "X-Api-Key"
NOON_EVENT_WEBHOOK_PATH = "/api/method/marketplace_integrations.api.noon_webhooks.event_notification"

# The only event_type this receiver currently acts on - any other
# event_type is acknowledged (200) without enqueuing anything, so Noon
# stops retrying a type we don't handle (#163 scope).
NOON_FBPI_ORDER_SYNC_EVENT_TYPE = "FBPI::ORDER_SYNC"

# #263: the return-reference event. Its payload shape (barcode, barcode_type) comes
# from an AI-written summary, not a Noon page, so it is unverified; the receiver
# stores the raw payload (capped) so the first real delivery can confirm it.
NOON_RETURN_REFERENCE_UPDATE_EVENT_TYPE = "RETURNS::REFERENCE_UPDATE"
NOON_RETURN_EVENT_PAYLOAD_LOG_MAX = 4096
NOON_RETURN_BARCODE_MAX = 255

# #253: Noon allows at most this many Event Notification destinations per project.
NOON_MAX_DESTINATIONS = 5

# #153 FBPI return reference lookup/evidence - docs/noon-integration/
# noon_api_full.md's "Returns" section documents this path/body/response
# shape, but flags its own paired .yml entry as empty/unconfirmed (see
# integrations/noon/returns.py's module docstring).
RETURN_REFERENCES_LIST_PATH = "/returns/v1/return-references/list"

# Review finding M4: bound how many of an account's most recent Noon orders are
# read to derive ``merchant_codes`` for a return lookup. A merchant's code is the
# same on nearly every order, so the recent orders are enough.
MERCHANT_CODES_SCAN_ORDERS = 200

# #153 review finding #3: bounds the daily AWB scan to shipments modified
# within this window (and not Cancelled/Discarded) rather than scanning
# every AWB this account has ever had. Not reused from
# ORDER_IMPORT_LOOKBACK_DAYS above - that 7-day window exists for a
# different purpose (re-listing recently-created orders to catch a later
# status change), not for bounding how long a return can plausibly still be
# in flight against an outbound shipment.
AWB_SCAN_LOOKBACK_DAYS = 30

# #153 review finding #4: Noon's ListReturnReferences contract caps
# merchant_codes at 200 items.
MERCHANT_CODES_MAX = 200

# #164: Needs Attention flags a shipment row still in Pending (CreateShipment
# not yet sent or not yet confirmed) longer than this, measured from the
# shipment row's creation. 1 hour is a placeholder with no documented basis.
NOON_PENDING_SHIPMENT_STUCK_HOURS = 1
