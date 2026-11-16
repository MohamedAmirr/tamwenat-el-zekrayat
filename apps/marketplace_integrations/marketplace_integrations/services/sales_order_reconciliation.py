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
from frappe import _
from frappe.utils import cstr, flt, fmt_money

from marketplace_integrations.services import sync_reason_codes as reasons

NO_ACTION = "NO_ACTION"
UPDATE_SUBMITTED_ORDER = "UPDATE_SUBMITTED_ORDER"
#: Only the shipping charge changed (#534): ``order_shipping_change`` applies it to the submitted order. ``plan`` adds
#: ``shipping: {"previous", "current"}`` to the result. A shipping change beside item changes rides on
#: ``UPDATE_SUBMITTED_ORDER`` with ``shipping_deferred`` (items first; the shipping follows on the next run).
UPDATE_SHIPPING = "UPDATE_SHIPPING"
CANCEL_ORDER = "CANCEL_ORDER"
CLOSE_REMAINDER = "CLOSE_REMAINDER"
NEEDS_ATTENTION = "NEEDS_ATTENTION"
UNSUPPORTED_COMPENSATING_TRANSACTION = "UNSUPPORTED_COMPENSATING_TRANSACTION"
INACTIVE_ERP_ORDER_STATUSES = {"Cancelled", "Closed"}

OUTCOMES = {
	NO_ACTION,
	UPDATE_SUBMITTED_ORDER,
	UPDATE_SHIPPING,
	CANCEL_ORDER,
	CLOSE_REMAINDER,
	NEEDS_ATTENTION,
	UNSUPPORTED_COMPENSATING_TRANSACTION,
}


class SalesOrderLinesMismatch(ValueError):
	"""The snapshot lines cannot be paired one-to-one with the Sales Order rows."""


# Most document names a merchant-readable message lists per document type (the both-documents
# refund message must stay under the 500-character stored sync message).
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
		"currency": sales_order.currency,
		"per_delivered": flt(sales_order.per_delivered),
		"per_billed": flt(sales_order.per_billed),
		"advance_paid": flt(sales_order.advance_paid),
		"grand_total": flt(sales_order.grand_total),
		"rounded_total": flt(sales_order.rounded_total),
		"precision": {
			"qty": frappe.get_precision("Sales Order Item", "qty"),
			"currency": frappe.get_precision(
				"Sales Order", "rounded_total", currency=sales_order.currency
			),
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
			.limit(_MAX_LINKED_DOCUMENTS + 1)
			.run(pluck=True)
		)
	return linked


def hold_cancel_for_draft_documents(reconciliation_plan, sales_order_name):
	"""Return ``reconciliation_plan``, or a Needs Attention plan when cancelling it would strand drafts.

	ERPNext cancels a Sales Order that still has a draft Delivery Note or Payment Entry (it
	leaves the draft pointing at a cancelled order) and rejects one with a draft Sales
	Invoice only with a generic error.  A ``CANCEL_ORDER`` plan is therefore held, with the
	drafts named, until the merchant submits, cancels or deletes them.  Queries run only
	for a ``CANCEL_ORDER`` plan; ``plan()`` itself stays free of database access.
	"""
	if reconciliation_plan["outcome"] != CANCEL_ORDER:
		return reconciliation_plan
	drafts = _draft_documents(sales_order_name)
	listed = "; ".join(
		named for named in (_documents({"linked_documents": drafts}, doctype) for doctype in drafts) if named
	)
	if not listed:
		return reconciliation_plan
	return _attention(
		_(
			"The marketplace cancelled or refunded this order, but ERPNext has draft documents for it "
			"({0}). The Sales Order was not cancelled. Submit, cancel or delete those documents, "
			"then run the update again."
		).format(listed),
		reconciliation_plan["changes"],
		reasons.DRAFT_DOCUMENTS_BLOCK_CANCEL,
	)


def hold_update_for_shipment(reconciliation_plan, sales_order_name):
	"""Return ``reconciliation_plan``, or a Needs Attention plan when the marketplace changed an order that
	ERPNext cannot follow without guessing (#500, ``order_edit.update_hold_reason``): the parcel is already with the
	carrier (a submitted Delivery Note), the order has more than one draft Delivery Note or a draft that does not ship
	the whole order, or the draft note's carrier shipment is Creating or Unknown (its COD cannot be updated yet).

	The marketplace is right, but the change is held for a person (or a later Retry Sync) instead of failing half
	way or overwriting a note. A draft note whose carrier shipment is Created is NOT a reason: the change is applied and
	the COD difference is listed as a COD difference. Queries run only for an ``UPDATE_SUBMITTED_ORDER`` or
	``UPDATE_SHIPPING`` plan (the latter: see below);
	``plan()`` itself stays free of database access.
	"""
	if reconciliation_plan["outcome"] == UPDATE_SHIPPING:
		# #534: only a Bosta shipment whose result is not known holds a shipping change (a pickup or an invoice is
		# handled by the executor: the order stays as it is and is flagged).
		from marketplace_integrations.services import order_shipping_change

		reason = order_shipping_change.hold_reason(sales_order_name)
		if not reason:
			return reconciliation_plan
		shipping = reconciliation_plan["shipping"]
		return {
			**_attention(
				order_shipping_change.held_reason(
					shipping["previous"], shipping["current"], reason, frappe.db.get_value("Sales Order", sales_order_name, "currency")
				),
				reconciliation_plan["changes"],
				reasons.ORDER_LOCKED_BY_SHIPMENT,
			),
			"shipping": shipping,
		}
	if reconciliation_plan["outcome"] != UPDATE_SUBMITTED_ORDER:
		return reconciliation_plan
	from marketplace_integrations.services import order_edit

	reason = order_edit.update_hold_reason(sales_order_name)
	if not reason:
		return reconciliation_plan
	return _attention(
		_(
			"{0} The marketplace changed the order after that, so ERPNext was not updated. "
			"Review the order with the carrier."
		).format(reason),
		reconciliation_plan["changes"],
		reasons.ORDER_LOCKED_BY_SHIPMENT,
	)


def _draft_documents(sales_order_name):
	drafts = {}
	for doctype, item_doctype, link_field in (
		*_LINKED_DOCUMENT_SOURCES,
		("Payment Entry", "Payment Entry Reference", "reference_name"),
	):
		parent = frappe.qb.DocType(doctype)
		item = frappe.qb.DocType(item_doctype)
		query = (
			frappe.qb.from_(item)
			.join(parent)
			.on(item.parent == parent.name)
			.select(parent.name)
			.distinct()
			.where(getattr(item, link_field) == sales_order_name)
			.where(parent.docstatus == 0)
		)
		if doctype == "Payment Entry":
			query = query.where(item.reference_doctype == "Sales Order")
		drafts[doctype] = query.orderby(parent.name).limit(_MAX_LINKED_DOCUMENTS + 1).run(pluck=True)
	return drafts


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


def _shipping_change(previous, current, erp_state):
	"""``{"previous", "current"}`` when the shipping charge differs between the two snapshots (an absent key is 0, compared
	at the order currency's precision), else ``None``. Nothing to compare without a baseline or a current snapshot."""
	if not previous or not current:
		return None
	precision = (erp_state.get("precision") or {}).get("currency", 2)
	old, new = flt(previous.get("shipping"), precision), flt(current.get("shipping"), precision)
	if old == new:
		return None
	return {"previous": old, "current": new}


def plan(previous, current, evidence, erp_state):
	"""Classify a desired commercial snapshot (``_plan``) with a marketplace shipping change (#534) handled:
	the planner judges everything else as if the shipping had not changed, then

	- nothing else to do on an active submitted order: ``UPDATE_SHIPPING`` (``shipping`` = old and new amount);
	- items change too: the ``UPDATE_SUBMITTED_ORDER`` plan carries ``shipping_deferred`` (the items are applied first,
	  the baseline keeps the old shipping, and the next run applies the shipping alone);
	- any other result (cancel, close, a Needs Attention reason) stands as it is: the shipping does not change it.

	While an item edit waits for the marketplace's confirmation (``outbound_edit_requested`` / ``_in_flight``) the
	shipping change is not looked at separately: ``_plan`` reports it (``SHIPPING_CHANGED``) until the edit settles."""
	evidence = evidence or {}
	shipping = _shipping_change(previous, current, erp_state)
	if not shipping or evidence.get("outbound_edit_requested") or evidence.get("outbound_edit_in_flight"):
		return _plan(previous, current, evidence, erp_state)
	rest = {**current}
	if "shipping" in previous:
		rest["shipping"] = previous["shipping"]
	else:
		rest.pop("shipping", None)
	result = _plan(previous, rest, evidence, erp_state)
	if result["outcome"] == UPDATE_SUBMITTED_ORDER:
		return {**result, "shipping_deferred": True}
	if (
		result["outcome"] == NO_ACTION
		and erp_state.get("docstatus") == 1
		and erp_state.get("status") not in INACTIVE_ERP_ORDER_STATUSES
	):
		return {
			**_result(
				UPDATE_SHIPPING,
				_("The shipping charge changed in the marketplace from {0} to {1}.").format(
					fmt_money(shipping["previous"], currency=erp_state.get("currency")),
					fmt_money(shipping["current"], currency=erp_state.get("currency")),
				),
				[{"kind": "header_changed", "field": "shipping", **shipping}],
			),
			"shipping": shipping,
		}
	return result


def _plan(previous, current, evidence, erp_state):
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
	- ``outbound_edit_requested``: ERPNext's item edit (#356) was accepted by the marketplace
	  but the order's snapshot does not reflect it yet; while the marketplace quantities
	  still equal the baseline this is ``NO_ACTION`` so the newer ERP quantities are not
	  reverted.  ``outbound_edit_in_flight``: the edit's outcome is unknown; in the same
	  situation the order needs attention, never ``NO_ACTION``.  Both are judged only after the
	  inactive-order and cancellation branches.
	- ``inactive_order_instruction``: merchant-readable text appended to the "no longer an
	  active submitted document" advisory (what to do on the marketplace).
	- ``source``: where the evidence comes from (e.g. ``"marketplace"``); informational.

	A Closed order with no delivery or billing is judged as if it were open; a resulting
	``CANCEL_ORDER`` / ``UPDATE_SUBMITTED_ORDER`` plan then carries ``reopen: True``
	(the executor must reopen it first).  Cancelled orders are never reopened.
	"""
	evidence = evidence or {}
	if not previous:
		return _attention(
			_("No reconciled snapshot baseline is available for this Sales Order."),
			reason_code=reasons.NO_BASELINE,
		)
	# A refund needs a compensating ERP transaction only once the ERP order has
	# delivery or billing; an untouched order falls through (e.g. CANCEL_ORDER).
	# Return evidence always needs one, until the prepared return document was
	# submitted (``compensating_document_submitted``, #364).
	if not evidence.get("compensating_document_submitted") and (
		evidence.get("return") or (evidence.get("refund") and _has_execution(erp_state))
	):
		return _result(
			UNSUPPORTED_COMPENSATING_TRANSACTION,
			_refund_message(evidence, erp_state)
			or _("Marketplace return or refund evidence needs a compensating ERP transaction."),
			reason_code=reasons.RETURN_REQUIRES_RECEIPT_REVIEW,
		)

	previous_items = _items_by_external_id(previous)
	current_items = _items_by_external_id(current)
	if previous_items is None or current_items is None:
		return _attention(
			_("The marketplace snapshot has duplicate or missing line identities."),
			reason_code=reasons.DUPLICATE_LINE_IDENTITY,
		)
	# Every line gone or at zero quantity is the same decision as a full cancellation.
	# An absent "items" key means "no current snapshot" (not "no lines").
	all_zero = "items" in current and not any(flt(line.get("qty")) > 0 for line in current["items"] or [])
	if any(
		flt(previous_items[external_item_id].get("qty")) <= 0 < flt(line.get("qty"))
		for external_item_id, line in current_items.items()
		if external_item_id in previous_items
	):
		return _attention(
			_("A marketplace line returned to a positive quantity; this needs a structural change."),
			reason_code=reasons.LINE_RESTORED_FROM_ZERO,
		)
	if erp_state.get("docstatus") != 1 or erp_state.get("status") in INACTIVE_ERP_ORDER_STATUSES:
		if (
			(evidence.get("outbound_cancel_requested") or evidence.get("compensating_document_submitted"))
			and evidence.get("cancellation") == "full"
			and _is_reopenable_closed_order(erp_state)
		):
			# Echo of our own outbound cancel (#181): ERPNext closed the order, asked the
			# marketplace to cancel it, and the marketplace now says cancelled.  Both sides
			# agree; the order stays Closed (it must not be reopened just to be cancelled).
			return _result(
				NO_ACTION,
				_("The marketplace order was cancelled at ERPNext's request; the Sales Order stays Closed."),
			)
		if _is_reopenable_closed_order(erp_state):
			# Closed with no delivery or billing: judge it as the open order it would be after
			# a reopen; only a cancel/update result carries the reopen flag (#180).
			reopened = _plan(previous, current, evidence, {**erp_state, "status": "To Deliver and Bill"})
			if reopened["outcome"] in (CANCEL_ORDER, UPDATE_SUBMITTED_ORDER):
				reopened["reopen"] = True
				reopened["message"] = _("{0} The Sales Order is Closed; it will be reopened first.").format(
					reopened["message"]
				)
			elif reopened["outcome"] == NO_ACTION:
				# Equivalent to the marketplace while it still expects fulfillment (a cancelling
				# intent never yields NO_ACTION): a manual close is divergence. Advisory only.
				return _attention(
					_(
						"The Sales Order is Closed in ERPNext but the marketplace still expects fulfillment; "
						"reopen it or review the order."
					),
					reason_code=reasons.CLOSED_EXPECTS_FULFILLMENT,
				)
			return reopened
		if (evidence.get("cancellation") == "full" or all_zero) and _inactive_order_converged(erp_state):
			# The marketplace cancelled the order and ERPNext is already cancelled (or
			# closed with nothing left to deliver): both sides agree, nothing to do.
			return _result(
				NO_ACTION, _("The cancelled marketplace order is already cancelled or closed in ERPNext.")
			)
		message = _("The linked ERPNext Sales Order is no longer an active submitted document.")
		# Platform-specific wording is built by the integration side (#181), never here.
		instruction = cstr(evidence.get("inactive_order_instruction")).strip()
		return _attention(
			f"{message} {instruction}" if instruction else message, reason_code=reasons.ORDER_INACTIVE
		)

	if evidence.get("cancellation") == "full" and evidence.get("cancelled_after_fulfillment"):
		# Reached only while the ERP order is still active (an already cancelled or
		# closed order converged above). The marketplace cancelled after the goods
		# left: only a delivery recorded in ERPNext makes this a compensating (return)
		# transaction; otherwise a person has to reconcile what physically happened.
		if _has_delivery(erp_state):
			return _result(
				UNSUPPORTED_COMPENSATING_TRANSACTION,
				_(
					"The marketplace order was cancelled after fulfillment and ERPNext already has delivery; "
					"a return or other compensating transaction is needed."
				),
				reason_code=reasons.CANCELLED_AFTER_FULFILLMENT_DELIVERED,
			)
		if _has_execution(erp_state):
			return _attention(
				_(
					"The marketplace order was cancelled after fulfillment, but ERPNext has billing without "
					"delivery; review what happened to the goods."
				),
				reason_code=reasons.CANCELLED_AFTER_FULFILLMENT_BILLED,
			)
		# No delivery and no billing: ERPNext never recorded the goods leaving, so the Sales Order
		# is cancelled like any other cancellation (below); what happened to the parcel is physical.

	header_changes = _header_changes(previous, current)
	changes = _line_changes(previous_items, current_items)
	if header_changes:
		shipping_change = next((change for change in header_changes if change["field"] == "shipping"), None)
		if shipping_change:
			# Reached only while an item edit waits for the marketplace (``plan`` handles every other shipping change).
			return _attention(
				_(
					"The shipping charge differs from the submitted Sales Order "
					"(Sales Order {0}, marketplace {1}) while an item edit waits for the marketplace's confirmation. "
					"Run the update again once the edit is confirmed."
				).format(shipping_change["previous"] or 0, shipping_change["current"] or 0),
				header_changes + changes,
				reasons.SHIPPING_CHANGED,
			)
		return _attention(
			_("A submitted Sales Order has structural commercial changes."),
			header_changes + changes,
			reasons.HEADER_CHANGED,
		)
	if (
		(evidence.get("outbound_edit_requested") or evidence.get("outbound_edit_in_flight"))
		and evidence.get("cancellation") != "full"
		and "items" in current
		and {key: flt(line.get("qty")) for key, line in previous_items.items()}
		== {key: flt(line.get("qty")) for key, line in current_items.items()}
	):
		# ERPNext sent its item edit to the marketplace (#356) but the marketplace order has not
		# caught up yet, so it still shows the old quantities.  Once the echo (or the refresh)
		# brings the new quantities, this no longer matches and the normal comparison runs.
		if evidence.get("outbound_edit_requested"):
			# Accepted by the marketplace: ERPNext holds the newer state, do not revert it.
			return _result(
				NO_ACTION,
				_("ERPNext's item edit was sent to the marketplace; waiting for it to be confirmed."),
			)
		return _attention(
			_(
				"ERPNext's item edit may not have reached the marketplace (the outcome is unknown); "
				"check the order in the marketplace."
			),
			reason_code=reasons.EDIT_OUTCOME_UNKNOWN,
		)

	# Case 10 (#364): an added or removed line is applied by ``_execute_update`` through
	# ``update_child_qty_rate``; ERPNext refuses a row it cannot add or drop (delivered,
	# ordered, in a work order, billed), which ends as Needs Action for the whole order.

	erp_lines = {line["sales_order_item"]: line for line in erp_state.get("lines") or []}
	equivalent = _effectively_equivalent(
		previous_items, current_items, erp_lines, erp_state.get("precision") or {}
	)
	if not equivalent and not changes:
		return _attention(
			_("The ERPNext Sales Order no longer represents the reconciled marketplace state."),
			reason_code=reasons.ERP_STATE_DIVERGED,
		)

	explicit_cancellation = evidence.get("cancellation") == "full"
	if all_zero and not explicit_cancellation and _has_delivery(erp_state):
		return _attention(
			_("All lines are zero but the order was already delivered; explicit cancellation is required."),
			changes,
			reasons.ALL_ZERO_DELIVERED,
		)
	cancelling = explicit_cancellation or all_zero
	if equivalent and not cancelling:
		# ERPNext already holds the current marketplace state (including after a
		# manual ERP-side edit): nothing to do, whatever its billing/delivery.
		return _result(NO_ACTION, _("Marketplace and ERPNext commercial state are effectively equivalent."))
	if not cancelling:
		blocked_reason = _lifecycle_block_reason(evidence, changes)
		if blocked_reason:
			return _attention(blocked_reason, changes, reasons.LIFECYCLE_BLOCKS_CHANGE)
		if any(
			change["kind"] == "line_changed" and change["field"] in _MAPPING_SOURCED_LINE_FIELDS
			for change in changes
		):
			return _attention(
				_(
					"The item, unit of measure or warehouse of a line changed (mapping or account change); "
					"this cannot be applied automatically."
				),
				changes,
				reasons.LINE_IDENTITY_CHANGED,
			)
	if cancelling and _has_execution(erp_state) and not _has_outstanding_remainder(erp_state):
		# Fully delivered (billed or not): only a compensating transaction can undo it,
		# so this takes precedence over the "already billed" blocker below.
		delivery_notes = _documents(erp_state, "Delivery Note")
		return _result(
			UNSUPPORTED_COMPENSATING_TRANSACTION,
			(
				_(
					"The marketplace cancelled this order, but {0} already delivered all of it. "
					"Create a Sales Return against {0} for the goods that came back, "
					"or review the order manually."
				).format(delivery_notes)
				if delivery_notes
				else _(
					"The cancelled marketplace order is already fully delivered in ERPNext; "
					"a return or other compensating transaction is needed."
				)
			),
			changes,
			reasons.CANCELLED_FULLY_DELIVERED,
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
				return _attention(
					_("The reconciled Sales Order line is missing from ERPNext."), changes, reasons.SO_LINE_MISSING
				)
			# An all-zero order closes whatever remains after delivery; only an
			# individual line reduction must stay above what was delivered.
			if not all_zero and flt(current_line.get("qty")) < flt(erp_line.get("delivered_qty")):
				return _attention(
					_("Requested quantity is below the quantity already delivered in ERPNext."),
					changes,
					reasons.QTY_BELOW_DELIVERED,
				)
			if flt(erp_line.get("billed_amt")):
				invoices = _documents(erp_state, "Sales Invoice")
				if cancelling and invoices:
					return _attention(
						_(
							"The marketplace cancelled this order, but {0} already bills it in ERPNext, "
							"so the Sales Order cannot be cancelled automatically. Cancel the Sales Invoice "
							"(or issue a Credit Note if it is submitted and paid), then run the update again."
						).format(invoices),
						changes,
						reasons.ALREADY_BILLED,
					)
				return _attention(
					_("A submitted Sales Order line is already billed in ERPNext."), changes, reasons.ALREADY_BILLED
				)

	if cancelling:
		if not _has_execution(erp_state):
			if flt(erp_state.get("advance_paid")) > 0:
				return _attention(
					_(
						"An advance payment exists on the Sales Order; reallocate or cancel it "
						"before the order can be cancelled."
					),
					changes,
					reasons.ADVANCE_BLOCKS_CANCEL,
				)
			if evidence.get("compensating_document_submitted"):
				# The prepared return was submitted (#364).  ERPNext refuses to cancel a Sales Order
				# that still has submitted Delivery Notes (the delivery and its return), so the
				# cancelled order is closed instead; ``compensating_document_submitted`` also keeps
				# the Closed order from being reopened (above).
				return _result(
					CLOSE_REMAINDER,
					_(
						"The returned goods were booked in ERPNext. ERPNext cannot cancel an order with "
						"submitted delivery documents, so the Sales Order is closed."
					),
					changes,
				)
			message = _("The marketplace order is fully cancelled before ERP execution.")
			if evidence.get("cancelled_after_fulfillment"):
				message = _(
					"The marketplace order was cancelled after fulfillment; ERPNext has no delivery or "
					"billing, so the Sales Order is cancelled. Check what happened to the parcel."
				)
			return _result(CANCEL_ORDER, message, changes)
		if _has_outstanding_remainder(erp_state):
			return _result(
				CLOSE_REMAINDER, _("Only the outstanding Sales Order remainder can be closed."), changes
			)

	if _has_execution(erp_state):
		message = _("ERPNext delivery or billing execution blocks an automatic submitted-order update")
		named = [_documents(erp_state, "Delivery Note"), _documents(erp_state, "Sales Invoice")]
		named = "; ".join(documents for documents in named if documents)
		return _attention(
			f"{message} ({named})." if named else f"{message}.", changes, reasons.EXECUTION_BLOCKS_UPDATE
		)
	projected_total = _projected_total_after_line_changes(previous, current, erp_state)
	currency_precision = (erp_state.get("precision") or {}).get("currency", 2)
	advance_paid = flt(erp_state.get("advance_paid"), currency_precision)
	if projected_total is not None and projected_total < advance_paid:
		return _attention(
			_(
				"This marketplace change would reduce the Sales Order total below its advance of {0}. "
				"Reallocate or cancel the advance before updating the order."
			).format(advance_paid),
			changes,
			reasons.TOTAL_BELOW_ADVANCE,
		)
	return _result(
		UPDATE_SUBMITTED_ORDER,
		_("The submitted Sales Order changes are eligible for ERPNext validation."),
		changes,
	)


def _projected_total_after_line_changes(previous, current, erp_state):
	"""Estimate the new Sales Order total by applying snapshot line deltas to its current total."""
	if erp_state.get("grand_total") is None:
		return None
	previous_items = _items_by_external_id(previous)
	current_items = _items_by_external_id(current)
	if previous_items is None or current_items is None:
		return None
	line_delta = 0.0
	# Changed and added lines (an added line has no previous amount: it adds its whole amount),
	# then removed lines (they subtract what the baseline had).
	for external_item_id, line in current_items.items():
		old_amount = (
			previous_items[external_item_id].get("fulfilled_total_price")
			if external_item_id in previous_items
			else 0
		)
		new_amount = line.get("fulfilled_total_price")
		if old_amount is None or new_amount is None:
			return None
		line_delta += flt(new_amount) - flt(old_amount)
	for external_item_id, line in previous_items.items():
		if external_item_id not in current_items:
			if line.get("fulfilled_total_price") is None:
				return None
			line_delta -= flt(line.get("fulfilled_total_price"))
	# ERPNext allocates advances against rounded_total (falling back to
	# grand_total). Project the changed grand total at that same currency
	# precision so sub-cent float residue cannot block an otherwise valid update.
	currency_precision = (erp_state.get("precision") or {}).get("currency", 2)
	base_total = erp_state.get("rounded_total") or erp_state.get("grand_total")
	return flt(flt(base_total) + line_delta, currency_precision)


def _documents(erp_state, doctype):
	"""``"Delivery Note A, B"`` (plural-aware) for the linked documents, or ``""`` if none are known.

	At most ``_MAX_LINKED_DOCUMENTS`` names are listed; ``_linked_documents()`` fetches one more
	so ", and more" is appended when there are further documents.
	"""
	names = [cstr(name) for name in (erp_state.get("linked_documents") or {}).get(doctype) or []]
	if not names:
		return ""
	listed = ", ".join(names[:_MAX_LINKED_DOCUMENTS])
	if len(names) > _MAX_LINKED_DOCUMENTS:
		listed += _(", and more")
	return f"{doctype}{'s' if len(names) > 1 else ''} {listed}"


def _refund_message(evidence, erp_state):
	"""Merchant-readable text for a refund/return on an order ERPNext already delivered or billed.

	Empty when ERPNext does not name the documents (the caller keeps the generic text).
	"""
	delivery_notes = _documents(erp_state, "Delivery Note")
	invoices = _documents(erp_state, "Sales Invoice")
	if not (delivery_notes or invoices):
		return ""
	lead = _("The marketplace refunded this order")
	if evidence.get("return"):
		lead = _("The marketplace reported a return for this order")
	if delivery_notes and invoices:
		# Kept short on purpose: with the longest listing (3 + 3 names of 30 characters, each
		# ", and more") this is 482 characters, under the 500 the stored sync message allows.
		return _(
			"{0}, but ERPNext already delivered ({1}) and invoiced ({2}) it. "
			"The Sales Order was not changed. Create a Sales Return and a Credit Note "
			"(compensating transactions) as needed, or review the order manually."
		).format(lead, delivery_notes, invoices)
	if delivery_notes:
		return _(
			"{0}, but it was already delivered in ERPNext ({1}). "
			"The Sales Order was not changed. If the goods came back, create a Sales Return "
			"against {1} (a compensating transaction), otherwise review the order manually."
		).format(lead, delivery_notes)
	return _(
		"{0}, but it was already invoiced in ERPNext ({1}). "
		"The Sales Order was not changed. Create a Credit Note against {1} for the refunded "
		"amount (a compensating transaction), or review the order manually."
	).format(lead, invoices)


def _items_by_external_id(snapshot):
	items = {}
	for line in snapshot.get("items") or []:
		external_item_id = cstr(line.get("external_item_id")).strip()
		if not external_item_id or external_item_id in items:
			return None
		items[external_item_id] = line
	return items


def _header_changes(previous, current):
	"""Commercial header differences between two snapshots.

	``items`` and ``refund_ids`` are compared elsewhere. The Address links
	(``shipping_address_name``, ``customer_address``) are not commercial: a submitted Sales
	Order cannot change them, so a difference (e.g. a phone-only order whose Sales Order
	got an Address after the sync) is no reason for attention and the baseline just advances.
	"""
	ignored = {"items", "refund_ids", "shipping_address_name", "customer_address"}
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
		sales_order_item = (previous_items.get(external_item_id) or {}).get("sales_order_item")
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
		return reason or _("The marketplace order can no longer change, so the change was not applied.")
	started = evidence.get("fulfillment_started")
	for change in changes:
		line_started = (
			started.get(cstr(change.get("external_item_id")).strip())
			if isinstance(started, dict)
			else bool(started)
		)
		if line_started:
			return reason or _(
				"Marketplace line {0} already started fulfillment, so its change was not applied."
			).format(change["external_item_id"])
	if permits is None:
		return reason or _("The marketplace order stage is unknown, so the change was not applied.")
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


def _attention(message, changes=None, reason_code=None):
	return _result(NEEDS_ATTENTION, message, changes, reason_code)


def _result(outcome, message, changes=None, reason_code=None):
	"""A plan result.  Every result other than NO_ACTION carries a ``reason_code``
	(``services/sync_reason_codes.py``), which decides the visible status; the executable
	outcomes (cancel, close, update) have none until an execution fails."""
	result = {"outcome": outcome, "message": message, "changes": changes or []}
	if reason_code:
		result["reason_code"] = reason_code
	return result


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
		# The planner returns CANCEL_ORDER only for an order with NO delivered quantity and NO billed amount (the
		# ALREADY_BILLED / delivered gates in ``plan`` come first), so the order has no submitted Delivery Note or Sales
		# Invoice and the cancel cascade (``order_cancel_cascade.cascade_cancel``) finds nothing to cancel: it neither
		# commits nor calls a carrier here, so the reconcile savepoint survives.
		# The marketplace cancelled the order: an inbound sync never pushes a cancel back to the marketplace. (Imported here:
		# order_cancel_cascade imports marketplace_outbound, which imports this module.)
		from marketplace_integrations.services.order_cancel_cascade import SKIP_PUSH_FLAG

		previous_flag = frappe.flags.get(SKIP_PUSH_FLAG)
		frappe.flags[SKIP_PUSH_FLAG] = True
		try:
			sales_order.cancel()
		finally:
			frappe.flags[SKIP_PUSH_FLAG] = previous_flag
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
	"""Apply quantity/rate changes, added lines and removed lines through ``update_child_qty_rate``.

	A line the baseline already paired with a row is updated in place; a line without a
	baseline row is added (case 10, #364); a row whose line is gone or at zero is left out
	of ``trans_items``, which makes ERPNext delete it.  ERPNext refuses a row it cannot add
	or drop (delivered, ordered, in a work order, billed) by raising a ``ValidationError``,
	which the caller turns into Needs Action for the whole order.
	"""
	from erpnext.controllers.accounts_controller import update_child_qty_rate

	previous_items = _items_by_external_id(previous) or {}
	current_items = _items_by_external_id(current) or {}
	rows = {row.name: row for row in sales_order.items}
	trans_items = []
	line_rows = {}
	added = []
	for external_item_id, line in current_items.items():
		if flt(line.get("qty")) <= 0:
			line_rows[external_item_id] = None
			continue  # omitted rows are deleted by ERPNext
		row_name = (previous_items.get(external_item_id) or {}).get("sales_order_item")
		if not row_name and external_item_id not in previous_items:
			added.append((external_item_id, line))
			continue
		row = rows.get(row_name)
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
	if added:
		trans_items.extend(_new_row_payload(sales_order, line, current) for _id, line in added)
	update_child_qty_rate("Sales Order", frappe.as_json(trans_items), sales_order.name)
	# #500: the order's draft Delivery Note follows the change and the reservation is settled.
	from marketplace_integrations.services import order_edit

	order_edit.after_marketplace_update(sales_order.name)
	sales_order = frappe.get_doc("Sales Order", sales_order.name)
	# The added rows are the ones that did not exist before, paired to the added lines by item.
	new_rows_by_item = {}
	for row in sales_order.items:
		if row.name not in rows:
			new_rows_by_item.setdefault(row.item_code, []).append(row.name)
	for external_item_id, line in added:
		candidates = new_rows_by_item.get(line.get("item_code"))
		if not candidates:
			raise ReconciliationMismatch(f"ERPNext did not add a row for marketplace line {external_item_id}.")
		line_rows[external_item_id] = candidates.pop(0)
	state = read_erp_state(sales_order)
	erp_lines = {line["sales_order_item"]: line for line in state["lines"]}
	# Equivalence is judged against the rows this update actually used, not the baseline's.
	paired = {
		external_item_id: {"sales_order_item": row_name} for external_item_id, row_name in line_rows.items()
	}
	if not _effectively_equivalent(paired, current_items, erp_lines, state["precision"]):
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


def _new_row_payload(sales_order, line, current):
	"""``update_child_qty_rate`` payload for a new row (no ``docname``).

	Adding a row needs create permission on Sales Order (ERPNext checks the session user) and a
	warehouse the account resolves; both are checked here so the refusal is explicit.
	"""
	from erpnext.stock.get_item_details import get_conversion_factor

	frappe.has_permission("Sales Order", "create", throw=True)
	if not line.get("warehouse"):
		raise ReconciliationMismatch(
			f"No warehouse is available for the new marketplace line {line.get('external_item_id')}."
		)
	uom = line.get("uom")
	return {
		"item_code": line.get("item_code"),
		"qty": flt(line.get("qty")),
		"rate": flt(line.get("effective_rate"), 6),
		"uom": uom,
		"conversion_factor": flt(get_conversion_factor(line.get("item_code"), uom).get("conversion_factor"))
		or 1,
		"warehouse": line.get("warehouse"),
		"delivery_date": cstr(current.get("delivery_date")) or cstr(sales_order.delivery_date),
	}
