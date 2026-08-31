# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Asynchronous Talabat order webhook processing."""

from marketplace_integrations.integrations.talabat.order_mapper import normalize_order
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order


def process_order_event(marketplace_account, payload):
	"""Normalize and idempotently persist one authenticated order event."""
	return upsert_marketplace_order(marketplace_account, normalize_order(payload))
