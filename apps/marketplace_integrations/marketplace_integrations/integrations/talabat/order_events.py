# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Asynchronous Talabat order webhook processing."""

import frappe

from marketplace_integrations.integrations.talabat.order_mapper import normalize_order
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order


def process_order_event(marketplace_account, payload):
	"""Normalize and idempotently persist one authenticated order event."""
	# The public webhook request is intentionally handled as Guest. Its
	# signature has already been validated before this job is enqueued, so the
	# trusted integration worker must not inherit Guest's document permissions.
	frappe.set_user("Administrator")
	return upsert_marketplace_order(marketplace_account, normalize_order(payload))
