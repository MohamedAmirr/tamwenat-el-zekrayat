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

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

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
		_record_order_action_error(order, sales_order.name, cancellation_reason, exc)
		frappe.db.commit()
		raise

	# The platform accepted the cancellation request. Record that durably before anything
	# that can fail or re-plan the order (see the module docstring).
	frappe.db.set_value(
		"Marketplace Order", order.name, "outbound_cancelled_on", now_datetime(), update_modified=False
	)
	_log_order_action(order, sales_order.name, cancellation_reason, raw_order)
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
			_record_order_action_error(order, sales_order.name, cancellation_reason, exc)
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
			_record_order_action_error(order, sales_order.name, cancellation_reason, error_text)
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


def _log_order_action(order, sales_order, cancellation_reason, raw_order):
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
			"request_data": frappe.as_json(
				{
					"action": _ORDER_ACTION_CANCEL,
					"platform": order.platform or "Talabat",
					"sales_order": sales_order,
					"cancellation_reason": cancellation_reason,
				},
				ensure_ascii=False,
			),
			"response_data": frappe.as_json(
				{
					"external_status": (raw_order or {}).get("status"),
					"job_id": (raw_order or {}).get("job_id"),
				},
				ensure_ascii=False,
			),
		}
	).insert(ignore_permissions=True)


def _record_order_action_error(order, sales_order, cancellation_reason, exc):
	"""Write the Error row; a failure to do so goes to the Error Log and is never raised."""
	savepoint = "marketplace_outbound_error_log_" + frappe.generate_hash(length=10)
	try:
		frappe.db.savepoint(savepoint)
		_log_order_action_error(order, sales_order, cancellation_reason, exc)
		frappe.db.release_savepoint(savepoint)
	except Exception:
		rollback_to_savepoint(savepoint, "Marketplace outbound cancel: rollback failed")
		frappe.log_error(
			title=_("Marketplace outbound cancel: failed to write failure log"),
			message=frappe.get_traceback(),
		)


def _log_order_action_error(order, sales_order, cancellation_reason, exc):
	"""One Error row for a failed platform call; the marker is deliberately not set."""
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
			"request_data": frappe.as_json(
				{
					"action": _ORDER_ACTION_CANCEL,
					"platform": order.platform or "Talabat",
					"sales_order": sales_order,
					"cancellation_reason": cancellation_reason,
				},
				ensure_ascii=False,
			),
			"traceback": cstr(exc).strip()[:500],
		}
	).insert(ignore_permissions=True)
