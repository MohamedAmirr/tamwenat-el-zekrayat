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
from frappe.utils import add_to_date, cstr, flt, getdate, now_datetime

from marketplace_integrations.integrations.shopify.order_reconciliation import refund_ids
from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)
from marketplace_integrations.services.sales_order_sync import sync_sales_order

ORDER_DOCTYPE = "Marketplace Order"

# Kept easy to tune - see the module docstring for why actual latency is this
# plus up to ~1 sweep interval, not an exact figure.
SYNC_DEBOUNCE_SECONDS = 45

_SYNC_RQ_JOB_ID_PREFIX = "marketplace-sync"


def bump_sync_debounce(marketplace_order, trigger_source, delay_seconds=SYNC_DEBOUNCE_SECONDS):
	"""Push ``marketplace_order``'s sync debounce window forward to
	``now + delay_seconds`` and record ``trigger_source`` as the pending sync's
	origin (#135). Always pushes forward, even if a window is already pending -
	never enqueues anything itself. If a window is already pending when this
	is called again (e.g. two webhooks touching the same order in quick
	succession), the LATEST call's ``trigger_source`` simply overwrites the
	previous one - no merge logic."""
	frappe.db.set_value(
		ORDER_DOCTYPE,
		marketplace_order,
		{
			"sync_debounce_until": add_to_date(now_datetime(), seconds=delay_seconds),
			"pending_sync_trigger_source": trigger_source,
		},
		update_modified=False,
	)


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
	quantities, whether the order is cancelled (the timestamp itself is not hashed),
	refund ids, Talabat/Shopify order statuses, and Noon shipment/unit membership.
	Also the snapshot's transaction/delivery date sources (``processed_on``,
	``received_on``, ``promised_for``, as dates), ``presentment_currency`` and the
	per-line ``*_missing`` flags, since the snapshot builder reads all of them.
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
		"items": sorted(items),
		"shipments": sorted(
			[cstr(row.integration_shipment_nr), cstr(row.status)] for row in order.get("noon_shipments") or []
		),
		"shipment_units": sorted(
			[cstr(row.integration_shipment_nr), cstr(row.mp_item_nr)]
			for row in order.get("noon_shipment_units") or []
		),
	}
	return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


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
	if current.pending_sync_trigger_source:
		sync_sales_order(marketplace_order, trigger_source=current.pending_sync_trigger_source)
	else:
		# Shouldn't happen given bump_sync_debounce() always sets both fields
		# together - defensive fallback to sync_sales_order()'s own "Manual"
		# default rather than crashing.
		sync_sales_order(marketplace_order)


# #180 scheduled reconciliation sweep (platform-neutral).
RECONCILIATION_SWEEP_BATCH = 200
# A persistent Attention status (Modified After Sync / Sync Error) is re-evaluated at most daily.
ATTENTION_RECHECK_HOURS = 24
# A Closed linked Sales Order whose order is otherwise Synced is re-checked at most every 24 hours (a converged Closed order would otherwise be re-bumped forever).
CLOSED_RECHECK_HOURS = 24
_ATTENTION_STATUSES = ("Modified After Sync", "Sync Error")


def _stuck_reconciliation_candidates(limit):
	"""Cheap SQL pre-filter only (no planning, no snapshot rebuild): linked Marketplace
	Orders with no pending debounce that are either in an Attention status or whose
	submitted Sales Order is Closed, and whose back-off window has elapsed.  Oldest
	check first (never-checked orders first)."""
	order = frappe.qb.DocType(ORDER_DOCTYPE)
	so = frappe.qb.DocType("Sales Order")
	now = now_datetime()
	checked = order.last_reconciliation_check_on
	attention_due = order.erpnext_sync_status.isin(_ATTENTION_STATUSES) & (
		checked.isnull() | (checked <= add_to_date(now, hours=-ATTENTION_RECHECK_HOURS))
	)
	closed_due = (
		(so.docstatus == 1)
		& (so.status == "Closed")
		& order.erpnext_sync_status.notin(_ATTENTION_STATUSES)
		& (checked.isnull() | (checked <= add_to_date(now, hours=-CLOSED_RECHECK_HOURS)))
	)
	rows = (
		frappe.qb.from_(order)
		.left_join(so)
		.on(so.name == order.erpnext_sales_order)
		.select(order.name)
		.where(order.erpnext_sales_order.isnotnull() & (order.erpnext_sales_order != ""))
		.where(order.sync_debounce_until.isnull())
		.where(attention_due | closed_due)
		.orderby(order.last_reconciliation_check_on)
		.orderby(order.name)
		.limit(limit)
	).run()
	return [row[0] for row in rows]


def sweep_stuck_sales_order_reconciliations():
	"""Hourly (``hooks.py`` ``scheduler_events["hourly"]``): find linked Marketplace Orders
	stuck in Modified After Sync / Sync Error or whose Sales Order was Closed, and bump
	their debounce with ``Scheduled Sweep``.  The minute job ``sweep_due_sales_order_syncs``
	then runs the single sync path; this function never syncs or plans itself.  Idempotent:
	a bumped order has a pending debounce (skipped) and a fresh check time (backed off)."""
	checked_on = now_datetime()
	for name in _stuck_reconciliation_candidates(RECONCILIATION_SWEEP_BATCH):
		bump_sync_debounce(name, "Scheduled Sweep")
		frappe.db.set_value(
			ORDER_DOCTYPE, name, "last_reconciliation_check_on", checked_on, update_modified=False
		)
