# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Shopify reconciliation evidence for a Marketplace Order (#178).

``build_evidence()`` is pure: it reads only the Marketplace Order document it is
given (no Shopify call, no write) and returns the platform-neutral evidence dict
``services/sales_order_reconciliation.plan()`` consumes.  Only the explicit,
persisted ``cancelled_at``, ``refunds`` and per-line fulfillment quantities are
interpreted.

Fulfillment fields come from shopify.dev's LineItem docs (not verified against
API 2026-07 or a live store; see docs/shopify/README.md, #178 part 2).
"""

import frappe
from frappe import _
from frappe.utils import cstr, flt

# displayFulfillmentStatus values that mean nothing was shipped (shopify.dev, checked
# 2026-10-11, not this repo's docs): FULFILLMENT_NOT_REQUIRED = the order has no items
# to fulfill; UNFULFILLED = none of the items are fulfilled (it replaces the legacy
# OPEN and RESTOCKED).  Every other value keeps the conservative behavior.
_NOTHING_FULFILLED_STATUSES = {"UNFULFILLED", "FULFILLMENT_NOT_REQUIRED"}


def build_evidence(order):
	"""Return full-cancellation and not-yet-reflected refund evidence."""
	evidence = {}
	if order.cancelled_at or _fully_refunded_before_fulfillment(order):
		evidence["cancellation"] = "full"
	if unreflected_refund_ids(order):
		evidence["refund"] = True
	evidence.update(_fulfillment_evidence(order))
	return evidence


def _fully_refunded_before_fulfillment(order):
	"""A refunded, not-cancelled order with nothing left to ship or charge.

	Shopify keeps such an order uncancelled but zeroes its current quantities and
	totals, so it is the same decision as a cancellation.  The refund's restock type
	is not used (not verified against shopify.dev).
	"""
	items = getattr(order, "items", None) or []
	return bool(
		items
		and getattr(order, "external_financial_status", None) == "REFUNDED"
		and getattr(order, "external_fulfillment_status", None) in _NOTHING_FULFILLED_STATUSES
		and all(not flt(item.current_quantity) for item in items)
		and not any(
			flt(getattr(order, field, None))
			for field in ("order_total", "shipping_total", "total_taxes", "duties_total")
		)
	)


def fulfillment_started(item):
	"""Whether a line has fulfilled units: ``True``/``False``, or ``None`` if unknown.

	fulfilled units = ``current_quantity - unfulfilled_quantity`` (floored at 0),
	started when > 0.  ``non_fulfillable_quantity`` is deliberately not subtracted:
	the docs do not say whether ``unfulfilledQuantity`` already excludes it, and
	subtracting when it does would hide fulfilled units.  Not subtracting can only
	over-report (a tip-like line reads as started), which blocks a change instead
	of letting one through.  A row without ``fulfillment_quantities_known`` (imported
	before #178, or fields absent) is unknown, never "not started".
	"""
	if not getattr(item, "fulfillment_quantities_known", None):
		return None
	return flt(item.current_quantity) - flt(item.unfulfilled_quantity) > 0


def _fulfillment_evidence(order):
	items = getattr(order, "items", None) or []
	if not items:
		return {}
	started = {}
	for item in items:
		line_started = fulfillment_started(item)
		if line_started is not None:
			started[cstr(item.external_item_id).strip()] = line_started
	evidence = {"source": "marketplace"}
	if len(started) < len(items):
		# Any unknown line: the stage of the order is unknown.  Known lines are still
		# reported so a changed started line keeps its specific reason.
		evidence["lifecycle_permits_change"] = None
		evidence["lifecycle_reason"] = _(
			"Shopify fulfillment quantities are not recorded for every line; "
			"refresh the order from Shopify before changing it."
		)
	elif all(started.values()):
		evidence["lifecycle_permits_change"] = False
		evidence["lifecycle_reason"] = _(
			"Shopify has already fulfilled every line, so the change was not applied."
		)
	else:
		# Before or partway through fulfillment: a changed line is judged on its own
		# ``fulfillment_started`` flag.
		evidence["lifecycle_permits_change"] = True
	evidence["fulfillment_started"] = started
	return evidence


def refund_ids(order):
	"""Return the ids of every persisted refund on the order."""
	refunds = order.refunds
	if not refunds:
		return []
	try:
		refunds = frappe.parse_json(refunds) if isinstance(refunds, str) else refunds
	except TypeError, ValueError:
		return []
	if not isinstance(refunds, list):
		return []
	return [cstr(refund.get("id") if isinstance(refund, dict) else refund) for refund in refunds]


def unreflected_refund_ids(order):
	"""Refund ids that are not already reflected in the stored baseline.

	A refund recorded in the baseline's ``refund_ids`` (present when the order
	was synced) is already part of the Sales Order quantities, so it is not new
	evidence.  A legacy baseline without ``refund_ids`` has no known history, so
	every current refund counts as new (conservative: the user is told, nothing
	is silently accepted).
	"""
	current = refund_ids(order)
	if not current:
		return []
	known = _baseline_refund_ids(order)
	if known is None:
		return current
	return [refund_id for refund_id in current if refund_id not in set(known)]


def _baseline_refund_ids(order):
	if not order.last_synced_snapshot:
		return None
	try:
		baseline = frappe.parse_json(order.last_synced_snapshot)
	except TypeError, ValueError:
		return None
	return baseline.get("refund_ids") if isinstance(baseline, dict) else None
