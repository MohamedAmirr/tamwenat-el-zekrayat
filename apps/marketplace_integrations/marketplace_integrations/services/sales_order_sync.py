# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Explicit Marketplace Order to ERPNext Sales Order synchronization.

Marketplace Order remains Talabat's operational record.  This module creates
a Sales Order after an authorized user explicitly asks for it (or an
automated trigger calls it, see ``trigger_source`` below) and immediately
submits it (#131) - a fresh Sales Order is never left in Draft.  Only the
existing-Sales-Order UPDATE branch (re-syncing an order whose linked Sales
Order is still Draft, e.g. one created before #131) leaves the document in
whatever docstatus it already had; once that Sales Order is submitted, a
later re-sync is classified by the reconciliation planner; supported outcomes
(cancel, close the remainder, update quantities/rates) are executed against the
submitted Sales Order (#179), everything else is an advisory.  It is never
re-submitted.

Every ``sync_sales_order()`` attempt - success, an expected ``SyncBlocker``,
or an unexpected exception - writes one ``Marketplace Sync Log`` row
recording the outcome and the ``trigger_source`` the caller passed in
("Manual" for the existing "Send to ERPNext" button; a webhook value for the
not-yet-built sub-issues #133/#134).

Commercial values are the historical, finalized marketplace transaction, not
whatever ERPNext's current Item Price/Pricing Rules would compute today:

    Marketplace Account.erpnext_selling_price_list = catalog offer/reference
    Marketplace Order fulfilled pricing            = historical accepted transaction
    ERPNext Sales Order                            = explicit copy of the final transaction

ERPNext's standard Sales Order controller (`set_missing_values()`) is used
only once, to resolve structural defaults (Item/UOM/conversion, accounts,
currency, company/customer party details) - never to price a line.  Every
line's `rate`/`price_list_rate` is then explicitly overwritten with the
Talabat fulfilled effective rate, `ignore_pricing_rule` is set, and totals
are recalculated from that overridden rate.  The current Selling Price List
remains only a pricing *context* on the Sales Order header (matching
Marketplace Account's configured reference), never the historical-order rate
source.
"""

import hashlib
import json
from copy import deepcopy

import frappe
from frappe import _
from frappe.utils import add_days, add_to_date, cint, cstr, flt, getdate, now_datetime, strip_html

from marketplace_integrations.integrations.shopify.order_reconciliation import refund_ids as _refund_ids
from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.marketplace_account import (
	shipping_account_problem,
)
from marketplace_integrations.marketplace_integrations.doctype.marketplace_product_mapping.marketplace_product_mapping import (
	compute_mapping_identity_key,
)
from marketplace_integrations.services import sync_reason_codes as reasons
from marketplace_integrations.services import sync_status
from marketplace_integrations.services.account_warehouse import validate_account_warehouse
from marketplace_integrations.services.mappings import resolve_mapping_and_item
from marketplace_integrations.services.marketplace_order_reconciliation import (
	CANCELLED_PAID_ERROR,
	CANCELLED_PAID_STATUSES,
	CANCELLED_UNPAID_STATUSES,
)
from marketplace_integrations.services.marketplace_outbound import platform_instruction
from marketplace_integrations.services.sales_order_reconciliation import (
	CANCEL_ORDER,
	CLOSE_REMAINDER,
	NEEDS_ATTENTION,
	NO_ACTION,
	UNSUPPORTED_COMPENSATING_TRANSACTION,
	UPDATE_SUBMITTED_ORDER,
	ReconciliationMismatch,
	SalesOrderLinesMismatch,
	execute,
	hold_cancel_for_draft_documents,
	plan,
	read_erp_state,
	snapshot_advanced_from,
	snapshot_with_line_rows,
	snapshot_with_sales_order_item_names,
)

FINAL_ITEM_STATUSES = {"IN_CART", "ADDITION"}

# Platform -> dotted path of its ``build_evidence(order)`` (#178; reads only persisted data).  Resolved
# lazily (``frappe.get_attr``) so this module never imports an integration at
# import time.  A blank platform is Talabat, like ``_validate_platform_supported``.
_RECONCILIATION_EVIDENCE_BUILDERS = {
	"Talabat": "marketplace_integrations.integrations.talabat.order_reconciliation.build_evidence",
	"Shopify": "marketplace_integrations.integrations.shopify.order_reconciliation.build_evidence",
	"Noon": "marketplace_integrations.integrations.noon.order_reconciliation.build_evidence",
}

# `effective_rate = fulfilled_total_price / fulfilled_quantity` is an
# intermediate value that ERPNext later multiplies back by `qty` and rounds
# to the Sales Order Item `amount` field's own currency precision
# (`_amount_precision()`, 2dp on this site). Rounding the division itself
# down to that SAME 2dp precision before the multiply is a double-rounding
# bug: e.g. 9.99 / 2 = 4.995, which rounds to 5.00 at 2dp, and 5.00 * 2 =
# 10.00 != 9.99 - `_verify_line_amounts()` then blocks a routine,
# non-evenly-divisible discounted line as though it were a real data
# problem. Rounding the division to 6dp instead (comfortably higher than any
# realistic currency precision) keeps the rounding remainder from the
# division below the final amount precision's rounding threshold for any
# realistic single order-line quantity, so `rate * qty`, rounded once more to
# `amount`'s precision, reliably reconstructs `fulfilled_total_price` exactly.
#
# This alone is not sufficient, though: ERPNext's own
# `calculate_taxes_and_totals()` unconditionally re-rounds `Sales Order
# Item.rate`/`price_list_rate` down to their OWN field precision
# (`round_floats_in()`, called before `calculate_item_rate()`, confirmed
# directly against the installed `erpnext/controllers/taxes_and_totals.py`)
# on every `calculate_taxes_and_totals()` call - including the one inside
# `AccountsController.validate()` that reruns on every `.insert()`/`.save()`.
# Since neither field has an explicit `precision` in ERPNext's own doctype
# JSON, both default to the site's `currency_precision` (2dp here) - the
# SAME precision as `amount` - so a 6dp Python value assigned to
# `so_item.rate` before calling `calculate_taxes_and_totals()` would
# otherwise be silently re-truncated back to 2dp regardless of how precisely
# it was computed, reproducing the exact same bug this rounds to fix.
# `fixtures/property_setter.json` therefore raises `Sales Order Item.rate`
# and `.price_list_rate`'s own field precision to `EFFECTIVE_RATE_PRECISION`
# via two Property Setters (never edit `erpnext`/`frappe` doctype JSON
# directly), so ERPNext's internal rounding no longer clips them below what
# this module already computed - verified end-to-end against the real
# `calculate_taxes_and_totals()` call, not assumed (see
# `docs/frappe-conventions.md`). `amount` itself is deliberately left at the
# site's normal `currency_precision` - only the two rate fields need the
# escape hatch.
EFFECTIVE_RATE_PRECISION = 6


_FRESH_SALES_ORDER_SAVEPOINT = "marketplace_fresh_sales_order"
_RECONCILE_EXECUTE_SAVEPOINT = "marketplace_reconcile_execute"

# Plan outcomes `_reconcile_submitted_order()` executes against ERPNext (#179): cancel,
# close the undelivered remainder, and update quantities/rates; every other outcome stays
# an advisory.  Form load/save (`set_sync_status_from_current_snapshot`) never executes.
_EXECUTABLE_OUTCOMES = {CANCEL_ORDER, CLOSE_REMAINDER, UPDATE_SUBMITTED_ORDER}


class SyncBlocker(frappe.ValidationError):
	"""A user-actionable prerequisite that prevents Sales Order synchronization.

	``reason_code`` (``services/sync_reason_codes.py``) says why; a blocker raised without
	one is the generic ``SYNC_BLOCKED``.  Mapping and price problems carry their own code so
	fixing them can re-sync the order automatically (#364).
	"""

	def __init__(self, *args, reason_code=reasons.SYNC_BLOCKED):
		super().__init__(*args)
		self.reason_code = reason_code


def sync_sales_order(marketplace_order, trigger_source="Manual"):
	"""Create or update one Marketplace Order's linked Sales Order (see ``_sync_sales_order``).

	The order is registered as "syncing now" for the duration, so the document-change recheck
	(``services/sales_order_recheck.py``) ignores the Sales Order changes this very run makes.
	"""
	syncing = frappe.flags.setdefault("marketplace_syncing_orders", set())
	already_registered = marketplace_order in syncing
	syncing.add(marketplace_order)
	try:
		return _sync_sales_order(marketplace_order, trigger_source)
	finally:
		if not already_registered:
			syncing.discard(marketplace_order)


def _sync_sales_order(marketplace_order, trigger_source):
	"""Create or update one Marketplace Order's linked Sales Order.

	The Marketplace Order row is locked before it is reloaded, so concurrent
	button clicks observe a successful link rather than creating a second Sales
	Order.  Expected blockers are persisted as sanitized sync state and returned
	to the API without raising, so that status write is not rolled back.

	A fresh Sales Order (the ``.insert()`` branch) is immediately submitted
	(#131) - unconditionally, for every caller, Talabat and Shopify alike.
	Re-syncing an order whose Sales Order is already submitted uses the
	reconciliation planner: supported outcomes (cancel, close the undelivered
	remainder, update qty/rate) are executed against that ERP document inside a
	savepoint (#179); anything else is an advisory.  It is never re-submitted.

	Every attempt - success, expected ``SyncBlocker``, or unexpected
	exception - writes one ``Marketplace Sync Log`` row via
	``_create_sync_log()``, tagged with the caller-supplied ``trigger_source``
	("Manual" for the existing "Send to ERPNext" button; a webhook value for
	the not-yet-built sub-issues #133/#134).
	"""
	frappe.db.get_value("Marketplace Order", marketplace_order, "name", for_update=True)
	order = frappe.get_doc("Marketplace Order", marketplace_order)
	if trigger_source in ("Manual", "Manual Resync") and cint(order.sync_retry_count):
		# A person asking again gets a fresh transient-retry budget (#364).
		order.sync_retry_count = 0

	# Platform support is checked BEFORE the try/except SyncBlocker block
	# below, deliberately: decision 7 forbids persisting a misleading "Sync
	# Error" status for an order that was never eligible for Talabat-shaped
	# sync in the first place. If this raised inside the try block instead,
	# `except SyncBlocker` would call `_save_failure()` and write/save a
	# "Sync Error" status + Version row for a Shopify order that nothing
	# ever attempted to sync. No status write, no save, for this specific
	# blocker.
	try:
		_validate_platform_supported(order)
	except SyncBlocker as exc:
		result = {"ok": False, "status": order.erpnext_sync_status, "error": str(exc)}
		_create_sync_log(order, trigger_source, "Invalid", None, result)
		return result

	if (
		order.platform == "Shopify"
		and not order.erpnext_sales_order
		and _reconciliation_evidence(order).get("cancellation") == "full"
	):
		return _skip_cancelled_before_sync(order, trigger_source)
	if order.platform == "Noon" and not order.erpnext_sales_order and _noon_fully_cancelled(order):
		return _skip_noon_cancelled_before_sync(order, trigger_source)

	snapshot = None
	allow_payment_mirroring = False
	try:
		account = _get_account(order)
		_validate_sync_permissions(order, account)
		allow_payment_mirroring = True
		sales_order = None
		evidence = {}
		if order.erpnext_sales_order:
			# Fetched once: the linked Sales Order and the reconciliation evidence
			# are reused below and by _save_success().
			sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
			evidence = _reconciliation_evidence(order)
			if _uses_stored_snapshot(sales_order.docstatus, evidence):
				snapshot = _parse_last_synced_snapshot(order)
				return _reconcile_submitted_order(order, sales_order, snapshot, evidence, trigger_source)
		try:
			_validate_account_configuration(account)
			lines = _build_order_lines(
				order, account, tolerate_zero_quantity=bool(sales_order and sales_order.docstatus != 0)
			)
			shipping = _shopify_shipping_amount(
				order, account, block_taxes=not (sales_order and sales_order.docstatus != 0)
			)
			if order.platform == "Shopify":
				customer = _get_or_create_shopify_customer(order, account)
				address = _resolve_shopify_address(order, customer) or {"name": None, "address_type": None}
			else:
				customer = _get_or_create_customer(account)
				address = None
		except SyncBlocker:
			# Refund/return evidence keeps priority over a rebuild blocker (e.g. a
			# disabled mapping): a delivered/billed order still needs the compensating
			# transaction, which matters more to tell the merchant.
			if _compensating_plan_outranks_blocker(order, sales_order, evidence):
				return _reconcile_submitted_order(order, sales_order, None, evidence, trigger_source)
			raise
		snapshot = _make_snapshot(
			account,
			customer,
			lines,
			_transaction_date(order),
			_delivery_date(order, account, lines),
			address,
			shipping,
		)
		current_hash = snapshot_hash(snapshot)

		if sales_order:
			if sales_order.docstatus != 0:
				return _reconcile_submitted_order(order, sales_order, snapshot, evidence, trigger_source)
			if order.last_synced_hash == current_hash:
				result = _save_success(order, current_hash, sales_order, updated=False, snapshot=snapshot)
				_create_sync_log(order, trigger_source, "Success", snapshot, result)
				return result
			_apply_snapshot(sales_order, snapshot, order.name, account.erpnext_shipping_account)
			_fill_buyer_phone(sales_order, order)
			_verify_line_amounts(sales_order, snapshot, account)
			_verify_grand_total(sales_order, snapshot, order, account)
			sales_order.save()
			result = _save_success(order, current_hash, sales_order, updated=True, snapshot=snapshot)
			_create_sync_log(order, trigger_source, "Success", snapshot, result)
			return result

		sales_order = frappe.new_doc("Sales Order")
		_apply_snapshot(sales_order, snapshot, order.name, account.erpnext_shipping_account)
		_fill_buyer_phone(sales_order, order)
		_verify_line_amounts(sales_order, snapshot, account)
		_verify_grand_total(sales_order, snapshot, order, account)
		# Verify the snapshot pairs with the inserted rows BEFORE submitting; on a
		# mismatch, or any failure of the insert/submit, roll the insert back so no
		# orphan draft Sales Order is left unlinked (a retry would otherwise create
		# a duplicate).
		frappe.db.savepoint(_FRESH_SALES_ORDER_SAVEPOINT)
		try:
			sales_order.insert()
			snapshot_with_sales_order_item_names(snapshot, sales_order)
			sales_order.submit()
		except SalesOrderLinesMismatch as exc:
			frappe.db.rollback(save_point=_FRESH_SALES_ORDER_SAVEPOINT)
			raise SyncBlocker(str(exc)) from exc
		except Exception:
			frappe.db.rollback(save_point=_FRESH_SALES_ORDER_SAVEPOINT)
			raise
		result = _save_success(order, current_hash, sales_order, updated=False, snapshot=snapshot)
		result = _flag_compensating_evidence_on_new_order(order, sales_order, snapshot) or result
		_create_sync_log(order, trigger_source, "Success", snapshot, result)
		return result
	except (SyncBlocker, SalesOrderLinesMismatch) as exc:
		# SalesOrderLinesMismatch carries a specific, user-actionable message.
		result = _save_failure(
			order, str(exc), getattr(exc, "reason_code", reasons.SNAPSHOT_LINE_MISMATCH)
		)
		_create_sync_log(order, trigger_source, "Invalid", snapshot, result)
		return result
	except frappe.PermissionError:
		raise
	except Exception as exc:
		if is_transient_failure(exc):
			return _handle_transient_failure(order, trigger_source, snapshot)
		# Deliberately no `message=` kwarg: frappe.log_error() only calls
		# frappe.get_traceback() itself when `message` is falsy - passing a
		# static string there (as this used to) replaces the real traceback
		# in the Error Log with that string, discarding the actual diagnostic
		# content for every plausible ERPNext-side throw this can catch
		# (warehouse/company mismatch, negative rate, below-cost validation,
		# delivery_date < transaction_date, etc.). The user-facing message
		# below is unaffected either way - it never came from this call.
		frappe.log_error(title=_("Marketplace Sales Order synchronization"))
		traceback_text = frappe.get_traceback()
		result = _save_failure(
			order,
			_(
				"ERPNext Sales Order synchronization failed. Please review the ERPNext configuration and try again."
			),
			reasons.SYNC_FAILED,
		)
		_create_sync_log(order, trigger_source, "Error", snapshot, result, traceback_text=traceback_text)
		return result
	finally:
		# Payment mirroring is a separate workflow with one savepoint per Shopify
		# transaction. It must never change the outcome of the Sales Order sync.
		if allow_payment_mirroring:
			_mirror_shopify_payments_independently(order.name, trigger_source)


def is_transient_failure(exc):
	"""True for a failure worth retrying later: a timeout, a lock or deadlock error, an HTTP 5xx
	or a 429 (#364).  Missing mapping or price, validation errors and permission errors are
	terminal and never reach this: they have their own handlers or raise ``frappe.ValidationError``.
	"""
	import requests

	if isinstance(exc, frappe.ValidationError | frappe.PermissionError):
		return False
	if isinstance(
		exc,
		frappe.QueryTimeoutError
		| frappe.QueryDeadlockError
		| frappe.DocumentLockedError
		| requests.exceptions.Timeout
		| requests.exceptions.ConnectionError,
	):
		return True
	if isinstance(exc, requests.exceptions.HTTPError):
		status = getattr(exc.response, "status_code", None)
		return bool(status) and (status == 429 or status >= 500)
	return False


def _handle_transient_failure(order, trigger_source, snapshot):
	"""Schedule the next transient retry, or give up with ``SYNC_RETRIES_EXHAUSTED``.

	Up to ``len(TRANSIENT_RETRY_DELAYS_SECONDS)`` retries (1, 5 and 15 minutes), carried by
	``sync_debounce_until`` (the minute sweep runs them) and ``sync_retry_count``.  The order
	shows Syncing while a retry is pending.  Never holds a lock across the wait.
	"""
	from marketplace_integrations.services.sales_order_sync_debounce import TRANSIENT_RETRY_DELAYS_SECONDS

	frappe.log_error(title=_("Marketplace Sales Order synchronization"))
	traceback_text = frappe.get_traceback()
	attempt = cint(order.sync_retry_count)
	if attempt >= len(TRANSIENT_RETRY_DELAYS_SECONDS):
		result = _save_failure(
			order,
			_("ERPNext Sales Order synchronization kept failing because of a temporary problem."),
			reasons.SYNC_RETRIES_EXHAUSTED,
		)
		_create_sync_log(order, trigger_source, "Error", snapshot, result, traceback_text=traceback_text)
		return result
	order.sync_retry_count = attempt + 1
	reasons.apply_sync_state(
		order,
		reasons.TRANSIENT_RETRY_SCHEDULED,
		_("A temporary problem interrupted the sync; retry {0} of {1} is scheduled.").format(
			attempt + 1, len(TRANSIENT_RETRY_DELAYS_SECONDS)
		),
	)
	order.sync_retry_count = attempt + 1
	order.sync_debounce_until = add_to_date(now_datetime(), seconds=TRANSIENT_RETRY_DELAYS_SECONDS[attempt])
	order.pending_sync_trigger_source = trigger_source
	order.flags.skip_sync_status_update = True
	order.save()
	result = {
		"ok": False,
		"status": order.erpnext_sync_status,
		"error": order.last_sync_error,
		"retry_scheduled": True,
	}
	_create_sync_log(order, trigger_source, "Error", snapshot, result, traceback_text=traceback_text)
	return result


def _mirror_shopify_payments_independently(marketplace_order, trigger_source, transaction_ids=None):
	"""Attempt eligible Shopify Payment Entries without changing Sales Order sync status.

	``transaction_ids`` limits the attempt to those transactions (the payment retry sweep)."""
	try:
		state = frappe.db.get_value(
			"Marketplace Order", marketplace_order, ["platform", "erpnext_sales_order"], as_dict=True
		)
		if not state or state.platform != "Shopify" or not state.erpnext_sales_order:
			return
		if frappe.db.get_value("Sales Order", state.erpnext_sales_order, "docstatus") != 1:
			return
		from marketplace_integrations.services.shopify_payments import mirror_shopify_payments

		mirror_shopify_payments(marketplace_order, trigger_source, transaction_ids)
	except Exception:
		try:
			frappe.log_error(title=_("Automatic Shopify payment mirroring"))
		except Exception:
			pass


def _skip_cancelled_before_sync(order, trigger_source):
	"""Resolve a full Shopify cancellation before any Sales Order exists.

	Unpaid or fully refunded cancellations become terminal ``Cancelled`` with a
	Success log. Paid or partially refunded cancellations become ``Needs Action``
	with an actionable refund instruction and an Invalid log. Unknown future
	financial statuses become a ``Sync Error`` for manual review (a new import would
	otherwise stay Syncing for ever). No Sales Order, customer, or Payment Entry is
	created on this path.
	"""
	if order.external_financial_status not in CANCELLED_UNPAID_STATUSES | CANCELLED_PAID_STATUSES:
		message = _("Cancelled Shopify order has an unrecognized financial status; review it manually.")
		result = _save_failure(order, message, reasons.SYNC_BLOCKED)
		_create_sync_log(order, trigger_source, "Invalid", None, result)
		return result
	if order.external_financial_status in CANCELLED_PAID_STATUSES:
		# A paid cancellation needs a person to refund the customer (#364: Needs Action).
		status = sync_status.NEEDS_ACTION
		error = _(CANCELLED_PAID_ERROR)
	else:
		status = sync_status.CANCELLED
		error = None
	if order.erpnext_sync_status != status or order.last_sync_error != error:
		if status == sync_status.NEEDS_ACTION:
			reasons.apply_sync_state(order, reasons.CANCELLED_PAID_REFUND_REQUIRED, error)
		else:
			order.erpnext_sync_status = status
			order.last_sync_error = error
			reasons.clear_sync_state(order)
		order.flags.skip_sync_status_update = True
		order.save()
	if status == sync_status.CANCELLED:
		result = {
			"ok": True,
			"skipped": True,
			"status": status,
			"message": _(
				"The Shopify order was cancelled or fully refunded before an ERPNext Sales Order was created; "
				"no Sales Order is needed."
			),
		}
		_create_sync_log(order, trigger_source, "Success", None, result)
	else:
		result = {"ok": False, "status": status, "error": error}
		_create_sync_log(order, trigger_source, "Invalid", None, result)
	return result


NOON_CANCELLED_EXTERNAL_STATUS = "CANCELLED"


def _noon_fully_cancelled(order):
	"""True when the Noon order has live units and every one is CANCELLED (out-of-stock units that
	are not cancelled keep the normal blocker path).

	Rows with ``current_quantity`` 0 are earlier states of a unit that Noon has since moved on (a
	retained CONFIRMED row next to its later CANCELLED row); they are not live units, so they must not
	keep a fully cancelled order out of ``Cancelled``."""
	rows = [row for row in (order.items or []) if flt(row.current_quantity) > 0]
	return bool(rows) and all(row.external_status == NOON_CANCELLED_EXTERNAL_STATUS for row in rows)


def _skip_noon_cancelled_before_sync(order, trigger_source):
	"""Resolve a fully cancelled Noon order before any Sales Order exists: terminal ``Cancelled``
	(#353), like the Shopify path above.  Nothing is created."""
	if order.erpnext_sync_status != sync_status.CANCELLED or order.last_sync_error:
		order.erpnext_sync_status = sync_status.CANCELLED
		order.last_sync_error = None
		reasons.clear_sync_state(order)
		order.flags.skip_sync_status_update = True
		order.save()
	result = {
		"ok": True,
		"skipped": True,
		"status": sync_status.CANCELLED,
		"message": _(
			"Every unit of the Noon order was cancelled before an ERPNext Sales Order was created; "
			"no Sales Order is needed."
		),
	}
	_create_sync_log(order, trigger_source, "Success", None, result)
	return result


def build_sales_order_snapshot(order, account=None, customer=None, for_submitted_order=False):
	"""Return the deterministic ERPNext-commercial input for an order.

	No Talabat customer PII, status, address, raw payload, or current Item
	Price belongs in the snapshot.  The historical Talabat fulfilled amount
	for each final line is the only commercial rate input.

	When no explicit ``customer`` is passed, this is the read-only
	snapshot-preview/status-check path (reachable from
	``get_current_sync_status()``, which runs on every Marketplace Order form
	load - not an explicit user-authorized sync). Shopify Customer/Address
	resolution therefore runs in lookup-only mode (``create=False``): it must
	never insert a new Customer or Address as a side effect of merely opening
	the form. If nothing has been resolved yet by a prior real sync, this
	falls back to the generic account Customer / a blank address purely so
	the snapshot/hash computation still succeeds - nothing is persisted.
	``sync_sales_order()`` (the actual explicit-sync path) never calls this
	function; it resolves the buyer Customer/Address itself with
	``create=True``.

	``for_submitted_order`` is set when the order is linked to a submitted Sales
	Order: a line the marketplace has reduced to zero units (e.g. a refund) is
	then represented as a zero-quantity line instead of blocking, so the
	reconciliation planner can see the change.  A never-synced order still blocks.
	"""
	account = account or _get_account(order)
	_validate_account_configuration(account)
	lines = _build_order_lines(order, account, tolerate_zero_quantity=for_submitted_order)
	if not customer:
		if order.platform == "Shopify":
			customer = (
				_get_or_create_shopify_customer(order, account, create=False) or account.erpnext_customer
			)
			address = _resolve_shopify_address(order, customer, create=False) or {
				"name": None,
				"address_type": None,
			}
		else:
			customer = _require_customer(account)
			address = None
	else:
		address = (
			(_resolve_shopify_address(order, customer, create=False) or {"name": None, "address_type": None})
			if order.platform == "Shopify"
			else None
		)
	return _make_snapshot(
		account,
		customer,
		lines,
		_transaction_date(order),
		_delivery_date(order, account, lines),
		address,
		_shopify_shipping_amount(order, account, block_taxes=not for_submitted_order),
	)


def _build_sales_order_lines(order, account):
	"""Validate and return the final UNIT lines that affect a Sales Order.

	The authoritative Talabat line amount is `fulfilled_total_price`: zero is
	a valid, final amount, a negative amount is never valid, and a genuinely
	missing (null) amount blocks the line. `effective_rate =
	fulfilled_total_price / fulfilled_quantity`, rounded to
	`EFFECTIVE_RATE_PRECISION` (see the module-level comment above that
	constant for why this is a fixed 6dp value rather than the resulting
	Sales Order Item's own currency precision).

	The null-vs-zero distinction cannot be read off `fulfilled_total_price`
	itself: it is a Frappe Float column (NOT NULL DEFAULT 0), so a genuinely
	absent Talabat `pricing.total_price` is already coerced to `0.0` by the
	time it reaches this row - indistinguishable here from a real zero
	fulfilled amount. `fulfilled_total_price_missing` is computed once, at
	parse time against the raw payload, before that coercion happens (see
	`integrations/talabat/order_mapper.py`), and is the only reliable signal
	for "missing" (#85 review finding #1).

	A row with a nonzero `fulfilled_unit_price` but a zero
	`fulfilled_total_price` is treated as suspicious/likely-missing data, not
	an honest free item: `fulfilled_total_price` is itself a NOT NULL Float
	column (see above), so a genuinely-missing Talabat total is
	indistinguishable from a real zero once persisted - but a nonzero unit
	price on the same row is strong evidence the total was supposed to be
	nonzero too. A row where both are genuinely `0` still passes through as a
	legitimate free item.
	A CANCELLED Talabat order never produces a Sales Order (mirrors the Shopify
	``cancelled_at`` block).  An order already linked to a submitted Sales Order
	never reaches here for cancellation: ``_uses_stored_snapshot()`` judges it
	against the stored baseline instead.
	"""

	if cstr(order.external_status).strip() == "CANCELLED":
		raise SyncBlocker(_("Cancelled Talabat orders cannot be synchronized."))
	lines = []
	for row in order.items or []:
		if row.external_status not in FINAL_ITEM_STATUSES:
			continue
		if row.pricing_type == "KG":
			raise SyncBlocker(
				_(
					"Talabat KG line {0} ({1}) cannot be synchronized until its ERPNext UOM setup is configured."
				).format(row.external_item_id, _line_identifier(row))
			)
		if row.pricing_type != "UNIT":
			raise SyncBlocker(
				_("Talabat line {0} ({1}) has an unsupported pricing type.").format(
					row.external_item_id, _line_identifier(row)
				)
			)
		if flt(row.fulfilled_quantity) <= 0:
			raise SyncBlocker(
				_(
					"Talabat line {0} ({1}) needs a positive fulfilled quantity before it can be synchronized."
				).format(row.external_item_id, _line_identifier(row))
			)
		# Both conditions matter: fulfilled_total_price_missing is the reliable
		# signal once a row has round-tripped through the DB's NOT NULL Float
		# coercion (see the module docstring above), but `is None` is kept
		# too as defence-in-depth for an in-memory caller that hasn't gone
		# through that save/reload yet - dropping it would leave exactly that
		# caller's `fulfilled_total_price=None`/flag-not-yet-set row treated
		# as a valid free line via `flt(None) == 0` (#85 review finding #8).
		if row.fulfilled_total_price_missing or row.fulfilled_total_price is None:
			raise SyncBlocker(
				_("Talabat line {0} ({1}) has no fulfilled total price and cannot be synchronized.").format(
					row.external_item_id, _line_identifier(row)
				),
				reason_code=reasons.PRICE_MISSING,
			)
		if flt(row.fulfilled_total_price) < 0:
			raise SyncBlocker(
				_(
					"Talabat line {0} ({1}) has a negative fulfilled total price and cannot be synchronized."
				).format(row.external_item_id, _line_identifier(row))
			)
		if flt(row.fulfilled_total_price) == 0 and flt(row.fulfilled_unit_price) > 0:
			raise SyncBlocker(
				_(
					"Talabat line {0} ({1}) has a nonzero unit price but a zero fulfilled total - this looks "
					"like missing pricing data, not a free item. Verify the Talabat order data before "
					"synchronizing."
				).format(row.external_item_id, _line_identifier(row))
			)

		mapping = _get_enabled_mapping(account.name, row.external_sku)
		if not mapping:
			raise SyncBlocker(
				_("No enabled Marketplace Product Mapping exists for Talabat SKU {0} on line {1}.").format(
					_line_identifier(row), row.external_item_id
				),
				reason_code=reasons.MAPPING_MISSING,
			)
		item_uom = mapping.sales_uom or frappe.get_cached_value("Item", mapping.item, "stock_uom")
		fulfilled_total_price = flt(row.fulfilled_total_price)
		effective_rate = flt(fulfilled_total_price / flt(row.fulfilled_quantity), EFFECTIVE_RATE_PRECISION)
		lines.append(
			{
				"external_item_id": cstr(row.external_item_id).strip(),
				"item_code": mapping.item,
				"qty": flt(row.fulfilled_quantity),
				"uom": item_uom,
				"warehouse": account.warehouse,
				"fulfilled_total_price": fulfilled_total_price,
				"effective_rate": effective_rate,
			}
		)

	if not lines:
		raise SyncBlocker(_("No final Talabat order lines are available to synchronize."))
	return sorted(lines, key=lambda line: line["external_item_id"])


def _build_order_lines(order, account, tolerate_zero_quantity=False):
	if order.platform == "Shopify":
		return _build_shopify_sales_order_lines(order, account, tolerate_zero_quantity)
	if order.platform == "Noon":
		return _build_noon_sales_order_lines(order, account, tolerate_zero_quantity)
	return _build_sales_order_lines(order, account)


# #146 decision 2: eligibility is at confirmation, not at Talabat's
# "final"/FINAL_ITEM_STATUSES notion - a Noon unit counts iff its
# external_status is exactly CONFIRMED, its integration_status is anything
# other than OUT_OF_STOCK, and its current_quantity is positive. #145's
# stale status-group rows are zeroed (not deleted) rather than removed, so
# current_quantity > 0 is what actually excludes them here.
NOON_ELIGIBLE_EXTERNAL_STATUS = "CONFIRMED"
NOON_INELIGIBLE_INTEGRATION_STATUS = "OUT_OF_STOCK"


def _build_noon_sales_order_lines(order, account, tolerate_zero_quantity=False):
	"""Build Sales Order lines from Noon FBPI confirmed-and-shippable units.

	#146 decision 3: eligible rows are grouped by ``external_sku`` into one
	line keyed ``sku:<partner_sku>`` - NOT by the row's own status-bearing
	``external_item_id`` (``sku:<partner_sku>:<mp_status>:<integration_status>``,
	see #145's ``order_mapper._aggregate_items()``) - so a normal
	ACKNOWLEDGED -> SHIPPED transition (a new #145 row under a new
	``external_item_id``, the old one zeroed) never changes this snapshot's
	hash or looks like a modification once already synced.

	``tolerate_zero_quantity`` (linked, submitted Sales Order only, #178) returns
	no lines instead of raising when no unit is eligible any more, so the
	reconciliation planner sees the removal; a never-synced order still raises.
	"""
	currency = _resolved_currency(account)
	# Noon sends lowercase ISO codes ("egp"); ERPNext currencies are uppercase.
	if order.presentment_currency and order.presentment_currency.upper() != currency.upper():
		raise SyncBlocker(
			_(
				"Noon order {0}'s presentment currency ({1}) does not match the resolved ERPNext currency "
				"({2}). Multi-currency Sales Order synchronization is not supported."
			).format(order.name, order.presentment_currency, currency)
		)

	groups = {}
	group_order = []
	for row in order.items or []:
		if row.external_status != NOON_ELIGIBLE_EXTERNAL_STATUS:
			continue
		if row.integration_status == NOON_INELIGIBLE_INTEGRATION_STATUS:
			continue
		if flt(row.current_quantity) <= 0:
			continue
		sku = cstr(row.external_sku).strip()
		key = f"sku:{sku}"
		group = groups.get(key)
		if not group:
			group = {"external_sku": sku, "qty": 0.0, "amount": 0.0}
			groups[key] = group
			group_order.append(key)
		group["qty"] += flt(row.current_quantity)
		group["amount"] += flt(row.transaction_amount)

	lines = []
	stored_dates = _stored_line_delivery_dates(order) if order.get("erpnext_sales_order") else None
	payload_dates = None if stored_dates is not None else _noon_payload_dates(order)
	for key in group_order:
		group = groups[key]
		if group["qty"] <= 0:
			continue
		resolved = resolve_mapping_and_item(account.name, group["external_sku"])
		if not resolved:
			raise SyncBlocker(
				_("No enabled Marketplace Product Mapping exists for Noon SKU {0}.").format(
					group["external_sku"] or _("blank")
				),
				reason_code=reasons.MAPPING_MISSING,
			)
		item_uom = resolved.get("sales_uom") or frappe.get_cached_value("Item", resolved["item"], "stock_uom")
		amount = flt(group["amount"])
		effective_rate = flt(amount / group["qty"], EFFECTIVE_RATE_PRECISION)
		line = {
			"external_item_id": key,
			"item_code": resolved["item"],
			"qty": group["qty"],
			"uom": item_uom,
			"warehouse": account.warehouse,
			"fulfilled_total_price": amount,
			"effective_rate": effective_rate,
		}
		# A linked order keeps the dates it was synced with (a line the stored snapshot has no date
		# for stays without one), so this field never makes an already-synced order look changed.
		if stored_dates is not None:
			if stored_dates.get(key):
				line["delivery_date"] = stored_dates[key]
		else:
			line_date = _noon_line_delivery_date(order, payload_dates, group["external_sku"])
			if line_date:
				line["delivery_date"] = str(line_date)
		lines.append(line)

	if not lines and not tolerate_zero_quantity:
		raise SyncBlocker(_("No eligible Noon order lines are available to synchronize."))
	return sorted(lines, key=lambda line: line["external_item_id"])


def _shopify_transactions(order):
	"""The order's stored Shopify transactions as a list of dicts ([] when absent or invalid)."""
	raw = order.get("payment_transactions")
	if isinstance(raw, str):
		try:
			raw = frappe.parse_json(raw)
		except ValueError:
			return []
	return [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []


def is_manual_payment_pending(order):
	"""A PENDING Shopify order whose payment is a manual method (COD, bank deposit, custom),
	or one staff created with payment terms ("Due on receipt") and no transaction yet.

	Shopify marks the transaction of a manual payment method with ``manualPaymentGateway``
	(Admin API 2025-01+); the gateway NAME is never used because merchants can rename it.
	PENDING alone is not enough: Shopify also uses it for online payments still processing,
	which never carry payment terms.
	"""
	if order.external_financial_status != "PENDING":
		return False
	transactions = _shopify_transactions(order)
	if not transactions:
		return _has_shopify_payment_terms(order)
	return any(
		row.get("manualPaymentGateway") is True and row.get("status") == "PENDING" for row in transactions
	)


def _has_shopify_payment_terms(order):
	"""Shopify sent ``paymentTerms`` with a type (kept only in ``latest_payload``)."""
	payload = order.get("latest_payload")
	if isinstance(payload, str):
		try:
			payload = frappe.parse_json(payload)
		except ValueError:
			return False
	terms = payload.get("paymentTerms") if isinstance(payload, dict) else None
	return isinstance(terms, dict) and bool(cstr(terms.get("paymentTermsType")).strip())


def _describe_shopify_transactions(order):
	"""Blocker detail naming what Shopify sent, so an unexpected store setup can be diagnosed."""
	rows = _shopify_transactions(order)
	if not rows:
		return " " + _("No payment transactions were received.")
	described = "; ".join(
		"{0}/{1}/{2}/manual={3}".format(
			cstr(row.get("kind")) or "?",
			cstr(row.get("status")) or "?",
			cstr(row.get("gateway")) or "?",
			{True: "yes", False: "no"}.get(row.get("manualPaymentGateway"), "unknown"),
		)
		for row in rows
	)
	return " " + _("Transactions (kind/status/gateway/manual): {0}.").format(described)


def _build_shopify_sales_order_lines(order, account, tolerate_zero_quantity=False):
	"""Build immutable Sales Order lines from Shopify transaction evidence.

	``tolerate_zero_quantity`` (linked, submitted Sales Order only) keeps a line the
	marketplace has reduced to zero units as a zero-quantity, zero-amount line, and
	lets a REFUNDED/PARTIALLY_REFUNDED order through to the refund planner (#178).
	"""
	if order.cancelled_at:
		if order.erpnext_sales_order:
			raise SyncBlocker(
				_(
					"The Shopify order was cancelled but its linked Sales Order {0} is still a Draft; "
					"delete or cancel it in ERPNext."
				).format(order.erpnext_sales_order)
			)
		raise SyncBlocker(_("Cancelled Shopify orders cannot be synchronized."))
	eligible_statuses = {"PAID", "PARTIALLY_PAID"}
	if tolerate_zero_quantity:
		eligible_statuses |= {"REFUNDED", "PARTIALLY_REFUNDED"}
	if order.external_financial_status not in eligible_statuses and not is_manual_payment_pending(order):
		raise SyncBlocker(
			_("Shopify order {0} is not financially eligible for synchronization ({1}).{2}").format(
				order.name,
				order.external_financial_status or _("unknown status"),
				_describe_shopify_transactions(order) if order.external_financial_status == "PENDING" else "",
			)
		)

	lines = []
	for row in order.items or []:
		zero_quantity = flt(row.current_quantity) <= 0
		if zero_quantity and not tolerate_zero_quantity:
			raise SyncBlocker(
				_("Shopify line {0} is fully refunded or has no current quantity.").format(
					row.product_name or row.external_item_id
				)
			)
		if not cstr(row.external_variant_id).strip():
			raise SyncBlocker(
				_(
					"Shopify line {0} has no product variant (the product may have been deleted in Shopify), "
					"so it cannot be mapped."
				).format(row.product_name or row.external_item_id)
			)
		resolved = resolve_mapping_and_item(account.name, row.external_variant_id)
		if not resolved:
			raise SyncBlocker(
				_("No enabled Shopify variant mapping exists for {0} (variant {1}, SKU {2}).").format(
					row.product_name or row.external_item_id,
					row.external_variant_id,
					cstr(row.external_sku).strip() or _("blank"),
				),
				reason_code=reasons.MAPPING_MISSING,
			)
		if zero_quantity:
			lines.append(
				{
					"external_item_id": cstr(row.external_item_id).strip(),
					"item_code": resolved["item"],
					"qty": 0.0,
					"uom": resolved.get("sales_uom")
					or frappe.get_cached_value("Item", resolved["item"], "stock_uom"),
					"warehouse": account.warehouse,
					"fulfilled_total_price": 0.0,
					"effective_rate": 0.0,
				}
			)
			continue
		if row.transaction_amount_missing or row.transaction_amount is None:
			raise SyncBlocker(
				_("Shopify line {0} has no transaction amount.").format(row.external_item_id),
				reason_code=reasons.PRICE_MISSING,
			)
		transaction_amount = flt(row.transaction_amount)
		if transaction_amount < 0:
			raise SyncBlocker(
				_("Shopify line {0} has a negative transaction amount.").format(row.external_item_id)
			)
		quantity = flt(row.current_quantity)
		effective_rate = flt(transaction_amount / quantity, EFFECTIVE_RATE_PRECISION)
		item_uom = resolved.get("sales_uom") or frappe.get_cached_value("Item", resolved["item"], "stock_uom")
		lines.append(
			{
				"external_item_id": cstr(row.external_item_id).strip(),
				"item_code": resolved["item"],
				"qty": quantity,
				"uom": item_uom,
				"warehouse": account.warehouse,
				"fulfilled_total_price": transaction_amount,
				"effective_rate": effective_rate,
			}
		)

	if not lines:
		raise SyncBlocker(_("No eligible Shopify order lines are available to synchronize."))
	return sorted(lines, key=lambda line: line["external_item_id"])


def _resolved_currency(account):
	"""Return the currency the resulting Sales Order will actually be in.

	ERPNext's real resolution order (confirmed directly against the
	installed `erpnext/accounts/party.py::get_party_details()`, called via
	`selling_controller.set_missing_values()`) is Customer.default_currency
	FIRST, falling back to the Sales Order's already-set `currency` (which
	itself is only ever backfilled from the Price List when still blank -
	never true here, since party details are resolved before that), and
	only finally to the Company's default currency. This function does not
	need to read `Customer.default_currency` itself to mirror that, though:
	`_validate_account_configuration()` requires both the Price List's
	currency AND the account's Customer's `default_currency` (when either is
	set) to already equal the Company's default currency (#85 review finding
	#1/#9, V1 single-currency assumption) - so by the time this runs, every
	link in that chain that could matter is guaranteed to already agree, and
	reading the Price List/Company currency here is equivalent to reading
	whichever of the three ERPNext would actually pick.

	Resolved from account/Price List configuration rather than a live Sales
	Order instance, so it is available before any Sales Order document
	exists (e.g. while only computing a snapshot for hash/status
	comparison).

	This is the source `_amount_precision()` (post-calculation amount
	verification) resolves its precision from (#85 review finding #8). The
	line rate itself is deliberately NOT resolved from this currency: see
	`EFFECTIVE_RATE_PRECISION`'s module-level comment for why a fixed 6dp
	value is used there instead of a currency-derived one.
	"""
	price_list_currency = frappe.get_cached_value(
		"Price List", account.erpnext_selling_price_list, "currency"
	)
	return price_list_currency or frappe.get_cached_value(
		"Company", account.erpnext_company, "default_currency"
	)


def _amount_precision(account):
	"""Return the ERPNext amount precision the resulting Sales Order Item will use."""
	return frappe.get_precision("Sales Order Item", "amount", currency=_resolved_currency(account))


def _shopify_shipping_amount(order, account, block_taxes=True):
	"""The Shopify shipping charge to carry on the Sales Order, or None when there is none.

	Only Shopify orders carry one.  ``shipping_total`` is Shopify's ``currentShippingPriceSet``:
	already net of shipping discounts and refunds.  Shopify includes tax in it when the shop's
	prices include tax (``taxesIncluded``, shopify.dev, external information).  Taxes and duties are not
	synced yet, so an order with either blocks instead of producing a wrong total, but only while a
	Sales Order is being created or a Draft one updated (``block_taxes``).  An order already linked
	to a submitted Sales Order is left to the reconciliation planner: that document exists and is
	never re-created from this snapshot, so "create it manually" would be wrong advice (legacy
	synced orders that carry tax must not flip to Needs Action).  The income account is only
	required once there is a charge to post.
	"""
	if order.platform != "Shopify":
		return None
	if block_taxes and (flt(order.total_taxes) or flt(order.duties_total)):
		raise SyncBlocker(
			_(
				"Shopify order {0} has taxes or duties (taxes {1}, duties {2}). Taxes and duties are not "
				"synced to ERPNext yet; create or adjust this order's Sales Order in ERPNext manually."
			).format(order.name, flt(order.total_taxes), flt(order.duties_total))
		)
	shipping = flt(order.shipping_total, _amount_precision(account))
	if shipping <= 0:
		return None
	if not account.erpnext_shipping_account:
		raise SyncBlocker(
			_(
				"Shopify order {0} has a shipping charge of {1}. Set the ERPNext Shipping Income Account on "
				"Marketplace Account {2} before synchronizing it."
			).format(order.name, shipping, account.name)
		)
	problem = shipping_account_problem(account.erpnext_shipping_account, account.erpnext_company)
	if problem:
		raise SyncBlocker(problem)
	return shipping


def _make_snapshot(account, customer, lines, transaction_date, delivery_date, address=None, shipping=None):
	snapshot = {
		"company": account.erpnext_company,
		"customer": customer,
		"selling_price_list": account.erpnext_selling_price_list,
		"transaction_date": transaction_date,
		"delivery_date": delivery_date,
		"items": lines,
	}
	if address is not None:
		snapshot["shipping_address_name"] = address["name"]
		snapshot["customer_address"] = (
			address["name"] if address["address_type"] == "Billing" and address["name"] else None
		)
	# Omitted unless there is a charge, so other platforms and free-shipping orders keep
	# their existing snapshots and hashes.  The income account is deliberately not part of
	# it: an account configuration change must not mark submitted orders as changed.
	if shipping:
		snapshot["shipping"] = shipping
	return snapshot


def set_sync_status_from_current_snapshot(order, preserve_sync_error=False):
	"""Set in-memory status while Marketplace Order is being saved.

	A source-order save must never be blocked merely because a mapping or later
	ERPNext configuration has changed.  A linked order whose marketplace state differs from
	its Sales Order is therefore never silently considered in sync: when the change is one
	the sync can apply itself the order becomes Syncing with the debounce bumped (#364);
	anything a person has to decide becomes Needs Action, and input the sync cannot build
	(unmapped item, missing price) a Sync Error.
	"""
	if preserve_sync_error and order.erpnext_sync_status == sync_status.SYNC_ERROR:
		return
	if not order.erpnext_sales_order or not order.last_synced_hash:
		return
	docstatus = frappe.db.get_value("Sales Order", order.erpnext_sales_order, "docstatus")
	sales_order = None
	evidence = {}
	try:
		evidence = _reconciliation_evidence(order)
		if docstatus != 0:
			sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
		if _uses_stored_snapshot(docstatus, evidence):
			current_snapshot = _parse_last_synced_snapshot(order) or {}
		else:
			current_snapshot = build_sales_order_snapshot(order, for_submitted_order=docstatus != 0)
	except SyncBlocker as exc:
		if _compensating_plan_outranks_blocker(order, sales_order, evidence):
			_apply_reconciliation_status(
				order, plan(_parse_last_synced_snapshot(order), {}, evidence, read_erp_state(sales_order))
			)
			return
		reasons.apply_sync_state(order, exc.reason_code, cstr(exc))
		return
	if docstatus != 0:
		reconciliation_plan = hold_cancel_for_draft_documents(
			plan(
				_parse_last_synced_snapshot(order), current_snapshot, evidence, read_erp_state(sales_order)
			),
			sales_order.name,
		)
		_apply_reconciliation_status(order, reconciliation_plan, queue_executable=True)
		if reconciliation_plan["outcome"] == NO_ACTION:
			# Runs inside Marketplace Order.before_save()/get_current_sync_status():
			# in-memory only, persisted by the very save already in progress.
			_advance_baseline(order, _parse_last_synced_snapshot(order), current_snapshot)
		return
	current_hash = snapshot_hash(current_snapshot)
	if current_hash == order.last_synced_hash:
		reasons.apply_synced(order)
	else:
		# A Draft Sales Order is updated in place by the next sync.
		queue_resync_in_memory(order)


def queue_resync_in_memory(order, trigger_source="Manual"):
	"""Mark ``order`` Syncing and push its debounce, in memory, for the save already in progress.

	``bump_sync_debounce()`` writes with ``frappe.db.set_value()``, which the surrounding
	document save would overwrite; this is its in-memory twin.  An order that is already
	Syncing keeps its window, so repeated form loads do not push it forward for ever.
	"""
	from marketplace_integrations.services.sales_order_sync_debounce import SYNC_DEBOUNCE_SECONDS

	if order.erpnext_sync_status == sync_status.SYNCING and order.sync_debounce_until:
		return
	order.erpnext_sync_status = sync_status.SYNCING
	# The earlier error or instruction is stale once a new sync is queued.
	order.last_sync_error = None
	order.sync_debounce_until = add_to_date(now_datetime(), seconds=SYNC_DEBOUNCE_SECONDS)
	order.pending_sync_trigger_source = trigger_source


def _compensating_plan_outranks_blocker(order, sales_order, evidence):
	"""True when refund/return evidence on a delivered/billed submitted order must win.

	A rebuild ``SyncBlocker`` (disabled mapping, missing price, ...) must not hide
	``UNSUPPORTED_COMPENSATING_TRANSACTION``; ``plan()`` checks that evidence before
	it looks at the (here unavailable) current snapshot, so an empty one is enough.
	"""
	if not sales_order or sales_order.docstatus == 0 or not evidence:
		return False
	if not (evidence.get("refund") or evidence.get("return")):
		return False
	reconciliation_plan = plan(_parse_last_synced_snapshot(order), {}, evidence, read_erp_state(sales_order))
	return reconciliation_plan["outcome"] == UNSUPPORTED_COMPENSATING_TRANSACTION


def _flag_compensating_evidence_on_new_order(order, sales_order, snapshot):
	"""Mark a just-created Sales Order Needs Action when return/refund evidence already exists.

	Evidence recorded while the order had no Sales Order (e.g. a return linked during
	Sync Error) bumped no re-plan, so the first sync must not report it Synced.  Only
	the unsupported compensating outcome is handled here; any other outcome keeps the
	plain success.  Returns the advisory result, or None.
	"""
	evidence = _reconciliation_evidence(order)
	if not (evidence.get("return") or evidence.get("refund")):
		return None
	reconciliation_plan = plan(
		_parse_last_synced_snapshot(order), snapshot, evidence, read_erp_state(sales_order)
	)
	if reconciliation_plan["outcome"] != UNSUPPORTED_COMPENSATING_TRANSACTION:
		return None
	return _save_reconciliation_advisory(order, reconciliation_plan)


def _uses_stored_snapshot(docstatus, evidence):
	"""A submitted Sales Order with full-cancellation evidence is judged against the stored baseline.

	A cancelled order can no longer be rebuilt into a snapshot, so only cancellation
	uses the stored baseline.  Every other evidence (refund, return, lifecycle) is
	judged against a freshly rebuilt snapshot.
	"""
	return docstatus != 0 and evidence.get("cancellation") == "full"


def _apply_reconciliation_status(order, reconciliation_plan, queue_executable=False):
	"""Map a reconciliation outcome to the in-memory sync status, reason and message.

	NO_ACTION is Synced.  An executable outcome (cancel, close, update) is only reached from
	the form-load/save recompute (``queue_executable``): the sync applies it, so the order becomes
	Syncing, except after an earlier execution of the same kind failed (re-queuing that would
	only repeat the failure).  Every other outcome is Needs Action with its reason code.
	"""
	outcome = reconciliation_plan["outcome"]
	if outcome == NO_ACTION:
		reasons.apply_synced(order)
	elif outcome in _EXECUTABLE_OUTCOMES and queue_executable:
		if order.get("sync_reason_code") not in reasons.EXECUTION_FAILED_REASONS:
			queue_resync_in_memory(order)
	else:
		reasons.apply_sync_state(
			order,
			reconciliation_plan.get("reason_code") or reasons.ERP_STATE_DIVERGED,
			reconciliation_plan["message"],
		)


def get_current_sync_status(marketplace_order):
	"""Calculate the persisted order's current status for the form load."""
	order = frappe.get_doc("Marketplace Order", marketplace_order)
	if not order.erpnext_sales_order or not order.last_synced_hash:
		return order.erpnext_sync_status

	previous_status = order.erpnext_sync_status
	previous_sync_error = order.last_sync_error
	previous_hash = order.last_synced_hash
	set_sync_status_from_current_snapshot(order, preserve_sync_error=True)
	if (
		order.erpnext_sync_status != previous_status
		or order.last_sync_error != previous_sync_error
		or order.last_synced_hash != previous_hash
	):
		order.flags.skip_sync_status_update = True
		order.save()
	return order.erpnext_sync_status


def _validate_platform_supported(order):
	"""Block ERPNext Sales Order synchronization for any platform other than
	Talabat or Shopify (#110). A stray sync attempt on an unsupported
	platform must not persist a misleading "Sync Error" status, and must fail
	with a clear, actionable message
	instead. A blank ``platform`` (e.g. a pre-#108 order whose backfill
	patch has not yet run) is treated as Talabat, matching every existing
	Marketplace Order's real origin today - it is never a Shopify order.
	"""
	if order.platform and order.platform not in {"Talabat", "Shopify", "Noon"}:
		raise SyncBlocker(
			_(
				"ERPNext Sales Order synchronization is only available for Talabat, Shopify and Noon orders. "
				"Marketplace Order {0} is a {1} order."
			).format(order.name, order.platform)
		)


def _get_account(order):
	account = frappe.get_doc("Marketplace Account", order.marketplace_account)
	if not account.enabled:
		raise SyncBlocker(_("Marketplace Account {0} is disabled.").format(account.name))
	return account


def _validate_sync_permissions(order, account):
	order.check_permission("write")
	account.check_permission("write")
	sales_order_permission = "write" if order.erpnext_sales_order else "create"
	if not frappe.has_permission("Sales Order", ptype=sales_order_permission):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if not account.erpnext_customer and not frappe.has_permission("Customer", ptype="create"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def _validate_account_configuration(account):
	if not account.erpnext_company:
		raise SyncBlocker(
			_("Marketplace Account {0} needs an ERPNext Company before synchronizing orders.").format(
				account.name
			)
		)
	if not account.erpnext_selling_price_list:
		raise SyncBlocker(
			_(
				"Marketplace Account {0} needs an ERPNext Selling Price List before synchronizing orders."
			).format(account.name)
		)
	price_list = frappe.get_doc("Price List", account.erpnext_selling_price_list)
	if not price_list.enabled or not price_list.selling:
		raise SyncBlocker(
			_("ERPNext Selling Price List {0} must be enabled and marked for selling.").format(
				price_list.name
			)
		)
	# V1 assumes single-currency operation: Talabat fulfilled amounts are bare
	# numbers in the outlet's local currency, and Marketplace Order itself
	# stores no currency of its own. If the Selling Price List's currency, or
	# the account's generic Customer's own default_currency, ever diverged
	# from the Company's default currency, those bare Talabat numbers would
	# get copied as a raw rate into a different currency without any
	# conversion - so both are enforced rather than silently trusted (#85
	# review finding #9, corrected by finding #1 to also cover the Customer:
	# ERPNext actually resolves a Sales Order's own `currency` from
	# Customer.default_currency FIRST - see `_resolved_currency()` - so a
	# Customer-level override slips past a Price-List-only check entirely
	# while `rate * qty == fulfilled_total_price` still holds numerically).
	company_currency = frappe.get_cached_value("Company", account.erpnext_company, "default_currency")
	if price_list.currency and price_list.currency != company_currency:
		raise SyncBlocker(
			_(
				"ERPNext Selling Price List {0}'s currency ({1}) must match Company {2}'s default "
				"currency ({3}) before synchronizing orders. Multi-currency Sales Order synchronization "
				"is not supported."
			).format(price_list.name, price_list.currency, account.erpnext_company, company_currency)
		)
	if account.erpnext_customer:
		customer_currency = frappe.get_cached_value("Customer", account.erpnext_customer, "default_currency")
		if customer_currency and customer_currency != company_currency:
			raise SyncBlocker(
				_(
					"ERPNext Customer {0}'s default currency ({1}) must match Company {2}'s default "
					"currency ({3}) before synchronizing orders. Multi-currency Sales Order synchronization "
					"is not supported."
				).format(
					account.erpnext_customer, customer_currency, account.erpnext_company, company_currency
				)
			)
	try:
		validate_account_warehouse(account, context=_("Sales Order synchronization"))
	except frappe.ValidationError as exc:
		# validate_account_warehouse() raises via frappe.throw(), which already
		# appended its (non-escape_html'd) message to frappe.local.message_log
		# before raising - catching the exception here does not undo that.
		# Left uncleared, that raw message could still reach the client
		# alongside the properly re-raised SyncBlocker, bypassing the
		# escaping discipline #85 review finding #7 established for exactly
		# this class of message. Clear it so only the SyncBlocker's own
		# str(exc) (escaped client-side by marketplace_order.js) is shown.
		frappe.clear_messages()
		raise SyncBlocker(str(exc)) from exc


def _get_or_create_customer(account):
	if account.erpnext_customer:
		return _require_customer(account)

	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": _generic_customer_name(account),
			"customer_type": "Company",
		}
	).insert()
	account.erpnext_customer = customer.name
	account.save()
	return customer.name


def _get_or_create_shopify_customer(order, account, create=True):
	"""Resolve a Shopify buyer by email, falling back to the account Customer.

	``create=False`` is the read-only snapshot-preview/status-check mode used
	by ``build_sales_order_snapshot()`` when it wasn't given an explicit
	``customer`` (see that function's docstring): it only looks up a Customer
	already resolved by a prior real sync and never inserts a new one. When
	nothing has been resolved yet, it returns the account's own generic
	Customer if one already exists, or ``None`` otherwise - it is the
	caller's responsibility to fall back sensibly for a snapshot-only value.
	"""
	email = cstr(order.customer_email).strip()
	if not email:
		if create:
			return _get_or_create_customer(account)
		return account.erpnext_customer or None

	customer_name = frappe.db.get_value("Customer", {"email_id": email}, "name")
	if customer_name:
		return customer_name
	if not create:
		return account.erpnext_customer or None

	first_name = cstr(order.customer_first_name or "").strip()
	last_name = cstr(order.customer_last_name or "").strip()
	full_name = " ".join(part for part in (first_name, last_name) if part) or email
	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": full_name,
			"customer_type": "Individual",
			"email_id": email,
			"mobile_no": order.customer_phone_number,
		}
	).insert()
	return customer.name


SHOPIFY_ADDRESS_TITLE_PREFIX = "Shopify Marketplace Address-"


def _shopify_address_title(customer):
	"""Return a stable, customer-specific title for this integration's Address."""
	customer_hash = hashlib.sha256(cstr(customer).encode()).hexdigest()[:10]
	return f"{SHOPIFY_ADDRESS_TITLE_PREFIX}{customer_hash}"


def _find_shopify_address(customer):
	"""Return the name of this integration's dedicated Address for a buyer, if any."""
	links = frappe.get_all(
		"Dynamic Link",
		filters={
			"parenttype": "Address",
			"parentfield": "links",
			"link_doctype": "Customer",
			"link_name": customer,
		},
		fields=["parent"],
	)
	address_title = _shopify_address_title(customer)
	return next(
		(
			row.parent
			for row in links
			if frappe.db.get_value("Address", row.parent, "address_title") == address_title
		),
		None,
	)


def _resolve_shopify_address(order, customer, create=True):
	"""Create/update this integration's dedicated Address for a buyer.

	``create=False`` is the read-only snapshot-preview/status-check mode used
	by ``build_sales_order_snapshot()`` (see that function's docstring): it
	only looks up an Address already created by a prior real sync and never
	inserts or updates one. When nothing exists yet, it returns ``None`` -
	the caller falls back to a blank address for the snapshot only.
	"""
	if not cstr(order.customer_email).strip() or not customer:
		return None

	try:
		if not create:
			address_name = _find_shopify_address(customer)
			if not address_name:
				return None
			address_type = frappe.db.get_value("Address", address_name, "address_type")
			return {"name": address_name, "address_type": address_type}

		shipping = _as_address_dict(order.shipping_address)
		billing = _as_address_dict(order.billing_address)
		# An explicitly present shipping object wins even if it is sparse; only
		# a null shipping object permits billing fallback.
		if order.shipping_address is not None:
			source, address_type = shipping, "Shipping"
		else:
			source, address_type = billing, "Billing"

		values = {
			"address_line1": cstr(source.get("address1") or "").strip(),
			"address_line2": cstr(source.get("address2") or "").strip(),
			"city": cstr(source.get("city") or "").strip(),
			"country": cstr(source.get("country") or "").strip(),
			"pincode": cstr(source.get("zip") or "").strip(),
		}
		if not any(values.values()):
			return None
		# Shopify province -> ERPNext state; city stays in city (#156).
		values["state"] = cstr(source.get("province") or "").strip()
		values["email_id"] = cstr(order.customer_email).strip()
		values["phone"] = cstr(order.customer_phone_number or "").strip()
		if values["country"] and not frappe.db.exists("Country", values["country"]):
			frappe.logger("marketplace_integrations").warning(
				"Shopify Address country was not recognized; defaulted to Egypt."
			)
			values["country"] = "Egypt"

		address_title = _shopify_address_title(customer)
		address_name = _find_shopify_address(customer)

		if address_name:
			address = frappe.get_doc("Address", address_name)
		else:
			address = frappe.new_doc("Address")
		address.address_title = address_title
		address.address_type = address_type
		for fieldname, value in values.items():
			setattr(address, fieldname, value)
		if not any(
			link.link_doctype == "Customer" and link.link_name == customer
			for link in address.get("links", [])
		):
			address.append("links", {"link_doctype": "Customer", "link_name": customer})
		address.is_primary_address = 1

		frappe.db.savepoint("shopify_address_sync")
		try:
			if address.is_new():
				address.insert()
			else:
				address.save()
		except Exception:
			frappe.db.rollback(save_point="shopify_address_sync")
			frappe.db.release_savepoint("shopify_address_sync")
			frappe.clear_messages()
			frappe.logger("marketplace_integrations").warning(
				"Shopify Address could not be saved; Sales Order synchronization will continue without it."
			)
			return None
		frappe.db.release_savepoint("shopify_address_sync")
		return {"name": address.name, "address_type": address_type}
	except Exception:
		frappe.clear_messages()
		frappe.logger("marketplace_integrations").warning(
			"Shopify Address could not be resolved; Sales Order synchronization will continue without it."
		)
		return None


def _as_address_dict(value):
	if isinstance(value, dict):
		return value
	if isinstance(value, str):
		try:
			parsed = json.loads(value)
			return parsed if isinstance(parsed, dict) else {}
		except (TypeError, ValueError):
			return {}
	return {}


def _require_customer(account):
	if not account.erpnext_customer:
		raise SyncBlocker(_("Marketplace Account {0} has no ERPNext Customer.").format(account.name))
	if not frappe.db.exists("Customer", account.erpnext_customer):
		raise SyncBlocker(
			_("ERPNext Customer {0} configured on Marketplace Account {1} does not exist.").format(
				account.erpnext_customer, account.name
			)
		)
	return account.erpnext_customer


def _generic_customer_name(account):
	return _("Marketplace Account {0}").format(account.name)


def _get_enabled_mapping(marketplace_account, external_sku):
	external_sku = cstr(external_sku).strip()
	if not external_sku:
		return None
	key = compute_mapping_identity_key(marketplace_account, external_sku)
	values = frappe.db.get_value(
		"Marketplace Product Mapping",
		{"mapping_identity_key": key, "enabled": 1},
		["item", "sales_uom"],
		as_dict=True,
	)
	return frappe._dict(values) if values else None


def _apply_snapshot(sales_order, snapshot, marketplace_order_name, shipping_account=None):
	"""Populate a Sales Order, then overwrite every line with the historical
	Talabat transaction amount so it survives ERPNext's own recalculation.

	``custom_marketplace_order`` (a fixture-defined Custom Field on core Sales
	Order, see ``fixtures/custom_field.json``) is set explicitly here rather
	than relying on the user opening the Marketplace Order form - both the
	Talabat and Shopify branches of ``sync_sales_order()`` share this one
	function, so the wiring is platform-agnostic. ``custom_marketplace_platform``
	is populated via that Custom Field's own ``fetch_from:
	custom_marketplace_order.platform`` the moment this field is set and the
	document is saved/inserted - confirmed to fire on a purely server-side
	``.insert()``/``.save()`` with no client/form involved, so it needs no
	separate assignment here.

	`set_missing_values()` is used exactly once, purely for structural
	defaults (Item/UOM/conversion, accounts, party/company details,
	currency) - not to price a line: `ignore_pricing_rule = 1` is set before
	it runs so no Pricing Rule is applied even for that first pass, and
	whatever Item Price it fetches for `rate`/`price_list_rate` is discarded
	immediately below.  `rate`/`price_list_rate`/`discount_*`/`is_free_item`
	are then explicitly overwritten from the snapshot's `effective_rate`,
	and totals are recalculated from that overridden rate.

	Because `rate`/`price_list_rate` are non-None the moment this function
	returns, ERPNext's own `AccountsController.validate()` - which reruns
	`set_missing_values(for_validate=True)` on every subsequent `.insert()`/
	`.save()` of this same document - cannot silently refill them from a
	current Item Price. This is specific to those two fields, not a general
	property of `set_missing_item_details()`: that function unconditionally
	overwrites several OTHER child row fields on every call regardless of
	their current value, via its own hard-coded `force_item_fields` tuple
	(`item_group`, `brand`, `stock_uom`, `is_fixed_asset`, `pricing_rules`,
	`weight_per_unit`, `weight_uom`, `total_weight`, `valuation_rate` -
	confirmed directly against the installed
	`erpnext/controllers/accounts_controller.py`). `rate` and
	`price_list_rate` are simply absent from that tuple, so they fall back to
	its plain `item.get(fieldname) is None` gate instead, which the explicit
	overwrite below already defeats. A separate `pricing_rule_removed` branch
	elsewhere in ERPNext resets `rate`/`discount_percentage`/`discount_amount`
	when a line's stale `pricing_rules` no longer resolve - genuinely
	unreachable here: `ignore_pricing_rule = 1` (set below) means no line
	from this function ever carries `pricing_rules` in the first place, and
	`sales_order.items` is rebuilt from scratch on every call, so no stale
	`pricing_rules` value can survive from a prior snapshot either.

	See also `docs/frappe-conventions.md`'s note on this same
	`force_item_fields`/None-check-gate/`ignore_pricing_rule` behavior.
	"""
	sales_order.company = snapshot["company"]
	sales_order.customer = snapshot["customer"]
	sales_order.selling_price_list = snapshot["selling_price_list"]
	sales_order.transaction_date = snapshot["transaction_date"]
	sales_order.delivery_date = snapshot["delivery_date"]
	sales_order.ignore_pricing_rule = 1
	sales_order.custom_marketplace_order = marketplace_order_name
	# A pre-existing Draft being re-synced may carry header discount/tax/
	# shipping configuration from a prior manual Desk edit made between
	# syncs. apply_discount_amount() (erpnext/controllers/taxes_and_totals.py)
	# redistributes `discount_amount` across every item's net_amount/net_rate
	# when it is set, and a Sales Taxes and Charges Template/Shipping Rule
	# can add further header-level amounts - #85 forbids any of that
	# silently cutting the merchandise value below the Talabat transaction
	# total, so every re-sync resets all of it explicitly rather than
	# trusting whatever the existing Draft happened to have (#85 review
	# finding #3).
	sales_order.apply_discount_on = "Grand Total"
	sales_order.additional_discount_percentage = 0
	sales_order.discount_amount = 0
	sales_order.taxes_and_charges = None
	sales_order.taxes = []
	sales_order.shipping_rule = None
	sales_order.items = []
	for line in snapshot["items"]:
		sales_order.append(
			"items",
			{
				"item_code": line["item_code"],
				"qty": line["qty"],
				"uom": line["uom"],
				"warehouse": line["warehouse"],
			},
		)
	sales_order.set_missing_values()

	for line, so_item in zip(snapshot["items"], sales_order.items, strict=True):
		so_item.price_list_rate = line["effective_rate"]
		so_item.rate = line["effective_rate"]
		so_item.discount_percentage = 0
		so_item.discount_amount = 0
		so_item.is_free_item = 1 if line["effective_rate"] == 0 else 0
		# Noon lines carry their own date (the header set above is the earliest, but ERPNext's own
		# validate_delivery_date later sets it to the latest item date); other lines
		# keep the header date `set_missing_values()` copied onto them.
		if line.get("delivery_date"):
			so_item.delivery_date = getdate(line["delivery_date"])

	# The one charge row, rebuilt from scratch on every call (no template or Shipping Rule is
	# used).  Shopify's shipping is already net of shipping discounts.
	if snapshot.get("shipping"):
		sales_order.append(
			"taxes",
			{
				"charge_type": "Actual",
				"account_head": shipping_account,
				"description": "Shipping",
				"tax_amount": snapshot["shipping"],
				"included_in_print_rate": 0,
			},
		)

	# Recalculates net/grand totals from the rates just overridden above and
	# the header fields just reset - no Sales Taxes and Charges Template,
	# Shipping Rule, or additional discount survives from before this call.
	sales_order.calculate_taxes_and_totals()
	if "shipping_address_name" in snapshot:
		sales_order.shipping_address_name = snapshot["shipping_address_name"]
		sales_order.customer_address = snapshot["customer_address"]


def buyer_phone_for_sales_order(order):
	"""The phone to store on a Shopify Sales Order's ``contact_mobile`` (#277), or ``""``."""
	if order.platform != "Shopify":
		return ""
	return cstr(order.customer_phone_number).strip()


def _fill_buyer_phone(sales_order, order):
	"""Copy the Shopify buyer phone into ``contact_mobile`` only while it is empty.

	``contact_mobile`` is staff-editable (Property Setter), so a typed value is never overwritten
	by a later sync.  The phone is deliberately not in the snapshot, so it never feeds the hash or
	marks the order as changed.
	"""
	phone = buyer_phone_for_sales_order(order)
	if phone and not cstr(sales_order.contact_mobile).strip():
		sales_order.contact_mobile = phone


def _verify_line_amounts(sales_order, snapshot, account):
	"""Block instead of silently drifting if a line's calculated amount does
	not reproduce the Talabat fulfilled total within ERPNext's own currency
	precision for that field (e.g. an effective-rate rounding remainder).

	Precision is resolved from `_amount_precision(account)` rather than
	`so_item.precision("amount")` independently, so this check and the header
	`_resolved_currency()` source can never resolve to different currencies
	(#85 review finding #8).
	"""
	precision = _amount_precision(account)
	for line, so_item in zip(snapshot["items"], sales_order.items, strict=True):
		expected = flt(line["fulfilled_total_price"], precision)
		actual = flt(so_item.amount, precision)
		if expected != actual:
			raise SyncBlocker(
				_(
					"Talabat line {0} ({1}) would post to ERPNext as {2} but the Talabat fulfilled total is {3}. "
					"This can happen when the fulfilled amount does not divide evenly by the fulfilled quantity "
					"at ERPNext's currency precision. Please review before retrying."
				).format(line["external_item_id"], so_item.item_code, actual, expected)
			)


def _verify_grand_total(sales_order, snapshot, order, account):
	"""Block unless the Sales Order total reproduces Shopify's order total (Shopify only).

	Compares ``grand_total`` (not ``rounded_total``) within one currency-precision unit.
	Nothing is forced to match: a gap is reported, never papered over with an adjustment
	line.  A recorded total of 0 is only accepted for a free order (no shipping, every item
	free); a missing total beside a non-zero item or shipping amount (a row imported before
	totals were stored) blocks until the order is refreshed from Shopify.  An ERPNext Actual
	charge is spread over the items by value, so it is lost when every item is free.
	"""
	if order.platform != "Shopify":
		return
	shipping = flt(snapshot.get("shipping"))
	shopify_total = flt(order.order_total)
	precision = _amount_precision(account)
	items_total = flt(sum(flt(row.amount) for row in sales_order.items), precision)
	if not shopify_total and not shipping and not items_total:
		return
	if not shopify_total:
		raise SyncBlocker(
			_(
				"Shopify's order total was not recorded for order {0}, so the Sales Order total cannot be "
				"verified. Use Refresh from Shopify on the order, then synchronize it again."
			).format(order.name)
		)
	if shipping and not items_total:
		raise SyncBlocker(
			_(
				"Shopify order {0} has a shipping charge of {1} but all of its items are free; ERPNext cannot "
				"post a shipping charge on a zero-value order. Handle this order in ERPNext manually."
			).format(order.name, shipping)
		)
	sales_order_total = flt(sales_order.grand_total, precision)
	if flt(abs(sales_order_total - shopify_total), precision) > 10 ** -cint(precision):
		raise SyncBlocker(
			_(
				"Shopify total {0} does not match the Sales Order total {1} (items {2} + shipping {3}). "
				"Review the Shopify order before retrying."
			).format(shopify_total, sales_order_total, items_total, shipping)
		)


def snapshot_hash(snapshot):
	commercial_snapshot = deepcopy(snapshot)
	for line in commercial_snapshot.get("items") or []:
		line.pop("sales_order_item", None)
	commercial_snapshot.pop("refund_ids", None)
	return hashlib.sha256(frappe.as_json(commercial_snapshot).encode()).hexdigest()


def _reconciliation_evidence(order):
	"""Return the platform adapter's explicit, persisted lifecycle evidence (#178).

	Interpretation of platform statuses/fields lives in each platform's
	``integrations/<platform>/order_reconciliation.build_evidence``; no generic
	display status is interpreted here.  A blank platform is Talabat.
	"""
	builder_path = _RECONCILIATION_EVIDENCE_BUILDERS.get(order.platform or "Talabat")
	evidence = frappe.get_attr(builder_path)(order) if builder_path else {}
	# Platform-neutral keys merged for every platform (#181), so both the sync path and
	# the status path (form load / save) see them.  The marker survives sync-log cleanup.
	if getattr(order, "outbound_cancelled_on", None):
		evidence = {**evidence, "outbound_cancel_requested": True}
	if getattr(order, "outbound_edit_fingerprint", None):
		# Shopify item edit (#356): only a confirmed ("completed") edit lets the planner keep
		# the newer ERP quantities; an unconfirmed one (any other state) needs a person.
		key = (
			"outbound_edit_requested"
			if getattr(order, "outbound_edit_state", None) == "completed"
			else "outbound_edit_in_flight"
		)
		evidence = {**evidence, key: True}
	if getattr(order, "erpnext_sales_order", None):
		instruction = platform_instruction(order)
		if instruction:
			evidence = {**evidence, "inactive_order_instruction": instruction}
	if _prepared_return_submitted(order):
		# The draft return this app prepared (#364) was submitted by a person: the compensating
		# transaction exists, so the return/refund evidence no longer blocks.
		evidence = {**evidence, "compensating_document_submitted": True}
	return evidence


def _prepared_return_submitted(order):
	if getattr(order, "sync_prepared_doctype", None) != "Delivery Note" or not getattr(
		order, "sync_prepared_document", None
	):
		return False
	values = frappe.db.get_value(
		"Delivery Note", order.sync_prepared_document, ["docstatus", "is_return"], as_dict=True
	)
	return bool(values and values.docstatus == 1 and values.is_return)


def _advance_baseline(order, previous, current_snapshot):
	"""Make an ERP-equivalent current snapshot the new in-memory baseline.

	Does not save: callers are already saving the order.  ERPNext matches the current
	marketplace state here (NO_ACTION), so the order's CURRENT refund ids are stored as
	``refund_ids``: a refund the merchant already reflected in the Sales Order must stop
	counting as unreflected evidence.  A no-op when the baseline hash already matches
	and its stored refund ids already equal the current ones (e.g. the stored-snapshot
	path).
	"""
	current_hash = snapshot_hash(current_snapshot)
	if not previous:
		return
	current_refund_ids = sorted(_refund_ids(order))
	if (
		current_hash == order.last_synced_hash
		and sorted(previous.get("refund_ids") or []) == current_refund_ids
	):
		return
	advanced = snapshot_advanced_from(previous, current_snapshot)
	advanced["refund_ids"] = current_refund_ids
	order.last_synced_hash = current_hash
	order.last_synced_snapshot = frappe.as_json(advanced)
	if _line_quantities(advanced) != _line_quantities(previous):
		# The marketplace now shows different quantities and ERPNext matches them, so the edit
		# marker has done its job.  Unchanged quantities mean it may still be pending (the
		# planner's NO_ACTION can be the marker's own guard): keep it.
		release_outbound_edit_marker(order)


def _line_quantities(snapshot):
	return {
		cstr(line.get("external_item_id")).strip(): flt(line.get("qty"))
		for line in snapshot.get("items") or []
	}


def release_outbound_edit_marker(order):
	"""Clear the Shopify item-edit marker (#356) in memory, for the save already in progress.

	Called wherever the baseline moves, because the marker only exists to protect the ERP
	quantities until the baseline catches up.  ``Marketplace Order.before_save`` otherwise
	restores the marker from the stored copy.
	"""
	if order.get("outbound_edit_fingerprint"):
		order.outbound_edit_fingerprint = None
		order.outbound_edit_state = None
		order.flags.clear_outbound_edit_marker = True


def rebaseline_after_outbound_edit(order, sales_order):
	"""Make the refreshed Shopify order the baseline after ERPNext's item edit (#356).

	``order`` is the freshly imported Marketplace Order, ``sales_order`` the current
	Sales Order.  The refreshed snapshot is paired with the Sales Order's rows (a line added
	by the edit has a new Shopify line id, so the old baseline cannot judge it) and accepted
	only when the planner, run with that paired snapshot as the baseline and without the edit
	marker, says ERPNext equals what Shopify now shows (``NO_ACTION``).  Anything else becomes
	Needs Action for staff to review (Syncing when the sync can apply it itself).  Saves
	``order``; does not commit.
	"""
	order.flags.skip_sync_status_update = True
	evidence = _reconciliation_evidence(order)
	evidence.pop("outbound_edit_requested", None)
	evidence.pop("outbound_edit_in_flight", None)
	try:
		snapshot = build_sales_order_snapshot(order, for_submitted_order=sales_order.docstatus != 0)
		paired = snapshot_with_sales_order_item_names(snapshot, sales_order, refund_ids=_refund_ids(order))
	except SalesOrderLinesMismatch:
		return _flag_after_outbound_edit(
			order,
			reasons.OUTBOUND_EDIT_MISMATCH,
			_("ERPNext's items do not match the order Shopify now shows; review the order."),
		)
	except SyncBlocker as exc:
		return _flag_after_outbound_edit(order, exc.reason_code, cstr(exc))
	reconciliation_plan = plan(paired, snapshot, evidence, read_erp_state(sales_order))
	if reconciliation_plan["outcome"] == NO_ACTION:
		release_outbound_edit_marker(order)
		return _save_success(order, snapshot_hash(snapshot), sales_order, True, snapshot)
	release_outbound_edit_marker(order)
	_apply_reconciliation_status(order, reconciliation_plan, queue_executable=True)
	order.save()
	return {"ok": False}


def _flag_after_outbound_edit(order, reason_code, message):
	release_outbound_edit_marker(order)
	reasons.apply_sync_state(order, reason_code, message)
	order.save()
	return {"ok": False}


def _reconcile_submitted_order(order, sales_order, snapshot, evidence, trigger_source):
	"""Plan one submitted-order reconciliation; execute it if supported, else record an advisory."""
	erp_state = read_erp_state(sales_order)
	previous = _parse_last_synced_snapshot(order)
	if not previous and snapshot:
		# Case 4 (#364): a missing baseline is adopted only when the marketplace and ERPNext
		# states are proven to match.
		previous = _adopt_baseline_if_equivalent(order, sales_order, snapshot, evidence, erp_state)
	reconciliation_plan = hold_cancel_for_draft_documents(
		plan(previous, snapshot or {}, evidence, erp_state), sales_order.name
	)
	# Single dispatch point between "act on ERPNext" and "advise only".
	if reconciliation_plan["outcome"] in _EXECUTABLE_OUTCOMES:
		return _execute_reconciliation_plan(
			order, sales_order, snapshot, reconciliation_plan, erp_state, trigger_source
		)
	if reconciliation_plan["outcome"] == NO_ACTION and snapshot:
		_advance_baseline(order, previous, snapshot)
	result = _save_reconciliation_advisory(order, reconciliation_plan)
	_create_sync_log(
		order,
		trigger_source,
		"Success" if reconciliation_plan["outcome"] == NO_ACTION else "Invalid",
		snapshot,
		result,
		reconciliation_plan=reconciliation_plan,
		erp_state=erp_state,
	)
	return result


def _adopt_baseline_if_equivalent(order, sales_order, snapshot, evidence, erp_state):
	"""Return a baseline for an order that has none (case 4), or ``None`` when equivalence is not proven.

	The current snapshot is paired with the Sales Order's rows and judged by the planner against
	itself: only NO_ACTION on an active submitted order, with no pending outbound edit, adopts it.
	For Shopify the Sales Order total must also reproduce Shopify's order total (the header the
	planner cannot see).  Anything else leaves the order without a baseline, which the planner
	reports as ``NO_BASELINE`` (Needs Action).  The adopted baseline is saved with the result
	by the caller's save; this function only sets it in memory.
	"""
	if (
		erp_state.get("docstatus") != 1
		or erp_state.get("status") in ("Cancelled", "Closed")
		or evidence.get("outbound_edit_requested")
		or evidence.get("outbound_edit_in_flight")
	):
		return None
	try:
		paired = snapshot_with_sales_order_item_names(snapshot, sales_order, refund_ids=_refund_ids(order))
		_verify_grand_total(sales_order, snapshot, order, _get_account(order))
	except (SalesOrderLinesMismatch, SyncBlocker):
		return None
	if plan(paired, snapshot, evidence, erp_state)["outcome"] != NO_ACTION:
		return None
	order.last_synced_hash = snapshot_hash(snapshot)
	order.last_synced_snapshot = frappe.as_json(paired)
	return paired


def _execute_reconciliation_plan(
	order, sales_order, snapshot, reconciliation_plan, erp_state, trigger_source
):
	"""Apply an executable plan inside a savepoint; persist state only after success.

	Runs in the caller's transaction (the Marketplace Order row is already locked and
	``erp_state`` was read after that lock); never commits.  The stored baseline is kept
	as is for cancel/close (not re-snapshotted); an update advances it.
	"""
	outcome = reconciliation_plan["outcome"]
	frappe.db.savepoint(_RECONCILE_EXECUTE_SAVEPOINT)
	try:
		executed = execute(
			reconciliation_plan,
			sales_order.name,
			_parse_last_synced_snapshot(order),
			snapshot or {},
			marketplace_order=order.name,
		)
		if outcome == UPDATE_SUBMITTED_ORDER:
			# Carry the exact line-to-row names execute() used (no re-pairing).
			order.last_synced_hash = snapshot_hash(snapshot)
			order.last_synced_snapshot = frappe.as_json(
				snapshot_with_line_rows(snapshot, executed["line_rows"], refund_ids=_refund_ids(order))
			)
			release_outbound_edit_marker(order)
		order.last_erpnext_sync_on = now_datetime()
		reasons.apply_synced(order)
		order.flags.skip_sync_status_update = True
		order.save()
		result = {
			"ok": True,
			"status": order.erpnext_sync_status,
			"sales_order": order.erpnext_sales_order,
			"updated": False,
			"error": None,
			"reconciliation": reconciliation_plan,
			"executed": executed,
			"executed_action": outcome,
			"executed_message": _executed_message(outcome, order.erpnext_sales_order),
		}
		if executed.get("reopened"):
			_create_sync_log(
				order,
				trigger_source,
				"Success",
				snapshot,
				{"before_status": "Closed", "reason": reconciliation_plan["message"], "closed_by": "unknown"},
				reconciliation_plan=reconciliation_plan,
				erp_state=erp_state,
				action="Reopen",
			)
		_create_sync_log(
			order,
			trigger_source,
			"Success",
			snapshot,
			result,
			reconciliation_plan=reconciliation_plan,
			erp_state=erp_state,
			action=outcome,
		)
		# Released last, and strictly: a missing savepoint here means the transaction was
		# already rolled back, which must end as a Sync Error, not a Success.
		frappe.db.release_savepoint(_RECONCILE_EXECUTE_SAVEPOINT)
		return result
	except ReconciliationMismatch as exc:
		rolled_back = _rollback_reconcile_savepoint()
		frappe.clear_messages()
		order.reload()
		attention = {
			"outcome": NEEDS_ATTENTION,
			"message": cstr(exc)
			if rolled_back
			else _(
				"The database transaction was already rolled back; verify the Sales Order state in ERPNext."
			),
			"changes": reconciliation_plan["changes"],
			"reason_code": reasons.RECONCILIATION_MISMATCH if rolled_back else reasons.VERIFY_ERP_STATE,
		}
		result = _save_reconciliation_advisory(order, attention)
		_create_sync_log(
			order,
			trigger_source,
			"Invalid",
			snapshot,
			result,
			reconciliation_plan=attention,
			erp_state=erp_state,
		)
		return result
	except Exception as exc:
		rolled_back = _rollback_reconcile_savepoint()
		traceback_text = None
		failure_code = reasons.ERP_VALIDATION_FAILED
		if isinstance(exc, frappe.PermissionError):
			message = _("You do not have permission to change the linked Sales Order in ERPNext.")
			log_status = "Invalid"
			failure_code = reasons.PERMISSION_DENIED
		elif isinstance(exc, frappe.ValidationError):
			message = strip_html(cstr(exc)) or _("ERPNext rejected the Sales Order change.")
			log_status = "Invalid"
		elif is_transient_failure(exc):
			frappe.clear_messages()
			order.reload()
			return _handle_transient_failure(order, trigger_source, snapshot)
		else:
			frappe.log_error(title=_("Marketplace Sales Order reconciliation"))
			traceback_text = frappe.get_traceback()
			message = _("ERPNext could not apply the Sales Order change. Please review it and try again.")
			log_status = "Error"
			failure_code = reasons.SYNC_FAILED
		frappe.clear_messages()
		order.reload()
		if reconciliation_plan.get("reopen"):
			if rolled_back:
				operation = _("cancel") if outcome == CANCEL_ORDER else _("update")
				message = _("Reopen and {0} failed; the Sales Order was left Closed. {1}").format(
					operation, message
				)
			else:
				# The savepoint was already gone: MariaDB rolled the whole transaction back,
				# so the Sales Order is still Closed; the state just needs a human check.
				attention = {
					"outcome": NEEDS_ATTENTION,
					"message": _(
						"The database transaction was already rolled back; verify the Sales Order state in ERPNext."
					),
					"changes": reconciliation_plan["changes"],
					"reason_code": reasons.VERIFY_ERP_STATE,
				}
				result = _save_reconciliation_advisory(order, attention)
				_create_sync_log(
					order,
					trigger_source,
					"Error",
					snapshot,
					result,
					traceback_text=traceback_text,
					reconciliation_plan=attention,
					erp_state=erp_state,
				)
				return result
		if failure_code == reasons.ERP_VALIDATION_FAILED and _changes_rows(reconciliation_plan):
			# ERPNext refused a row it could not add or drop (case 10): the whole order needs a person.
			attention = {
				"outcome": NEEDS_ATTENTION,
				"message": f"{_('ERPNext refused to add or remove a Sales Order row.')} {message}",
				"changes": reconciliation_plan["changes"],
				"reason_code": reasons.ROW_CHANGE_REFUSED,
			}
			result = _save_reconciliation_advisory(order, attention)
			_create_sync_log(
				order,
				trigger_source,
				"Invalid",
				snapshot,
				result,
				reconciliation_plan=attention,
				erp_state=erp_state,
			)
			return result
		result = _save_failure(order, message, failure_code)
		result["reconciliation"] = reconciliation_plan
		_create_sync_log(
			order,
			trigger_source,
			log_status,
			snapshot,
			result,
			traceback_text=traceback_text,
			reconciliation_plan=reconciliation_plan,
			erp_state=erp_state,
		)
		return result


def _changes_rows(reconciliation_plan):
	"""True when the plan adds or removes Sales Order rows (case 10)."""
	return any(
		change.get("kind") in ("item_added", "item_removed") for change in reconciliation_plan["changes"]
	)


def _executed_message(outcome, sales_order_name):
	if outcome == CANCEL_ORDER:
		return _("Sales Order {0} was cancelled").format(sales_order_name)
	if outcome == CLOSE_REMAINDER:
		return _("Sales Order {0} was closed").format(sales_order_name)
	return _("Sales Order {0} was updated").format(sales_order_name)


def _rollback_reconcile_savepoint():
	"""Roll back to the savepoint; False when it was already gone (nothing was undone)."""
	try:
		frappe.db.rollback(save_point=_RECONCILE_EXECUTE_SAVEPOINT)
	except Exception as exc:
		# The savepoint can already be gone (e.g. MariaDB rolled the whole transaction
		# back on a deadlock); anything else is a real error.
		if "SAVEPOINT" not in cstr(exc).upper():
			raise
		return False
	return True


def _create_sync_log(
	order,
	trigger_source,
	status,
	snapshot,
	response,
	traceback_text=None,
	reconciliation_plan=None,
	erp_state=None,
	action=None,
):
	"""Write one ``Marketplace Sync Log`` row for this attempt (#131).

	``ignore_permissions=True``: this doctype's create permission is
	restricted to System Manager, but ``sync_sales_order()`` itself runs
	under whatever session actually triggered it (the manual button's user
	today; a webhook/background job with no interactive user for #132-#134) -
	none of those callers should be blocked from recording an audit trail of
	their own attempt merely because they aren't System Manager.

	``request_data`` is the built snapshot when one was computed before the
	failure (or on success); a blocker raised before any snapshot exists
	(e.g. the platform gate, or missing account/configuration) logs just the
	Marketplace Order name instead - there is nothing else to capture yet.
	"""
	now = now_datetime()
	request_data = snapshot if snapshot is not None else {"marketplace_order": order.name}
	if reconciliation_plan:
		request_data = {
			"snapshot": request_data,
			"reconciliation": {
				"action": reconciliation_plan["outcome"],
				"changes": reconciliation_plan["changes"],
				"erp_state": erp_state,
			},
		}
		if action:
			request_data["action"] = action
	frappe.get_doc(
		{
			"doctype": "Marketplace Sync Log",
			"marketplace_order": order.name,
			"status": status,
			"trigger_source": trigger_source,
			"attempted_at": now,
			"completed_at": now,
			"request_data": json.dumps(request_data, ensure_ascii=False, default=str),
			"response_data": json.dumps(response, ensure_ascii=False, default=str),
			"traceback": traceback_text,
		}
	).insert(ignore_permissions=True)


def _save_success(order, current_hash, sales_order, updated, snapshot):
	# Pair first: a mismatch must raise before any link/baseline field is set, so
	# the order is never left half-linked in memory.
	persisted_snapshot = snapshot_with_sales_order_item_names(
		snapshot, sales_order, refund_ids=_refund_ids(order)
	)
	order.erpnext_sales_order = sales_order.name
	order.last_synced_hash = current_hash
	order.last_synced_snapshot = frappe.as_json(persisted_snapshot)
	order.last_erpnext_sync_on = now_datetime()
	reasons.apply_synced(order)
	order.flags.skip_sync_status_update = True
	order.save()
	return {"ok": True, "sales_order": sales_order.name, "updated": updated}


def _parse_last_synced_snapshot(order):
	if not order.last_synced_snapshot:
		return None
	try:
		parsed = frappe.parse_json(order.last_synced_snapshot)
		return parsed if isinstance(parsed, dict) else None
	except (TypeError, ValueError):
		return None


def _save_reconciliation_advisory(order, reconciliation_plan):
	_apply_reconciliation_status(order, reconciliation_plan)
	message = order.last_sync_error
	order.flags.skip_sync_status_update = True
	order.save()
	return {
		"ok": reconciliation_plan["outcome"] == NO_ACTION,
		"status": order.erpnext_sync_status,
		"sales_order": order.erpnext_sales_order,
		"updated": False,
		"error": None if reconciliation_plan["outcome"] == NO_ACTION else message,
		"reconciliation": reconciliation_plan,
	}


def _save_failure(order, message, reason_code=reasons.SYNC_FAILED):
	reasons.apply_sync_state(order, reason_code, message)
	order.flags.skip_sync_status_update = True
	order.save()
	return {"ok": False, "status": order.erpnext_sync_status, "error": order.last_sync_error}


def confirm_goods_received(marketplace_order):
	"""Record that the returned goods were received and prepare the draft return (Group C, #364).

	Reached only through ``api/marketplace_orders.py::confirm_goods_received``, which checks the
	role (Stock Manager / System Manager) on the server.  Only an order waiting for receipt
	(Needs Action with a receipt-gated reason) qualifies.  A marketplace or Bosta status is never
	treated as receipt: this explicit call is the only evidence.

	Non-Noon platforms get ONE draft return Delivery Note made with ERPNext's own
	``make_sales_return`` from the one submitted Delivery Note of the Sales Order (full return;
	staff edit the quantities before submitting).  Noon reuses its existing Noon Return flow:
	the call records the receipt acknowledgement on the linked Noon Return and links it; its
	inspection step creates the draft Sales Return.  Nothing is submitted.  Idempotent: a second
	call, or an existing non-cancelled return for the same Delivery Note, creates no second draft.
	The order row is locked first, so two clicks cannot both create a draft.
	"""
	frappe.db.get_value("Marketplace Order", marketplace_order, "name", for_update=True)
	order = frappe.get_doc("Marketplace Order", marketplace_order)
	if order.erpnext_sync_status != sync_status.NEEDS_ACTION or (
		order.sync_reason_code not in reasons.RECEIPT_GATED_REASONS
	):
		frappe.throw(_("This order is not waiting for goods to be received."))
	if not order.erpnext_sales_order:
		frappe.throw(_("This order has no linked Sales Order."))

	if order.platform == "Noon":
		prepared_doctype, prepared_document, state, message = _confirm_noon_return_receipt(order)
	else:
		prepared_doctype, prepared_document, state, message = _prepare_delivery_note_return(order)

	if not order.receipt_confirmed_on:
		order.receipt_confirmed_by = frappe.session.user
		order.receipt_confirmed_on = now_datetime()
	order.sync_prepared_doctype = prepared_doctype
	order.sync_prepared_document = prepared_document
	order.sync_action_state = state
	order.last_sync_error = message
	order.flags.skip_sync_status_update = True
	order.save()
	return {
		"ok": True,
		"prepared_doctype": prepared_doctype,
		"prepared_document": prepared_document,
		"sync_action_state": state,
		"message": message,
	}


def _existing_prepared_document(order, doctype):
	"""The order's prepared document while it is draft or submitted; a cancelled or deleted one is replaced."""
	if order.sync_prepared_doctype != doctype or not order.sync_prepared_document:
		return None
	docstatus = frappe.db.get_value(doctype, order.sync_prepared_document, "docstatus")
	return order.sync_prepared_document if docstatus in (0, 1) else None


def _prepare_delivery_note_return(order):
	from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

	existing = _existing_prepared_document(order, "Delivery Note")
	if existing:
		return "Delivery Note", existing, sync_status.DRAFT_PREPARED, _prepared_message(existing)
	frappe.has_permission("Delivery Note", "create", throw=True)
	delivery_notes = read_erp_state(frappe.get_doc("Sales Order", order.erpnext_sales_order)).get(
		"linked_documents", {}
	).get("Delivery Note") or []
	if not delivery_notes:
		frappe.throw(
			_("Sales Order {0} has no submitted Delivery Note, so there is nothing to return.").format(
				order.erpnext_sales_order
			)
		)
	if len(delivery_notes) > 1:
		frappe.throw(
			_(
				"Sales Order {0} has several Delivery Notes ({1}); create the Sales Return manually from the one the goods came back for."
			).format(order.erpnext_sales_order, ", ".join(delivery_notes))
		)
	source = delivery_notes[0]
	# A return someone already made for this Delivery Note counts: never create a second one.
	already = frappe.get_all(
		"Delivery Note",
		filters={"return_against": source, "docstatus": ["in", [0, 1]]},
		pluck="name",
		order_by="creation asc",
		limit=1,
	)
	if already:
		return "Delivery Note", already[0], sync_status.DRAFT_PREPARED, _prepared_message(already[0])
	draft = make_sales_return(source)
	draft.insert()
	return "Delivery Note", draft.name, sync_status.DRAFT_PREPARED, _prepared_message(draft.name)


def _prepared_message(document):
	return _(
		"Draft return {0} was prepared. Check the goods and quantities, then submit it. Nothing was submitted automatically."
	).format(document)


def _confirm_noon_return_receipt(order):
	"""Noon: acknowledge the linked Noon Return's receipt (its own flow) and link it."""
	returns = frappe.get_all(
		"Noon Return",
		filters={"marketplace_order": order.name},
		fields=["name", "receipt_acknowledged_on", "sales_return"],
		order_by="creation asc",
	)
	if not returns:
		frappe.throw(
			_("No Noon Return is linked to this order yet. Look up the return on the order first.")
		)
	for row in returns:
		if not row.receipt_acknowledged_on:
			frappe.get_doc("Noon Return", row.name).acknowledge_receipt(
				_("Confirmed from Marketplace Order {0}").format(order.name)
			)
	target = returns[0]
	if target.sales_return:
		return "Noon Return", target.name, sync_status.DRAFT_PREPARED, _prepared_message(target.sales_return)
	return (
		"Noon Return",
		target.name,
		sync_status.WAITING_FOR_USER,
		_("Receipt was recorded on Noon Return {0}. Record its inspection to prepare the draft Sales Return.").format(
			target.name
		),
	)


def _line_identifier(row):
	return cstr(row.external_sku).strip() or _("no SKU")


def _lead_days(account):
	"""The account's generic Delivery Lead Days (Shopify and Talabat), never negative."""
	return max(cint(account.get("delivery_lead_days")), 0)


def _stored_snapshot_of(order):
	"""The last synced snapshot of a LINKED order, else ``{}``."""
	return (_parse_last_synced_snapshot(order) or {}) if order.get("erpnext_sales_order") else {}


def _stored_line_delivery_dates(order):
	"""``{external_item_id: "YYYY-MM-DD"}`` of the lines the linked order was synced with ({} if none stored)."""
	return {
		line.get("external_item_id"): line["delivery_date"]
		for line in _stored_snapshot_of(order).get("items") or []
		if isinstance(line, dict) and line.get("delivery_date")
	}


def _noon_payload_dates(order):
	from marketplace_integrations.integrations.noon.fulfillment import estimated_shipping_dates_by_sku

	return estimated_shipping_dates_by_sku(order)


def _noon_line_delivery_date(order, payload_dates, sku):
	"""A Noon line's delivery date: its SKU's earliest `estimated_shipping_date`, else the received date.

	Never earlier than the received date (ERPNext rejects a delivery date before the order date). Never raises.
	"""
	received = getdate(order.received_on) if order.received_on else None
	date = payload_dates.get(sku)
	if date and received and date < received:
		date = received
	return date or received


def _delivery_date(order, account, lines=None):
	# A linked order keeps the delivery date it was synced with, so changing the account's lead
	# days (or a platform date) never makes already-synced orders look modified.
	stored = _stored_snapshot_of(order).get("delivery_date")
	if stored:
		return getdate(stored)
	if order.platform == "Shopify":
		if not order.processed_on:
			raise SyncBlocker(
				_("Shopify Processed On is required to set the ERPNext Sales Order Delivery Date.")
			)
		# Shopify sends no promised date; the account's lead days stand in for it.
		return add_days(getdate(order.processed_on), _lead_days(account))
	if order.platform == "Noon":
		# The date comes only from Noon: the header is the earliest line date (each line carries its
		# SKU's `estimated_shipping_date`); no usable date falls back to received_on.
		if not order.received_on:
			raise SyncBlocker(_("Noon Received On is required to set the ERPNext Sales Order Delivery Date."))
		line_dates = [getdate(line["delivery_date"]) for line in lines or [] if line.get("delivery_date")]
		return min(line_dates) if line_dates else getdate(order.received_on)
	if not order.promised_for:
		raise SyncBlocker(_("Talabat Promised For is required to set the ERPNext Sales Order Delivery Date."))
	return add_days(getdate(order.promised_for), _lead_days(account))


def _transaction_date(order):
	if order.platform == "Shopify":
		if not order.processed_on:
			raise SyncBlocker(
				_("Shopify Processed On is required to set the ERPNext Sales Order Transaction Date.")
			)
		return getdate(order.processed_on)
	if order.platform == "Noon":
		if not order.received_on:
			raise SyncBlocker(
				_("Noon Received On is required to set the ERPNext Sales Order Transaction Date.")
			)
		return getdate(order.received_on)
	# received_on (Talabat order.sys.created_at) is not in openapi.json's
	# required list for order.OrderSys, and the Marketplace Order field
	# itself is read-only/non-mandatory - so this is not formally guaranteed
	# to be set. In practice Talabat's own system stamps every order with a
	# creation timestamp; this guard exists as the fail-closed, actionable
	# behavior for the (currently unobserved) case where it is ever blank,
	# rather than silently defaulting the Sales Order's Transaction Date to
	# something else (#85 review finding #11).
	if not order.received_on:
		raise SyncBlocker(
			_("Talabat Received On is required to set the ERPNext Sales Order Transaction Date.")
		)
	return getdate(order.received_on)
