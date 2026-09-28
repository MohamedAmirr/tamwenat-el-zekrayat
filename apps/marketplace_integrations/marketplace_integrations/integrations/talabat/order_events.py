# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Asynchronous Talabat order webhook processing."""

import frappe

from marketplace_integrations.integrations.talabat.order_mapper import normalize_order
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.services.sales_order_sync_debounce import bump_sync_debounce


def process_order_event(marketplace_account, payload):
	"""Normalize and idempotently persist one authenticated order event."""
	# The public webhook request is intentionally handled as Guest. Its
	# signature has already been validated before this job is enqueued, so the
	# trusted integration worker must not inherit Guest's document permissions.
	frappe.set_user("Administrator")
	order = upsert_marketplace_order(marketplace_account, normalize_order(payload))
	# Schedule the eventual Sales Order sync via the shared per-order debounce
	# (#132) rather than enqueueing a sync job directly - a burst of webhooks
	# for the same order only ever results in one eventual sync.
	bump_sync_debounce(order.name)
	return order
