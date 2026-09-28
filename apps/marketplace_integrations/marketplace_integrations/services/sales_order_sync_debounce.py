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
  ``Marketplace Order.sync_debounce_until`` to now + ``SYNC_DEBOUNCE_SECONDS``,
  always pushing the window forward. No enqueue happens here - a caller (e.g.
  a webhook handler) may call this many times in quick succession for the same
  order without ever enqueueing more than the one eventual sync that follows.
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
  value once it's due. A match consumes the window (cleared to ``None``) and
  calls ``sync_sales_order()``.

Per-order RQ dedup job id (``{_SYNC_RQ_JOB_ID_PREFIX}-{order}``) means a sweep
tick for an order whose previous debounced-sync job is still
queued/running is a no-op (RQ's own ``deduplicate=True``), not a pile-up -
matching ``enqueue_account_sync()``'s own dedup guarantee. No per-account or
global concurrency cap - debounce is strictly per Marketplace Order.
"""

import frappe
from frappe.utils import add_to_date, now_datetime

from marketplace_integrations.services.sales_order_sync import sync_sales_order

ORDER_DOCTYPE = "Marketplace Order"

# Kept easy to tune - see the module docstring for why actual latency is this
# plus up to ~1 sweep interval, not an exact figure.
SYNC_DEBOUNCE_SECONDS = 45

_SYNC_RQ_JOB_ID_PREFIX = "marketplace-sync"


def bump_sync_debounce(marketplace_order, delay_seconds=SYNC_DEBOUNCE_SECONDS):
	"""Push ``marketplace_order``'s sync debounce window forward to
	``now + delay_seconds``. Always pushes forward, even if a window is
	already pending - never enqueues anything itself."""
	frappe.db.set_value(
		ORDER_DOCTYPE,
		marketplace_order,
		"sync_debounce_until",
		add_to_date(now_datetime(), seconds=delay_seconds),
		update_modified=False,
	)


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
	``sync_debounce_until``: a mismatch against ``expected_debounce_until``
	(read by the sweep tick that enqueued this job) means a newer
	``bump_sync_debounce()`` call has since pushed the window further out, so
	this no-ops - the next sweep tick will pick up the new value once it's
	due. A match consumes the window (cleared to ``None``, so a later sweep
	tick doesn't re-fire for the same debounce) and runs the real sync.
	"""
	current_debounce_until = frappe.db.get_value(ORDER_DOCTYPE, marketplace_order, "sync_debounce_until")
	if current_debounce_until != expected_debounce_until:
		return
	frappe.db.set_value(
		ORDER_DOCTYPE,
		marketplace_order,
		"sync_debounce_until",
		None,
		update_modified=False,
	)
	sync_sales_order(marketplace_order)
