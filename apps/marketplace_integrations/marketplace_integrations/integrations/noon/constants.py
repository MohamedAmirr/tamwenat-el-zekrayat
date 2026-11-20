# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Countries supported by the checked-in Noon pricing contract for #144."""

# Country set: docs/noon-integration/noon_api_full.md and the official
# Set and Update Prices guide. Currency pairing approved for #144 on
# 2026-09-29; never infer a currency for another country code.
COUNTRY_CURRENCY = {"ae": "AED", "sa": "SAR", "eg": "EGP"}
PRICING_CHUNK_SIZE = 100
STOCK_CHUNK_SIZE = 100
# A 100-item stock/price chunk outlasts the shared 10 s transport timeout; we
# disconnected and Noon logged 499 "context canceled". Used only by those two pushes.
PUSH_TIMEOUT_SECONDS = 60
# #516: a stock-update chunk that answers HTTP 504 (gateway timeout) is sent again after each of
# these waits in seconds, then the chunk's mappings end in Error. Safe because every item carries
# the absolute target qty (the same payload is re-sent by every hourly full push), so a repeat
# of a request Noon already applied changes nothing. Only the stock push does this; 429 and every
# other status are returned unchanged.
STOCK_504_BACKOFF_SECONDS = (2, 5, 10)

# ListFbpiOrders documents up to 50 orders per page
# (docs/noon-integration/noon_api_full.md, #145's facts table).
ORDERS_PAGE_SIZE = 50

# #145 decision 18: the nightly 23:59 catch-up (services/marketplace_order_reconciliation.py::
# enqueue_nightly_order_reconciliation, which is the daily 7-day re-list) and a manual or
# webhook-queued import list the last 7 days of orders (not just since the previous run)
# because ListFbpiOrders only filters on order creation time, so it would otherwise never
# surface a later item status change (mp_status/integration_status) on an already-seen order.
ORDER_IMPORT_LOOKBACK_DAYS = 7

# #525: Noon's support guidance is to reconcile with ListFbpiOrders every 15-30 minutes
# with a window that overlaps the previous run. A 25-minute cron cannot be evenly spaced,
# so the poll runs at :01, :27 and :53 (gaps of 26, 26 and 8 minutes). All three are odd and
# none is a multiple of 5, so none shares a tick with the */2 sweep, the */10 or the
# on-the-5 jobs (5, 15, 30, 35, 59, Frappe's own); minute 0 is avoided (#367). The poll
# lists only the last 2 hours, wide enough to cover a missed run; the 23:59 catch-up above
# does the 7-day re-list.
NOON_ORDER_POLL_CRON = "1,27,53 * * * *"
ORDER_POLL_WINDOW_HOURS = 2

# #525: a ListFbpiOrders page that answers HTTP 429 is retried (same body and next_token)
# after each of these waits in seconds, then fails as before. Retry-After is ignored.
LIST_ORDERS_429_BACKOFF_SECONDS = (2, 4, 8)

# #146 FBPI shipment fulfillment paths - docs/noon-integration/noon_api_full.md
# (locked plan's "Noon API facts used" table). All go through
# integrations/noon/client.py::NoonClient.request() unchanged.
SHIPMENT_CREATE_PATH = "/fbpi/v1/shipment/create"
SHIPMENT_GET_PATH = "/fbpi/v1/shipment/get"
SHIPMENT_CANCEL_PATH = "/fbpi/v1/shipment/cancel"
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
# #400: the only ``event_schema_version`` Noon documents for it (accepted as integer or string).
NOON_ORDER_SYNC_SCHEMA_VERSION = 1

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

# #153 review finding #3: bounds the daily AWB scan to Delivery Notes with a live
# Noon shipment modified within this window rather than scanning
# every AWB this account has ever had. Not reused from
# ORDER_IMPORT_LOOKBACK_DAYS above - that 7-day window exists for a
# different purpose (re-listing recently-created orders to catch a later
# status change), not for bounding how long a return can plausibly still be
# in flight against an outbound shipment.
AWB_SCAN_LOOKBACK_DAYS = 30

# #153 review finding #4: Noon's ListReturnReferences contract caps
# merchant_codes at 200 items.
MERCHANT_CODES_MAX = 200

# Changed-stock sweep (integrations/noon/sweep.py::sweep_changed_bins), registered
# in hooks.py scheduler_events["cron"]. Frappe has no every-N-minutes bucket.
NOON_INVENTORY_SWEEP_CRON = "*/2 * * * *"

# Each tick re-reads Bins back to (watermark - this many minutes), so a missed
# or failed enqueue is recovered on a later tick without a retry mechanism.
NOON_INVENTORY_SWEEP_OVERLAP_MINUTES = 10
