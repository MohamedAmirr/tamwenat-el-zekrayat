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
from frappe.query_builder.functions import Lower
from frappe.utils import cstr, escape_html, flt, get_datetime, now_datetime, strip_html

from marketplace_integrations.integrations.noon import handover_state
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
from marketplace_integrations.integrations.noon.expected_change import record_out_of_stock
from marketplace_integrations.integrations.noon.orders import get_order as get_fbpi_order
from marketplace_integrations.integrations.noon.orders import import_order
from marketplace_integrations.services.delivery_note_drafts import create_draft_delivery_note, submit_note
from marketplace_integrations.services.mappings import resolve_item, resolve_mapping_and_item
from marketplace_integrations.services.noon_sales_invoice import queue_sales_invoice

RQ_JOB_PREFIX = "marketplace-noon-fulfillment"
# The only courier: the value Noon documents for AWBs it generated.
NOON_COURIER = "noon"

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


def item_names_by_sku(order, skus):
	"""``{sku: Item.item_name}`` for the SKUs that have an enabled product mapping, one lookup per
	distinct SKU. Noon's order payload carries no product title, so an unmapped SKU is simply
	absent (callers show the SKU alone)."""
	names = {}
	for sku in {cstr(sku).strip() for sku in skus if cstr(sku).strip()}:
		resolved = resolve_mapping_and_item(order.marketplace_account, sku)
		item = resolved and resolved.get("item")
		item_name = frappe.db.get_value("Item", item, "item_name") if item else None
		if item_name:
			names[sku] = item_name
	return names


def unit_label(sku, names):
	"""``Item name (SKU)``, or the SKU alone when it is not mapped."""
	sku = cstr(sku).strip()
	name = names.get(sku)
	return f"{name} ({sku})" if name else sku


def shipment_number(integration_shipment_nr):
	"""The ``n`` of ``<fbpi_order_nr>-<n>``."""
	return cstr(integration_shipment_nr).rsplit("-", 1)[-1]


def shipment_items(order, integration_shipment_nrs=None, names=None):
	"""``{shipment_nr: [{"label": "Item name (SKU)", "qty": n}, ...]}`` from the shipment unit
	rows, grouped by SKU in first-seen order."""
	rows = [
		row
		for row in order.noon_shipment_units or []
		if integration_shipment_nrs is None or row.integration_shipment_nr in integration_shipment_nrs
	]
	if names is None:
		names = item_names_by_sku(order, [row.partner_sku for row in rows])
	grouped = {}
	for row in rows:
		sku = cstr(row.partner_sku).strip()
		entry = grouped.setdefault(row.integration_shipment_nr, {}).setdefault(
			sku, {"label": unit_label(sku, names), "qty": 0}
		)
		entry["qty"] += 1
	return {nr: list(by_sku.values()) for nr, by_sku in grouped.items()}


def _unit_skus(order):
	"""``{mp_item_nr: partner_sku}`` from the shipment unit rows and the latest payload."""
	skus = {}
	for item in _latest_payload_items(order):
		if isinstance(item, dict):
			skus[cstr(item.get("mp_item_nr")).strip()] = cstr(item.get("partner_sku")).strip()
	for row in order.noon_shipment_units or []:
		skus[cstr(row.mp_item_nr).strip()] = cstr(row.partner_sku).strip()
	return skus


def _unit_text(order, mp_item_nr):
	"""Readable name of one unit for error messages: ``Item name (SKU)``; falls back to the
	``mp_item_nr`` only when the SKU is unknown."""
	mp_item_nr = cstr(mp_item_nr).strip()
	sku = _unit_skus(order).get(mp_item_nr)
	if not sku:
		return mp_item_nr
	return unit_label(sku, item_names_by_sku(order, [sku]))


def get_shippable_units(order, include_pending_oos=False):
	"""Return ``[{"mp_item_nr": ..., "partner_sku": ..., "item_name": ...}, ...]`` (``item_name``
	only for a SKU with a product mapping) for units
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
	names = item_names_by_sku(order, [unit["partner_sku"] for unit in shippable])
	for unit in shippable:
		if unit["partner_sku"] in names:
			unit["item_name"] = names[unit["partner_sku"]]
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
		# cancel_shipment() refuses while a Delivery Note of the shipment is live.
		if any(not _live_delivery_notes(order, row) for row in created):
			actions += ["cancel_shipment", "cancel_on_noon"]
		actions.append("add_awb")
	# mark_handed_over(): a Created shipment with a draft Delivery Note to submit or units no live
	# Delivery Note covers yet (#390). It submits the note, so the session user needs Delivery Note
	# submit permission; the server checks that too.
	if any(_can_mark_handed_over(order, row) for row in created) and frappe.has_permission(
		"Delivery Note", "submit"
	):
		actions.append("mark_handed_over")
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
	# AWB added through Add AWB or a Pending/Error invoice.
	created_nrs = {row.integration_shipment_nr for row in created}
	if (
		statuses & {"Pending", "Error", "Cancel Pending"}
		or any(
			row.integration_shipment_nr in created_nrs and row.status == "Pending"
			for row in order.noon_shipment_awbs or []
		)
		or any(row.invoice_status in ("Pending", "Error") for row in created)
	):
		actions.append("retry")
	if created and order.noon_order_invoice_status in ("Pending", "Error"):
		actions.append("retry_order_invoice")

	if any(
		row.status in _DISCARDABLE_SHIPMENT_STATUSES and not _live_delivery_notes(order, row) for row in shipments
	):
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


def _with_readable_keys(order, request_data):
	"""Adds ``order_reference`` and, when the call names a shipment, ``shipment_label`` and
	``items`` next to the existing keys (which other code parses and which stay as they are)."""
	readable = {"order_reference": order.platform_order_reference or ""}
	shipment_nr = request_data.get("integration_shipment_nr")
	if shipment_nr:
		readable["shipment_label"] = f"Shipment {shipment_number(shipment_nr)}"
		readable["items"] = [
			f"{item['label']} x{item['qty']}" if item["qty"] > 1 else item["label"]
			for item in shipment_items(order, {shipment_nr}).get(shipment_nr, [])
		]
	return {**readable, **request_data}


def _log_fulfillment(order, action, status, request_data=None, response_data=None, traceback_text=None):
	"""One committed Marketplace Sync Log row per Noon fulfillment call
	(#146 decision 21). Never includes invoice bytes - callers are
	responsible for keeping ``request_data`` free of them."""
	now = now_datetime()
	request_data = _with_readable_keys(order, request_data or {})
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
			"request_data": frappe.as_json({"action": action, **request_data}, ensure_ascii=False),
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


AWB_NR_MAX_LENGTH = 140  # Data column length of both AWB Nr fields


def _awb_already_used_message(awb_nr):
	# Deliberately does not name the other order.
	return _("AWB {0} is already in use.").format(frappe.bold(frappe.utils.escape_html(awb_nr)))


def _pool_awb_exists_ignoring_case(marketplace_account, awb_nr):
	noon_awb = frappe.qb.DocType("Noon AWB")
	return bool(
		frappe.qb.from_(noon_awb)
		.select(noon_awb.name)
		.where(noon_awb.marketplace_account == marketplace_account)
		.where(Lower(noon_awb.awb_nr) == awb_nr.lower())
		.limit(1)
		.run()
	)


def _record_typed_awb(order, account, awb_nr, integration_shipment_nr):
	"""Reserve a sticker AWB typed or scanned by the user (the order is locked).

	Rejected, ignoring letter case, when the AWB is already on any shipment AWB row of an
	order on the same account (any shipment status) or on a ``Noon AWB`` pool row of the
	account (Available or Used). Otherwise it is inserted as a ``Used`` ``Noon AWB`` row,
	so the pool's unique key rejects an exact-case simultaneous request on another order
	at DB level. Residual gap: two simultaneous requests differing only in letter case are
	not blocked at DB level (the identity key stays case-sensitive so existing pool rows
	keep matching their own key)."""
	from marketplace_integrations.marketplace_integrations.doctype.noon_awb.noon_awb import (
		compute_awb_identity_key,
	)

	if len(awb_nr) > AWB_NR_MAX_LENGTH:
		frappe.throw(_("AWB Nr cannot be longer than {0} characters.").format(AWB_NR_MAX_LENGTH))
	lowered = awb_nr.lower()
	shipment_awb = frappe.qb.DocType("Marketplace Order Shipment AWB")
	parents = (
		frappe.qb.from_(shipment_awb)
		.select(shipment_awb.parent)
		.where(shipment_awb.parenttype == "Marketplace Order")
		.where(Lower(shipment_awb.awb_nr) == lowered)
		.run(pluck=True)
	)
	if parents and frappe.db.exists(
		"Marketplace Order", {"name": ["in", parents], "marketplace_account": order.marketplace_account}
	):
		frappe.throw(_awb_already_used_message(awb_nr))
	if _pool_awb_exists_ignoring_case(order.marketplace_account, awb_nr) or frappe.db.exists(
		"Noon AWB", {"awb_identity_key": compute_awb_identity_key(order.marketplace_account, awb_nr)}
	):
		frappe.throw(_awb_already_used_message(awb_nr))
	try:
		# ignore_permissions: the endpoint already checked write on the Marketplace Order,
		# and the pool's own generate() inserts Noon AWB rows the same way.
		frappe.get_doc(
			{
				"doctype": "Noon AWB",
				"marketplace_account": order.marketplace_account,
				"awb_nr": awb_nr,
				"country_code": account.noon_country_code,
				"status": "Used",
				"marketplace_order": order.name,
				"integration_shipment_nr": integration_shipment_nr,
				"used_on": now_datetime(),
			}
		).insert(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		# Counter-named doctype: the unique awb_identity_key collision is a
		# UniqueValidationError with a "must be unique" message queued.
		frappe.clear_messages()
		frappe.throw(_awb_already_used_message(awb_nr))


def create_shipment(marketplace_order_name, mp_item_nrs, awb_source="Noon", awb_nr=None):
	"""Request path: allocate a shipment number, reject any unit already in
	an active shipment, write Pending shipment/unit/AWB rows, commit, then
	enqueue the Noon write (#146 decision 13/14/20).

	``awb_nr`` is an optional AWB printed on a Noon sticker. When given (spaces-only
	counts as empty) it is recorded as the shipment's AWB; otherwise the AWB is
	claimed or fetched by the worker as before."""
	# Noon is the only courier: every new shipment takes a Noon AWB. Shipments
	# already stored with awb_source "Own Courier" stay readable and retryable.
	if awb_source != "Noon":
		frappe.throw(_("New shipments can only use a Noon AWB."))
	order = _lock_and_load_order(marketplace_order_name)
	account, _client = _account_and_client(order)

	mp_item_nrs = [cstr(nr).strip() for nr in (mp_item_nrs or []) if cstr(nr).strip()]
	if not mp_item_nrs:
		frappe.throw(_("At least one unit must be selected."))

	shippable = {row["mp_item_nr"]: row["partner_sku"] for row in get_shippable_units(order)}
	not_shippable = [nr for nr in mp_item_nrs if nr not in shippable]
	if not_shippable:
		frappe.throw(
			_("Units {0} are not shippable (already shipped, cancelled, or not yet acknowledged).").format(
				", ".join(_unit_text(order, nr) for nr in not_shippable)
			)
		)


	integration_shipment_nr = _next_shipment_number(order)
	awb_nr = cstr(awb_nr).strip()
	if awb_nr:
		_record_typed_awb(order, account, awb_nr, integration_shipment_nr)
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
	if awb_nr:
		order.append(
			"noon_shipment_awbs",
			{
				"integration_shipment_nr": integration_shipment_nr,
				"courier": NOON_COURIER,
				"awb_nr": awb_nr,
				"source": "Noon",
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
		# Our own pre-shipment refresh: echo_guard=False so it never consumes an expected-change
		# marker (services/echo_guard.py) that a later poll should compare.
		import_order(account, raw_order, bump_trigger_source="Manual", echo_guard=False)
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
			_fail_shipment(order, shipment, _("Unit {0} is no longer shippable.").format(_unit_text(order, unit.mp_item_nr)))
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
	# once, before CreateShipment, and reused on every retry. A row may already
	# exist at this point because the user typed the AWB from a Noon sticker in
	# create_shipment(); then the pool claim and GetNoonLogisticsAWBs are skipped
	# and every retry resends that same AWB.
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
				"courier": NOON_COURIER,
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
				"Shipment status changed to {0} before the shipment was created on Noon; "
				"stopped so no untracked shipment is created on Noon."
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
	_refuse_while_delivery_note_is_live(order, shipment)
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
	handover_state.clear_entries(shipment)  # a cancelled shipment no longer owes a Delivery Note
	order.save()
	frappe.db.commit()
	_log_fulfillment(order, "CancelShipment", "Success", {"integration_shipment_nr": integration_shipment_nr})


# ---------------------------------------------------------------------------
# Cancelling the Sales Order: wait for Noon (#376, docs/noon-integration/sales-order-cancel.md)
# ---------------------------------------------------------------------------


def sales_order_cancel_blockers(order):
	"""Translated reasons the linked Sales Order may not be cancelled yet; empty when it may.

	Every shipment that is not Cancelled or Discarded blocks: a handed-over one (``handed_over_on``
	or a live Delivery Note) with the "cancel on Noon, then the Delivery Note" instruction, any
	other (Pending, Created, Cancel Pending, Error) with its status. Shipment numbers come from
	Noon-sourced text and are escaped, the reasons are thrown into HTML."""
	blockers = []
	for shipment in order.noon_shipments or []:
		if shipment.status not in _ACTIVE_SHIPMENT_STATUSES:
			continue
		nr = escape_html(cstr(shipment.integration_shipment_nr))
		if _handed_over(order, shipment):
			blockers.append(
				_(
					"Shipment {0} was handed over. Cancel it on Noon first, then cancel the linked Delivery Note, then cancel the Sales Order."
				).format(nr)
			)
		else:
			blockers.append(
				_(
					"Shipment {0} is {1} on Noon. Use Cancel on Noon on the Sales Order and cancel the Sales Order once every shipment is Cancelled."
				).format(nr, _(shipment.status))
			)
	return blockers


def get_cancel_on_noon_preview(order):
	"""Read-only text source of the Cancel on Noon dialog: the Created shipments that will be
	cancelled (``created``), those skipped because a Delivery Note is live (``skipped``), each with
	its items, and the reasons the Sales Order cancel is still refused (``blockers``)."""
	items = shipment_items(order)
	created, skipped = [], []
	for shipment in order.noon_shipments or []:
		if shipment.status != "Created":
			continue
		nr = shipment.integration_shipment_nr
		entry = {"integration_shipment_nr": nr, "items": items.get(nr, [])}
		(skipped if _live_delivery_notes(order, shipment) else created).append(entry)
	return {"created": created, "skipped": skipped, "blockers": sales_order_cancel_blockers(order)}


def cancel_created_shipments(marketplace_order_name):
	"""Queue the existing ``cancel_shipment`` for every Created shipment of the order.

	Units not in a shipment are not touched and nothing is marked out of stock. A shipment with a
	live Delivery Note is skipped (``cancel_shipment`` refuses it). One shipment failing to queue does
	not stop the others. Returns ``{"queued": [nr], "skipped": [nr], "failed": [{nr, error}]}``;
	each ``cancel_shipment`` call commits, so what was queued stays queued."""
	order = _lock_and_load_order(marketplace_order_name)
	candidates, skipped = [], []
	for shipment in order.noon_shipments or []:
		if shipment.status != "Created":
			continue
		nr = shipment.integration_shipment_nr
		(skipped if _live_delivery_notes(order, shipment) else candidates).append(nr)
	frappe.db.commit()
	queued, failed = [], []
	for nr in candidates:
		try:
			cancel_shipment(marketplace_order_name, nr)
			queued.append(nr)
		except Exception as exc:
			frappe.db.rollback()
			failed.append({"integration_shipment_nr": nr, "error": strip_html(cstr(exc)).strip()})
	return {"queued": queued, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------------------
# Mark handed over (#368)
# ---------------------------------------------------------------------------


def _live_delivery_notes(order, shipment):
	"""Live (draft or submitted) Delivery Notes of the shipment; see ``handover_state.live_notes``."""
	return handover_state.live_notes(order.noon_shipment_units, shipment.integration_shipment_nr)


def _handed_over(order, shipment):
	"""True once the shipment has a live Delivery Note or was recorded as handed over (its units
	were already covered by a Delivery Note someone else made)."""
	return bool(_live_delivery_notes(order, shipment) or shipment.handed_over_on)


def _refuse_while_delivery_note_is_live(order, shipment):
	live = _live_delivery_notes(order, shipment)
	if live:
		frappe.throw(
			_(
				"Shipment {0} still has a live Delivery Note ({1}). Delete or cancel that Delivery Note first."
			).format(shipment.integration_shipment_nr, ", ".join(live))
		)


def _uncovered_rows(order, integration_shipment_nr):
	"""Unit rows of the shipment that no live Delivery Note covers."""
	rows = handover_state.unit_rows(order.noon_shipment_units, integration_shipment_nr)
	return [row for row in rows if not handover_state.is_covered(row)]


def _can_mark_handed_over(order, shipment):
	"""Mark Handed Over has something to do: a draft to submit, or uncovered units to add.  Nothing
	when the shipment's note is already submitted (a unit that is left then goes to Needs Attention)
	or when it was recorded as handed over with nothing unresolved."""
	nr = shipment.integration_shipment_nr
	units = order.noon_shipment_units
	if handover_state.draft_note(units, nr):
		return True
	if handover_state.submitted_note(units, nr):
		return False
	if not _uncovered_rows(order, nr):
		return False
	return not shipment.handed_over_on or bool(handover_state.load_entries(shipment))


def _shipment_units(order, integration_shipment_nr):
	return [
		{"external_sku": cstr(row.partner_sku).strip(), "qty": 1}
		for row in order.noon_shipment_units or []
		if row.integration_shipment_nr == integration_shipment_nr
	]


def _row_units(rows):
	"""Service units for unit rows; ``row`` lets ``_draft_and_record`` link the row to the note."""
	return [{"external_sku": cstr(row.partner_sku).strip(), "qty": 1, "row": row} for row in rows]


def _entry_units(entries):
	"""Units of the stored entries.  An entry with an SKU is resolved again (the mapping may have
	changed since); one without an SKU keeps its Item."""
	return [
		{"external_sku": e["external_sku"], "qty": e.get("qty")}
		if e.get("external_sku")
		else {"item": e.get("item") or "", "qty": e.get("qty")}
		for e in entries
	]


def _entry_unit_rows(order, shipment, entries):
	"""Units of the stored entries, each backed by an uncovered unit row of the shipment with the
	same SKU where there is one (so the row can be linked to the note).  Entry quantity beyond the
	rows stays a plain unit, as before."""
	free = _uncovered_rows(order, shipment.integration_shipment_nr)
	units = []
	for entry in entries:
		sku = cstr(entry.get("external_sku")).strip()
		need = int(flt(entry.get("qty")))
		matching = [row for row in free if cstr(row.partner_sku).strip() == sku][:need]
		for row in matching:
			free.remove(row)
		units.extend(_row_units(matching))
		if need > len(matching):
			rest = dict(entry, qty=need - len(matching))
			units.extend(_entry_units([rest]))
	return units


def _entry_key(entry):
	return entry.get("external_sku") or entry.get("item") or ""


def _expected_units(order):
	"""``{item: units}`` the order's Created shipments hold, resolved through the mappings."""
	counts = {}
	resolved = {}
	for shipment in order.noon_shipments or []:
		if shipment.status != "Created":
			continue
		for unit in _shipment_units(order, shipment.integration_shipment_nr):
			sku = unit["external_sku"]
			if sku not in resolved:
				resolved[sku] = resolve_item(order.marketplace_account, sku)
			if resolved[sku]:
				counts[resolved[sku]] = counts.get(resolved[sku], 0) + 1
	return counts


def _link_units(order, units, delivered, note):
	"""Record ``note`` on the unit rows that the service really put on it (in memory).

	``delivered`` is ``{item: qty}``; each row takes one of its Item's quantity, in order, so units
	the service left out (unmapped, not on the order, excess, covered by someone else's note) stay
	unlinked and uncovered.
	"""
	budget = dict(delivered)
	items = {}
	for unit in units:
		row = unit.get("row")
		if row is None:
			continue
		sku = unit["external_sku"]
		if sku not in items:
			items[sku] = resolve_item(order.marketplace_account, sku)
		item = items[sku]
		if item and flt(budget.get(item)) >= 1:
			handover_state.record_note([row], note)
			budget[item] = flt(budget[item]) - 1


def _draft_and_record(order, shipment, units, after_sync=False, queue_sync=True):
	"""Run the shared service for ``units`` and update the shipment's records (in memory).

	Rows are added to the shipment's draft Delivery Note when it has one, else a new draft is
	created; the callers never call this for a shipment whose note is already submitted (one note
	per shipment, #390).  Each unit row that really went on the note gets it as ``delivery_note``.
	Units that a Delivery Note already covers (the Sales Order is not behind the platform order)
	count as resolved.  Replaces the unresolved record of the SKUs/Items in ``units`` with what is
	still unresolved (records of other SKUs stay): an entry becomes ``final`` (kept in Needs Action)
	when its reason is ``unmapped``, it already was final, or ``after_sync`` says the Sales Order
	sync it waited for has run.  Returns the service result.
	"""
	previous_entries = handover_state.load_entries(shipment)
	previous = {_entry_key(e): e for e in previous_entries}
	add_to = handover_state.draft_note(order.noon_shipment_units, shipment.integration_shipment_nr)
	result = create_draft_delivery_note(
		order, units, queue_sync=queue_sync, expected_units=_expected_units(order), add_to=add_to
	)
	if result["delivery_note"]:
		_link_units(order, units, result["delivered"], result["delivery_note"])
	if (result["delivery_note"] or result["covered"]) and not shipment.handed_over_on:
		shipment.handed_over_on = now_datetime()
	passed = {u.get("external_sku") or u.get("item") or "" for u in units}
	remaining = [e for e in previous_entries if _entry_key(e) not in passed]
	for entry in result["unresolved"]:
		prior = previous.get(_entry_key(entry))
		entry["final"] = bool(
			entry["reason"] == handover_state.UNMAPPED or after_sync or (prior and prior.get("final"))
		)
		remaining.append(entry)
	handover_state.store_entries(shipment, remaining)
	return result


def _submit_shipment_draft(order, integration_shipment_nr, draft):
	"""Save ``order``, submit the shipment's draft Delivery Note and record the outcome.
	Returns ``(order, error)``: ``order`` is a fresh copy, ``error`` is None on success.

	The order is saved first so the unit links and records are stored.  The submit runs in a
	savepoint (``submit_note``), so a failure (for example not enough stock) leaves the draft
	exactly as it was; the escaped error is stored in ``delivery_note_error`` and the next
	trigger tries again.  A submit queues a Sales Order re-check (``doc_events`` on the Delivery
	Note) that writes the order row directly, so the order is loaded again before
	``delivery_note_error`` is written: saving the stale copy would undo that queued re-check.
	"""
	order.flags.skip_sync_status_update = True
	order.save()
	error = None
	try:
		submit_note(draft)
	except Exception as exc:
		message = escape_html(strip_html(cstr(exc)).strip() or _("The Delivery Note could not be submitted."))
		error = message[:500]
	order = frappe.get_doc("Marketplace Order", order.name)
	shipment = _find_shipment_row(order, integration_shipment_nr)
	if (shipment.delivery_note_error or None) != error:
		shipment.delivery_note_error = error
		order.flags.skip_sync_status_update = True
		order.save()
	if not error and order.erpnext_sales_order:
		# #377: a submitted note goes onto the order's draft Sales Invoice (never for a draft note).
		queue_sales_invoice(order.name, order.erpnext_sales_order)
	return order, error


def mark_handed_over(marketplace_order_name, integration_shipment_nr):
	"""Manual "all shipped" override: put every uncovered unit of a Created shipment on its draft
	Delivery Note, then submit it (#390; was draft-only in #368).

	Noon's checked-in API has no handover endpoint, so this makes no Noon call.  The user needs
	Delivery Note submit permission (checked here).  Every uncovered unit (SHIPPED or not) goes to
	the shipment's draft through the shared service (``services/delivery_note_drafts.py``); the
	draft, including one an earlier import made, is then submitted.  A submit failure keeps the
	draft, stores the error on the shipment (``delivery_note_error``) and is returned as
	``submit_error``.  Idempotent: a shipment with nothing uncovered and no draft returns its
	existing note and creates nothing.  A shipment whose note is already submitted but still has
	uncovered units is refused (one note per shipment).  Units the service could not draft are
	stored on the shipment (``handover_state``) and keep the order in Needs Action until they are
	drafted.  Deleting the draft uncovers its units.  Runs synchronously under the order lock.
	"""
	frappe.has_permission("Delivery Note", "submit", throw=True)
	order = _lock_and_load_order(marketplace_order_name)
	shipment = _find_shipment_row(order, integration_shipment_nr)
	if not shipment or shipment.status != "Created":
		frappe.throw(_("Shipment {0} is not in a state that can be marked handed over.").format(integration_shipment_nr))
	units = order.noon_shipment_units
	rows = _uncovered_rows(order, integration_shipment_nr)
	draft = handover_state.draft_note(units, integration_shipment_nr)
	if rows and not draft:
		submitted = handover_state.submitted_note(units, integration_shipment_nr)
		if submitted:
			frappe.throw(
				_("Delivery Note {0} of shipment {1} is already submitted, so the remaining units cannot be added to it.").format(
					submitted, integration_shipment_nr
				)
			)
	if not rows and not draft:
		live = _live_delivery_notes(order, shipment)
		return {
			"delivery_note": live[0] if live else None,
			"created": False,
			"submitted": False,
			"submit_error": None,
			"unresolved": [dict(e, text=handover_state.entry_text(e)) for e in handover_state.load_entries(shipment)],
		}

	result = _draft_and_record(order, shipment, _row_units(rows)) if rows else None
	draft = handover_state.draft_note(units, integration_shipment_nr)
	submit_error = None
	if draft:
		order, submit_error = _submit_shipment_draft(order, integration_shipment_nr, draft)
		shipment = _find_shipment_row(order, integration_shipment_nr)
	else:
		# The status is kept by handover_state.reassert() on this save; skip the re-plan so it
		# cannot run before the record is final.
		order.flags.skip_sync_status_update = True
		order.save()
	frappe.db.commit()
	return {
		"delivery_note": draft,
		"created": bool(result and result["delivery_note"]) and not result["added_to_existing"],
		"added_to_existing": bool(result and result["added_to_existing"]),
		"submitted": bool(draft) and not submit_error,
		"submit_error": submit_error,
		"unresolved": [dict(e, text=handover_state.entry_text(e)) for e in handover_state.load_entries(shipment)],
	}


def _sales_order_submitted(order):
	return bool(
		order.erpnext_sales_order and frappe.db.get_value("Sales Order", order.erpnext_sales_order, "docstatus") == 1
	)


def queue_shipped_units(marketplace_order_name):
	"""Queue ``process_shipped_units`` for one order (deduplicated per order, after commit)."""
	return frappe.enqueue(
		process_shipped_units,
		queue="long",
		timeout=600,
		enqueue_after_commit=True,
		job_id=f"{RQ_JOB_PREFIX}-{marketplace_order_name}-shipped",
		deduplicate=True,
		marketplace_order=marketplace_order_name,
	)


def queue_shipped_units_if_needed(order):
	"""Called by ``import_order`` after the order is saved: queue ``process_shipped_units`` when a
	Created shipment has a SHIPPED unit no live Delivery Note covers, or all its active units are
	SHIPPED while its note is still a draft (#390)."""
	payload = handover_state.payload_items(order.latest_payload)
	if handover_state.shipped_work_pending(payload, order.noon_shipments or [], order.noon_shipment_units or []):
		queue_shipped_units(order.name)
	if order.get("noon_sales_invoice_error") and order.erpnext_sales_order:
		queue_sales_invoice(order.name, order.erpnext_sales_order)  # #377: retry a failed invoice


def process_shipped_units(marketplace_order):
	"""Background job (#390): one Delivery Note per shipment from the units Noon reports SHIPPED.

	Per Created shipment: SHIPPED units that no live note covers go onto the shipment's draft (the
	first ones create it); when every active unit (not cancelled, not out of stock) is SHIPPED the
	draft is submitted.  A shipment whose note is already submitted gets no second note; its
	leftover SHIPPED units show in Needs Attention (``services/marketplace_attention.py``).  Waits
	(does nothing) until the Sales Order is submitted; the next import queues it again.

	Runs as the system, like ``retry_unresolved_handovers``: it is queued by the import or the
	webhook, detached from any session, and must create and submit Delivery Notes whatever the
	user whose action triggered the import may do.  Each shipment runs in its own savepoint; a
	failure is logged and the shipment is retried by the next trigger.
	"""
	if not frappe.db.exists("Marketplace Order", marketplace_order):
		return
	frappe.set_user("Administrator")
	order = _lock_and_load_order(marketplace_order)
	if not _sales_order_submitted(order):
		frappe.db.commit()
		return
	payload = handover_state.payload_items(order.latest_payload)
	for nr in [row.integration_shipment_nr for row in order.noon_shipments or [] if row.status == "Created"]:
		savepoint = "noon_shipped_units"
		frappe.db.savepoint(savepoint)
		try:
			order = _process_shipped_shipment(order, nr, payload)
			frappe.db.release_savepoint(savepoint)
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			frappe.log_error(
				title="Noon shipped units failed", message=f"{order.name} {nr}\n{frappe.get_traceback()}"
			)
			order = frappe.get_doc("Marketplace Order", marketplace_order)
	frappe.db.commit()


def _process_shipped_shipment(order, integration_shipment_nr, payload):
	"""Draft the shipment's uncovered SHIPPED units and submit when all active units are SHIPPED.
	Saves the order when it changed anything and returns it (a fresh copy after a submit, see
	``_submit_shipment_draft``)."""
	nr = integration_shipment_nr
	shipment = _find_shipment_row(order, nr)
	units = order.noon_shipment_units
	plan = handover_state.shipment_plan(payload, handover_state.unit_rows(units, nr))
	changed = False
	draft = handover_state.draft_note(units, nr)
	shipped = [row for row in plan["shipped"] if not handover_state.is_covered(row)]
	if shipped and (draft or not handover_state.submitted_note(units, nr)):
		_draft_and_record(order, shipment, _row_units(shipped))
		draft = handover_state.draft_note(units, nr)
		changed = True
	if draft and plan["all_shipped"]:
		order, _error = _submit_shipment_draft(order, nr, draft)
		return order
	if not draft and shipment.delivery_note_error:
		shipment.delivery_note_error = None  # the draft is gone, so there is nothing left to retry
		changed = True
	if changed:
		order.flags.skip_sync_status_update = True
		order.save()
	return order


def retry_unresolved_handovers(marketplace_order, after_sync=False):
	"""Background job: draft the unresolved handover units of a Noon order again (#368).

	Queued after a Marketplace Product Mapping save and after a Sales Order sync of an order with
	an unresolved record.  Runs as the system (the user who handed the shipment over already
	passed the Delivery Note permission check).  Only shipments that were handed over (live
	Delivery Note or recorded handover) are retried, plus those whose record covers every unit
	(nothing was ever drafted): one whose Delivery Note was deleted waits for a manual Mark Handed
	Over, because drafting only the leftover units would hide the rest.  Queues a sync only when
	not after one (``after_sync`` false), so the chain sync -> retry -> sync cannot loop.
	``after_sync`` marks entries that are still unresolved as final, so the order goes to Needs
	Action.  Each shipment is retried in its own savepoint: a failure is logged and the shipment
	keeps its record.  When the Sales Order is not submitted a retry cannot help, so the entries
	become final at once.  #390: units are added to the shipment's draft only; once its note is
	submitted nothing is added (one note per shipment) and the entries become final, so the order
	shows them in Needs Action.  A retry never submits.
	"""
	if not frappe.db.exists("Marketplace Order", marketplace_order):
		return
	frappe.set_user("Administrator")
	order = _lock_and_load_order(marketplace_order)
	sales_order_docstatus = (
		frappe.db.get_value("Sales Order", order.erpnext_sales_order, "docstatus") if order.erpnext_sales_order else None
	)
	changed = False
	for shipment in handover_state.active_shipments(order):
		entries = handover_state.load_entries(shipment)
		if not entries or shipment.status != "Created":
			continue
		nr = shipment.integration_shipment_nr
		if sales_order_docstatus != 1:
			for entry in entries:
				entry["final"] = True
			handover_state.store_entries(shipment, entries)
			changed = True
			continue
		covers_all = sum(flt(e.get("qty")) for e in entries) >= len(_shipment_units(order, nr))
		if not _handed_over(order, shipment) and not covers_all:
			if after_sync:
				for entry in entries:
					entry["final"] = True
				handover_state.store_entries(shipment, entries)
				changed = True
			continue
		if not handover_state.draft_note(order.noon_shipment_units, nr) and handover_state.submitted_note(
			order.noon_shipment_units, nr
		):
			for entry in entries:
				entry["final"] = True
			handover_state.store_entries(shipment, entries)
			changed = True
			continue
		savepoint = "noon_handover_retry"
		frappe.db.savepoint(savepoint)
		try:
			_draft_and_record(
				order,
				shipment,
				_entry_unit_rows(order, shipment, entries),
				after_sync=after_sync,
				queue_sync=not after_sync,
			)
			frappe.db.release_savepoint(savepoint)
			changed = True
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			frappe.log_error(
				title="Noon handover retry failed",
				message=f"{order.name} {shipment.integration_shipment_nr}\n{frappe.get_traceback()}",
			)
	if changed:
		order.flags.skip_sync_status_update = True
		order.save()
	frappe.db.commit()


def orders_with_unresolved_handovers(marketplace_account=None):
	"""Names of Noon Marketplace Orders that have a shipment with an unresolved handover record."""
	parents = frappe.get_all(
		"Marketplace Order Shipment",
		filters={"parenttype": "Marketplace Order", "skipped_units": ["!=", ""]},
		pluck="parent",
		parent_doctype="Marketplace Order",
	)
	if not parents or not marketplace_account:
		return sorted(set(parents))
	return frappe.get_all(
		"Marketplace Order",
		filters={"name": ["in", list(set(parents))], "marketplace_account": marketplace_account},
		pluck="name",
	)


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
	_refuse_while_delivery_note_is_live(order, shipment)
	shipment.status = "Discarded"
	shipment.last_error = None
	handover_state.clear_entries(shipment)  # a discarded shipment no longer owes a Delivery Note
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
	# Noon is the only courier. Noon documents the value as lowercase 'noon'.
	if courier.lower() != NOON_COURIER:
		frappe.throw(_("Noon is the only supported courier."))
	courier = NOON_COURIER
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
			"source": "Noon",
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

		# #364 expected-change marker: UpdateOrder succeeded, so record what we sent. The own
		# refresh below passes echo_guard=False so it leaves the marker for a later poll.
		record_out_of_stock(order.name, mp_item_nrs)
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
				# Our own refresh: echo_guard=False leaves the expected-change marker for a later poll.
				import_order(account, raw_order, bump_trigger_source="Manual", echo_guard=False)
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
