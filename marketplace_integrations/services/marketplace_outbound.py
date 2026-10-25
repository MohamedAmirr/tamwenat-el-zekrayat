# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Outbound order actions: ask the marketplace to cancel an order (#181).

Everything the merchant does in ERPNext that must be mirrored on a marketplace goes
through here; today that is one action, the platform cancel.  Permission checks are
the caller's (``api/marketplace_outbound.py``); this module never checks them.

``get_platform_cancel_option()`` is read-only.  ``request_platform_cancel()`` re-checks
the option under the Marketplace Order row lock, calls the platform, then records the
echo guard (``Marketplace Order.outbound_cancelled_on`` plus an ``Order Action`` sync
log row) and COMMITS it before the returned order is upserted.  The order must be
committed first because the upsert's save re-plans the order
(``sales_order_sync.set_sync_status_from_current_snapshot``) and the planner has to see
the marker, or it would reopen the Closed Sales Order to cancel it (#180).  Committing
here also means a later rollback in the same request cannot lose a platform call that
already happened.

Platform rules (V1, no merchant-visible override):

- Talabat: eligible only from ``RECEIVED`` / ``UPDATE_CART``; every other or unknown
  status is blocked.  The reason is one of Talabat's ``CANCELLATION_REASONS``.
- Noon: always blocked (no order-level cancel; ``UpdateOrder`` / ``CancelShipment``
  per ``docs/noon-integration/noon_api_full.md`` are per unit / shipment).
- Shopify: not already cancelled, no authorized payment hold, and every line's
  fulfillment state is known and unstarted. Shopify checks for active returns and
  its own fulfillment restrictions.
- Any other platform, or a Sales Order with no Marketplace Order: not applicable.

ERP rule for every platform: no delivered quantity and no billed amount on any line.
"""

import hashlib
import json

import frappe
from frappe import _
from frappe.utils import cstr, flt, now_datetime

from marketplace_integrations.integrations.talabat.orders import CANCELLATION_REASONS
from marketplace_integrations.services.marketplace_orders import rollback_to_savepoint
from marketplace_integrations.services.sales_order_reconciliation import _has_execution, read_erp_state

ELIGIBLE = "eligible"
BLOCKED = "blocked"
NOT_APPLICABLE = "not_applicable"

# Talabat statuses (docs/talabat-integration/openapi.json order.Order.status) from which
# a vendor may still cancel.
TALABAT_CANCELLABLE_STATUSES = {"RECEIVED", "UPDATE_CART"}

_ORDER_ACTION_CANCEL = "platform_cancel"
_ORDER_ACTION_EDIT = "shopify_order_edit"
_EDIT_IN_FLIGHT = "in_flight"
_EDIT_COMPLETED = "completed"


def get_shopify_order_edit_preview(sales_order):
	"""Return the locally-calculated, no-network preview for a Shopify edit."""
	sales_order = _as_sales_order(sales_order)
	marketplace_order = sales_order.get("custom_marketplace_order")
	if not marketplace_order:
		return {"state": NOT_APPLICABLE, "changes": [], "reasons": []}
	order = frappe.get_doc("Marketplace Order", marketplace_order)
	if order.platform != "Shopify":
		return {"state": NOT_APPLICABLE, "changes": [], "reasons": []}
	return _shopify_edit_preview(sales_order, order)


def send_shopify_order_edit(sales_order, fingerprint):
	"""Recheck and send an explicit Shopify order edit, then re-baseline it.

	``fingerprint`` is the one the preview returned: staff confirmed exactly that change
	list, so a different one is rejected.  The same fingerprint is stored on the
	Marketplace Order (``outbound_edit_fingerprint`` / ``outbound_edit_state``) under the
	row lock and committed BEFORE the HTTP call, like the cancel marker: a repeated or
	concurrent send of the same changes is refused, and an ambiguous failure keeps it.
	While any marker is set, every preview and send is refused.  The state becomes
	``completed`` once Shopify accepts the commit; it is cleared when the refreshed order
	is re-baselined (``sales_order_sync.rebaseline_after_outbound_edit``) or by a System
	Manager (``clear_edit_marker_manually``).  Only ``completed`` makes the planner keep
	the ERP quantities (``plan()``, evidence ``outbound_edit_requested``); ``in_flight``
	means the outcome is unknown and the order stays Modified After Sync.

	COMMITS the caller's open transaction (before the lock, before the platform call and
	after it), like ``request_platform_cancel``.
	"""
	from marketplace_integrations.integrations.shopify.order_edit import (
		OrderEditOutcomeUnknown,
		commit_order_edit,
	)

	sales_order = _as_sales_order(sales_order)
	marketplace_order = sales_order.get("custom_marketplace_order")
	if not marketplace_order:
		frappe.throw(_("Sales Order {0} is not linked to a marketplace order.").format(sales_order.name))
	# Lock only while reading the current local state.  An HTTP call must never run
	# while the MariaDB row lock is held.
	order = _lock_and_load_order(marketplace_order)
	if order.platform != "Shopify":
		frappe.throw(_("Marketplace order {0} is not a Shopify order.").format(order.name))
	preview = _shopify_edit_preview(frappe.get_doc("Sales Order", sales_order.name), order)
	if preview["state"] != ELIGIBLE:
		frappe.throw(" ".join(preview["reasons"]) or _("No Shopify item changes are ready to send."))
	if cstr(fingerprint).strip() != preview["fingerprint"]:
		frappe.throw(_("The item changes are not the ones you reviewed. Review them again."))
	changes = preview["changes"]
	fingerprint = preview["fingerprint"]
	frappe.db.set_value(
		"Marketplace Order",
		order.name,
		{"outbound_edit_fingerprint": fingerprint, "outbound_edit_state": _EDIT_IN_FLIGHT},
		update_modified=False,
	)
	frappe.db.commit()
	details = {"changes": changes}
	try:
		account = frappe.get_doc("Marketplace Account", order.marketplace_account)
		result = commit_order_edit(
			account,
			order.external_order_id,
			changes,
			_("Item changes sent from ERPNext Sales Order {0}.").format(sales_order.name),
		)
	except Exception as exc:
		if not isinstance(exc, OrderEditOutcomeUnknown):
			# Confirmed not committed on Shopify: free the marker so the changes can be retried,
			# but only if it is still the one this call set.
			clear_outbound_edit_marker(order.name, only_if_fingerprint=fingerprint)
		_record_order_action_error(order, sales_order.name, details, exc, action=_ORDER_ACTION_EDIT)
		frappe.db.commit()
		raise
	success_messages = result.get("success_messages") or []
	# Shopify applied it.  Record that durably before anything that can fail (refresh).
	_update_edit_marker(order.name, {"outbound_edit_state": _EDIT_COMPLETED}, only_if_fingerprint=fingerprint)
	_log_order_action(
		order,
		sales_order.name,
		details,
		{"success_messages": success_messages},
		action=_ORDER_ACTION_EDIT,
	)
	frappe.db.commit()
	refreshed = _refresh_after_shopify_edit(order, sales_order.name)
	return {"ok": True, "refreshed": refreshed, "success_messages": success_messages}


def _refresh_after_shopify_edit(order, sales_order_name):
	"""Re-import the order and re-baseline it; a failure here must not hide the edit.

	The edit and the ``completed`` marker are already committed, so a failure is logged
	and reported as ``False`` (the marker stays: "completed, refresh pending"; the echo
	webhook or a manual refresh reconciles).
	"""
	from marketplace_integrations.integrations.shopify.orders import import_order
	from marketplace_integrations.services.sales_order_sync import rebaseline_after_outbound_edit

	savepoint = "marketplace_outbound_edit_refresh"
	frappe.db.savepoint(savepoint)
	try:
		refreshed = import_order(order.marketplace_account, order.external_order_id)
		if not refreshed:
			frappe.throw(_("Shopify returned no order after committing the item changes."))
		rebaseline_after_outbound_edit(refreshed, frappe.get_doc("Sales Order", sales_order_name))
	except Exception:
		# MariaDB error 1020 rolls back the whole transaction, savepoint included; the edit
		# and the marker are already committed, so a full rollback loses nothing.
		rollback_to_savepoint(savepoint, "Marketplace outbound edit: rollback failed")
		frappe.log_error(title=_("Marketplace outbound Shopify edit: refreshing the order failed"))
		return False
	frappe.db.release_savepoint(savepoint)
	frappe.db.commit()
	return True


_CLOSED_SALES_ORDER_STATUSES = {"Closed", "Cancelled", "On Hold"}


def _shopify_edit_preview(sales_order, order):
	reasons = []
	if sales_order.docstatus != 1 or sales_order.status in _CLOSED_SALES_ORDER_STATUSES:
		reasons.append(
			_("Only a submitted Sales Order that is not Closed, On Hold or Cancelled can be sent to Shopify.")
		)
	if order.cancelled_at:
		reasons.append(_("The Shopify order is cancelled and its items cannot be edited."))
	# PARTIALLY_REFUNDED is blocked too (V1 decision D5): refunds already adjusted the lines.
	if cstr(order.external_financial_status).strip().upper() in {"REFUNDED", "PARTIALLY_REFUNDED"}:
		reasons.append(_("The Shopify order is refunded and its items cannot be edited."))
	if cstr(order.shop_currency).strip().upper() != cstr(sales_order.currency).strip().upper():
		reasons.append(_("The Sales Order currency does not match the Shopify shop currency."))
	if order.get("outbound_edit_fingerprint"):
		# Any marker blocks, not only one with the same changes: an earlier edit is either
		# unconfirmed or not yet reflected in the baseline, so another one could stack on it.
		reasons.append(
			_(
				"An item edit was already sent to Shopify; refresh the order to confirm it before sending another."
			)
			if cstr(order.get("outbound_edit_state")) == _EDIT_COMPLETED
			else _(
				"An earlier item edit may or may not have reached Shopify; check the order in Shopify, "
				"then ask a System Manager to clear the edit marker on the Marketplace Order."
			)
		)
	if _has_discount_allocations(order):
		reasons.append(
			_(
				"The Shopify order has discounts on its items; Shopify recalculates them when items change, "
				"so edit this order in Shopify."
			)
		)
	previous = _parse_snapshot(order)
	if not previous:
		reasons.append(_("No synced Shopify line baseline is available for this Sales Order."))
		return _edit_preview_result(order, [], reasons)
	baseline_lines = previous.get("items") or []
	if any(flt(line.get("qty")) > 0 and not line.get("sales_order_item") for line in baseline_lines):
		reasons.append(
			_(
				"The synced Shopify baseline does not identify every Sales Order line; "
				"synchronize the order again before editing."
			)
		)
		return _edit_preview_result(order, [], reasons)
	previous_by_row = {
		line["sales_order_item"]: line for line in baseline_lines if line.get("sales_order_item")
	}
	marketplace_items = {item.external_item_id: item for item in order.items or []}
	changes = []
	seen_rows = set()
	for row in sales_order.items or []:
		baseline = previous_by_row.get(row.name)
		if baseline:
			seen_rows.add(row.name)
			if cstr(row.item_code) != cstr(baseline.get("item_code")) or cstr(row.uom) != cstr(
				baseline.get("uom")
			):
				_add_reason(
					reasons,
					_(
						"An item or unit of measure was changed on a synced line; it cannot be swapped on Shopify."
					),
				)
			if flt(row.qty) != flt(baseline.get("qty")):
				_reason_for_changed_row(row, reasons)
				_reason_for_shopify_line(marketplace_items.get(baseline.get("external_item_id")), reasons)
				changes.append(
					{
						"kind": "set_quantity",
						"external_item_id": baseline["external_item_id"],
						"quantity": flt(row.qty),
						"label": row.item_name or row.item_code,
						"old_quantity": flt(baseline.get("qty")),
					}
				)
		else:
			_reason_for_changed_row(row, reasons)
			mapping = _single_shopify_mapping(order.marketplace_account, row.item_code)
			if not mapping:
				reasons.append(
					_("Item {0} needs exactly one enabled Shopify variant mapping.").format(row.item_code)
				)
				continue
			if flt(row.conversion_factor or 1) != 1:
				reasons.append(_("Item {0} must use a sales UOM conversion of 1.").format(row.item_code))
			changes.append(
				{
					"kind": "add_variant",
					"external_variant_id": mapping.external_variant_id,
					"quantity": flt(row.qty),
					"label": row.item_name or row.item_code,
					"old_quantity": 0,
				}
			)
	for row_name, baseline in previous_by_row.items():
		if row_name not in seen_rows:
			_reason_for_shopify_line(marketplace_items.get(baseline.get("external_item_id")), reasons)
			changes.append(
				{
					"kind": "set_quantity",
					"external_item_id": baseline["external_item_id"],
					"quantity": 0,
					"label": baseline.get("item_code") or baseline["external_item_id"],
					"old_quantity": flt(baseline.get("qty")),
				}
			)
	# Checked on the float, before any cast: int() would silently round 1.5 to 1.
	if any(change["quantity"] < 0 or change["quantity"] != int(change["quantity"]) for change in changes):
		reasons.append(_("Shopify item quantities must be whole non-negative numbers."))
	fingerprint = _edit_fingerprint(changes, order.last_synced_hash) if changes else None
	if not changes and not reasons:
		reasons.append(_("There are no item changes to send to Shopify."))
	return _edit_preview_result(
		order,
		changes,
		reasons,
		fingerprint=fingerprint,
		totals=(
			_baseline_total(previous),
			sum(flt(row.amount) for row in sales_order.items or []) + flt(previous.get("shipping")),
		),
	)


def _edit_preview_result(order, changes, reasons, fingerprint=None, totals=(0, 0)):
	return {
		"state": BLOCKED if reasons else ELIGIBLE,
		"platform": "Shopify",
		"marketplace_order": order.name,
		"changes": changes,
		"reasons": reasons,
		"fingerprint": fingerprint,
		# Items plus shipping, before any tax: the synced baseline against the Sales Order's own
		# line amounts now (a taxed Sales Order total would not be comparable with the baseline).
		"old_total": totals[0],
		"new_total": totals[1],
		"total_difference": totals[1] - totals[0],
	}


def _baseline_total(snapshot):
	"""The baseline's line amounts plus shipping (the Sales Order's items and shipping charge)."""
	lines = sum(flt(line.get("fulfilled_total_price")) for line in snapshot.get("items") or [])
	return flt(lines + flt(snapshot.get("shipping")))


def _edit_fingerprint(changes, baseline_hash):
	return hashlib.sha256(
		json.dumps(
			{"changes": changes, "baseline": cstr(baseline_hash)}, sort_keys=True, default=str
		).encode()
	).hexdigest()


def _add_reason(reasons, reason):
	if reason not in reasons:
		reasons.append(reason)


def _has_discount_allocations(order):
	"""True when any Shopify line carries discount allocations (stored raw in ``promotions``)."""
	for item in order.items or []:
		value = item.get("promotions")
		if not value:
			continue
		try:
			value = frappe.parse_json(value)
		except ValueError:
			return True  # unreadable: fail closed
		if value:
			return True
	return False


def _reason_for_changed_row(sales_order_item, reasons):
	"""Delivery and billing on a changed or added Sales Order row stop an edit."""
	if flt(sales_order_item.delivered_qty) or flt(sales_order_item.billed_amt):
		_add_reason(reasons, _("A changed line already has delivered quantity or a billed amount."))
	if frappe.db.exists("Delivery Note Item", {"so_detail": sales_order_item.name, "docstatus": 0}):
		_add_reason(reasons, _("A changed line is on a draft Delivery Note; submit or delete it first."))


def _reason_for_shopify_line(marketplace_item, reasons):
	"""A changed Shopify line must be known to be unfulfilled."""
	if not marketplace_item:
		_add_reason(
			reasons, _("The Shopify fulfillment state for a changed line is unknown; refresh it first.")
		)
		return
	from marketplace_integrations.integrations.shopify.order_reconciliation import fulfillment_started

	if fulfillment_started(marketplace_item) is not False:
		_add_reason(reasons, _("A changed Shopify line is fulfilled or has an unknown fulfillment state."))


def _single_shopify_mapping(account, item_code):
	# get_all: a system-internal lookup that builds the change list; the caller is already
	# authorized on the Sales Order and Marketplace Order by api/marketplace_outbound.py.
	rows = frappe.get_all(
		"Marketplace Product Mapping",
		filters={"marketplace_account": account, "item": item_code, "enabled": 1},
		fields=["external_variant_id"],
	)
	return frappe._dict(rows[0]) if len(rows) == 1 and cstr(rows[0].external_variant_id).strip() else None


def _parse_snapshot(order):
	try:
		value = frappe.parse_json(order.last_synced_snapshot)
		return value if isinstance(value, dict) else None
	except (TypeError, ValueError):
		return None


def get_platform_cancel_option(sales_order):
	"""Whether, and how, the marketplace order behind ``sales_order`` can be cancelled.

	``sales_order`` is a Sales Order name or document.  Returns a plain dict:
	``state`` (``eligible`` / ``blocked`` / ``not_applicable``), ``platform``,
	``marketplace_order``, ``instruction`` (what to do on the platform), ``reasons``
	(why it is blocked), ``cancellation_reasons`` (the values the platform accepts,
	empty when none applies) and three platform-neutral booleans a client can use to
	skip the dialog without parsing text: ``already_requested`` (the echo-guard marker is
	set), ``platform_cancelled`` (the platform reports the order fully cancelled, by the
	same evidence the echo rule reads) and ``erp_executed`` (delivered quantity or billed
	amount on the Sales Order).  Never writes.
	"""
	sales_order = _as_sales_order(sales_order)
	marketplace_order = sales_order.get("custom_marketplace_order")
	if not marketplace_order:
		return _option(NOT_APPLICABLE, None, None, "", [])
	return _build_option(sales_order, frappe.get_doc("Marketplace Order", marketplace_order))


def platform_instruction(order):
	"""Merchant-readable text for what to do on the platform about this order.

	Used in the reconciliation advisory for a Sales Order that is Cancelled (or Closed
	after delivery) while the platform order may still be active.  Platform wording
	lives here, not in the platform-neutral planner.
	"""
	platform = order.platform or "Talabat"
	if getattr(order, "outbound_cancelled_on", None):
		return _(
			"ERPNext already asked {0} to cancel this order; refresh the order to confirm its status."
		).format(platform)
	if platform == "Talabat":
		status = cstr(order.external_status).strip()
		if status == "CANCELLED":
			return _("The order is already cancelled on Talabat.")
		if status in TALABAT_CANCELLABLE_STATUSES:
			return _("Cancel the order on Talabat so the customer is not left waiting.")
		return _(
			"The Talabat order is {0} and can no longer be cancelled from ERPNext; handle it with Talabat."
		).format(status or _("in an unknown status"))
	if platform == "Shopify":
		if getattr(order, "cancelled_at", None):
			return _("The order is already cancelled on Shopify.")
		return _("Cancel the Shopify order from ERPNext when it is eligible.")
	if platform == "Noon":
		return _("ERPNext cannot cancel Noon orders; handle the order in Noon.")
	return ""


def request_platform_cancel(sales_order, cancellation_reason=None):
	"""Ask the platform to cancel the order behind ``sales_order``; record the echo guard.

	Re-checks eligibility under the Marketplace Order row lock.  A second request (the
	marker is already set) sends no API call and returns ``already_requested``.  Raises
	``frappe.ValidationError`` when the order is not eligible (nothing recorded).  When the
	platform call fails, one ``Order Action`` Error sync log row is written and committed
	before the error is re-raised; the marker stays unset, so a retry calls the platform
	again.  An invalid Talabat cancellation reason is rejected before the platform call,
	with no log row.

	COMMITS the caller's open transaction (before the row lock and after the platform
	call), so callers, the #183 client flow included, must never invoke it in the middle
	of a Sales Order cancel/close transaction.
	"""
	sales_order = _as_sales_order(sales_order)
	marketplace_order = sales_order.get("custom_marketplace_order")
	if not marketplace_order:
		frappe.throw(_("Sales Order {0} is not linked to a marketplace order.").format(sales_order.name))

	order = _lock_and_load_order(marketplace_order)
	if order.outbound_cancelled_on:
		return {"ok": True, "already_requested": True, "marketplace_order": order.name}

	# The lock is held, so the Sales Order is re-read here: another session may have
	# delivered or billed it since the caller looked.
	option = _build_option(frappe.get_doc("Sales Order", sales_order.name), order)
	if option["state"] != ELIGIBLE:
		frappe.throw(
			_("The marketplace order cannot be cancelled from ERPNext: {0}").format(
				" ".join(option["reasons"]) or option["instruction"]
			)
		)

	cancellation_reason = cstr(cancellation_reason).strip()
	if option["platform"] == "Talabat" and cancellation_reason not in CANCELLATION_REASONS:
		frappe.throw(_("A valid cancellation reason is required."))
	if option["platform"] == "Shopify":
		from marketplace_integrations.integrations.shopify.orders import SHOPIFY_ORDER_CANCEL_REASONS

		if cancellation_reason not in SHOPIFY_ORDER_CANCEL_REASONS:
			frappe.throw(_("A valid Shopify cancellation reason is required."))

	try:
		raw_order = _send_cancel(order, cancellation_reason)
	except Exception as exc:
		# Never raises, so the platform error below is always what the caller sees.
		_record_order_action_error(order, sales_order.name, {"cancellation_reason": cancellation_reason}, exc)
		frappe.db.commit()
		raise

	# The platform accepted the cancellation request. Record that durably before anything
	# that can fail or re-plan the order (see the module docstring).
	frappe.db.set_value(
		"Marketplace Order", order.name, "outbound_cancelled_on", now_datetime(), update_modified=False
	)
	_log_order_action(
		order,
		sales_order.name,
		{"cancellation_reason": cancellation_reason},
		{"external_status": (raw_order or {}).get("status"), "job_id": (raw_order or {}).get("job_id")},
	)
	frappe.db.commit()
	job_result = None
	if order.platform == "Shopify":
		from marketplace_integrations.integrations.shopify.orders import poll_order_cancel_job

		try:
			job_result = poll_order_cancel_job(
				order.marketplace_account, raw_order.get("job_id") if isinstance(raw_order, dict) else None
			)
		except Exception as exc:
			# The mutation was accepted but its outcome could not be confirmed. Keep the
			# marker to prevent a duplicate cancel; record the polling failure for staff.
			_record_order_action_error(
				order, sales_order.name, {"cancellation_reason": cancellation_reason}, exc
			)
			frappe.db.commit()
			return {
				"ok": True,
				"already_requested": False,
				"marketplace_order": order.name,
				"refreshed": False,
				"job_id": raw_order.get("job_id"),
				"job_pending": True,
			}
		if isinstance(job_result, dict) and cstr(job_result.get("status")).strip().upper() == "FAILED":
			clear_outbound_cancelled_marker(order.name)
			errors = job_result.get("errors") or []
			messages = [
				cstr(error.get("message")).strip()
				for error in errors
				if isinstance(error, dict) and cstr(error.get("message")).strip()
			]
			error_text = "; ".join(messages) or _("Shopify cancellation job failed.")
			_record_order_action_error(
				order, sales_order.name, {"cancellation_reason": cancellation_reason}, error_text
			)
			frappe.db.commit()
			frappe.throw(error_text)
		if not isinstance(job_result, dict) or cstr(job_result.get("status")).strip().upper() != "SUCCEEDED":
			return {
				"ok": True,
				"already_requested": False,
				"marketplace_order": order.name,
				"refreshed": False,
				"job_id": raw_order.get("job_id"),
				"job_pending": True,
			}

	refreshed = _upsert_returned_order(order, raw_order)
	return {
		"ok": True,
		"already_requested": False,
		"marketplace_order": order.name,
		"refreshed": refreshed,
		"job_id": raw_order.get("job_id") if isinstance(raw_order, dict) else None,
		"job_pending": False,
	}


def clear_outbound_cancelled_marker(marketplace_order):
	"""Clear the echo-guard marker; the only supported way to do so.

	``MarketplaceOrder.before_save`` restores the marker on every save, so a stale copy
	cannot wipe it.  Clearing goes straight to the database (no controller hooks) and is
	meant for the failure path of a cancel that did not really happen (#182).  Does not
	commit.
	"""
	frappe.db.set_value(
		"Marketplace Order", marketplace_order, "outbound_cancelled_on", None, update_modified=False
	)


def clear_outbound_edit_marker(marketplace_order, only_if_fingerprint=None):
	"""Clear the Shopify item-edit marker (#356) after a confirmed pre-commit failure.

	With ``only_if_fingerprint`` the clear is skipped unless the stored marker still is that
	one (another writer may have moved it meanwhile).  Does not commit.
	"""
	_update_edit_marker(
		marketplace_order,
		{"outbound_edit_fingerprint": None, "outbound_edit_state": None},
		only_if_fingerprint=only_if_fingerprint,
	)


def clear_edit_marker_manually(marketplace_order):
	"""System Manager's recovery for an edit marker that will not clear on its own (#356).

	The caller (``api/marketplace_outbound.py``) has checked the role.  The clear is logged
	as a comment on the Marketplace Order.  Does not commit.
	"""
	order = frappe.get_doc("Marketplace Order", marketplace_order)
	if not order.get("outbound_edit_fingerprint"):
		frappe.throw(_("This Marketplace Order has no Shopify item edit marker to clear."))
	state = cstr(order.get("outbound_edit_state")) or _EDIT_IN_FLIGHT
	clear_outbound_edit_marker(order.name, only_if_fingerprint=order.outbound_edit_fingerprint)
	order.add_comment(
		"Info",
		_("{0} cleared the Shopify item edit marker (state: {1}).").format(frappe.session.user, state),
	)
	return {"ok": True}


def _update_edit_marker(marketplace_order, values, only_if_fingerprint=None):
	"""Write marker fields straight to the database, optionally only while the fingerprint matches.

	``before_save`` restores the marker on every document save, so a document save cannot
	clear it.  A single UPDATE with the fingerprint in its WHERE is atomic (no read-then-write
	race), and like ``frappe.db.set_value`` it skips controller hooks, which is intended here.
	A raw write does not invalidate the document cache, hence the explicit clear.
	"""
	table = frappe.qb.DocType("Marketplace Order")
	query = frappe.qb.update(table).where(table.name == marketplace_order)
	for fieldname, value in values.items():
		query = query.set(getattr(table, fieldname), value)
	if only_if_fingerprint:
		query = query.where(table.outbound_edit_fingerprint == only_if_fingerprint)
	query.run()
	frappe.clear_document_cache("Marketplace Order", marketplace_order)


def _as_sales_order(sales_order):
	if isinstance(sales_order, str):
		return frappe.get_doc("Sales Order", sales_order)
	return sales_order


def _option(
	state, platform, marketplace_order, instruction, reasons, cancellation_reasons=None, **no_dialog_flags
):
	return {
		"state": state,
		"platform": platform,
		"marketplace_order": marketplace_order,
		"instruction": instruction,
		"reasons": reasons,
		"cancellation_reasons": cancellation_reasons or [],
		"already_requested": False,
		"platform_cancelled": False,
		"erp_executed": False,
		**no_dialog_flags,
	}


def _build_option(sales_order, order):
	# Imported here: sales_order_sync imports platform_instruction from this module.
	from marketplace_integrations.services.sales_order_sync import _reconciliation_evidence

	platform = order.platform or "Talabat"
	instruction = platform_instruction(order)
	flags = {
		"already_requested": bool(order.get("outbound_cancelled_on")),
		"platform_cancelled": _reconciliation_evidence(order).get("cancellation") == "full",
		"erp_executed": _has_execution(read_erp_state(sales_order)),
	}
	if platform not in ("Talabat", "Shopify", "Noon"):
		return _option(NOT_APPLICABLE, platform, order.name, instruction, [], **flags)

	reasons = []
	if flags["already_requested"]:
		reasons.append(_("Cancellation was already requested on {0}.").format(platform))
	if flags["erp_executed"]:
		reasons.append(
			_("The Sales Order already has delivered quantity or a billed amount, so it cannot be cancelled.")
		)
	if platform == "Talabat":
		status = cstr(order.external_status).strip()
		if status == "CANCELLED":
			reasons.append(_("The Talabat order is already cancelled."))
		elif status not in TALABAT_CANCELLABLE_STATUSES:
			reasons.append(
				_(
					"The Talabat order is {0}; it can only be cancelled while it is RECEIVED or UPDATE_CART."
				).format(status or _("in an unknown status"))
			)
	elif platform == "Noon":
		reasons.append(_("Noon orders cannot be cancelled from ERPNext."))
	else:
		if order.cancelled_at:
			reasons.append(_("The Shopify order is already cancelled."))
		if _shopify_has_authorized_payment(order):
			reasons.append(
				_("The Shopify order has an authorized payment hold; cancel it in Shopify after reviewing the hold.")
			)
		fulfillment_problem = _shopify_fulfillment_problem(order)
		if fulfillment_problem:
			reasons.append(fulfillment_problem)

	if platform == "Talabat":
		cancellation_reasons = sorted(CANCELLATION_REASONS)
	elif platform == "Shopify":
		from marketplace_integrations.integrations.shopify.orders import SHOPIFY_ORDER_CANCEL_REASONS

		cancellation_reasons = list(SHOPIFY_ORDER_CANCEL_REASONS)
	else:
		cancellation_reasons = []
	return _option(
		BLOCKED if reasons else ELIGIBLE,
		platform,
		order.name,
		instruction,
		reasons,
		cancellation_reasons,
		**flags,
	)


def _shopify_has_authorized_payment(order):
	"""Whether stored Shopify evidence shows an authorization hold that cancel voids."""
	if cstr(order.external_financial_status).strip().upper() == "AUTHORIZED":
		return True
	transactions = order.payment_transactions
	if isinstance(transactions, str):
		try:
			transactions = frappe.parse_json(transactions)
		except (TypeError, ValueError):
			return False
	if not isinstance(transactions, list):
		return False
	return any(
		isinstance(transaction, dict)
		and cstr(transaction.get("kind")).strip().upper() == "AUTHORIZATION"
		and cstr(transaction.get("status")).strip().upper() in {"PENDING", "SUCCESS"}
		for transaction in transactions
	)


def _shopify_fulfillment_problem(order):
	"""Require each line's fulfillment state to be known and not started."""
	from marketplace_integrations.integrations.shopify.order_reconciliation import fulfillment_started

	items = order.items or []
	for item in items:
		started = fulfillment_started(item)
		if started is True:
			return _(
				"Shopify fulfillment has started for one or more lines; cancel the order in Shopify after review."
			)
		if started is None:
			return _(
				"Shopify fulfillment status is unknown for one or more lines; refresh the order before cancelling."
			)
	return ""


def _lock_and_load_order(marketplace_order):
	"""Lock, then load, one Marketplace Order row.

	Commits first, with nothing between the commit and the lock: under MariaDB
	``innodb_snapshot_isolation=ON`` an earlier read in the same transaction (the caller's
	permission reads included) can make the locking read fail with error 1020 and roll
	back the whole transaction (``docs/frappe-conventions.md``, Concurrency).
	"""
	frappe.db.commit()
	frappe.db.get_value("Marketplace Order", marketplace_order, "name", for_update=True)
	return frappe.get_doc("Marketplace Order", marketplace_order)


def _send_cancel(order, cancellation_reason):
	"""Call the platform; Talabat returns an order and Shopify returns an async JobResult ID."""
	if order.platform == "Shopify":
		from marketplace_integrations.integrations.shopify.orders import cancel_order

		return cancel_order(order.marketplace_account, order.external_order_id, cancellation_reason)

	from marketplace_integrations.api.talabat_orders import send_order_update

	return send_order_update(order, "CANCELLED", cancellation_reason)


def _upsert_returned_order(order, raw_order):
	"""Upsert the platform's returned order; a failure here must not hide the cancel.

	The platform call and the marker are already committed, so a failure is logged and
	reported as ``refreshed: False`` (the next webhook or manual refresh reconciles).
	"""
	from marketplace_integrations.integrations.talabat.order_mapper import normalize_order
	from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order

	savepoint = "marketplace_outbound_upsert"
	frappe.db.savepoint(savepoint)
	try:
		if order.platform == "Shopify":
			from marketplace_integrations.integrations.shopify.orders import import_order

			refreshed = import_order(order.marketplace_account, order.external_order_id)
			if not refreshed:
				frappe.throw(_("Shopify returned no order when refreshing after cancellation."))
		else:
			normalized = normalize_order(raw_order)
			if normalized["external_order_id"] != cstr(order.external_order_id).strip():
				frappe.throw(_("Talabat returned a different order ID."))
			upsert_marketplace_order(order.marketplace_account, normalized)
	except Exception:
		# MariaDB error 1020 rolls back the whole transaction, savepoint included; the cancel
		# and the marker are already committed, so a full rollback loses nothing.
		rollback_to_savepoint(savepoint, "Marketplace outbound cancel: rollback failed")
		frappe.log_error(title=_("Marketplace outbound cancel: refreshing the returned order failed"))
		return False
	frappe.db.release_savepoint(savepoint)
	return True


def _log_order_action(order, sales_order, details, response, action=_ORDER_ACTION_CANCEL):
	"""One Success ``Order Action`` row: ``details`` extend the request, ``response`` is the platform's."""
	now = now_datetime()
	frappe.get_doc(
		{
			"doctype": "Marketplace Sync Log",
			"sync_type": "Order Action",
			"marketplace_account": order.marketplace_account,
			"marketplace_order": order.name,
			"status": "Success",
			"trigger_source": "Manual",
			"attempted_at": now,
			"completed_at": now,
			# No customer data from the platform response: just the status it reports.
			"request_data": _action_request_data(order, sales_order, details, action),
			"response_data": frappe.as_json(response, ensure_ascii=False),
		}
	).insert(ignore_permissions=True)


def _record_order_action_error(order, sales_order, details, exc, action=_ORDER_ACTION_CANCEL):
	"""Write the Error row; a failure to do so goes to the Error Log and is never raised."""
	savepoint = "marketplace_outbound_error_log_" + frappe.generate_hash(length=10)
	try:
		frappe.db.savepoint(savepoint)
		_log_order_action_error(order, sales_order, details, exc, action)
		frappe.db.release_savepoint(savepoint)
	except Exception:
		rollback_to_savepoint(savepoint, "Marketplace outbound action: rollback failed")
		frappe.log_error(
			title=_("Marketplace outbound action: failed to write failure log"),
			message=frappe.get_traceback(),
		)


def _log_order_action_error(order, sales_order, details, exc, action=_ORDER_ACTION_CANCEL):
	"""One Error row for a failed platform call; a cancel marker is deliberately not set."""
	now = now_datetime()
	frappe.get_doc(
		{
			"doctype": "Marketplace Sync Log",
			"sync_type": "Order Action",
			"marketplace_account": order.marketplace_account,
			"marketplace_order": order.name,
			"status": "Error",
			"trigger_source": "Manual",
			"attempted_at": now,
			"completed_at": now,
			"request_data": _action_request_data(order, sales_order, details, action),
			"traceback": cstr(exc).strip()[:500],
		}
	).insert(ignore_permissions=True)


def _action_request_data(order, sales_order, details, action):
	return frappe.as_json(
		{"action": action, "platform": order.platform or "Talabat", "sales_order": sales_order, **details},
		ensure_ascii=False,
	)
