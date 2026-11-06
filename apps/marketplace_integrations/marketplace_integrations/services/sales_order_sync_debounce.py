# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Per-Marketplace-Order debounce + sweep for ``sync_sales_order()`` (#132).

Depends on #131: the function this schedules is the now-auto-submitting
``sync_sales_order()`` (``services/sales_order_sync.py``). This module owns
only WHEN it gets called automatically - neither webhook path (sub-issues
4/5) enqueues a sync job directly; both call ``bump_sync_debounce()`` instead.

Implementation reality: stock Frappe/RQ has no delayed-enqueue primitive, so
this is a hybrid debounce + short-interval cron sweep, modeled directly on
``integrations/shopify/inventory_sync.py``'s ``sweep_changed_bins()`` +
``enqueue_account_sync()``'s ``job_id``/``deduplicate=True`` dedup pattern -
not a true RQ delayed job (out of scope; don't attempt to build one from
scratch).

- ``bump_sync_debounce()``: a cheap ``frappe.db.set_value()`` write that sets
  both ``Marketplace Order.sync_debounce_until`` to now + ``SYNC_DEBOUNCE_SECONDS``
  and ``Marketplace Order.pending_sync_trigger_source`` to the caller-supplied
  ``trigger_source`` (#135), always pushing the window forward and overwriting
  whichever trigger source was previously pending. No enqueue happens here - a
  caller (e.g. a webhook handler) may call this many times in quick succession
  for the same order without ever enqueueing more than the one eventual sync
  that follows; if two different webhooks bump the same order within one
  window, the LATEST call's trigger_source simply wins - no merge logic.
- ``sweep_due_sales_order_syncs()``: scheduled every minute
  (``hooks.py``'s ``scheduler_events["cron"]``, ``"* * * * *"`` - tighter than
  a 1-minute cron bucket doesn't exist, so actual latency is
  ``SYNC_DEBOUNCE_SECONDS`` plus up to ~1 sweep interval; this is a documented
  approximation, not an exact 45s guarantee). Finds every Marketplace Order
  whose debounce window is due and enqueues one deduplicated
  ``_run_debounced_sync()`` job per order, carrying the exact
  ``sync_debounce_until`` value read at sweep time.
- ``_run_debounced_sync()``: re-reads the order's CURRENT
  ``sync_debounce_until`` at execution time. A mismatch against the value the
  sweep read means a newer bump pushed the window further out after the sweep
  tick already read it - that job no-ops; the next sweep tick picks up the new
  value once it's due. A match consumes the window (both
  ``sync_debounce_until`` and ``pending_sync_trigger_source`` cleared to
  ``None`` together) and calls ``sync_sales_order()`` with the
  ``pending_sync_trigger_source`` value just read, so the ``Marketplace Sync
  Log`` row this produces records the real originating webhook rather than
  falling back to ``sync_sales_order()``'s own "Manual" default (#135).

Per-order RQ dedup job id (``{_SYNC_RQ_JOB_ID_PREFIX}-{order}``) means a sweep
tick for an order whose previous debounced-sync job is still
queued/running is a no-op (RQ's own ``deduplicate=True``), not a pile-up -
matching ``enqueue_account_sync()``'s own dedup guarantee. No per-account or
global concurrency cap - debounce is strictly per Marketplace Order.
"""

import hashlib
import json

import frappe
from frappe.utils import add_to_date, cstr, flt, get_datetime, getdate, now_datetime

from marketplace_integrations.integrations.shopify.order_reconciliation import refund_ids
from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)
from marketplace_integrations.services.sales_order_sync import _shopify_transactions, sync_sales_order
from marketplace_integrations.services.shopify_payments import (
	ELIGIBLE_KINDS,
	ELIGIBLE_STATUS,
	manual_payment_totals,
)
from marketplace_integrations.services.sync_status import CANCELLED, NEEDS_ACTION, SYNC_ERROR, SYNCED, SYNCING

ORDER_DOCTYPE = "Marketplace Order"

# Kept easy to tune - see the module docstring for why actual latency is this
# plus up to ~1 sweep interval, not an exact figure.
SYNC_DEBOUNCE_SECONDS = 45

# Transient-failure retries (#364): timeouts, lock/deadlock errors and HTTP 5xx/429 are retried
# after 1, 5 and 15 minutes (``sync_retry_count`` counts them, the retry itself rides on
# ``sync_debounce_until`` so the minute sweep and its ``job_id`` dedup run it).  After the last
# one the order becomes Sync Error with SYNC_RETRIES_EXHAUSTED.  Terminal failures (missing
# mapping or price, validation errors) are never retried.
TRANSIENT_RETRY_DELAYS_SECONDS = (60, 300, 900)

_SYNC_RQ_JOB_ID_PREFIX = "marketplace-sync"

# Shopify payment mirroring failures (Payment Sync Log status Error, e.g. a deadlock) are retried by
# the minute sweep, at most this many times, only while the failures are this recent (#425).
PAYMENT_RETRY_ATTEMPTS = 3
PAYMENT_RETRY_WINDOW_MINUTES = 60
_PAYMENT_RETRY_RQ_JOB_ID_PREFIX = "marketplace-payment-retry"


def bump_sync_debounce(
	marketplace_order, trigger_source, delay_seconds=SYNC_DEBOUNCE_SECONDS, extra_values=None
):
	"""Push ``marketplace_order``'s sync debounce window forward to
	``now + delay_seconds``, record ``trigger_source`` as the pending sync's
	origin (#135) and set the order to Syncing in the same write (#364; an order
	cancelled before any Sales Order exists stays Cancelled).
	Always pushes forward, even if a window is already pending -
	never enqueues anything itself. If a window is already pending when this
	is called again (e.g. two webhooks touching the same order in quick
	succession), the LATEST call's ``trigger_source`` simply overwrites the
	previous one - no merge logic. ``extra_values`` are written in the same
	``set_value`` call (one atomic write), e.g. the sweep's check time."""
	values = {
		**(extra_values or {}),
		"sync_debounce_until": add_to_date(now_datetime(), seconds=delay_seconds),
		"pending_sync_trigger_source": trigger_source,
	}
	if frappe.db.get_value(ORDER_DOCTYPE, marketplace_order, "erpnext_sync_status") != CANCELLED:
		values["erpnext_sync_status"] = SYNCING
	frappe.db.set_value(ORDER_DOCTYPE, marketplace_order, values, update_modified=False)


def bump_sync_debounce_unless_pending(order_row, trigger_source):
	"""Like ``bump_sync_debounce()``, but leave an order alone when a window set by the
	same ``trigger_source`` is still pending, so a re-run does not push it forward.
	``order_row`` needs ``name``, ``sync_debounce_until`` and ``pending_sync_trigger_source``.
	Returns True when the window was bumped."""
	if (
		order_row.pending_sync_trigger_source == trigger_source
		and order_row.sync_debounce_until
		and get_datetime(order_row.sync_debounce_until) > now_datetime()
	):
		return False
	bump_sync_debounce(order_row.name, trigger_source)
	return True


_FINGERPRINT_ITEM_FIELDS = (
	"external_item_id",
	"external_sku",
	"external_status",
	"integration_status",
	"fulfilled_quantity",
	"fulfilled_total_price",
	"current_quantity",
	"unfulfilled_quantity",
	"non_fulfillable_quantity",
	"transaction_amount",
	# Snapshot blockers/amounts read these (sales_order_sync.py line builders).
	"pricing_type",
	"fulfilled_unit_price",
	"fulfilled_total_price_missing",
	"transaction_amount_missing",
)


def _fingerprint_value(row, field):
	if field.endswith(("quantity", "price", "amount")):
		return flt(row.get(field), 6)
	if field.endswith("_missing"):
		return int(bool(row.get(field)))
	return cstr(row.get(field))


def order_evidence_fingerprint(order):
	"""Pure: a hash of what could change a linked order's Sales Order snapshot or its
	reconciliation evidence (#178), so an import can tell a real change from a refresh.

	Covers line ids/SKUs/quantities/amounts/statuses and Shopify fulfillment
	quantities, successful SALE/CAPTURE transaction ids, whether the order is
	cancelled (the timestamp itself is not hashed), refund ids, Talabat/Shopify
	order statuses, and (Noon) the units its Delivery Notes started.
	Also the snapshot's transaction/delivery date sources (``processed_on``,
	``received_on``, ``promised_for``, as dates), ``presentment_currency`` and the
	per-line ``*_missing`` flags, since the snapshot builder reads all of them.
	``shipping_lines`` is deliberately NOT hashed: a linked order's delivery date is frozen in its
	stored snapshot, and unlinked orders read the latest stored row.
	Deliberately excludes volatile fields (``last_platform_update_on``,
	``latest_payload`` timestamps, ``modified``), and, for Noon, the zero-quantity
	stale status-group rows the importer zeroes instead of deleting.
	"""
	is_noon = order.platform == "Noon"
	items = []
	for row in order.get("items") or []:
		if is_noon and flt(row.get("current_quantity")) <= 0:
			continue
		items.append(
			[_fingerprint_value(row, field) for field in _FINGERPRINT_ITEM_FIELDS]
			+ [int(bool(row.get("fulfillment_quantities_known")))]
		)
	payload = {
		"platform": cstr(order.platform),
		"external_status": cstr(order.external_status),
		"financial_status": cstr(order.external_financial_status),
		"post_picked_up": int(bool(order.post_picked_up)),
		"cancelled": bool(order.cancelled_at),
		"dates": [
			cstr(getdate(order.get(field))) if order.get(field) else ""
			for field in ("processed_on", "received_on", "promised_for")
		],
		"presentment_currency": cstr(order.get("presentment_currency")),
		"refund_ids": sorted(refund_ids(order)),
		"successful_payment_transactions": _successful_shopify_payment_details(order),
		"items": sorted(items),
		"started_units": _noon_started_units(order) if is_noon else [],
	}
	return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _noon_started_units(order):
	"""Sorted units of the live Noon Delivery Notes of the order (the old shipment child tables are gone)."""
	if not order.get("name"):
		return []
	from marketplace_integrations.integrations.noon.delivery_note_shipment import started_units_of_order

	return sorted(started_units_of_order(order.name))


def _successful_shopify_payment_details(order):
	"""Sorted relevant details of successful SALE/CAPTURE transactions (#231)."""
	if order.platform != "Shopify":
		return []
	transactions = order.get("payment_transactions")
	if isinstance(transactions, str):
		try:
			transactions = json.loads(transactions)
		except (TypeError, ValueError):
			return []
	if isinstance(transactions, dict):
		transactions = transactions.get("nodes") or []
	if not isinstance(transactions, list):
		return []
	transactions_by_id = {}
	for txn in transactions:
		if (
			not isinstance(txn, dict)
			or txn.get("kind") not in {"SALE", "CAPTURE"}
			or txn.get("status") != "SUCCESS"
		):
			continue
		transaction_id = cstr(txn.get("id")).strip()
		if transaction_id:
			money = (txn.get("amountSet") or {}).get("shopMoney") or {}
			transactions_by_id[transaction_id] = [
				transaction_id,
				# None and an empty gateway are distinct source values; preserve both.
				txn.get("gateway"),
				money.get("amount"),
				money.get("currencyCode"),
				txn.get("processedAt"),
				txn.get("createdAt"),
			]
	return [transactions_by_id[key] for key in sorted(transactions_by_id)]


def linked_order_fingerprint(marketplace_account, external_order_id):
	"""Fingerprint of an existing Marketplace Order that already has a linked Sales
	Order, else ``None`` (no order yet, or not linked: nothing for a re-sync to reconcile).
	"""
	name = frappe.db.get_value(
		"Marketplace Order",
		{"order_identity_key": compute_order_identity_key(marketplace_account, external_order_id)},
		"name",
	)
	if not name or not frappe.db.get_value("Marketplace Order", name, "erpnext_sales_order"):
		return None
	# Same builder as the "after" side (``bump_if_linked_order_changed()``), so an
	# unchanged or replayed import hashes identically.  ``frappe.get_doc(doctype, name)``
	# does not validate links on load, and it reads child rows directly (no
	# permission-checked ``get_list``), so this also works for a non-Administrator
	# caller such as a manual import run as the queuing user.
	return order_evidence_fingerprint(frappe.get_doc(ORDER_DOCTYPE, name))


def existing_order_fingerprint(marketplace_account, external_order_id):
	"""Fingerprint of an existing Marketplace Order, linked or not, else ``None`` (a new order).

	Unlike ``linked_order_fingerprint()`` this does not require a Sales Order: the Talabat
	webhook uses it to bump only a new order or one whose evidence really changed (#364).
	"""
	name = frappe.db.get_value(
		ORDER_DOCTYPE,
		{"order_identity_key": compute_order_identity_key(marketplace_account, external_order_id)},
		"name",
	)
	if not name:
		return None
	return order_evidence_fingerprint(frappe.get_doc(ORDER_DOCTYPE, name))


def bump_if_linked_order_changed(order_name, before_fingerprint, trigger_source):
	"""Bump the sync debounce when an import changed a linked order (#178).

	``before_fingerprint`` comes from ``linked_order_fingerprint()`` taken before the
	import; ``None`` (new or unlinked order) never bumps here.  Same transaction as the
	import: ``bump_sync_debounce()`` is a plain ``frappe.db.set_value()``.
	"""
	if before_fingerprint is None:
		return False
	after = order_evidence_fingerprint(frappe.get_doc(ORDER_DOCTYPE, order_name))
	if after == before_fingerprint:
		return False
	bump_sync_debounce(order_name, trigger_source)
	return True


def sweep_due_sales_order_syncs():
	"""Scheduled cron sweep (``"* * * * *"``, see ``hooks.py``'s
	``scheduler_events["cron"]``): find every Marketplace Order whose
	``sync_debounce_until`` is set and due (``<= now``), and enqueue one
	deduplicated ``_run_debounced_sync()`` job per order.

	Cheap by construction: one bulk read of due orders, then per-order
	enqueues (themselves just RQ dedup checks) - no Sales Order work happens
	here or in anything this function calls directly.

	The explicit ``["sync_debounce_until", "is", "set"]`` filter matters, not
	just style: Frappe's own filter builder (``db_query.py``) treats a bare
	``<=`` comparison against a Datetime field as "NULL coalesces to the
	earliest possible date" (so an index can still be used), which makes a
	NULL ``sync_debounce_until`` (the normal state for an order with nothing
	pending) match a lone ``<=`` filter as though it were always due. Without
	the "is set" filter, every Marketplace Order in the system - not just
	ones with a real pending debounce - would be swept and enqueued on every
	tick.
	"""
	due_orders = frappe.get_all(
		ORDER_DOCTYPE,
		filters=[
			["sync_debounce_until", "is", "set"],
			["sync_debounce_until", "<=", now_datetime()],
		],
		fields=["name", "sync_debounce_until"],
	)
	for order in due_orders:
		frappe.enqueue(
			_run_debounced_sync,
			queue="default",
			timeout=300,
			job_id=f"{_SYNC_RQ_JOB_ID_PREFIX}-{order.name}",
			deduplicate=True,
			marketplace_order=order.name,
			expected_debounce_until=order.sync_debounce_until,
		)
	_enqueue_payment_retries()


def _payment_retry_candidates():
	"""``{marketplace_order: [transaction_id, ...]}`` of the Shopify payments worth retrying.

	A transaction qualifies when its trailing run of consecutive Error Payment log rows (counted
	without a time limit, ending at the first non-Error row) starts within the retry window, ends
	with a transient failure and holds no more than the original run plus ``PAYMENT_RETRY_ATTEMPTS``
	retries.  After that the last Error row stays for a person.

	A transaction whose latest Payment row is a duplicate-guard Invalid row also qualifies, with no
	time limit, once the guard no longer blocks (the covering manual entry was cancelled or no
	longer covers; same rule as Needs Attention, from the row's ``guard_sales_order`` and
	``guard_required``) and the transaction is still pending.  The retry then writes a Success or
	a new row, so the row stops being the latest and the candidate state ends.

	Orders whose Sales Order is not submitted (or missing) are dropped.
	"""
	window_start = get_datetime(add_to_date(now_datetime(), minutes=-PAYMENT_RETRY_WINDOW_MINUTES))
	recent = frappe.get_all(
		"Marketplace Sync Log",
		filters={
			"sync_type": "Payment",
			"status": "Error",
			"attempted_at": [">=", window_start],
		},
		fields=["marketplace_order"],
		limit_page_length=0,
	)
	guarded = frappe.get_all(
		"Marketplace Sync Log",
		filters={
			"sync_type": "Payment",
			"status": "Invalid",
			"response_data": ["like", '%"guard_sales_order"%'],
		},
		fields=["marketplace_order"],
		limit_page_length=0,
	)
	orders = sorted({row.marketplace_order for row in [*recent, *guarded] if row.marketplace_order})
	if not orders:
		return {}
	# One query for every Payment row of these orders; newest first, grouped in Python.
	history = {}
	for row in frappe.get_all(
		"Marketplace Sync Log",
		filters={"sync_type": "Payment", "marketplace_order": ["in", orders]},
		fields=["marketplace_order", "request_data", "status", "response_data", "attempted_at", "creation"],
		order_by="attempted_at desc, creation desc",
		limit_page_length=0,
	):
		history.setdefault((row.marketplace_order, row.request_data), []).append(row)
	candidates = {}
	guard_rows = []
	for (order, request_data), rows in history.items():
		if rows[0].status == "Invalid":
			try:
				guard = json.loads(rows[0].response_data or "{}") or {}
				transaction_id = cstr((json.loads(request_data or "{}") or {}).get("transaction_id")).strip()
			except ValueError:
				continue
			if isinstance(guard, dict) and guard.get("guard_sales_order") and transaction_id:
				guard_rows.append(
					(order, transaction_id, guard["guard_sales_order"], guard.get("guard_required"))
				)
			continue
		run = []
		for row in rows:
			if row.status != "Error":
				break
			run.append(row)
		# The original failure plus PAYMENT_RETRY_ATTEMPTS retries.
		if not run or len(run) > PAYMENT_RETRY_ATTEMPTS:
			continue
		if get_datetime(run[-1].attempted_at or run[-1].creation) < window_start:
			continue
		try:
			transient = (json.loads(run[0].response_data or "{}") or {}).get("transient")
			transaction_id = cstr((json.loads(request_data or "{}") or {}).get("transaction_id")).strip()
		except ValueError:
			continue
		if transient and transaction_id:
			candidates.setdefault(order, []).append(transaction_id)
	for order, transaction_id in _unblocked_guard_transactions(guard_rows):
		candidates.setdefault(order, []).append(transaction_id)
	if not candidates:
		return {}
	# Only orders with a submitted Sales Order can record a payment; skip the rest.
	sales_orders = {
		row.name: row.erpnext_sales_order
		for row in frappe.get_all(
			ORDER_DOCTYPE,
			filters={"name": ["in", list(candidates)]},
			fields=["name", "erpnext_sales_order"],
			limit_page_length=0,
		)
	}
	submitted = set(
		frappe.get_all(
			"Sales Order",
			filters={"name": ["in", [name for name in sales_orders.values() if name]], "docstatus": 1},
			pluck="name",
			limit_page_length=0,
		)
	)
	return {
		order: sorted(ids)
		for order, ids in sorted(candidates.items())
		if sales_orders.get(order) in submitted
	}


def _unblocked_guard_transactions(guard_rows):
	"""``[(order, transaction_id)]`` of duplicate-guard rows worth re-running.

	``guard_rows`` are ``(order, transaction_id, guard_sales_order, guard_required)`` of the rows
	that are the latest for their transaction.  A row qualifies when the guard no longer blocks
	(the manual total of its Sales Order is zero or below the required amount, as in Needs
	Attention) and the transaction is still pending: a successful SALE/CAPTURE of the order
	without a Payment Entry.  Without the second test the re-run could neither record a payment
	nor write a row, and would be queued every minute.
	"""
	if not guard_rows:
		return []
	totals = manual_payment_totals({row[2] for row in guard_rows})
	open_rows = []
	for order, transaction_id, sales_order, required in guard_rows:
		total = flt(totals.get(sales_order), 2)
		if total <= 0 or total < flt(required, 2):
			open_rows.append((order, transaction_id))
	if not open_rows:
		return []
	transactions = {
		row.name: _shopify_transactions(row)
		for row in frappe.get_all(
			ORDER_DOCTYPE,
			filters={"name": ["in", sorted({order for order, _id in open_rows})]},
			fields=["name", "payment_transactions"],
			limit_page_length=0,
		)
	}
	recorded = set(
		frappe.get_all(
			"Payment Entry",
			filters={"custom_marketplace_transaction_id": ["in", sorted({tid for _o, tid in open_rows})]},
			pluck="custom_marketplace_transaction_id",
			limit_page_length=0,
		)
	)
	return [
		(order, transaction_id)
		for order, transaction_id in open_rows
		if transaction_id not in recorded
		and any(
			cstr(txn.get("id")).strip() == transaction_id
			and txn.get("kind") in ELIGIBLE_KINDS
			and txn.get("status") == ELIGIBLE_STATUS
			for txn in transactions.get(order, [])
		)
	]


def _enqueue_payment_retries():
	"""Retry transient payment-mirroring failures, one attempt per sweep run (#425).

	Payment mirroring runs separately from the Sales Order sync, so the retry calls only the
	payment step and never touches the order's sync status, debounce or retry counter.
	"""
	for order, transaction_ids in _payment_retry_candidates().items():
		frappe.enqueue(
			_retry_payment_mirroring,
			queue="default",
			timeout=300,
			job_id=f"{_PAYMENT_RETRY_RQ_JOB_ID_PREFIX}-{order}",
			deduplicate=True,
			marketplace_order=order,
			transaction_ids=transaction_ids,
		)


def _retry_payment_mirroring(marketplace_order, transaction_ids=None):
	from marketplace_integrations.services.sales_order_sync import _mirror_shopify_payments_independently

	_mirror_shopify_payments_independently(marketplace_order, "Scheduled Sweep", transaction_ids)


def _run_debounced_sync(marketplace_order, expected_debounce_until):
	"""Debounced-sync job body. Re-reads the order's CURRENT
	``sync_debounce_until`` (and, alongside it, the currently-pending
	``pending_sync_trigger_source``, #135): a mismatch of the debounce
	timestamp against ``expected_debounce_until`` (read by the sweep tick that
	enqueued this job) means a newer ``bump_sync_debounce()`` call has since
	pushed the window further out, so this no-ops - the next sweep tick will
	pick up the new value once it's due. A match consumes the window (both
	fields cleared to ``None`` together, so a later sweep tick doesn't re-fire
	for the same debounce) and runs the real sync, passing the just-read
	trigger source through so the resulting ``Marketplace Sync Log`` row
	records the real originating webhook rather than defaulting to "Manual".
	"""
	current = frappe.db.get_value(
		ORDER_DOCTYPE,
		marketplace_order,
		["sync_debounce_until", "pending_sync_trigger_source"],
		as_dict=True,
	)
	if current.sync_debounce_until != expected_debounce_until:
		return
	frappe.db.set_value(
		ORDER_DOCTYPE,
		marketplace_order,
		{"sync_debounce_until": None, "pending_sync_trigger_source": None},
		update_modified=False,
	)
	try:
		if current.pending_sync_trigger_source:
			sync_sales_order(marketplace_order, trigger_source=current.pending_sync_trigger_source)
		else:
			# Shouldn't happen given bump_sync_debounce() always sets both fields
			# together - defensive fallback to sync_sales_order()'s own "Manual"
			# default rather than crashing.
			sync_sales_order(marketplace_order)
	except Exception:
		# An escaped failure (deadlock / lock wait under a burst, ...) must not drop
		# the order: the window was consumed above, so put it back for the next sweep.
		frappe.db.rollback()
		bump_sync_debounce(marketplace_order, current.pending_sync_trigger_source or "Scheduled Sweep")
		frappe.db.commit()
		raise


# #180 scheduled reconciliation sweep (platform-neutral).
RECONCILIATION_SWEEP_BATCH = 200
# A persistent Attention status (Needs Action / Sync Error, or a Syncing order whose window got
# lost) is re-evaluated at most daily.
ATTENTION_RECHECK_HOURS = 24
# A Closed or Cancelled linked Sales Order whose order is otherwise Synced is re-checked at most every 24 hours (a converged one would otherwise be re-bumped forever).
CLOSED_RECHECK_HOURS = 24
_ATTENTION_STATUSES = (NEEDS_ACTION, SYNC_ERROR, SYNCING)


def _stuck_reconciliation_candidates(limit):
	"""Cheap SQL pre-filter only (no planning, no snapshot rebuild): linked Marketplace
	Orders with no pending debounce that are either in an Attention status or whose
	submitted Sales Order is Closed or Cancelled (#181), and whose back-off window has elapsed;
	orders with an outbound cancel marker are skipped.  Oldest
	check first (never-checked orders first)."""
	order = frappe.qb.DocType(ORDER_DOCTYPE)
	so = frappe.qb.DocType("Sales Order")
	now = now_datetime()
	checked = order.last_reconciliation_check_on
	attention_due = order.erpnext_sync_status.isin(_ATTENTION_STATUSES) & (
		checked.isnull() | (checked <= add_to_date(now, hours=-ATTENTION_RECHECK_HOURS))
	)
	not_attention = order.erpnext_sync_status.notin(_ATTENTION_STATUSES)
	backed_off = checked.isnull() | (checked <= add_to_date(now, hours=-CLOSED_RECHECK_HOURS))
	# Closed with no delivery/billing: #180 reopens or cancels it.
	closed_due = (
		(so.docstatus == 1)
		& (so.status == "Closed")
		& (so.per_delivered == 0)
		& (so.per_billed == 0)
		& not_attention
		& backed_off
	)
	# ERPNext-Cancelled, or Closed after delivery/billing (#181): nothing for ERPNext to do, but
	# the platform order may still be active, which the planner surfaces as Needs Action
	# with the platform instruction (a Close/Reopen has no Sales Order hook here).
	# An order already converged (Synced and checked at least once) is skipped: re-planning it
	# daily would only append a no-change log row.  Needs Action / Sync Error / never
	# checked still qualify.
	converged = (order.erpnext_sync_status == SYNCED) & checked.isnotnull()
	inactive_due = (
		(so.docstatus == 2)
		| ((so.docstatus == 1) & (so.status == "Closed") & ((so.per_delivered > 0) | (so.per_billed > 0)))
	) & (not_attention & backed_off & ~converged)
	linked = order.erpnext_sales_order.isnotnull() & (order.erpnext_sales_order != "")
	# A Shopify order with no Sales Order yet whose webhook-triggered sync never ran or was
	# lost: the reconcile import only bumps linked orders, so nothing else would ever
	# sync it.  Never checked, or an old failure, qualifies; cancelled orders are skipped, except
	# one still Syncing with no window (the outer query requires none): it was never resolved, so
	# the sync must run to settle it as Cancelled / Needs Action.
	unlinked_due = (
		(order.platform == "Shopify")
		& order.erpnext_sync_status.isin((SYNCING, SYNC_ERROR, NEEDS_ACTION))
		& (order.cancelled_at.isnull() | (order.erpnext_sync_status == SYNCING))
		& (checked.isnull() | (checked <= add_to_date(now, hours=-ATTENTION_RECHECK_HOURS)))
	)
	rows = (
		frappe.qb.from_(order)
		.left_join(so)
		.on(so.name == order.erpnext_sales_order)
		.select(order.name)
		.where(order.sync_debounce_until.isnull())
		# Talabat's accepted cancel response is already synchronous. For Shopify,
		# skip only after its cancellation echo is persisted; an accepted async job
		# that has not cancelled the order must still reach Needs Action.
		.where(
			(order.outbound_cancelled_on.isnull())
			| ((order.platform == "Shopify") & order.cancelled_at.isnull())
		)
		.where((linked & (attention_due | closed_due | inactive_due)) | (~linked & unlinked_due))
		.orderby(order.last_reconciliation_check_on)
		.orderby(order.name)
		.limit(limit)
	).run()
	return [row[0] for row in rows]


def sweep_stuck_sales_order_reconciliations():
	"""Hourly (``hooks.py`` ``scheduler_events["hourly"]``): find linked Marketplace Orders
	stuck in Needs Action / Sync Error or whose Sales Order was Closed or Cancelled, and bump
	their debounce with ``Scheduled Sweep``.  The minute job ``sweep_due_sales_order_syncs``
	then runs the single sync path; this function never syncs or plans itself.  Idempotent:
	a bumped order has a pending debounce (skipped) and a fresh check time (backed off)."""
	checked_on = now_datetime()
	for name in _stuck_reconciliation_candidates(RECONCILIATION_SWEEP_BATCH):
		# A webhook may have bumped this order since the candidate query: its sync runs
		# anyway, so leave its trigger source alone rather than overwrite it.
		if frappe.db.get_value(ORDER_DOCTYPE, name, "sync_debounce_until"):
			continue
		# One write: the check time can never advance without the bump.
		bump_sync_debounce(name, "Scheduled Sweep", extra_values={"last_reconciliation_check_on": checked_on})
