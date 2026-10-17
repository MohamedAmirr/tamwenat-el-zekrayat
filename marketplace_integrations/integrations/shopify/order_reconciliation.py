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

# displayFulfillmentStatus values that mean nothing was shipped.  External information
# (shopify.dev, checked 2026-10-11, not this repo's docs): on the pinned API version
# (2026-07, SHOPIFY_API_VERSION) a fully refunded unshipped order reports UNFULFILLED
# (https://shopify.dev/docs/api/admin-graphql/2026-07/enums/OrderDisplayFulfillmentStatus);
# FULFILLMENT_NOT_REQUIRED ("The order has no items to be fulfilled") is only returned for
# such orders from API 2026-10 on
# (https://shopify.dev/changelog/posts/orderdisplayfulfillmentstatus-now-returns-fulfillmentnotrequired.md:
# "Earlier versions are unchanged and still return UNFULFILLED for these orders").  Both stay
# so the rule is also correct after a version bump.  Every other value keeps the
# conservative behavior.
_NOTHING_FULFILLED_STATUSES = {"UNFULFILLED", "FULFILLMENT_NOT_REQUIRED"}

# RefundLineItem.restockType values (external information, shopify.dev, checked 2026-10-11,
# https://shopify.dev/docs/api/admin-graphql/2026-07/enums/RefundLineItemRestockType):
# RETURN = the line was returned (restocking items that were fulfilled); CANCEL = canceled
# (restocking unfulfilled items); NO_RESTOCK = not restocked; LEGACY_RESTOCK is deprecated.
# A RETURN line means goods may have shipped, so the order is never auto-cancelled.  To
# switch to a strict "CANCEL only" rule, replace this with an allowed set.
_BLOCKING_RESTOCK_TYPES = {"RETURN"}


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
	totals, so it is the same decision as a cancellation.  Refund data that cannot be read,
	or any refund line restocked as RETURN, makes the order not eligible.
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
		and not _has_returned_refund_line(order)
	)


def _has_returned_refund_line(order):
	"""Whether any refund line is restocked as a return, or the refunds cannot be read.

	Reads ``refunds[].refundLineItems.nodes[].restockType`` as the mapper stores Shopify's raw
	refunds.  Absent refunds, refunds without line items (money-only or shipping-only) and
	lines without a restock type carry no return signal.  A refunds value that is present but
	not a readable list (corrupt JSON, wrong shape) is treated as a return, the conservative
	reading: the order is simply not auto-cancelled.
	"""
	refunds = order.refunds
	if not refunds:
		return False
	try:
		refunds = frappe.parse_json(refunds) if isinstance(refunds, str) else refunds
	except (TypeError, ValueError):
		return True
	if not isinstance(refunds, list):
		return True
	for refund in refunds:
		connection = refund.get("refundLineItems") if isinstance(refund, dict) else None
		nodes = connection.get("nodes") if isinstance(connection, dict) else None
		for node in nodes if isinstance(nodes, list) else []:
			if isinstance(node, dict) and cstr(node.get("restockType")).strip() in _BLOCKING_RESTOCK_TYPES:
				return True
	return False


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
	except (TypeError, ValueError):
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
	except (TypeError, ValueError):
		return None
	return baseline.get("refund_ids") if isinstance(baseline, dict) else None
