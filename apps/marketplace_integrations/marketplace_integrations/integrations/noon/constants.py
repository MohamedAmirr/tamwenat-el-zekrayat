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
