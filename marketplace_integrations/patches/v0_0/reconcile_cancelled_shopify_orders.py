# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Classify existing unlinked Shopify full cancellations after the prior cleanup patch."""

import frappe
from frappe import _

from marketplace_integrations.integrations.shopify.order_reconciliation import build_evidence

_UNPAID_STATUSES = {"PENDING", "VOIDED", "EXPIRED", "AUTHORIZED", "REFUNDED"}
_PAID_STATUSES = {"PAID", "PARTIALLY_PAID", "PARTIALLY_REFUNDED"}
_REFUND_ERROR = _(
	"Cancelled in Shopify but the payment was not fully refunded; refund it in Shopify"
)


def execute():
	if not frappe.db.table_exists("Marketplace Order"):
		return 0

	# Use the same evidence builder as runtime. In particular, fully refunded
	# orders without cancelled_at qualify only when the complete conservative
	# unfulfilled/zero-quantity/zero-total/no-return predicate holds.
	candidates = frappe.get_all(
		"Marketplace Order",
		filters={
			"platform": "Shopify",
			"erpnext_sync_status": ["in", ["Not Synced", "Sync Error"]],
			"erpnext_sales_order": ["is", "not set"],
		},
		fields=["name"],
	)
	updated = 0
	for candidate in candidates:
		order = frappe.get_doc("Marketplace Order", candidate.name)
		if build_evidence(order).get("cancellation") != "full":
			continue

		financial_status = order.external_financial_status
		if financial_status in _UNPAID_STATUSES:
			target = {"erpnext_sync_status": "Cancelled", "last_sync_error": None}
		elif financial_status in _PAID_STATUSES:
			target = {"erpnext_sync_status": "Sync Error", "last_sync_error": _REFUND_ERROR}
		else:
			# Unknown future statuses remain unchanged for manual review.
			continue
		if any(order.get(field) != value for field, value in target.items()):
			# Derived sync-state repair intentionally skips document hooks, Versions,
			# modified timestamps, and Sync Log creation, matching the old cleanup patch.
			frappe.db.set_value("Marketplace Order", order.name, target, update_modified=False)
			updated += 1
	return updated
