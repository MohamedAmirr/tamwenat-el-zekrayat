# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Countries supported by the checked-in Noon pricing contract for #144."""

# Country set: docs/noon-integration/noon_api_full.md and the official
# Set and Update Prices guide. Currency pairing approved for #144 on
# 2026-09-29; never infer a currency for another country code.
COUNTRY_CURRENCY = {"ae": "AED", "sa": "SAR", "eg": "EGP"}
PRICING_CHUNK_SIZE = 100
STOCK_CHUNK_SIZE = 100
