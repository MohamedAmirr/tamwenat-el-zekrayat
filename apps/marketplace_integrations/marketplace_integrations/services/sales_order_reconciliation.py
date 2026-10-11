# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Read-only planning for reconciling submitted Marketplace Sales Orders.

``plan()`` and ``read_erp_state()`` never mutate the Sales Order they inspect.
``execute()`` (#179) is the only mutating entry point: it applies a supported
plan through ERPNext's native submitted-document operations and leaves
persistence of the Marketplace Order, savepoints and audit logging to the caller.
"""

from copy import deepcopy

import frappe
from frappe.utils import cstr, flt

NO_ACTION = "NO_ACTION"
UPDATE_SUBMITTED_ORDER = "UPDATE_SUBMITTED_ORDER"
CANCEL_ORDER = "CANCEL_ORDER"
CLOSE_REMAINDER = "CLOSE_REMAINDER"
NEEDS_ATTENTION = "NEEDS_ATTENTION"
UNSUPPORTED_COMPENSATING_TRANSACTION = "UNSUPPORTED_COMPENSATING_TRANSACTION"
INACTIVE_ERP_ORDER_STATUSES = {"Cancelled", "Closed"}

OUTCOMES = {
	NO_ACTION,
	UPDATE_SUBMITTED_ORDER,
	CANCEL_ORDER,
	CLOSE_REMAINDER,
	NEEDS_ATTENTION,
	UNSUPPORTED_COMPENSATING_TRANSACTION,
}


class SalesOrderLinesMismatch(ValueError):
	"""The snapshot lines cannot be paired one-to-one with the Sales Order rows."""


# Most document names a merchant-readable message lists.
_MAX_LINKED_DOCUMENTS = 3
# (document, its item table, the item field that points at the Sales Order)
_LINKED_DOCUMENT_SOURCES = (
	("Delivery Note", "Delivery Note Item", "against_sales_order"),
	("Sales Invoice", "Sales Invoice Item", "sales_order"),
)


def read_erp_state(sales_order):
	"""Return the execution facts a reconciliation decision may depend on.

	``linked_documents`` (submitted, non-return Delivery Notes and Sales Invoices that
	reference the order) is only read, and only present, once a line has delivery or
	billing: an untouched order costs no extra query.  It names the documents in
	merchant-readable messages and never changes an outcome.
	"""
	state = {
		"sales_order": sales_order.name,
		"docstatus": sales_order.docstatus,
		"status": sales_order.status,
		"per_delivered": flt(sales_order.per_delivered),
		"per_billed": flt(sales_order.per_billed),
		"advance_paid": flt(sales_order.advance_paid),
		"precision": {
			"qty": frappe.get_precision("Sales Order Item", "qty"),
			# Lines are compared by amount, at the precision the Sales Order's own
			# currency gives the amount field (the rule `_verify_line_amounts()` uses).
			"amount": frappe.get_precision("Sales Order Item", "amount", currency=sales_order.currency),
		},
		"lines": [
			{
				"sales_order_item": item.name,
				"item_code": item.item_code,
				"uom": item.uom,
				"warehouse": item.warehouse,
				"qty": flt(item.qty),
				"delivered_qty": flt(item.delivered_qty),
				"billed_amt": flt(item.billed_amt),
				"rate": flt(item.rate),
				"amount": flt(item.amount),
			}
			for item in sales_order.items
		],
	}
	if _has_execution(state):
		state["linked_documents"] = _linked_documents(sales_order.name)
	return state


def _linked_documents(sales_order_name):
	linked = {}
	for doctype, item_doctype, link_field in _LINKED_DOCUMENT_SOURCES:
		parent = frappe.qb.DocType(doctype)
		item = frappe.qb.DocType(item_doctype)
		linked[doctype] = (
			frappe.qb.from_(item)
			.join(parent)
			.on(item.parent == parent.name)
			.select(parent.name)
			.distinct()
			.where(getattr(item, link_field) == sales_order_name)
			.where(parent.docstatus == 1)
			.where(parent.is_return == 0)
			.orderby(parent.name)
			.limit(_MAX_LINKED_DOCUMENTS)
			.run(pluck=True)
		)
	return linked


def snapshot_with_sales_order_item_names(snapshot, sales_order, refund_ids=None):
	"""Copy a successful commercial snapshot and attach ERP line identities.

	The ERP child-row name and ``refund_ids`` (the marketplace refunds already
	reflected in this baseline) are reconciliation evidence, not commercial
	input; both are intentionally excluded from ``sales_order_sync.snapshot_hash()``
	and from the header comparison in ``plan()``.  ``refund_ids=None`` leaves the
	key out (a legacy baseline whose refund history is unknown).
	"""
	persisted = deepcopy(snapshot)
	all_lines = persisted.get("items") or []
	# A zero-quantity line (refunded to zero) is expected to be absent from the Sales
	# Order; it is kept in the baseline without an ERP row identity.
	lines = [line for line in all_lines if flt(line.get("qty")) > 0]
	for line in all_lines:
		if flt(line.get("qty")) <= 0:
			line["sales_order_item"] = None
	if len(lines) != len(sales_order.items):
		raise SalesOrderLinesMismatch(
			f"Sales Order {sales_order.name} has {len(sales_order.items)} item rows but the "
			f"marketplace snapshot has {len(lines)} lines."
		)
	# Pair by item identity (item_code; identical item codes pair in row order)
	# rather than by position, so a reordered Sales Order cannot mis-map names.
	rows_by_item_code = {}
	for sales_order_item in sales_order.items:
		rows_by_item_code.setdefault(sales_order_item.item_code, []).append(sales_order_item.name)
	for line in lines:
		candidates = rows_by_item_code.get(line.get("item_code"))
		if not candidates:
			raise SalesOrderLinesMismatch(
				f"Sales Order {sales_order.name} has no remaining item row for item "
				f"{line.get('item_code')} (marketplace line {line.get('external_item_id')})."
			)
		line["sales_order_item"] = candidates.pop(0)
	if refund_ids is not None:
		persisted["refund_ids"] = sorted(refund_ids)
	return persisted


def snapshot_with_line_rows(snapshot, line_rows, refund_ids=None):
	"""Copy ``snapshot`` attaching the exact ERP row per line (``line_rows`` from ``execute()``)."""
	persisted = deepcopy(snapshot)
	for line in persisted.get("items") or []:
		line["sales_order_item"] = line_rows.get(cstr(line.get("external_item_id")).strip())
	if refund_ids is not None:
		persisted["refund_ids"] = sorted(refund_ids)
	return persisted


def snapshot_advanced_from(previous, current):
	"""Copy ``current`` as the new baseline, keeping ``previous``'s ERP line identities.

	Used when ERPNext already matches the current marketplace state (NO_ACTION):
	the ERP rows are unchanged, so their names carry over from the previous baseline.
	The previous ``refund_ids`` are copied as a default; the caller that knows the
	order's current refunds (``sales_order_sync._advance_baseline()``) overwrites them.
	"""
	advanced = deepcopy(current)
	previous_items = _items_by_external_id(previous) or {}
	for line in advanced.get("items") or []:
		line["sales_order_item"] = (previous_items.get(cstr(line.get("external_item_id")).strip()) or {}).get(
			"sales_order_item"
		)
	if "refund_ids" in previous:
		advanced["refund_ids"] = previous["refund_ids"]
	return advanced


def plan(previous, current, evidence, erp_state):
	"""Classify a desired commercial snapshot without applying any ERP change.

	``evidence`` is the platform-neutral dict an integration adapter builds; every
	key is optional and an absent key means "no such evidence":

	- ``cancellation``: ``"full"`` when the marketplace cancelled the whole order.
	- ``cancelled_after_fulfillment``: with ``cancellation``, the marketplace says the
	  goods had already left the merchant.
	- ``refund`` / ``return``: bool, compensating-transaction evidence.
	- ``lifecycle_permits_change``: ``True``/``False`` whether the marketplace
	  lifecycle still allows the order to change; ``None`` (key present) means the
	  stage is unknown.  ``lifecycle_reason`` is the merchant-readable explanation.
	- ``fulfillment_started``: ``{external_item_id: bool}`` per line, or one bool for
	  the whole order.
	- ``outbound_cancel_requested``: ERPNext already asked the marketplace to cancel the
	  order (#181); with ``cancellation == "full"`` on a Closed order with no delivery or
	  billing this is the echo of that request (``NO_ACTION``, no reopen).
	- ``inactive_order_instruction``: merchant-readable text appended to the "no longer an
	  active submitted document" advisory (what to do on the marketplace).
	- ``source``: where the evidence comes from (e.g. ``"marketplace"``); informational.

	A Closed order with no delivery or billing is judged as if it were open; a resulting
	``CANCEL_ORDER`` / ``UPDATE_SUBMITTED_ORDER`` plan then carries ``reopen: True``
	(the executor must reopen it first).  Cancelled orders are never reopened.
	"""
	evidence = evidence or {}
	if not previous:
		return _attention("No reconciled snapshot baseline is available for this Sales Order.")
	# A refund needs a compensating ERP transaction only once the ERP order has
	# delivery or billing; an untouched order falls through (e.g. CANCEL_ORDER).
	# Return evidence always needs one.
	if evidence.get("return") or (evidence.get("refund") and _has_execution(erp_state)):
		return _result(
			UNSUPPORTED_COMPENSATING_TRANSACTION,
			_refund_message(evidence, erp_state)
			or "Marketplace return or refund evidence needs a compensating ERP transaction.",
		)

	previous_items = _items_by_external_id(previous)
	current_items = _items_by_external_id(current)
	if previous_items is None or current_items is None:
		return _attention("The marketplace snapshot has duplicate or missing line identities.")
	# Every line gone or at zero quantity is the same decision as a full cancellation.
	# An absent "items" key means "no current snapshot" (not "no lines").
	all_zero = "items" in current and not any(flt(line.get("qty")) > 0 for line in current["items"] or [])
	if any(
		flt(previous_items[external_item_id].get("qty")) <= 0 < flt(line.get("qty"))
		for external_item_id, line in current_items.items()
		if external_item_id in previous_items
	):
		return _attention(
			"A marketplace line returned to a positive quantity; this needs a structural change."
		)
	if erp_state.get("docstatus") != 1 or erp_state.get("status") in INACTIVE_ERP_ORDER_STATUSES:
		if (
			evidence.get("outbound_cancel_requested")
			and evidence.get("cancellation") == "full"
			and _is_reopenable_closed_order(erp_state)
		):
			# Echo of our own outbound cancel (#181): ERPNext closed the order, asked the
			# marketplace to cancel it, and the marketplace now says cancelled.  Both sides
			# agree; the order stays Closed (it must not be reopened just to be cancelled).
			return _result(
				NO_ACTION,
				"The marketplace order was cancelled at ERPNext's request; the Sales Order stays Closed.",
			)
		if _is_reopenable_closed_order(erp_state):
			# Closed with no delivery or billing: judge it as the open order it would be after
			# a reopen; only a cancel/update result carries the reopen flag (#180).
			reopened = plan(previous, current, evidence, {**erp_state, "status": "To Deliver and Bill"})
			if reopened["outcome"] in (CANCEL_ORDER, UPDATE_SUBMITTED_ORDER):
				reopened["reopen"] = True
				reopened["message"] = (
					f"{reopened['message']} The Sales Order is Closed; it will be reopened first."
				)
			elif reopened["outcome"] == NO_ACTION:
				# Equivalent to the marketplace while it still expects fulfillment (a cancelling
				# intent never yields NO_ACTION): a manual close is divergence. Advisory only.
				return _attention(
					"The Sales Order is Closed in ERPNext but the marketplace still expects fulfillment; "
					"reopen it or review the order."
				)
			return reopened
		if (evidence.get("cancellation") == "full" or all_zero) and _inactive_order_converged(erp_state):
			# The marketplace cancelled the order and ERPNext is already cancelled (or
			# closed with nothing left to deliver): both sides agree, nothing to do.
			return _result(
				NO_ACTION, "The cancelled marketplace order is already cancelled or closed in ERPNext."
			)
		message = "The linked ERPNext Sales Order is no longer an active submitted document."
		# Platform-specific wording is built by the integration side (#181), never here.
		instruction = cstr(evidence.get("inactive_order_instruction")).strip()
		return _attention(f"{message} {instruction}" if instruction else message)

	if evidence.get("cancellation") == "full" and evidence.get("cancelled_after_fulfillment"):
		# Reached only while the ERP order is still active (an already cancelled or
		# closed order converged above). The marketplace cancelled after the goods
		# left: only a delivery recorded in ERPNext makes this a compensating (return)
		# transaction; otherwise a person has to reconcile what physically happened.
		if _has_delivery(erp_state):
			return _result(
				UNSUPPORTED_COMPENSATING_TRANSACTION,
				"The marketplace order was cancelled after fulfillment and ERPNext already has delivery; "
				"a return or other compensating transaction is needed.",
			)
		if _has_execution(erp_state):
			return _attention(
				"The marketplace order was cancelled after fulfillment, but ERPNext has billing without "
				"delivery; review what happened to the goods."
			)
		# No delivery and no billing: ERPNext never recorded the goods leaving, so the Sales Order
		# is cancelled like any other cancellation (below); what happened to the parcel is physical.

	header_changes = _header_changes(previous, current)
	changes = _line_changes(previous_items, current_items)
	if header_changes:
		shipping_change = next((change for change in header_changes if change["field"] == "shipping"), None)
		if shipping_change:
			return _attention(
				"The shipping charge differs from the submitted Sales Order "
				f"(Sales Order {shipping_change['previous'] or 0}, marketplace {shipping_change['current'] or 0}). "
				"A submitted Sales Order's charges are not edited automatically; cancel and amend it.",
				header_changes + changes,
			)
		return _attention(
			"A submitted Sales Order has structural commercial changes.", header_changes + changes
		)
	structural_kinds = {"item_added"} if all_zero else {"item_added", "item_removed"}
	if any(change["kind"] in structural_kinds for change in changes):
		return _attention("A submitted Sales Order has structural line changes.", changes)

	erp_lines = {line["sales_order_item"]: line for line in erp_state.get("lines") or []}
	equivalent = _effectively_equivalent(
		previous_items, current_items, erp_lines, erp_state.get("precision") or {}
	)
	if not equivalent and not changes:
		return _attention("The ERPNext Sales Order no longer represents the reconciled marketplace state.")

	explicit_cancellation = evidence.get("cancellation") == "full"
	if all_zero and not explicit_cancellation and _has_delivery(erp_state):
		return _attention(
			"All lines are zero but the order was already delivered; explicit cancellation is required.",
			changes,
		)
	cancelling = explicit_cancellation or all_zero
	if equivalent and not cancelling:
		# ERPNext already holds the current marketplace state (including after a
		# manual ERP-side edit): nothing to do, whatever its billing/delivery.
		return _result(NO_ACTION, "Marketplace and ERPNext commercial state are effectively equivalent.")
	if not cancelling:
		blocked_reason = _lifecycle_block_reason(evidence, changes)
		if blocked_reason:
			return _attention(blocked_reason, changes)
		if any(
			change["kind"] == "line_changed" and change["field"] in _MAPPING_SOURCED_LINE_FIELDS
			for change in changes
		):
			return _attention(
				"The item, unit of measure or warehouse of a line changed (mapping or account change); "
				"this cannot be applied automatically.",
				changes,
			)
	if cancelling and _has_execution(erp_state) and not _has_outstanding_remainder(erp_state):
		# Fully delivered (billed or not): only a compensating transaction can undo it,
		# so this takes precedence over the "already billed" blocker below.
		delivery_notes = _documents(erp_state, "Delivery Note")
		return _result(
			UNSUPPORTED_COMPENSATING_TRANSACTION,
			(
				f"The marketplace cancelled this order, but {delivery_notes} already delivered all of it. "
				f"Create a Sales Return against {delivery_notes} for the goods that came back, "
				"or review the order manually."
				if delivery_notes
				else "The cancelled marketplace order is already fully delivered in ERPNext; "
				"a return or other compensating transaction is needed."
			),
			changes,
		)
	if changes or cancelling:
		# Delivery/billing only blocks when something would actually change; an
		# unchanged, equivalent billed or delivered order is simply NO_ACTION.
		for external_item_id, previous_line in previous_items.items():
			if flt(previous_line.get("qty")) <= 0:
				continue  # never had an ERP row
			current_line = current_items.get(external_item_id) or {}
			erp_line = erp_lines.get(previous_line.get("sales_order_item"))
			if not erp_line:
				return _attention("The reconciled Sales Order line is missing from ERPNext.", changes)
			# An all-zero order closes whatever remains after delivery; only an
			# individual line reduction must stay above what was delivered.
			if not all_zero and flt(current_line.get("qty")) < flt(erp_line.get("delivered_qty")):
				return _attention(
					"Requested quantity is below the quantity already delivered in ERPNext.", changes
				)
			if flt(erp_line.get("billed_amt")):
				invoices = _documents(erp_state, "Sales Invoice")
				if cancelling and invoices:
					return _attention(
						f"The marketplace cancelled this order, but {invoices} already bills it in ERPNext, "
						"so the Sales Order cannot be cancelled automatically. Cancel the Sales Invoice "
						"(or issue a Credit Note if it is submitted and paid), then run the update again.",
						changes,
					)
				return _attention("A submitted Sales Order line is already billed in ERPNext.", changes)

	if cancelling:
		if not _has_execution(erp_state):
			if flt(erp_state.get("advance_paid")) > 0:
				return _attention(
					"An advance payment exists on the Sales Order; reallocate or cancel it "
					"before the order can be cancelled.",
					changes,
				)
			message = "The marketplace order is fully cancelled before ERP execution."
			if evidence.get("cancelled_after_fulfillment"):
				message = (
					"The marketplace order was cancelled after fulfillment; ERPNext has no delivery or "
					"billing, so the Sales Order is cancelled. Check what happened to the parcel."
				)
			return _result(CANCEL_ORDER, message, changes)
		if _has_outstanding_remainder(erp_state):
			return _result(
				CLOSE_REMAINDER, "Only the outstanding Sales Order remainder can be closed.", changes
			)

	if _has_execution(erp_state):
		message = "ERPNext delivery or billing execution blocks an automatic submitted-order update"
		named = [_documents(erp_state, "Delivery Note"), _documents(erp_state, "Sales Invoice")]
		named = "; ".join(documents for documents in named if documents)
		return _attention(f"{message} ({named})." if named else f"{message}.", changes)
	return _result(
		UPDATE_SUBMITTED_ORDER,
		"The submitted Sales Order changes are eligible for ERPNext validation.",
		changes,
	)


def _documents(erp_state, doctype):
	"""``"Delivery Note A, B"`` (plural-aware) for the linked documents, or ``""`` if none are known."""
	names = [cstr(name) for name in (erp_state.get("linked_documents") or {}).get(doctype) or []]
	if not names:
		return ""
	return f"{doctype}{'s' if len(names) > 1 else ''} {', '.join(names)}"


def _refund_message(evidence, erp_state):
	"""Merchant-readable text for a refund/return on an order ERPNext already delivered or billed.

	Empty when ERPNext does not name the documents (the caller keeps the generic text).
	"""
	delivery_notes = _documents(erp_state, "Delivery Note")
	invoices = _documents(erp_state, "Sales Invoice")
	if not (delivery_notes or invoices):
		return ""
	lead = "The marketplace refunded this order"
	if evidence.get("return"):
		lead = "The marketplace reported a return for this order"
	if delivery_notes and invoices:
		return (
			f"{lead}, but it was already delivered ({delivery_notes}) and invoiced ({invoices}) in ERPNext. "
			f"The Sales Order was not changed. If the goods came back, create a Sales Return against "
			f"{delivery_notes}; create a Credit Note against {invoices} for the refunded amount "
			"(compensating transactions), or review the order manually."
		)
	if delivery_notes:
		return (
			f"{lead}, but it was already delivered in ERPNext ({delivery_notes}). "
			"The Sales Order was not changed. If the goods came back, create a Sales Return "
			f"against {delivery_notes} (a compensating transaction), otherwise review the order manually."
		)
	return (
		f"{lead}, but it was already invoiced in ERPNext ({invoices}). "
		f"The Sales Order was not changed. Create a Credit Note against {invoices} for the refunded "
		"amount (a compensating transaction), or review the order manually."
	)


def _items_by_external_id(snapshot):
	items = {}
	for line in snapshot.get("items") or []:
		external_item_id = cstr(line.get("external_item_id")).strip()
		if not external_item_id or external_item_id in items:
			return None
		items[external_item_id] = line
	return items


def _header_changes(previous, current):
	ignored = {"items", "refund_ids"}
	keys = (set(previous) | set(current)) - ignored
	return [
		{"kind": "header_changed", "field": key, "previous": previous.get(key), "current": current.get(key)}
		for key in sorted(keys)
		if not _same_value(previous.get(key), current.get(key))
	]


def _line_changes(previous_items, current_items):
	changes = []
	for external_item_id in sorted(set(previous_items) | set(current_items)):
		previous = previous_items.get(external_item_id)
		current = current_items.get(external_item_id)
		if previous is None:
			changes.append({"kind": "item_added", "external_item_id": external_item_id})
		elif current is None:
			changes.append({"kind": "item_removed", "external_item_id": external_item_id})
		else:
			for field in ("qty", "effective_rate", "fulfilled_total_price", "item_code", "uom", "warehouse"):
				if not _same_value(previous.get(field), current.get(field)):
					changes.append(
						{
							"kind": "line_changed",
							"external_item_id": external_item_id,
							"field": field,
							"previous": previous.get(field),
							"current": current.get(field),
						}
					)
	return changes


def _same_value(previous, current):
	"""Compare persisted JSON values with the in-memory date values Frappe returns."""
	if previous == current:
		return True
	return str(previous) == str(current)


def _effectively_equivalent(previous_items, current_items, erp_lines, precision=None):
	"""Define convergence once, per marketplace line and its ERP row.

	Money is compared as the line amount against the marketplace fulfilled total
	at currency precision, not as a rate: ERPNext cannot always reproduce a
	marketplace total from a rate, but the amount is what posts.
	"""
	# Zero-quantity lines are expected absent from the Sales Order.
	current_items = {key: line for key, line in current_items.items() if flt(line.get("qty")) > 0}
	if len(current_items) != len(erp_lines):
		return False
	precision = precision or {}
	qty_precision = precision.get("qty")
	amount_precision = precision.get("amount")
	for external_item_id, line in current_items.items():
		sales_order_item = previous_items[external_item_id].get("sales_order_item")
		erp_line = erp_lines.get(sales_order_item)
		if not erp_line:
			return False
		if (
			flt(line.get("qty"), qty_precision) != flt(erp_line.get("qty"), qty_precision)
			or line.get("item_code") != erp_line.get("item_code")
			or line.get("uom") != erp_line.get("uom")
			or line.get("warehouse") != erp_line.get("warehouse")
			or flt(line.get("fulfilled_total_price"), amount_precision)
			!= flt(erp_line.get("amount"), amount_precision)
		):
			return False
		if flt(erp_line.get("delivered_qty")) > flt(line.get("qty")):
			return False
	return True


# Line fields whose value comes from the marketplace order.  ``item_code``, ``uom`` and
# ``warehouse`` come from this app's mapping/account configuration, so the marketplace
# lifecycle has no say over them and must not block a change to them.
_MARKETPLACE_SOURCED_LINE_FIELDS = {"qty", "effective_rate", "fulfilled_total_price"}
_MAPPING_SOURCED_LINE_FIELDS = {"item_code", "uom", "warehouse"}


def _lifecycle_block_reason(evidence, changes):
	"""Why the marketplace lifecycle does not allow applying ``changes``, or ``None``.

	Only marketplace-sourced changes (quantity and price/amount) are judged; a change
	confined to item, UOM or warehouse is never blamed on the marketplace lifecycle.
	"""
	if "lifecycle_permits_change" not in evidence and not evidence.get("fulfillment_started"):
		return None
	changes = [
		change
		for change in changes
		if change.get("kind") == "line_changed" and change.get("field") in _MARKETPLACE_SOURCED_LINE_FIELDS
	]
	if not changes:
		return None
	permits = evidence.get("lifecycle_permits_change", True)
	reason = cstr(evidence.get("lifecycle_reason")).strip()
	if permits is False:
		return reason or "The marketplace order can no longer change, so the change was not applied."
	started = evidence.get("fulfillment_started")
	for change in changes:
		line_started = (
			started.get(cstr(change.get("external_item_id")).strip())
			if isinstance(started, dict)
			else bool(started)
		)
		if line_started:
			return (
				reason
				or f"Marketplace line {change['external_item_id']} already started fulfillment, "
				"so its change was not applied."
			)
	if permits is None:
		return reason or "The marketplace order stage is unknown, so the change was not applied."
	return None


def _inactive_order_converged(erp_state):
	"""ERP Cancelled/docstatus 2, or Closed after delivery (what ``CLOSE_REMAINDER`` leaves).

	A Closed order with no delivery is not converged: ``plan()`` may reopen it (#180).
	"""
	if erp_state.get("docstatus") == 2 or erp_state.get("status") == "Cancelled":
		return True
	return (
		erp_state.get("docstatus") == 1 and erp_state.get("status") == "Closed" and _has_delivery(erp_state)
	)


def _is_reopenable_closed_order(erp_state):
	"""A submitted, Closed order with no delivery and no billing (reopening loses nothing)."""
	return (
		erp_state.get("docstatus") == 1
		and erp_state.get("status") == "Closed"
		and not _has_execution(erp_state)
	)


def _has_execution(erp_state):
	return any(
		flt(line.get("delivered_qty")) or flt(line.get("billed_amt")) for line in erp_state.get("lines") or []
	)


def _has_delivery(erp_state):
	return any(flt(line.get("delivered_qty")) for line in erp_state.get("lines") or [])


def _has_outstanding_remainder(erp_state):
	return any(flt(line.get("qty")) > flt(line.get("delivered_qty")) for line in erp_state.get("lines") or [])


def _attention(message, changes=None):
	return _result(NEEDS_ATTENTION, message, changes)


def _result(outcome, message, changes=None):
	return {"outcome": outcome, "message": message, "changes": changes or []}


class ReconciliationMismatch(Exception):
	"""ERPNext, after an update, still does not hold the marketplace amounts."""


def reopen_closed_order(sales_order, outcome, marketplace_order=None):
	"""Reopen a Closed Sales Order inside the caller's savepoint (#180); never commits.

	``update_status("Draft")`` performs no permission check, so submit permission is
	required here.  ERPNext records neither who closed the order nor the reopen (no
	Version row), so the audit is a Sales Order Comment.  Reloads the document.
	"""
	sales_order.check_permission("submit")
	sales_order.update_status("Draft")
	sales_order.reload()
	sales_order.add_comment(
		"Comment",
		f"Reopened by marketplace reconciliation ({outcome}) for Marketplace Order "
		f"{marketplace_order or ''}; ERPNext does not record who closed it (closed by: unknown).",
	)


def execute(reconciliation_plan, sales_order_name, previous=None, current=None, marketplace_order=None):
	"""Apply a supported plan to a freshly loaded Sales Order and return what happened.

	Raises whatever ERPNext raises; the caller owns the savepoint, the Marketplace
	Order persistence and the audit log.  Never commits.  ``previous`` (the baseline
	with Sales Order item names) and ``current`` snapshots are needed for
	``UPDATE_SUBMITTED_ORDER``, which raises ``ReconciliationMismatch`` when ERPNext
	cannot reproduce the marketplace amounts (the caller rolls back).
	"""
	outcome = reconciliation_plan["outcome"]
	if outcome not in (CANCEL_ORDER, CLOSE_REMAINDER, UPDATE_SUBMITTED_ORDER):
		raise ValueError(f"Reconciliation outcome {outcome} is not executable.")
	sales_order = frappe.get_doc("Sales Order", sales_order_name)
	reopened = bool(reconciliation_plan.get("reopen"))
	if reopened:
		reopen_closed_order(sales_order, outcome, marketplace_order)
	if outcome == UPDATE_SUBMITTED_ORDER:
		executed = _execute_update(sales_order, previous or {}, current or {})
		executed["reopened"] = reopened
		return executed
	if outcome == CANCEL_ORDER:
		sales_order.cancel()
	else:
		# SalesOrder.update_status() itself performs no permission check (ERPNext's
		# whitelisted wrapper does); closing needs submit permission like cancel does.
		sales_order.check_permission("submit")
		sales_order.update_status("Closed")
	return {
		"outcome": outcome,
		"sales_order": sales_order.name,
		"status": sales_order.status,
		"reopened": reopened,
	}


def _execute_update(sales_order, previous, current):
	from erpnext.controllers.accounts_controller import update_child_qty_rate

	previous_items = _items_by_external_id(previous) or {}
	current_items = _items_by_external_id(current) or {}
	rows = {row.name: row for row in sales_order.items}
	trans_items = []
	line_rows = {}
	for external_item_id, line in current_items.items():
		if flt(line.get("qty")) <= 0:
			line_rows[external_item_id] = None
			continue  # omitted rows are deleted by ERPNext
		row = rows.get((previous_items.get(external_item_id) or {}).get("sales_order_item"))
		if not row:
			raise ReconciliationMismatch(
				f"The Sales Order row for marketplace line {external_item_id} was not found."
			)
		line_rows[external_item_id] = row.name
		trans_items.append(
			{
				"docname": row.name,
				"item_code": row.item_code,
				# update_child_qty_rate assigns description = d.get("description"), so
				# omitting it wipes the row's description.
				"description": row.description,
				"qty": flt(line.get("qty")),
				"rate": flt(line.get("effective_rate"), 6),
				"uom": row.uom,
				"conversion_factor": row.conversion_factor,
				"delivery_date": cstr(row.delivery_date) if row.delivery_date else None,
			}
		)
	update_child_qty_rate("Sales Order", frappe.as_json(trans_items), sales_order.name)
	sales_order = frappe.get_doc("Sales Order", sales_order.name)
	state = read_erp_state(sales_order)
	erp_lines = {line["sales_order_item"]: line for line in state["lines"]}
	if not _effectively_equivalent(previous_items, current_items, erp_lines, state["precision"]):
		raise ReconciliationMismatch(
			"ERPNext cannot reproduce the marketplace amounts for the changed lines."
		)
	return {
		"outcome": UPDATE_SUBMITTED_ORDER,
		"sales_order": sales_order.name,
		"status": sales_order.status,
		# marketplace external_item_id -> Sales Order row name (None: line removed)
		"line_rows": line_rows,
	}
