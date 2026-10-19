# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon FBPI shipment fulfillment (#146 section C).

Every request-path function here follows the same shape: lock the
Marketplace Order row (``frappe.db.get_value(..., for_update=True)``), reload
it, validate and write whatever Pending state the action needs, save through
the parent document (bumping ``modified`` so ``check_if_latest()`` also
guards concurrent Desk edits), commit, then enqueue the actual Noon HTTP call
on the ``long`` queue (#146 decision 10/20) - no Noon HTTP call ever happens
inside a Desk request.

Every worker (``_run_*``) re-acquires the same row lock before it writes the
outcome back, for the identical reason ``integrations/noon/orders.py``'s
``import_order()`` does (see that module's docstring): without it, a Noon
poll running concurrently could load a stale in-memory copy of this order
and silently delete the very shipment/unit/AWB rows this module just wrote,
via ``Document.save()``'s ``update_children()``.

**#146 round-3 review finding #3 (lock duration):** a worker that calls
several Noon HTTP endpoints in sequence (``run_create_shipment()``,
``run_mark_out_of_stock()``, ``run_add_awb()``, ``run_upload_invoice()``)
does NOT hold the order row lock across any of those HTTP calls - each
holds the lock only long enough to read what it needs or write a result,
committing (which releases the lock) immediately before every Noon call and
re-acquiring it immediately after that call returns. Holding the lock for
the worst-case ~4-call sequence (up to ~40s) would otherwise block the
hourly poll, Desk saves, and any other fulfillment action on the same order,
risking ``QueryTimeoutError`` against the 50s ``innodb_lock_wait_timeout``.
Every re-acquisition re-checks the shipment's status against the status
read immediately before the call that just returned; any change (not just a
cancel) aborts that worker and logs it, since a concurrent writer could only
have changed it by committing its own write while this worker's lock was
released. ``run_mark_out_of_stock()`` now follows this same shape for BOTH of
its Noon calls, including the post-success ``GetFbpiOrder`` refresh (round-5
fix, review finding #5 - that refresh used to run with the lock still held).

**Round-5 fix (review finding #1):** every lock acquisition in this module
goes through ``_lock_and_load_order()``, which now commits immediately before
its own ``FOR UPDATE`` lock (see that function's docstring) - so none of the
``frappe.db.commit()`` calls scattered through this module that precede a
Noon HTTP call need to "reach far enough forward" on their own; the next
lock always re-establishes a fresh read view no matter what read happened
between that commit and the lock (a pagination/auth read inside the HTTP
call included). The one exception, left deliberately as-is per the review:
``import_order()``'s own raw-SQL lock (in ``integrations/noon/orders.py``) is
held across its internal ``_maybe_fetch_customer_data()`` HTTP call as a side
effect of that module's own lock-first fix - worst case ~30s, under the 50s
``innodb_lock_wait_timeout``, and not worth the extra release/reacquire
complexity for a one-off customer-data backfill.

**AWB pool:** for AWB source Noon, ``run_create_shipment()`` first claims one
pre-generated ``Noon AWB`` (``integrations/noon/awb_pool.py``) in its own short
transaction, with the order lock released, and only falls back to the qty-1
``GetNoonLogisticsAWBs`` call when the pool is empty. The AWB is still chosen
once per shipment (the claim records order + shipment, so a repeat returns the
same row) and a claimed AWB never returns to the pool.
"""

import base64
import json

import frappe
from frappe import _
from frappe.utils import cstr, get_datetime, now_datetime

from marketplace_integrations.integrations.noon.awb_pool import claim_pool_awb
from marketplace_integrations.integrations.noon.catalog import get_account, validate_connection_enabled
from marketplace_integrations.integrations.noon.client import NoonClient
from marketplace_integrations.integrations.noon.constants import (
	FBPI_ORDER_INVOICE_UPLOAD_PATH,
	FBPI_ORDER_UPDATE_PATH,
	NOON_LOGISTICS_AWBS_GET_PATH,
	SHIPMENT_CANCEL_PATH,
	SHIPMENT_COURIER_AWBS_ADD_PATH,
	SHIPMENT_CREATE_PATH,
	SHIPMENT_GET_PATH,
	SHIPMENT_INVOICE_MAX_BYTES,
	SHIPMENT_INVOICE_UPLOAD_PATH,
)
from marketplace_integrations.integrations.noon.orders import get_order as get_fbpi_order
from marketplace_integrations.integrations.noon.orders import import_order

RQ_JOB_PREFIX = "marketplace-noon-fulfillment"

MP_STATUS_CONFIRMED = "MP_ITEM_STATUS_CONFIRMED"
INTEGRATION_STATUS_ACKNOWLEDGED = "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED"
UPDATE_ORDER_ITEM_STATUS_OUT_OF_STOCK = "UPDATE_ORDER_REQUEST_ITEM_STATUS_OUT_OF_STOCK"

#: Shipment statuses whose units are still considered claimed (#146 review
#: finding #2). ``Cancelled`` and ``Discarded`` are deliberately absent -
#: a shipment in either status releases its units back to shippable.
_ACTIVE_SHIPMENT_STATUSES = {"Pending", "Created", "Cancel Pending", "Error"}


def _invoice_job_id(order_name, integration_shipment_nr, idempotency_key):
	return f"{RQ_JOB_PREFIX}-{order_name}-{integration_shipment_nr}-invoice-{idempotency_key}"


def _order_invoice_job_id(order_name, idempotency_key):
	return f"{RQ_JOB_PREFIX}-{order_name}-order-invoice-{idempotency_key}"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _lock_and_load_order(marketplace_order_name):
	"""Lock, then load, one Marketplace Order row (#146 decision 10).

	Round-5 fix (review finding #1): commits HERE, immediately before the
	``FOR UPDATE`` lock below, rather than relying on every call site to have
	committed at the right spot on its own. MariaDB's
	``innodb_snapshot_isolation=ON`` scopes a transaction's read view to the
	WHOLE transaction, not to a function/savepoint - a plain consistent read
	anywhere earlier in the same transaction (including one made implicitly
	inside ``NoonClient.request()``/``get_password()`` on every single Noon
	API call, which reads the ``Noon Connection``/``__Auth`` rows) establishes
	that read view ahead of this lock, and a later locking read that then hits
	a row committed after that view was created raises error 1020
	(``frappe.QueryDeadlockError``), rolling back the whole transaction -
	destroying whatever savepoint a caller may have open. Every call site in
	this module reaches this helper either as the very first statement of a
	request/worker function (nothing to lose) or immediately after a Noon HTTP
	call it just released the lock for (exactly where a fresh commit is
	needed) - so committing unconditionally here, before the lock, is safe
	everywhere it's called and removes the need for every caller to manage
	this commit placement by hand. See ``docs/frappe-conventions.md`` for the
	general rule this fixes.
	"""
	frappe.db.commit()
	frappe.db.get_value("Marketplace Order", marketplace_order_name, "name", for_update=True)
	order = frappe.get_doc("Marketplace Order", marketplace_order_name)
	if order.platform != "Noon":
		frappe.throw(_("Marketplace Order {0} is not a Noon order.").format(order.name))
	return order


def _account_and_client(order):
	account = get_account(order.marketplace_account)
	validate_connection_enabled(account)
	return account, NoonClient(account.connection)


def _latest_payload_items(order):
	if not order.latest_payload:
		return []
	try:
		payload = (
			json.loads(order.latest_payload)
			if isinstance(order.latest_payload, str)
			else order.latest_payload
		)
	except (TypeError, ValueError):
		return []
	if not isinstance(payload, dict):
		return []
	items = payload.get("items")
	return items if isinstance(items, list) else []


def _active_shipment_mp_item_nrs(order, exclude_shipment_nr=None):
	"""mp_item_nrs already claimed by a shipment in ``_ACTIVE_SHIPMENT_STATUSES``
	(#146 decision 11/13; review finding #2 - a whitelist of active statuses,
	not a ``!= "Cancelled"`` blacklist, so a newly-discarded shipment also
	releases its units without this function needing its own update)."""
	active_shipment_nrs = {
		row.integration_shipment_nr
		for row in order.noon_shipments or []
		if row.status in _ACTIVE_SHIPMENT_STATUSES and row.integration_shipment_nr != exclude_shipment_nr
	}
	return {
		cstr(row.mp_item_nr).strip()
		for row in order.noon_shipment_units or []
		if row.integration_shipment_nr in active_shipment_nrs
	}


def _pending_oos_units(order):
	"""Units reserved for a queued UpdateOrder call on this order."""
	raw = order.noon_pending_oos_units
	if not raw:
		return set()
	try:
		units = json.loads(raw) if isinstance(raw, str) else raw
	except (TypeError, ValueError):
		frappe.throw(_("Pending Noon Out of Stock Units is invalid."))
	if not isinstance(units, list) or any(not isinstance(unit, str) or not unit for unit in units):
		frappe.throw(_("Pending Noon Out of Stock Units is invalid."))
	return set(units)


def _release_pending_oos_units(marketplace_order_name, mp_item_nrs):
	order = _lock_and_load_order(marketplace_order_name)
	order.noon_pending_oos_units = frappe.as_json(sorted(_pending_oos_units(order) - set(mp_item_nrs)))
	order.save()
	frappe.db.commit()


def get_shippable_units(order, include_pending_oos=False):
	"""Return ``[{"mp_item_nr": ..., "partner_sku": ...}, ...]`` for units
	currently shippable per #146 decision 11: CONFIRMED + ACKNOWLEDGED (raw
	Noon enum values) in ``latest_payload.items[]``, and not already claimed
	by an active (non-Cancelled) shipment or reserved for queued Out of Stock.
	The worker can include its own reserved units while rechecking eligibility.
	"""
	claimed = _active_shipment_mp_item_nrs(order)
	if not include_pending_oos:
		claimed.update(_pending_oos_units(order))
	shippable = []
	seen = set()
	for item in _latest_payload_items(order):
		if not isinstance(item, dict):
			continue
		mp_item_nr = cstr(item.get("mp_item_nr")).strip()
		if not mp_item_nr or mp_item_nr in seen or mp_item_nr in claimed:
			continue
		if item.get("mp_status") != MP_STATUS_CONFIRMED:
			continue
		if item.get("integration_status") != INTEGRATION_STATUS_ACKNOWLEDGED:
			continue
		seen.add(mp_item_nr)
		shippable.append({"mp_item_nr": mp_item_nr, "partner_sku": cstr(item.get("partner_sku")).strip()})
	return shippable


def get_available_actions(order):
	"""Which Noon Fulfillment Desk actions currently have something to act on,
	using the same eligibility rules the request paths below enforce (the
	server still re-validates every action; this only lets the form hide
	buttons that would just fail). Returns ``{"actions": [...],
	"cancelled_on_noon": bool}``; ``cancelled_on_noon`` is true only when no
	action applies and the payload has no CONFIRMED unit left."""
	shipments = order.noon_shipments or []
	statuses = {row.status for row in shipments}
	actions = []

	if get_shippable_units(order):
		actions += ["create_shipment", "mark_out_of_stock"]

	created = [row for row in shipments if row.status == "Created"]
	if created:
		actions.append("cancel_shipment")
		actions.append("add_awb")
	# upload_invoice() also refuses while an upload is already Pending, and
	# (no mixing) while an order-level invoice is Pending/Uploaded.
	if not _order_invoice_blocks_shipment_invoice(order) and any(
		row.invoice_status != "Pending" for row in created
	):
		actions.append("upload_invoice")
	if (
		created
		and order.noon_order_invoice_status != "Pending"
		and not _shipment_invoices_block_order_invoice(order)
	):
		actions.append("upload_order_invoice")

	# Mirrors retry(): Pending/Error re-run CreateShipment, Cancel Pending
	# re-runs CancelShipment, and a Created shipment can retry a Pending
	# Own Courier AWB or a Pending/Error invoice.
	created_nrs = {row.integration_shipment_nr for row in created}
	if (
		statuses & {"Pending", "Error", "Cancel Pending"}
		or any(
			row.integration_shipment_nr in created_nrs and row.status == "Pending" and row.source == "Own Courier"
			for row in order.noon_shipment_awbs or []
		)
		or any(row.invoice_status in ("Pending", "Error") for row in created)
	):
		actions.append("retry")
	if created and order.noon_order_invoice_status in ("Pending", "Error"):
		actions.append("retry_order_invoice")

	if statuses & set(_DISCARDABLE_SHIPMENT_STATUSES):
		actions.append("discard_shipment")
	if _pending_oos_units(order):
		actions.append("release_stuck_oos_reservation")

	items = [item for item in _latest_payload_items(order) if isinstance(item, dict)]
	cancelled_on_noon = (
		not actions and bool(items) and not any(item.get("mp_status") == MP_STATUS_CONFIRMED for item in items)
	)
	return {"actions": actions, "cancelled_on_noon": cancelled_on_noon}


def _next_shipment_number(order):
	"""``<fbpi_order_nr>-<n>``, n = 1 + every shipment ever created on this
	order (including cancelled ones) - never reused (#146 decision 13)."""
	return f"{order.external_order_id}-{len(order.noon_shipments or []) + 1}"


def _log_fulfillment(order, action, status, request_data=None, response_data=None, traceback_text=None):
	"""One committed Marketplace Sync Log row per Noon fulfillment call
	(#146 decision 21). Never includes invoice bytes - callers are
	responsible for keeping ``request_data`` free of them."""
	now = now_datetime()
	frappe.get_doc(
		{
			"doctype": "Marketplace Sync Log",
			"sync_type": "Fulfillment",
			"marketplace_account": order.marketplace_account,
			"marketplace_order": order.name,
			"status": status,
			"trigger_source": "Manual",
			"attempted_at": now,
			"completed_at": now,
			"request_data": frappe.as_json({"action": action, **(request_data or {})}, ensure_ascii=False),
			"response_data": frappe.as_json(response_data, ensure_ascii=False)
			if response_data is not None
			else None,
			"traceback": traceback_text,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


# ---------------------------------------------------------------------------
# Create shipment
# ---------------------------------------------------------------------------


def create_shipment(marketplace_order_name, mp_item_nrs, awb_source, courier=None, awb_nr=None):
	"""Request path: allocate a shipment number, reject any unit already in
	an active shipment, write Pending shipment/unit/AWB rows, commit, then
	enqueue the Noon write (#146 decision 13/14/20)."""
	order = _lock_and_load_order(marketplace_order_name)
	_account_and_client(order)

	mp_item_nrs = [cstr(nr).strip() for nr in (mp_item_nrs or []) if cstr(nr).strip()]
	if not mp_item_nrs:
		frappe.throw(_("At least one unit must be selected."))

	shippable = {row["mp_item_nr"]: row["partner_sku"] for row in get_shippable_units(order)}
	not_shippable = [nr for nr in mp_item_nrs if nr not in shippable]
	if not_shippable:
		frappe.throw(
			_("Units {0} are not shippable (already shipped, cancelled, or not yet acknowledged).").format(
				", ".join(not_shippable)
			)
		)

	if awb_source not in ("Noon", "Own Courier"):
		frappe.throw(_("AWB Source must be Noon or Own Courier."))
	if awb_source == "Own Courier":
		courier = cstr(courier).strip()
		awb_nr = cstr(awb_nr).strip()
		if not courier or not awb_nr:
			frappe.throw(_("Courier and AWB Nr are required for Own Courier shipments."))

	integration_shipment_nr = _next_shipment_number(order)
	order.append(
		"noon_shipments",
		{
			"integration_shipment_nr": integration_shipment_nr,
			"status": "Pending",
			"awb_source": awb_source,
			# Explicit: an appended child row without `creation` inherits the
			# PARENT's creation on save, which would make the #164 stuck-Pending
			# age count from the order's import time instead of this shipment's.
			"creation": now_datetime(),
		},
	)
	for mp_item_nr in mp_item_nrs:
		order.append(
			"noon_shipment_units",
			{
				"integration_shipment_nr": integration_shipment_nr,
				"mp_item_nr": mp_item_nr,
				"partner_sku": shippable[mp_item_nr],
			},
		)
	if awb_source == "Own Courier":
		order.append(
			"noon_shipment_awbs",
			{
				"integration_shipment_nr": integration_shipment_nr,
				"courier": courier,
				"awb_nr": awb_nr,
				"source": "Own Courier",
				"status": "Pending",
			},
		)
	order.save()
	frappe.db.commit()

	job = frappe.enqueue(
		run_create_shipment,
		queue="long",
		timeout=600,
		job_id=f"{RQ_JOB_PREFIX}-{order.name}-{integration_shipment_nr}",
		deduplicate=True,
		marketplace_order_name=order.name,
		integration_shipment_nr=integration_shipment_nr,
	)
	return {"integration_shipment_nr": integration_shipment_nr, "queued": job is not None}


def run_create_shipment(marketplace_order_name, integration_shipment_nr):
	"""Worker: fresh GetFbpiOrder + re-import (#146 decision 12), re-verify
	the shipment's units are still shippable, then idempotently create the
	shipment on Noon (GetShipment-first-on-retry, decision 15).

	Per the module docstring's "#146 round-3 review finding #3" note: the
	order lock is released (``frappe.db.commit()``) before every Noon HTTP
	call below and re-acquired immediately after via ``_reload_shipment()``,
	which also re-checks the shipment's status against ``status_before``
	(read once, before the first HTTP call) - any change aborts this worker
	rather than risk overwriting whatever a concurrent writer (e.g. a cancel
	completing while this worker's lock was released) just committed.
	"""
	frappe.set_user("Administrator")
	order = _lock_and_load_order(marketplace_order_name)
	try:
		account, client = _account_and_client(order)
	except Exception:
		# #146 review finding #4: a disabled connection must not silently
		# abort this worker with no trace.
		order = _fail_shipment_by_name(
			marketplace_order_name, integration_shipment_nr, _("Failed to resolve the Noon connection.")
		)
		_log_fulfillment(
			order,
			"CreateShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			traceback_text=frappe.get_traceback(),
		)
		return
	shipment = _find_shipment_row(order, integration_shipment_nr)
	if not shipment:
		frappe.db.commit()
		return
	status_before = shipment.status
	frappe.db.commit()  # release the order lock before the Noon HTTP call below

	try:
		raw_order = get_fbpi_order(account, order.external_order_id)
	except Exception:
		order = _fail_shipment_by_name(
			marketplace_order_name,
			integration_shipment_nr,
			_("Failed to refresh the order from Noon before shipment creation."),
		)
		_log_fulfillment(
			order,
			"CreateShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			traceback_text=frappe.get_traceback(),
		)
		return

	# #146 round-5 fix (review finding #1): commit HERE, immediately after
	# get_fbpi_order()'s HTTP call returns and before import_order()'s own
	# raw-SQL ``FOR UPDATE`` lock - that HTTP call made its own plain
	# consistent read (the Noon Connection row, inside NoonClient.request())
	# in the transaction opened by the commit at line 360 above; without this
	# second commit, import_order()'s lock would run in that same
	# already-stale transaction instead of a fresh one. See
	# docs/frappe-conventions.md and import_order()'s own docstring.
	frappe.db.commit()

	# #146 review finding #5: import_order() takes no savepoint of its own -
	# give it one here so a mid-import failure rolls back cleanly instead of
	# being silently committed by this worker's own later, unrelated commit.
	savepoint = "noon_create_shipment_import_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(savepoint)
	try:
		import_order(account, raw_order, bump_trigger_source="Manual")
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		order = _fail_shipment_by_name(
			marketplace_order_name,
			integration_shipment_nr,
			_("Failed to refresh the order from Noon before shipment creation."),
		)
		_log_fulfillment(
			order,
			"CreateShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			traceback_text=frappe.get_traceback(),
		)
		return
	else:
		frappe.db.release_savepoint(savepoint)
		# import_order() itself never commits - only its caller does, and
		# this worker is that caller here.
		frappe.db.commit()

	order, shipment = _reload_shipment(marketplace_order_name, integration_shipment_nr)
	if not shipment:
		frappe.db.commit()
		return
	if shipment.status != status_before:
		frappe.db.commit()
		_log_fulfillment(
			order,
			"CreateShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			_shipment_status_changed_message(status_before, shipment.status),
		)
		return

	unit_rows = [
		row
		for row in order.noon_shipment_units or []
		if row.integration_shipment_nr == integration_shipment_nr
	]
	fresh_items = {
		cstr(item.get("mp_item_nr")).strip(): item
		for item in _latest_payload_items(order)
		if isinstance(item, dict)
	}
	for unit in unit_rows:
		item = fresh_items.get(cstr(unit.mp_item_nr).strip())
		if (
			not item
			or item.get("mp_status") != MP_STATUS_CONFIRMED
			or item.get("integration_status") != INTEGRATION_STATUS_ACKNOWLEDGED
		):
			_fail_shipment(order, shipment, _("Unit {0} is no longer shippable.").format(unit.mp_item_nr))
			_log_fulfillment(
				order,
				"CreateShipment",
				"Error",
				{"integration_shipment_nr": integration_shipment_nr, "mp_item_nr": unit.mp_item_nr},
			)
			return
	frappe.db.commit()  # release the lock before the GetShipment idempotency check below

	try:
		get_response = client.request(
			"POST",
			SHIPMENT_GET_PATH,
			json={
				"warehouse_code": account.noon_warehouse_code,
				"integration_shipment_nr": integration_shipment_nr,
			},
		)
	except Exception:
		order = _fail_shipment_by_name(
			marketplace_order_name, integration_shipment_nr, _("Failed to check shipment status with Noon.")
		)
		_log_fulfillment(
			order,
			"GetShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			traceback_text=frappe.get_traceback(),
		)
		return

	order, shipment = _reload_shipment(marketplace_order_name, integration_shipment_nr)
	if not shipment:
		frappe.db.commit()
		return
	if shipment.status != status_before:
		frappe.db.commit()
		_log_fulfillment(
			order,
			"CreateShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			_shipment_status_changed_message(status_before, shipment.status),
		)
		return

	if get_response.status_code == 200:
		_apply_created_shipment(order, shipment)
		_log_fulfillment(
			order,
			"CreateShipment",
			"Success",
			{"integration_shipment_nr": integration_shipment_nr, "note": "found via GetShipment retry"},
		)
		return
	if not _is_shipment_not_found(get_response):
		message = client.error_message(get_response)
		_fail_shipment(order, shipment, message)
		_log_fulfillment(
			order,
			"GetShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			message,
		)
		return

	# Decision 14: a Noon-generated AWB is fetched/saved/committed exactly
	# once, before CreateShipment, and reused on every retry.
	awb_rows = [
		row
		for row in order.noon_shipment_awbs or []
		if row.integration_shipment_nr == integration_shipment_nr
	]
	if shipment.awb_source == "Noon" and not awb_rows:
		frappe.db.commit()  # release the lock before the AWB claim / fetch

		# A pre-generated pool AWB (integrations/noon/awb_pool.py) is claimed
		# in its own short transaction; with an empty pool, fetch one AWB
		# from Noon exactly as before. A claimed AWB never returns to the
		# pool, even if this shipment is later discarded.
		pool_awb = claim_pool_awb(
			account.name, account.noon_country_code, marketplace_order_name, integration_shipment_nr
		)
		awb_response = None
		if not pool_awb:
			try:
				awb_response = client.request(
					"POST",
					NOON_LOGISTICS_AWBS_GET_PATH,
					json={"country_code": account.noon_country_code, "qty": 1},
				)
			except Exception:
				order = _fail_shipment_by_name(
					marketplace_order_name, integration_shipment_nr, _("Failed to fetch a Noon AWB.")
				)
				_log_fulfillment(
					order,
					"CreateShipment",
					"Error",
					{"integration_shipment_nr": integration_shipment_nr},
					traceback_text=frappe.get_traceback(),
				)
				return

		order, shipment = _reload_shipment(marketplace_order_name, integration_shipment_nr)
		if not shipment:
			frappe.db.commit()
			return
		if shipment.status != status_before:
			frappe.db.commit()
			_log_fulfillment(
				order,
				"CreateShipment",
				"Error",
				{"integration_shipment_nr": integration_shipment_nr},
				_shipment_status_changed_message(status_before, shipment.status),
			)
			return

		if pool_awb:
			awb_nrs = [pool_awb]
		else:
			if awb_response.status_code != 200:
				_fail_shipment(order, shipment, client.error_message(awb_response))
				_log_fulfillment(
					order,
					"CreateShipment",
					"Error",
					{"integration_shipment_nr": integration_shipment_nr},
					client.error_message(awb_response),
				)
				return
			try:
				awb_nrs = (awb_response.json() or {}).get("awb_nrs") or []
			except ValueError:
				awb_nrs = []
		if not awb_nrs:
			_fail_shipment(order, shipment, _("Noon returned no AWB number."))
			_log_fulfillment(
				order, "CreateShipment", "Error", {"integration_shipment_nr": integration_shipment_nr}
			)
			return
		order.append(
			"noon_shipment_awbs",
			{
				"integration_shipment_nr": integration_shipment_nr,
				"courier": "noon",
				"awb_nr": awb_nrs[0],
				"source": "Noon",
				"status": "Pending",
			},
		)
		order.save()
		frappe.db.commit()

		order, shipment = _reload_shipment(marketplace_order_name, integration_shipment_nr)
		if not shipment:
			frappe.db.commit()
			return
		awb_rows = [
			row
			for row in order.noon_shipment_awbs or []
			if row.integration_shipment_nr == integration_shipment_nr
		]

	# #146 round-5 fix (review finding #2): re-verify the status immediately
	# before the final CreateShipment call below, whether or not a Noon AWB
	# was just fetched above - a concurrent discard_shipment() could have run
	# in the gap since the last check (the AWB branch's own reload just above
	# has none of its own), and sending CreateShipment to Noon after a
	# concurrent Discard would create an orphaned Noon shipment with no local
	# record claiming it. This check must stay here, right before the HTTP
	# call - checking only after the call returns is too late to prevent the
	# orphan, only to notice it.
	order, shipment = _reload_shipment(marketplace_order_name, integration_shipment_nr)
	if not shipment:
		frappe.db.commit()
		return
	if shipment.status != status_before:
		frappe.db.commit()
		_log_fulfillment(
			order,
			"CreateShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			_(
				"Shipment status changed to {0} before CreateShipment could be sent; "
				"aborting to avoid creating an orphaned Noon shipment."
			).format(shipment.status),
		)
		return

	awb_rows = [
		row
		for row in order.noon_shipment_awbs or []
		if row.integration_shipment_nr == integration_shipment_nr
	]
	unit_rows = [
		row
		for row in order.noon_shipment_units or []
		if row.integration_shipment_nr == integration_shipment_nr
	]
	external_order_id = order.external_order_id
	frappe.db.commit()  # release the lock before the final CreateShipment HTTP call

	try:
		create_response = client.request(
			"POST",
			SHIPMENT_CREATE_PATH,
			json={
				"warehouse_code": account.noon_warehouse_code,
				"integration_shipment_nr": integration_shipment_nr,
				"fbpi_order_nr": external_order_id,
				"awbs": [{"courier": row.courier, "awb_nr": row.awb_nr} for row in awb_rows],
				"items": [{"mp_item_nr": row.mp_item_nr} for row in unit_rows],
			},
		)
	except Exception:
		order = _fail_shipment_by_name(
			marketplace_order_name, integration_shipment_nr, _("Failed to create the shipment on Noon.")
		)
		_log_fulfillment(
			order,
			"CreateShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			traceback_text=frappe.get_traceback(),
		)
		return

	order, shipment = _reload_shipment(marketplace_order_name, integration_shipment_nr)
	if not shipment:
		frappe.db.commit()
		return
	if shipment.status != status_before:
		frappe.db.commit()
		_log_fulfillment(
			order,
			"CreateShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			_shipment_status_changed_message(status_before, shipment.status),
		)
		return

	if create_response.status_code != 200:
		_fail_shipment(order, shipment, client.error_message(create_response))
		_log_fulfillment(
			order,
			"CreateShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			client.error_message(create_response),
		)
		return

	awb_rows = [
		row
		for row in order.noon_shipment_awbs or []
		if row.integration_shipment_nr == integration_shipment_nr
	]
	shipment.status = "Created"
	shipment.created_on = now_datetime()
	shipment.last_error = None
	for row in awb_rows:
		row.status = "Added"
	order.save()
	frappe.db.commit()
	_log_fulfillment(order, "CreateShipment", "Success", {"integration_shipment_nr": integration_shipment_nr})


def _apply_created_shipment(order, shipment):
	"""Reconcile local state from a successful GetShipment retry response
	(``awbs[]``/``items[]``, no status field) - GetShipment returning 200 at
	all is what proves the shipment already exists on Noon's side; our own
	unit/AWB rows remain the source of truth for what it was created with."""
	shipment.status = "Created"
	if not shipment.created_on:
		shipment.created_on = now_datetime()
	shipment.last_error = None
	for row in order.noon_shipment_awbs or []:
		if row.integration_shipment_nr == shipment.integration_shipment_nr:
			row.status = "Added"
	order.save()
	frappe.db.commit()


def _find_shipment_row(order, integration_shipment_nr):
	return next(
		(row for row in order.noon_shipments or [] if row.integration_shipment_nr == integration_shipment_nr),
		None,
	)


def _fail_shipment_by_name(marketplace_order_name, integration_shipment_nr, message):
	"""Lock, reload, and mark one shipment Error by name/number alone - used
	from an ``except`` branch that caught a transport-level exception (#146
	review finding #4) and so has no fresh ``order``/``shipment`` object of
	its own to pass to ``_fail_shipment()`` below. Returns the reloaded
	``order`` so the caller can still write a ``Marketplace Sync Log`` row."""
	order = _lock_and_load_order(marketplace_order_name)
	shipment = _find_shipment_row(order, integration_shipment_nr)
	if shipment:
		shipment.status = "Error"
		shipment.last_error = cstr(message).strip()[:500]
		order.save()
	frappe.db.commit()
	return order


def _fail_shipment(order, shipment, message):
	_fail_shipment_by_name(order.name, shipment.integration_shipment_nr, message)


def _reload_shipment(marketplace_order_name, integration_shipment_nr):
	"""Re-lock and reload one shipment row after a Noon HTTP call that was
	made without holding the order lock (#146 review finding #3). Returns
	``(order, shipment)`` - ``shipment`` is ``None`` if the row is gone."""
	order = _lock_and_load_order(marketplace_order_name)
	shipment = _find_shipment_row(order, integration_shipment_nr)
	return order, shipment


def _is_shipment_not_found(response):
	"""True when GetShipment says the shipment does not exist on Noon.

	Production Noon answers HTTP 400 INVALID_ARGUMENT "No shipment found with
	integration_shipment_nr ..." (not the 404 first assumed, #146 verification
	item 4). Any other 400, and every other error, is NOT treated as absence.
	"""
	if response.status_code == 404:
		return True
	if response.status_code != 400:
		return False
	try:
		error = (response.json() or {}).get("error")
	except (ValueError, AttributeError):
		return False
	message = error.get("message") if isinstance(error, dict) else None
	return isinstance(message, str) and "No shipment found" in message


def _shipment_status_changed_message(expected_status, actual_status):
	return _("Shipment status changed from {0} to {1} while the Noon request was in flight.").format(
		expected_status, actual_status
	)


# ---------------------------------------------------------------------------
# Cancel shipment
# ---------------------------------------------------------------------------


def cancel_shipment(marketplace_order_name, integration_shipment_nr):
	"""Request path (#146 decision 17): only ``Created`` shipments can be
	cancelled. The UI warns the caller that Noon also cancels the
	shipment's units - this function itself does not re-derive that
	warning."""
	order = _lock_and_load_order(marketplace_order_name)
	_account_and_client(order)
	shipment = _find_shipment_row(order, integration_shipment_nr)
	if not shipment or shipment.status != "Created":
		frappe.throw(_("Shipment {0} is not in a cancellable state.").format(integration_shipment_nr))
	shipment.status = "Cancel Pending"
	order.save()
	frappe.db.commit()

	job = frappe.enqueue(
		run_cancel_shipment,
		queue="long",
		timeout=300,
		job_id=f"{RQ_JOB_PREFIX}-{order.name}-{integration_shipment_nr}-cancel",
		deduplicate=True,
		marketplace_order_name=order.name,
		integration_shipment_nr=integration_shipment_nr,
	)
	return {"queued": job is not None}


def run_cancel_shipment(marketplace_order_name, integration_shipment_nr):
	"""A single Noon call (unlike ``run_create_shipment()``'s up to four), so
	the order lock stays held across it rather than released/re-acquired -
	#146 review finding #3 only applies to workers chaining several Noon
	HTTP calls. #146 review finding #4 still applies here: a transport-level
	exception (timeout, connection error) must leave the same committed
	``Marketplace Sync Log`` row and ``last_error`` a non-200 response
	already leaves, not an unrecoverable silent RQ job failure."""
	frappe.set_user("Administrator")
	order = _lock_and_load_order(marketplace_order_name)
	shipment = _find_shipment_row(order, integration_shipment_nr)
	if not shipment:
		frappe.db.commit()
		return
	try:
		account, client = _account_and_client(order)
	except Exception:
		shipment.last_error = cstr(frappe.get_traceback()).strip()[:500]
		order.save()
		frappe.db.commit()
		_log_fulfillment(
			order,
			"CancelShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			traceback_text=frappe.get_traceback(),
		)
		return

	# GetShipment has no status field (decision 17) - resending Cancel is
	# the only re-check available; this relies on CancelShipment itself
	# being safe to resend against an already-cancelled shipment.
	try:
		response = client.request(
			"POST",
			SHIPMENT_CANCEL_PATH,
			json={
				"warehouse_code": account.noon_warehouse_code,
				"integration_shipment_nr": integration_shipment_nr,
			},
		)
	except Exception:
		shipment.last_error = cstr(frappe.get_traceback()).strip()[:500]
		order.save()
		frappe.db.commit()
		_log_fulfillment(
			order,
			"CancelShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			traceback_text=frappe.get_traceback(),
		)
		return
	if response.status_code != 200:
		shipment.last_error = client.error_message(response)[:500]
		order.save()
		frappe.db.commit()
		_log_fulfillment(
			order,
			"CancelShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			client.error_message(response),
		)
		return

	shipment.status = "Cancelled"
	shipment.cancelled_on = now_datetime()
	shipment.last_error = None
	order.save()
	frappe.db.commit()
	_log_fulfillment(order, "CancelShipment", "Success", {"integration_shipment_nr": integration_shipment_nr})


# ---------------------------------------------------------------------------
# Discard shipment / release stuck reservations (#146 review finding #2)
# ---------------------------------------------------------------------------

_DISCARDABLE_SHIPMENT_STATUSES = ("Error", "Cancel Pending")


def discard_shipment(marketplace_order_name, integration_shipment_nr):
	"""Abandon a shipment stuck in ``Error`` (a failed CreateShipment with no
	other recovery path) or ``Cancel Pending`` (a CancelShipment call that
	never got a 200 response) - these otherwise permanently claim their
	units via ``_active_shipment_mp_item_nrs()``'s ``_ACTIVE_SHIPMENT_STATUSES``
	whitelist, with no recovery except a manual DB edit.

	Deliberately NOT available from ``Pending``/``Created``/``Cancelled`` -
	each of those has its own lifecycle and must not be short-circuited this
	way. Purely a local bookkeeping action: there is no Noon "abandon
	shipment" API, so this never enqueues a Noon HTTP call - it only stops
	this app from tracking/claiming the shipment's units locally.
	"""
	order = _lock_and_load_order(marketplace_order_name)
	shipment = _find_shipment_row(order, integration_shipment_nr)
	if not shipment or shipment.status not in _DISCARDABLE_SHIPMENT_STATUSES:
		frappe.throw(
			_("Shipment {0} can only be discarded from Error or Cancel Pending.").format(
				integration_shipment_nr
			)
		)
	shipment.status = "Discarded"
	shipment.last_error = None
	order.save()
	frappe.db.commit()
	_log_fulfillment(
		order, "DiscardShipment", "Success", {"integration_shipment_nr": integration_shipment_nr}
	)
	return {"discarded": True}


def release_stuck_oos_reservation(marketplace_order_name):
	"""Manually clear every Pending Noon Out of Stock unit reservation on
	this order - recovers from a hard-killed worker process that never
	reached ``run_mark_out_of_stock()``'s ``finally`` release (#146 review
	finding #2). A separate whitelisted action rather than part of
	``discard_shipment()`` above, since this reservation lives on the order
	itself (``noon_pending_oos_units``), not on any one shipment row. A
	legitimate in-flight job's own ``finally`` simply finds nothing left to
	release once this has already cleared it."""
	order = _lock_and_load_order(marketplace_order_name)
	released = sorted(_pending_oos_units(order))
	if not released:
		frappe.throw(_("There is no pending Out of Stock reservation on this order."))
	order.noon_pending_oos_units = frappe.as_json([])
	order.save()
	frappe.db.commit()
	_log_fulfillment(order, "ReleaseStuckOosReservation", "Success", {"released_mp_item_nrs": released})
	return {"released": released}


# ---------------------------------------------------------------------------
# Add AWB
# ---------------------------------------------------------------------------


def add_awb(marketplace_order_name, integration_shipment_nr, courier, awb_nr):
	"""Request path (#146 decision 16): only ``Created`` shipments."""
	order = _lock_and_load_order(marketplace_order_name)
	_account_and_client(order)
	shipment = _find_shipment_row(order, integration_shipment_nr)
	if not shipment or shipment.status != "Created":
		frappe.throw(_("Shipment {0} must be Created before adding an AWB.").format(integration_shipment_nr))
	courier = cstr(courier).strip()
	awb_nr = cstr(awb_nr).strip()
	if not courier or not awb_nr:
		frappe.throw(_("Courier and AWB Nr are required."))
	if any(
		row.integration_shipment_nr == integration_shipment_nr and row.awb_nr == awb_nr
		for row in order.noon_shipment_awbs or []
	):
		frappe.throw(
			_("AWB {0} is already recorded for shipment {1}.").format(awb_nr, integration_shipment_nr)
		)

	order.append(
		"noon_shipment_awbs",
		{
			"integration_shipment_nr": integration_shipment_nr,
			"courier": courier,
			"awb_nr": awb_nr,
			"source": "Own Courier",
			"status": "Pending",
		},
	)
	order.save()
	frappe.db.commit()

	job = frappe.enqueue(
		run_add_awb,
		queue="long",
		timeout=300,
		job_id=f"{RQ_JOB_PREFIX}-{order.name}-{integration_shipment_nr}-awb-{awb_nr}",
		deduplicate=True,
		marketplace_order_name=order.name,
		integration_shipment_nr=integration_shipment_nr,
		awb_nr=awb_nr,
	)
	return {"queued": job is not None}


def run_add_awb(marketplace_order_name, integration_shipment_nr, awb_nr):
	"""Up to two sequential Noon calls (GetShipment, then possibly
	AddShipmentCourierAwbs) - the order lock is released before each and
	re-acquired immediately after (#146 review finding #3), re-checking the
	shipment's status against ``status_before`` on each re-acquisition."""
	frappe.set_user("Administrator")
	order = _lock_and_load_order(marketplace_order_name)
	shipment = _find_shipment_row(order, integration_shipment_nr)
	if not shipment or shipment.status != "Created":
		for row in order.noon_shipment_awbs or []:
			if (
				row.integration_shipment_nr == integration_shipment_nr
				and row.awb_nr == awb_nr
				and row.status == "Pending"
			):
				row.status = "Skipped"
				order.save()
				break
		frappe.db.commit()
		_log_fulfillment(
			order,
			"AddShipmentCourierAwbs",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr, "awb_nr": awb_nr},
			_("Shipment is no longer Created."),
		)
		return
	try:
		account, client = _account_and_client(order)
	except Exception:
		_log_fulfillment(
			order,
			"AddShipmentCourierAwbs",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr, "awb_nr": awb_nr},
			traceback_text=frappe.get_traceback(),
		)
		return
	awb_row = next(
		(
			row
			for row in order.noon_shipment_awbs or []
			if row.integration_shipment_nr == integration_shipment_nr and row.awb_nr == awb_nr
		),
		None,
	)
	if not awb_row:
		frappe.db.commit()
		return
	status_before = shipment.status
	frappe.db.commit()  # release the order lock before the GetShipment HTTP call

	# Decision 16: a retry checks GetShipment.awbs for this awb_nr first.
	try:
		get_response = client.request(
			"POST",
			SHIPMENT_GET_PATH,
			json={
				"warehouse_code": account.noon_warehouse_code,
				"integration_shipment_nr": integration_shipment_nr,
			},
		)
	except Exception:
		order, shipment = _reload_shipment(marketplace_order_name, integration_shipment_nr)
		frappe.db.commit()
		_log_fulfillment(
			order,
			"GetShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr, "awb_nr": awb_nr},
			traceback_text=frappe.get_traceback(),
		)
		return

	order, shipment = _reload_shipment(marketplace_order_name, integration_shipment_nr)
	if not shipment:
		frappe.db.commit()
		return
	if shipment.status != status_before:
		frappe.db.commit()
		_log_fulfillment(
			order,
			"AddShipmentCourierAwbs",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr, "awb_nr": awb_nr},
			_shipment_status_changed_message(status_before, shipment.status),
		)
		return

	if get_response.status_code != 200 and not _is_shipment_not_found(get_response):
		frappe.db.commit()
		_log_fulfillment(
			order,
			"GetShipment",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr, "awb_nr": awb_nr},
			client.error_message(get_response),
		)
		return
	already_present = False
	if get_response.status_code == 200:
		try:
			body = get_response.json()
		except ValueError:
			body = None
		if (
			not isinstance(body, dict)
			or not isinstance(body.get("awbs"), list)
			or any(
				not isinstance(entry, dict) or not isinstance(entry.get("awb_nr"), str)
				for entry in body["awbs"]
			)
		):
			frappe.db.commit()
			_log_fulfillment(
				order,
				"GetShipment",
				"Error",
				{"integration_shipment_nr": integration_shipment_nr, "awb_nr": awb_nr},
				_("Noon returned an invalid GetShipment AWB list."),
			)
			return
		already_present = any(entry["awb_nr"] == awb_nr for entry in body["awbs"])

	if not already_present:
		awb_row = next(
			(
				row
				for row in order.noon_shipment_awbs or []
				if row.integration_shipment_nr == integration_shipment_nr and row.awb_nr == awb_nr
			),
			None,
		)
		if not awb_row:
			frappe.db.commit()
			return
		frappe.db.commit()  # release the lock before the AddShipmentCourierAwbs HTTP call

		try:
			response = client.request(
				"POST",
				SHIPMENT_COURIER_AWBS_ADD_PATH,
				json={
					"warehouse_code": account.noon_warehouse_code,
					"integration_shipment_nr": integration_shipment_nr,
					"awbs": [{"courier": awb_row.courier, "awb_nr": awb_nr}],
				},
			)
		except Exception:
			order, shipment = _reload_shipment(marketplace_order_name, integration_shipment_nr)
			frappe.db.commit()
			_log_fulfillment(
				order,
				"AddShipmentCourierAwbs",
				"Error",
				{"integration_shipment_nr": integration_shipment_nr, "awb_nr": awb_nr},
				traceback_text=frappe.get_traceback(),
			)
			return

		order, shipment = _reload_shipment(marketplace_order_name, integration_shipment_nr)
		if not shipment:
			frappe.db.commit()
			return
		if shipment.status != status_before:
			frappe.db.commit()
			_log_fulfillment(
				order,
				"AddShipmentCourierAwbs",
				"Error",
				{"integration_shipment_nr": integration_shipment_nr, "awb_nr": awb_nr},
				_shipment_status_changed_message(status_before, shipment.status),
			)
			return

		if response.status_code != 200:
			frappe.db.commit()
			_log_fulfillment(
				order,
				"AddShipmentCourierAwbs",
				"Error",
				{"integration_shipment_nr": integration_shipment_nr, "awb_nr": awb_nr},
				client.error_message(response),
			)
			return

	order, shipment = _reload_shipment(marketplace_order_name, integration_shipment_nr)
	awb_row = next(
		(
			row
			for row in order.noon_shipment_awbs or []
			if row.integration_shipment_nr == integration_shipment_nr and row.awb_nr == awb_nr
		),
		None,
	)
	if awb_row:
		awb_row.status = "Added"
		order.save()
	frappe.db.commit()
	_log_fulfillment(
		order,
		"AddShipmentCourierAwbs",
		"Success",
		{"integration_shipment_nr": integration_shipment_nr, "awb_nr": awb_nr},
	)


# ---------------------------------------------------------------------------
# Mark Out of Stock (UpdateOrder)
# ---------------------------------------------------------------------------


def mark_out_of_stock(marketplace_order_name, mp_item_nrs):
	"""Request path (#146 decision 18): same shippable-unit rule as Create
	Shipment. The submitted-Sales-Order warning is client-side only (decision
	8) - this function does not block on it."""
	order = _lock_and_load_order(marketplace_order_name)
	_account_and_client(order)
	mp_item_nrs = list(dict.fromkeys(cstr(nr).strip() for nr in (mp_item_nrs or []) if cstr(nr).strip()))
	if not mp_item_nrs:
		frappe.throw(_("At least one unit must be selected."))
	shippable = {row["mp_item_nr"] for row in get_shippable_units(order)}
	not_shippable = [nr for nr in mp_item_nrs if nr not in shippable]
	if not_shippable:
		frappe.throw(_("Units {0} cannot be marked Out of Stock.").format(", ".join(not_shippable)))
	order.noon_pending_oos_units = frappe.as_json(sorted(_pending_oos_units(order) | set(mp_item_nrs)))
	order.save()
	frappe.db.commit()

	try:
		job = frappe.enqueue(
			run_mark_out_of_stock,
			queue="long",
			timeout=300,
			job_id=f"{RQ_JOB_PREFIX}-{order.name}-oos-{frappe.generate_hash(length=16)}",
			deduplicate=True,
			marketplace_order_name=order.name,
			mp_item_nrs=mp_item_nrs,
		)
	except Exception:
		_release_pending_oos_units(order.name, mp_item_nrs)
		raise
	if job is None:
		_release_pending_oos_units(order.name, mp_item_nrs)
	return {"queued": job is not None}


def run_mark_out_of_stock(marketplace_order_name, mp_item_nrs):
	"""The order lock is released before the UpdateOrder HTTP call and before
	the post-success GetFbpiOrder refresh, and re-acquired immediately after
	each (#146 review finding #3; round-5 fix, review finding #5 - the
	GetFbpiOrder refresh used to still hold the lock acquired for the
	UpdateOrder result check, contrary to what this docstring already
	claimed). The outer ``except`` is new per #146 review finding #4: a
	transport-level exception from ``_account_and_client()`` or
	``client.request()`` used to propagate out of this worker with no
	committed ``Marketplace Sync Log`` row at all - the ``finally`` below
	already released the OOS reservation in that case (so units never stayed
	stuck), but the failure itself was invisible beyond a bare RQ job-failure
	log entry. A concurrent ``release_stuck_oos_reservation()`` clearing the
	local reservation mid-call no longer causes this worker to drop evidence
	of the actual Noon outcome silently - it still writes a Sync Log row for
	whatever UpdateOrder actually returned (round-5 fix, review finding #3)."""
	frappe.set_user("Administrator")
	order = _lock_and_load_order(marketplace_order_name)
	if not set(mp_item_nrs) <= _pending_oos_units(order):
		frappe.db.commit()
		return
	try:
		still_shippable = {row["mp_item_nr"] for row in get_shippable_units(order, include_pending_oos=True)}
		if not set(mp_item_nrs) <= still_shippable:
			frappe.db.commit()
			_log_fulfillment(
				order,
				"UpdateOrder",
				"Error",
				{"mp_item_nrs": mp_item_nrs},
				_("Units are no longer eligible to mark Out of Stock."),
			)
			return
		account, client = _account_and_client(order)
		external_order_id = order.external_order_id
		frappe.db.commit()  # release the order lock before the Noon HTTP call below

		response = client.request(
			"POST",
			FBPI_ORDER_UPDATE_PATH,
			json={
				"fbpi_order_nr": external_order_id,
				"items": [
					{"mp_item_nr": nr, "status": UPDATE_ORDER_ITEM_STATUS_OUT_OF_STOCK} for nr in mp_item_nrs
				],
			},
		)

		order = _lock_and_load_order(marketplace_order_name)
		reservation_released_concurrently = not set(mp_item_nrs) <= _pending_oos_units(order)
		if reservation_released_concurrently:
			# #146 round-5 fix (review finding #3): the local reservation is
			# gone (e.g. release_stuck_oos_reservation() ran while the Noon
			# call above was in flight), but UpdateOrder may have already
			# succeeded on Noon's side - still write a Sync Log row recording
			# the actual Noon outcome rather than silently dropping the only
			# evidence of it. Returning here (not falling through to the
			# refresh below) is still correct: with no local reservation left
			# to reconcile, there is nothing for the refresh to protect.
			frappe.db.commit()
			note = "Local Out of Stock reservation was released concurrently while this call was in flight."
			if response.status_code != 200:
				_log_fulfillment(
					order,
					"UpdateOrder",
					"Error",
					{"mp_item_nrs": mp_item_nrs, "note": note},
					client.error_message(response),
				)
			else:
				_log_fulfillment(order, "UpdateOrder", "Success", {"mp_item_nrs": mp_item_nrs, "note": note})
			return
		if response.status_code != 200:
			frappe.db.commit()
			_log_fulfillment(
				order, "UpdateOrder", "Error", {"mp_item_nrs": mp_item_nrs}, client.error_message(response)
			)
			return

		# #146 round-5 fix (review finding #5): this worker used to hold the
		# order lock (re-acquired just above) across the GetFbpiOrder HTTP
		# call below - the module/function docstrings already claimed it was
		# released here, but nothing actually committed before the call.
		# Release it now, same as every other HTTP call in this module.
		frappe.db.commit()
		try:
			raw_order = get_fbpi_order(account, external_order_id)
			# #146 round-5 fix (review finding #1): commit again, immediately
			# before import_order()'s own raw-SQL lock - the HTTP call above
			# made its own plain consistent read in the transaction the
			# commit just above opened; without this second commit that lock
			# would not be first-in-transaction. See
			# docs/frappe-conventions.md and import_order()'s docstring.
			frappe.db.commit()
			savepoint = "noon_oos_refresh_" + frappe.generate_hash(length=10)
			frappe.db.savepoint(savepoint)
			try:
				import_order(account, raw_order, bump_trigger_source="Manual")
			except Exception:
				frappe.db.rollback(save_point=savepoint)
				raise
			else:
				frappe.db.release_savepoint(savepoint)
				frappe.db.commit()  # import_order() itself never commits - this worker does
		except Exception:
			frappe.log_error(title="Noon fulfillment: OOS refresh failed", message=frappe.get_traceback())
		order = _lock_and_load_order(marketplace_order_name)
		frappe.db.commit()
		_log_fulfillment(order, "UpdateOrder", "Success", {"mp_item_nrs": mp_item_nrs})
	except Exception:
		order = _lock_and_load_order(marketplace_order_name)
		frappe.db.commit()
		_log_fulfillment(
			order, "UpdateOrder", "Error", {"mp_item_nrs": mp_item_nrs}, traceback_text=frappe.get_traceback()
		)
	finally:
		_release_pending_oos_units(marketplace_order_name, mp_item_nrs)


# ---------------------------------------------------------------------------
# Shipment invoice
# ---------------------------------------------------------------------------


def _validate_invoice_file(file_url):
	"""Shared by the shipment and order invoice request paths: the File must
	be readable, private, a real PDF and within Noon's 5 MiB decoded limit."""
	file_doc = frappe.get_doc("File", {"file_url": file_url})
	file_doc.check_permission("read")
	# #146 review finding #9: this field's own description says "Private PDF
	# attachment", but nothing enforced it - the Attach dialog lets a user
	# mark the upload public, which would expose a customer invoice at a
	# guessable unauthenticated /files/ URL forever (File.is_private is never
	# re-checked after upload). Reject outright rather than silently flip it,
	# so the user re-uploads deliberately as private instead of being
	# surprised later by a quietly-corrected visibility flag.
	if not file_doc.is_private:
		frappe.throw(_("Invoice file must be private, not public."))
	raw_bytes = file_doc.get_content()
	if isinstance(raw_bytes, str):
		raw_bytes = raw_bytes.encode()
	if not raw_bytes.startswith(b"%PDF-"):
		frappe.throw(_("Invoice must be a PDF file."))
	if len(raw_bytes) > SHIPMENT_INVOICE_MAX_BYTES:
		frappe.throw(_("Invoice file exceeds the 5 MiB limit."))


def _order_invoice_blocks_shipment_invoice(order):
	"""No mixing: a shipment invoice is refused while the order-level invoice
	is Pending or Uploaded."""
	return order.noon_order_invoice_status in ("Pending", "Uploaded")


def _shipment_invoices_block_order_invoice(order):
	"""No mixing: the order-level invoice is refused while any shipment
	invoice is Pending or Uploaded."""
	return any(row.invoice_status in ("Pending", "Uploaded") for row in order.noon_shipments or [])


def upload_invoice(marketplace_order_name, integration_shipment_nr, file_url, invoice_nr, issued_at):
	"""Request path (#146 decision 19): only ``Created`` shipments. Reads
	and validates the File here (never logs its bytes), generates a fresh
	idempotency key for this explicit upload action, and writes Pending
	invoice state before enqueuing the actual Noon call."""
	order = _lock_and_load_order(marketplace_order_name)
	_account_and_client(order)
	shipment = _find_shipment_row(order, integration_shipment_nr)
	if not shipment or shipment.status != "Created":
		frappe.throw(
			_("Shipment {0} must be Created before uploading an invoice.").format(integration_shipment_nr)
		)
	if shipment.invoice_status == "Pending":
		frappe.throw(
			_("An invoice upload is already pending for shipment {0}.").format(integration_shipment_nr)
		)
	if _order_invoice_blocks_shipment_invoice(order):
		frappe.throw(
			_(
				"This order already has an order-level invoice (Pending or Uploaded); "
				"shipment invoices cannot be mixed with it."
			)
		)
	invoice_nr = cstr(invoice_nr).strip()
	if not invoice_nr or not issued_at:
		frappe.throw(_("Invoice Nr and Issued At are required."))

	_validate_invoice_file(file_url)

	shipment.invoice_file = file_url
	shipment.invoice_nr = invoice_nr
	shipment.invoice_issued_at = issued_at
	shipment.invoice_idempotency_key = frappe.generate_hash(length=32)
	shipment.invoice_status = "Pending"
	# #146 round-5 fix (review finding #7): clear any previous attempt's
	# error here too, so a fresh upload doesn't still show a stale error
	# alongside its new Pending status.
	shipment.invoice_last_error = None
	order.save()
	frappe.db.commit()

	job = frappe.enqueue(
		run_upload_invoice,
		queue="long",
		timeout=300,
		job_id=_invoice_job_id(order.name, integration_shipment_nr, shipment.invoice_idempotency_key),
		deduplicate=True,
		marketplace_order_name=order.name,
		integration_shipment_nr=integration_shipment_nr,
		invoice_idempotency_key=shipment.invoice_idempotency_key,
	)
	return {"queued": job is not None}


def run_upload_invoice(marketplace_order_name, integration_shipment_nr, invoice_idempotency_key):
	"""The order lock is released before reading the File and calling Noon,
	and re-acquired immediately after (#146 review finding #3). Re-checked
	on re-acquisition against ``invoice_idempotency_key`` (the existing
	per-upload dedup key, a stronger guard here than a bare shipment-status
	comparison since it also catches a deliberate re-upload superseding this
	one) rather than ``_shipment_status_changed_message()``'s generic
	status-equality check.

	Invoice errors are written to ``shipment.invoice_last_error``, a
	dedicated field - not the shared ``shipment.last_error`` a
	CreateShipment/CancelShipment failure also writes (#146 review "new
	problem": a successful cancel used to wipe evidence of a prior invoice
	error, and an invoice error could overwrite/be overwritten by an
	unrelated shipment-level error on the same field)."""
	frappe.set_user("Administrator")
	order = _lock_and_load_order(marketplace_order_name)
	shipment = _find_shipment_row(order, integration_shipment_nr)
	if (
		not shipment
		or not shipment.invoice_file
		or shipment.invoice_idempotency_key != invoice_idempotency_key
		or shipment.invoice_status not in ("Pending", "Error")
	):
		frappe.db.commit()
		return
	if shipment.status != "Created":
		message = _("Shipment is no longer Created.")
		shipment.invoice_status = "Error"
		shipment.invoice_last_error = message
		order.save()
		frappe.db.commit()
		_log_fulfillment(
			order,
			"UploadShipmentInvoice",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			message,
		)
		return
	try:
		account, client = _account_and_client(order)
	except Exception:
		shipment.invoice_status = "Error"
		shipment.invoice_last_error = cstr(frappe.get_traceback()).strip()[:500]
		order.save()
		frappe.db.commit()
		_log_fulfillment(
			order,
			"UploadShipmentInvoice",
			"Error",
			{"integration_shipment_nr": integration_shipment_nr},
			traceback_text=frappe.get_traceback(),
		)
		return
	invoice_file = shipment.invoice_file
	invoice_nr = shipment.invoice_nr
	issued_at = (
		get_datetime(shipment.invoice_issued_at).strftime("%Y-%m-%d") if shipment.invoice_issued_at else ""
	)
	request_metadata = {
		"integration_shipment_nr": integration_shipment_nr,
		"invoice_nr": invoice_nr,
		"issued_at": issued_at,
	}
	frappe.db.commit()  # release the order lock before reading the File and calling Noon

	try:
		file_doc = frappe.get_doc("File", {"file_url": invoice_file})
		raw_bytes = file_doc.get_content()
		if isinstance(raw_bytes, str):
			raw_bytes = raw_bytes.encode()
		encoded = base64.b64encode(raw_bytes).decode()
		response = client.request(
			"POST",
			SHIPMENT_INVOICE_UPLOAD_PATH,
			json={
				"warehouse_code": account.noon_warehouse_code,
				"integration_shipment_nr": integration_shipment_nr,
				"invoice": encoded,
				"invoice_nr": invoice_nr,
				"issued_at": issued_at,
				"idempotency_key": invoice_idempotency_key,
			},
		)
	except Exception:
		order = _lock_and_load_order(marketplace_order_name)
		shipment = _find_shipment_row(order, integration_shipment_nr)
		if shipment and shipment.invoice_idempotency_key == invoice_idempotency_key:
			shipment.invoice_status = "Error"
			shipment.invoice_last_error = cstr(frappe.get_traceback()).strip()[:500]
			order.save()
		frappe.db.commit()
		# Never include the base64 payload in the Sync Log.
		_log_fulfillment(
			order, "UploadShipmentInvoice", "Error", request_metadata, traceback_text=frappe.get_traceback()
		)
		return

	order = _lock_and_load_order(marketplace_order_name)
	shipment = _find_shipment_row(order, integration_shipment_nr)
	if not shipment or shipment.invoice_idempotency_key != invoice_idempotency_key:
		# A deliberate re-upload (fresh key) or the shipment itself vanishing
		# superseded this call while it was in flight - don't write a
		# misleading log entry for a key that's no longer current.
		frappe.db.commit()
		return

	if response.status_code != 200:
		shipment.invoice_status = "Error"
		shipment.invoice_last_error = client.error_message(response)[:500]
		order.save()
		frappe.db.commit()
		_log_fulfillment(
			order, "UploadShipmentInvoice", "Error", request_metadata, client.error_message(response)
		)
		return

	shipment.invoice_status = "Uploaded"
	shipment.invoice_uploaded_on = now_datetime()
	shipment.invoice_last_error = None
	order.save()
	frappe.db.commit()
	_log_fulfillment(order, "UploadShipmentInvoice", "Success", request_metadata)


# ---------------------------------------------------------------------------
# Order invoice (UploadFbpiOrderInvoice)
# ---------------------------------------------------------------------------


def upload_order_invoice(marketplace_order_name, file_url, invoice_nr, issued_at):
	"""Request path for the order-level invoice. Same shape as
	``upload_invoice()``. Precondition: at least one ``Created`` shipment
	(mirrors the shipment-invoice gating). No mixing: refused while any
	shipment invoice is Pending/Uploaded. Re-uploading over an Uploaded
	order invoice is allowed (Noon supersedes the previous one)."""
	order = _lock_and_load_order(marketplace_order_name)
	_account_and_client(order)
	if not any(row.status == "Created" for row in order.noon_shipments or []):
		frappe.throw(_("The order needs at least one Created shipment before uploading an order invoice."))
	if order.noon_order_invoice_status == "Pending":
		frappe.throw(_("An order invoice upload is already pending."))
	if _shipment_invoices_block_order_invoice(order):
		frappe.throw(
			_(
				"A shipment invoice is already Pending or Uploaded on this order; "
				"an order invoice cannot be mixed with it."
			)
		)
	invoice_nr = cstr(invoice_nr).strip()
	if not invoice_nr or not issued_at:
		frappe.throw(_("Invoice Nr and Issued At are required."))
	_validate_invoice_file(file_url)

	order.noon_order_invoice_file = file_url
	order.noon_order_invoice_nr = invoice_nr
	order.noon_order_invoice_issued_at = issued_at
	order.noon_order_invoice_idempotency_key = frappe.generate_hash(length=32)
	order.noon_order_invoice_status = "Pending"
	order.noon_order_invoice_last_error = None
	order.save()
	frappe.db.commit()

	job = frappe.enqueue(
		run_upload_order_invoice,
		queue="long",
		timeout=300,
		job_id=_order_invoice_job_id(order.name, order.noon_order_invoice_idempotency_key),
		deduplicate=True,
		marketplace_order_name=order.name,
		invoice_idempotency_key=order.noon_order_invoice_idempotency_key,
	)
	return {"queued": job is not None}


def _set_order_invoice_error(order, message):
	order.noon_order_invoice_status = "Error"
	order.noon_order_invoice_last_error = message
	order.save()
	frappe.db.commit()


def run_upload_order_invoice(marketplace_order_name, invoice_idempotency_key):
	"""Worker for ``upload_order_invoice()``. Releases the order lock around
	the File read and Noon call and re-checks ``invoice_idempotency_key`` on
	re-acquisition, exactly like ``run_upload_invoice()``. Errors go to the
	dedicated ``noon_order_invoice_last_error`` field."""
	frappe.set_user("Administrator")
	order = _lock_and_load_order(marketplace_order_name)
	if (
		not order.noon_order_invoice_file
		or order.noon_order_invoice_idempotency_key != invoice_idempotency_key
		or order.noon_order_invoice_status not in ("Pending", "Error")
	):
		frappe.db.commit()
		return
	if not any(row.status == "Created" for row in order.noon_shipments or []):
		message = _("The order no longer has a Created shipment.")
		_set_order_invoice_error(order, message)
		_log_fulfillment(order, "UploadOrderInvoice", "Error", {}, message)
		return
	try:
		_account, client = _account_and_client(order)
	except Exception:
		_set_order_invoice_error(order, cstr(frappe.get_traceback()).strip()[:500])
		_log_fulfillment(order, "UploadOrderInvoice", "Error", {}, traceback_text=frappe.get_traceback())
		return
	invoice_file = order.noon_order_invoice_file
	invoice_nr = order.noon_order_invoice_nr
	issued_at = (
		get_datetime(order.noon_order_invoice_issued_at).strftime("%Y-%m-%d")
		if order.noon_order_invoice_issued_at
		else ""
	)
	fbpi_order_nr = order.external_order_id
	request_metadata = {"fbpi_order_nr": fbpi_order_nr, "invoice_nr": invoice_nr, "issued_at": issued_at}
	frappe.db.commit()  # release the order lock before reading the File and calling Noon

	try:
		file_doc = frappe.get_doc("File", {"file_url": invoice_file})
		raw_bytes = file_doc.get_content()
		if isinstance(raw_bytes, str):
			raw_bytes = raw_bytes.encode()
		encoded = base64.b64encode(raw_bytes).decode()
		response = client.request(
			"POST",
			FBPI_ORDER_INVOICE_UPLOAD_PATH,
			json={
				"fbpi_order_nr": fbpi_order_nr,
				"invoice": encoded,
				"invoice_nr": invoice_nr,
				"issued_at": issued_at,
				"idempotency_key": invoice_idempotency_key,
			},
		)
	except Exception:
		order = _lock_and_load_order(marketplace_order_name)
		if order.noon_order_invoice_idempotency_key == invoice_idempotency_key:
			order.noon_order_invoice_status = "Error"
			order.noon_order_invoice_last_error = cstr(frappe.get_traceback()).strip()[:500]
			order.save()
		frappe.db.commit()
		# Never include the base64 payload in the Sync Log.
		_log_fulfillment(
			order, "UploadOrderInvoice", "Error", request_metadata, traceback_text=frappe.get_traceback()
		)
		return

	order = _lock_and_load_order(marketplace_order_name)
	if order.noon_order_invoice_idempotency_key != invoice_idempotency_key:
		# A deliberate re-upload (fresh key) superseded this call in flight.
		frappe.db.commit()
		return

	if response.status_code != 200:
		_set_order_invoice_error(order, client.error_message(response)[:500])
		_log_fulfillment(order, "UploadOrderInvoice", "Error", request_metadata, client.error_message(response))
		return

	order.noon_order_invoice_status = "Uploaded"
	order.noon_order_invoice_last_error = None
	order.save()
	frappe.db.commit()
	_log_fulfillment(order, "UploadOrderInvoice", "Success", request_metadata)


def retry_order_invoice(marketplace_order_name):
	"""Re-enqueue a Pending/Error order invoice upload with its saved key."""
	order = _lock_and_load_order(marketplace_order_name)
	if order.noon_order_invoice_status not in ("Pending", "Error") or not order.noon_order_invoice_file:
		frappe.throw(_("The order invoice has nothing to retry."))
	job = frappe.enqueue(
		run_upload_order_invoice,
		queue="long",
		timeout=300,
		job_id=_order_invoice_job_id(order.name, order.noon_order_invoice_idempotency_key),
		deduplicate=True,
		marketplace_order_name=order.name,
		invoice_idempotency_key=order.noon_order_invoice_idempotency_key,
	)
	return {"queued": job is not None}


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


def retry(marketplace_order_name, integration_shipment_nr):
	"""Re-run a Pending/Error shipment's last action (#146 decision 20)."""
	order = _lock_and_load_order(marketplace_order_name)
	shipment = _find_shipment_row(order, integration_shipment_nr)
	if not shipment:
		frappe.throw(_("Shipment {0} was not found.").format(integration_shipment_nr))

	if shipment.status in ("Pending", "Error"):
		job = frappe.enqueue(
			run_create_shipment,
			queue="long",
			timeout=600,
			job_id=f"{RQ_JOB_PREFIX}-{order.name}-{integration_shipment_nr}",
			deduplicate=True,
			marketplace_order_name=order.name,
			integration_shipment_nr=integration_shipment_nr,
		)
		return {"queued": job is not None}
	if shipment.status == "Cancel Pending":
		job = frappe.enqueue(
			run_cancel_shipment,
			queue="long",
			timeout=300,
			job_id=f"{RQ_JOB_PREFIX}-{order.name}-{integration_shipment_nr}-cancel",
			deduplicate=True,
			marketplace_order_name=order.name,
			integration_shipment_nr=integration_shipment_nr,
		)
		return {"queued": job is not None}

	pending_awb = next(
		(
			row
			for row in order.noon_shipment_awbs or []
			if row.integration_shipment_nr == integration_shipment_nr
			and row.status == "Pending"
			and row.source == "Own Courier"
		),
		None,
	)
	if pending_awb and shipment.status == "Created":
		job = frappe.enqueue(
			run_add_awb,
			queue="long",
			timeout=300,
			job_id=f"{RQ_JOB_PREFIX}-{order.name}-{integration_shipment_nr}-awb-{pending_awb.awb_nr}",
			deduplicate=True,
			marketplace_order_name=order.name,
			integration_shipment_nr=integration_shipment_nr,
			awb_nr=pending_awb.awb_nr,
		)
		return {"queued": job is not None}

	if shipment.status == "Created" and shipment.invoice_status in ("Pending", "Error"):
		job = frappe.enqueue(
			run_upload_invoice,
			queue="long",
			timeout=300,
			job_id=_invoice_job_id(order.name, integration_shipment_nr, shipment.invoice_idempotency_key),
			deduplicate=True,
			marketplace_order_name=order.name,
			integration_shipment_nr=integration_shipment_nr,
			invoice_idempotency_key=shipment.invoice_idempotency_key,
		)
		return {"queued": job is not None}

	frappe.throw(_("Shipment {0} has nothing to retry.").format(integration_shipment_nr))
