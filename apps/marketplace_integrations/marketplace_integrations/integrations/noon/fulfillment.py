# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon FBPI order-level fulfillment: Mark Out of Stock and the order invoice (#146 section C).

Shipments are created, cancelled and refreshed from the Delivery Note
(``integrations/noon/delivery_note_shipment.py``, #415); the old Marketplace Order shipment flow, AWB pool,
handover chain and shipment child tables are gone. What stays here is the order-level actions plus the
helpers the Delivery Note adapter shares (``_lock_and_load_order``, ``_account_and_client``,
``_latest_payload_items``, ``_is_shipment_not_found``, ``_is_definite_refusal``, ``get_fbpi_order``).

Every request-path function here follows the same shape: lock the Marketplace Order row
(``frappe.db.get_value(..., for_update=True)``), reload it, validate and write whatever Pending state the
action needs, save through the parent document (bumping ``modified`` so ``check_if_latest()`` also guards
concurrent Desk edits), commit, then enqueue the actual Noon HTTP call on the ``long`` queue (#146 decision
10/20) - no Noon HTTP call ever happens inside a Desk request.

Every worker (``run_*``) re-acquires the same row lock before it writes the outcome back, for the identical
reason ``integrations/noon/orders.py``'s ``import_order()`` does (see that module's docstring): without it, a
Noon poll running concurrently could load a stale in-memory copy of this order and silently overwrite what
this module just wrote.

**#146 round-3 review finding #3 (lock duration):** a worker that calls several Noon HTTP endpoints in
sequence (``run_mark_out_of_stock()``) does NOT hold the order row lock across any of those HTTP calls - each
holds the lock only long enough to read what it needs or write a result, committing (which releases the
lock) immediately before every Noon call and re-acquiring it immediately after that call returns.

**Round-5 fix (review finding #1):** every lock acquisition in this module goes through
``_lock_and_load_order()``, which commits immediately before its own ``FOR UPDATE`` lock (see that
function's docstring) - so the next lock always re-establishes a fresh read view no matter what read
happened between that commit and the lock (a pagination/auth read inside the HTTP call included).
"""

import base64
import hashlib
import json

import frappe
from frappe import _
from frappe.utils import cstr, escape_html, get_datetime, now_datetime, strip_html

from marketplace_integrations.integrations.noon.catalog import (
	fbpi_ready,
	get_account,
	validate_connection_enabled,
)
from marketplace_integrations.integrations.noon.client import SERVICE_NAME, NoonClient
from marketplace_integrations.integrations.noon.constants import (
	FBPI_ORDER_INVOICE_UPLOAD_PATH,
	FBPI_ORDER_UPDATE_PATH,
	SHIPMENT_INVOICE_MAX_BYTES,
)
from marketplace_integrations.integrations.noon.expected_change import record_out_of_stock
from marketplace_integrations.integrations.noon.orders import get_order as get_fbpi_order
from marketplace_integrations.integrations.noon.orders import import_order
from marketplace_integrations.services import carrier_log
from marketplace_integrations.services.mappings import resolve_item, resolve_mapping_and_item

RQ_JOB_PREFIX = "marketplace-noon-fulfillment"
# The only courier: the value Noon documents for AWBs it generated.
NOON_COURIER = "noon"

MP_STATUS_CONFIRMED = "MP_ITEM_STATUS_CONFIRMED"
INTEGRATION_STATUS_ACKNOWLEDGED = "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED"
INTEGRATION_STATUS_OUT_OF_STOCK = "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"
UPDATE_ORDER_ITEM_STATUS_OUT_OF_STOCK = "UPDATE_ORDER_REQUEST_ITEM_STATUS_OUT_OF_STOCK"
#: ``Integration Request.custom_operation_type`` of every Noon call of Mark as Out of Stock.
OUT_OF_STOCK_OPERATION = "Out of Stock"
#: How long the dialog can read the outcome of one Mark as Out of Stock press (seconds).
_OOS_RESULT_TTL = 3600
#: ``Integration Request.custom_operation_type`` of every Noon call of an invoice upload (order and shipment).
INVOICE_OPERATION = "Invoice"
#: How long the dialog can read the outcome of one invoice upload (seconds).
_INVOICE_RESULT_TTL = 3600


def _order_invoice_job_id(order_name, idempotency_key):
	return f"{RQ_JOB_PREFIX}-{order_name}-order-invoice-{idempotency_key}"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _lock_and_load_order(marketplace_order_name, commit_first=True, as_system=False):
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
	if commit_first:
		# False only for the Delivery Note-first worker, which commits and locks the Delivery Note
		# row itself first (nothing but locking reads between that lock and this one).
		frappe.db.commit()
	frappe.db.get_value("Marketplace Order", marketplace_order_name, "name", for_update=True)
	order = frappe.get_doc("Marketplace Order", marketplace_order_name)
	# as_system: the Delivery Note entry points (delivery_note_shipment.py) checked document-level
	# permission on the note and write the order as the system (a Stock User has no Marketplace Order rights).
	order.flags.ignore_permissions = bool(as_system)
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


def _release_pending_oos_units(marketplace_order_name, mp_item_nrs, skip_if_inflight=False):
	"""Free units from the reservation. Internal bookkeeping: written as the system (the callers checked the
	user's right on the Sales Order; a Sales Order writer has no Marketplace Order rights).
	``skip_if_inflight`` (the stuck-reservation release): the in-flight check is made again inside the order
	lock, so a press that reserved units after the job's first check keeps its reservation."""
	order = _lock_and_load_order(marketplace_order_name, as_system=True)
	if skip_if_inflight and _oos_inflight(order.name):
		frappe.db.commit()
		return
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


def get_shippable_units(order, include_pending_oos=False):
	"""Return ``[{"mp_item_nr": ..., "partner_sku": ..., "item_name": ...}, ...]`` (``item_name``
	only for a SKU with a product mapping) for units currently shippable per #146 decision 11:
	CONFIRMED + ACKNOWLEDGED (raw Noon enum values) in ``latest_payload.items[]``, and not already
	started by a live Noon Delivery Note (``started_units_of_order``) or reserved for queued Out of
	Stock. The worker can include its own reserved units while rechecking eligibility.
	"""
	from marketplace_integrations.integrations.noon.delivery_note_shipment import started_units_of_order

	claimed = started_units_of_order(order.name)
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
	actions = []
	if _pending_oos_units(order):
		actions.append("release_stuck_oos_reservation")

	items = [item for item in _latest_payload_items(order) if isinstance(item, dict)]
	cancelled_on_noon = (
		not actions and bool(items) and not any(item.get("mp_status") == MP_STATUS_CONFIRMED for item in items)
	)
	return {"actions": actions, "cancelled_on_noon": cancelled_on_noon}


def order_invoice_state(order):
	"""Which order-invoice buttons the Sales Order offers: ``{"can_upload", "can_retry", "uploaded"}``. Upload
	needs a Created shipment and no upload in flight; Retry follows a Pending or Error upload. The server
	re-validates both."""
	from marketplace_integrations.integrations.noon.delivery_note_shipment import has_created_note

	status = order.noon_order_invoice_status
	return {
		"can_upload": bool(has_created_note(order.name)) and status != "Pending",
		"can_retry": status in ("Pending", "Error") and bool(order.noon_order_invoice_file),
		"uploaded": status == "Uploaded",
	}


def _with_readable_keys(order, request_data):
	"""Adds ``order_reference`` next to the existing keys (which other code parses and which stay as they are)."""
	return {"order_reference": order.platform_order_reference or "", **request_data}


def _log_fulfillment(
	order, action, status, request_data=None, response_data=None, traceback_text=None, commit=True
):
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
	if commit:
		frappe.db.commit()


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


def _is_definite_refusal(status_code):
	"""A 4xx answer other than 408/429 means Noon refused; anything else may or may not have acted."""
	return 400 <= status_code < 500 and status_code not in (408, 429)


_OOS_INFLIGHT = "noon-oos-inflight|{}|{}"
_OOS_INFLIGHT_TTL = 600  # a little over the job timeout (300 s): a hard-killed worker's mark expires


# One cache key per request (no get-then-set of a shared list): two presses cannot overwrite each other's mark.
def _oos_inflight(order_name):
	"""The marks of the Out of Stock requests of this order that are queued or running (truthy while any)."""
	return frappe.cache().get_keys(_OOS_INFLIGHT.format(order_name, "*"))


def _oos_inflight_add(order_name, request_id):
	frappe.cache().set_value(_OOS_INFLIGHT.format(order_name, request_id), 1, expires_in_sec=_OOS_INFLIGHT_TTL)


def _oos_inflight_remove(order_name, request_id):
	frappe.cache().delete_value(_OOS_INFLIGHT.format(order_name, request_id))


def release_stuck_oos_reservation(marketplace_order_name):
	"""Queue the recovery of a Pending Noon Out of Stock reservation orphaned by a worker that died before
	``run_mark_out_of_stock()``'s ``finally`` (#146 review finding #2). Refuses while an Out of Stock job of
	this order is queued or running (it owns the reservation). The job asks Noon first and frees only the
	units Noon does not show Out of Stock (``run_release_stuck_oos``). Returns ``{"queued"}``."""
	order = _lock_and_load_order(marketplace_order_name)
	if not _pending_oos_units(order):
		frappe.db.commit()
		frappe.throw(_("There is no pending Out of Stock reservation on this order."))
	frappe.db.commit()
	if _oos_inflight(order.name):
		frappe.throw(_("An Out of Stock request of this order is still running. Wait for it to finish."))
	job = frappe.enqueue(
		run_release_stuck_oos,
		queue="long",
		timeout=300,
		job_id=f"{RQ_JOB_PREFIX}-{order.name}-oos-release",
		deduplicate=True,
		marketplace_order_name=order.name,
	)
	return {"queued": job is not None}


def run_release_stuck_oos(marketplace_order_name):
	"""Worker of the recovery. Asks Noon (GetFbpiOrder) which reserved units are Out of Stock there: those stay
	reserved (Noon acted; the Mark as Out of Stock Retry finishes the local step), the others are freed. An
	unusable answer from Noon frees nothing. Logged to the Marketplace Sync Log."""
	frappe.set_user("Administrator")
	order = _lock_and_load_order(marketplace_order_name)
	held = sorted(_pending_oos_units(order))
	if not held or _oos_inflight(order.name):
		frappe.db.commit()
		return
	try:
		account, _client = _account_and_client(order)
		external_order_id = order.external_order_id
		frappe.db.commit()  # release the order lock before the Noon call
		# logged like the other Noon calls (an Integration Request row; the answer holds customer data and is not kept)
		log = carrier_log.open_log(
			SERVICE_NAME,
			order.name,
			OUT_OF_STOCK_OPERATION,
			reference=external_order_id,
			reference_doctype="Marketplace Order",
		)
		frappe.db.commit()
		try:
			raw_order = get_fbpi_order(account, external_order_id)
		except Exception as exc:
			frappe.clear_messages()
			carrier_log.close_log(log, "Unknown", error=strip_html(cstr(exc))[:200])
			frappe.db.commit()
			raise
		carrier_log.close_log(log, "Success", http_status=200)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		_log_fulfillment(
			order, "ReleaseStuckOosReservation", "Error", {"mp_item_nrs": held}, traceback_text=frappe.get_traceback()
		)
		return
	at_noon = [nr for nr in held if _is_out_of_stock(raw_order, nr)]
	free = [nr for nr in held if nr not in at_noon]
	if free and not _oos_inflight(order.name):
		_release_pending_oos_units(order.name, free, skip_if_inflight=True)
	_log_fulfillment(
		order,
		"ReleaseStuckOosReservation",
		"Success",
		{"released_mp_item_nrs": free, "kept_out_of_stock_at_noon": at_noon},
	)


# ---------------------------------------------------------------------------
# Mark Out of Stock (UpdateOrder)
# ---------------------------------------------------------------------------


class _OutOfStockFailed(Exception):
	"""Noon definitely refused the UpdateOrder request (the message is Noon's text)."""


class _OutOfStockUnknown(Exception):
	"""A Noon call has no conclusive answer: Noon may or may not have acted."""


def _result_key(sales_order, request_id):
	return f"noon-oos-result|{sales_order}|{request_id}"


def _set_result(sales_order, request_id, state, message=""):
	"""What the wait dialog reads (``out_of_stock_result``): ``done``, ``failed``, ``unknown`` or
	``local_failed``. Display only: the marker ``noon_pending_oos_units`` and the Integration Request rows
	are the record."""
	frappe.cache().set_value(
		_result_key(sales_order, request_id),
		{"state": state, "message": cstr(message)[:500]},
		expires_in_sec=_OOS_RESULT_TTL,
	)


def out_of_stock_result(sales_order, request_id):
	"""The outcome of one press for the wait dialog: ``{"state", "message"}``, ``pending`` until the job
	has written one."""
	return frappe.cache().get_value(_result_key(sales_order, request_id)) or {"state": "pending", "message": ""}


def _is_out_of_stock(raw_order, mp_item_nr):
	"""Does GetFbpiOrder show this unit as ``INTEGRATION_ITEM_STATUS_OUT_OF_STOCK``?"""
	for item in (raw_order or {}).get("items") or []:
		if isinstance(item, dict) and cstr(item.get("mp_item_nr")).strip() == mp_item_nr:
			return item.get("integration_status") == INTEGRATION_STATUS_OUT_OF_STOCK
	return False


def _unresolved_create_message():
	return _(
		"A Delivery Note of this order is still being sent to Noon, or Noon's answer is unknown. Wait for it, or check the shipment, before marking Out of Stock."
	)


def _live_notes_by_item(sales_order):
	"""``{item_code: [Delivery Note names]}`` of the Draft or submitted Delivery Notes holding the Sales
	Order's items (Noon accepts Out of Stock only before a shipment exists)."""
	rows = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": sales_order, "docstatus": ["<", 2], "parenttype": "Delivery Note"},
		fields=["item_code", "parent"],
		parent_doctype="Delivery Note",
		order_by="parent",
	)
	notes = {}
	for row in rows:
		names = notes.setdefault(row.item_code, [])
		if row.parent not in names:
			names.append(row.parent)
	return notes


def _units_by_item(order):
	"""``{item_code: {"units": [mp_item_nr], "skus": {partner_sku}}}``: the units Mark as Out of Stock can
	act on. Shippable units (``get_shippable_units``, which reads the Delivery Notes) plus the units this
	app already reserved for an earlier press (Noon may have confirmed them, so only the local step is
	left), each placed on its Item through the SKU mapping."""
	from marketplace_integrations.integrations.noon.delivery_note_shipment import started_units_of_order

	sku_by_unit = {row["mp_item_nr"]: row["partner_sku"] for row in get_shippable_units(order, include_pending_oos=True)}
	held = _pending_oos_units(order)
	if held:
		started = started_units_of_order(order.name)
		for item in _latest_payload_items(order):
			nr = cstr(item.get("mp_item_nr")).strip() if isinstance(item, dict) else ""
			if nr in held and nr not in started and nr not in sku_by_unit:
				sku_by_unit[nr] = cstr(item.get("partner_sku")).strip()
	item_by_sku = {}
	result = {}
	for nr, sku in sku_by_unit.items():
		if sku not in item_by_sku:
			item_by_sku[sku] = resolve_item(order.marketplace_account, sku) if sku else None
		item_code = item_by_sku[sku]
		if item_code:
			entry = result.setdefault(item_code, {"units": [], "skus": set()})
			entry["units"].append(nr)
			entry["skus"].add(sku)
	return result


def out_of_stock_rows(sales_order):
	"""Names of the Sales Order Item rows the Mark as Out of Stock action is offered for: the row's Item is
	in no live Delivery Note, has units Noon can still mark and is mapped to exactly one SKU. The server
	re-checks everything when the action is used."""
	values = frappe.db.get_value("Sales Order", sales_order, ["docstatus", "custom_marketplace_order"], as_dict=True)
	if not values or values.docstatus != 1 or not values.custom_marketplace_order:
		return []
	if frappe.db.get_value("Marketplace Order", values.custom_marketplace_order, "platform") != "Noon":
		return []
	order = frappe.get_doc("Marketplace Order", values.custom_marketplace_order)
	blocked = _live_notes_by_item(sales_order)
	units = _units_by_item(order)
	rows = frappe.get_all(
		"Sales Order Item",
		filters={"parent": sales_order, "parenttype": "Sales Order"},
		fields=["name", "item_code"],
		parent_doctype="Sales Order",
		order_by="idx",
	)
	return [
		row.name
		for row in rows
		if row.item_code not in blocked and units.get(row.item_code) and len(units[row.item_code]["skus"]) == 1
	]


def mark_row_out_of_stock(sales_order_name, row_name):
	"""Request path of Mark as Out of Stock for one Sales Order Item row (the API checked write permission
	on the Sales Order). Refuses unless the account's warehouse is FBPI, refuses while the row's Item is in
	a Draft or submitted Delivery Note (naming it), reserves the Item's units in ``noon_pending_oos_units``
	and queues the job. Pressing again while the reservation of the same units exists is the Retry: the job
	then asks Noon first. Returns ``{"queued", "request_id"}``."""
	values = frappe.db.get_value("Sales Order", sales_order_name, ["docstatus", "custom_marketplace_order"], as_dict=True)
	if not values or values.docstatus != 1 or not values.custom_marketplace_order:
		frappe.throw(_("Sales Order {0} is not a submitted Noon order.").format(sales_order_name))
	# The Delivery Note entry points do the same: the user's right is the Sales Order's, the order row is
	# written as the system (a Sales Order writer needs no Marketplace Order rights).
	order = _lock_and_load_order(values.custom_marketplace_order, as_system=True)
	account, _client = _account_and_client(order)
	ready, reason = fbpi_ready(account)
	if not ready:
		frappe.throw(reason)
	item_code = frappe.db.get_value(
		"Sales Order Item", {"name": row_name, "parent": sales_order_name, "parenttype": "Sales Order"}, "item_code"
	)
	if not item_code:
		frappe.throw(_("Row {0} is not an item of Sales Order {1}.").format(cstr(row_name), sales_order_name))
	from marketplace_integrations.integrations.noon.delivery_note_shipment import has_unresolved_create

	if has_unresolved_create(order.name):
		frappe.throw(_unresolved_create_message())
	notes = _live_notes_by_item(sales_order_name).get(item_code)
	if notes:
		frappe.throw(
			_(
				"Item {0} is in Delivery Note {1}. Noon accepts Out of Stock only before a shipment exists: remove the item from a draft note, or cancel the note, first."
			).format(
				frappe.bold(escape_html(item_code)), ", ".join(frappe.bold(escape_html(note)) for note in notes)
			)
		)
	entry = _units_by_item(order).get(item_code)
	if not entry or not entry["units"]:
		frappe.throw(_("No unit of item {0} can be marked Out of Stock.").format(frappe.bold(escape_html(item_code))))
	if len(entry["skus"]) > 1:
		frappe.throw(
			_("Item {0} is mapped to more than one Noon SKU on this order ({1}).").format(
				frappe.bold(escape_html(item_code)), escape_html(", ".join(sorted(entry["skus"])))
			)
		)
	units = sorted(entry["units"])
	held = _pending_oos_units(order)
	is_retry = bool(held & set(units))
	request_id = frappe.generate_hash(length=16)
	newly_reserved = set(units) - held
	# The mark is set BEFORE the reservation is saved, while the order lock is held: the stuck-reservation
	# release refuses while any mark is set, so it can never see the new reservation without the mark.
	_oos_inflight_add(order.name, request_id)
	try:
		order.noon_pending_oos_units = frappe.as_json(sorted(held | set(units)))
		order.save()
		frappe.db.commit()
	except Exception:
		_oos_inflight_remove(order.name, request_id)
		raise
	try:
		job = frappe.enqueue(
			run_mark_out_of_stock,
			queue="long",
			timeout=300,
			job_id=f"{RQ_JOB_PREFIX}-{order.name}-oos-{hashlib.sha1(','.join(units).encode()).hexdigest()[:16]}",
			deduplicate=True,
			marketplace_order_name=order.name,
			mp_item_nrs=units,
			sales_order=sales_order_name,
			request_id=request_id,
			check_first=is_retry,
		)
	except Exception:
		_oos_inflight_remove(order.name, request_id)
		if newly_reserved:
			_release_pending_oos_units(order.name, newly_reserved)
		raise
	if job is None:
		_oos_inflight_remove(order.name, request_id)  # no job of this press will clear it
		if newly_reserved:
			# Already queued (a retry of the same units reserved nothing new): the running job keeps its reservation.
			_release_pending_oos_units(order.name, newly_reserved)
	return {"queued": job is not None, "request_id": request_id}


def _open_log(sales_order, request_id, external_order_id):
	log = carrier_log.open_log(
		SERVICE_NAME,
		sales_order,
		OUT_OF_STOCK_OPERATION,
		correlation_id=request_id,
		reference=external_order_id,
		reference_doctype="Sales Order",
	)
	frappe.db.commit()  # logged before the call: a worker that dies mid-call leaves the row
	return log


def _read_noon_order(account, external_order_id, sales_order, request_id):
	"""GetFbpiOrder, logged. Nothing of the answer is stored in the log (it holds customer data). Raises
	``_OutOfStockUnknown`` when Noon gives no usable answer."""
	log = _open_log(sales_order, request_id, external_order_id)
	try:
		raw_order = get_fbpi_order(account, external_order_id)
	except Exception as exc:
		frappe.clear_messages()
		message = _("Noon did not answer the order lookup ({0}).").format(strip_html(cstr(exc))[:200])
		carrier_log.close_log(log, "Unknown", error=message)
		frappe.db.commit()
		raise _OutOfStockUnknown(message) from exc
	carrier_log.close_log(log, "Success", http_status=200)
	frappe.db.commit()
	return raw_order


def _send_out_of_stock(client, external_order_id, units, sales_order, request_id):
	"""UpdateOrder, logged. Returns on 200; raises ``_OutOfStockFailed`` for a definite refusal (a 4xx other
	than 408/429) and ``_OutOfStockUnknown`` for anything else. Never sent twice automatically."""
	log = _open_log(sales_order, request_id, external_order_id)
	try:
		response = client.request(
			"POST",
			FBPI_ORDER_UPDATE_PATH,
			json={
				"fbpi_order_nr": external_order_id,
				"items": [{"mp_item_nr": nr, "status": UPDATE_ORDER_ITEM_STATUS_OUT_OF_STOCK} for nr in units],
			},
		)
	except Exception as exc:
		message = _("The Noon request failed ({0}). It may or may not have reached Noon.").format(cstr(exc)[:200])
		carrier_log.close_log(log, "Unknown", error=message)
		frappe.db.commit()
		raise _OutOfStockUnknown(message) from exc
	if response.status_code == 200:
		carrier_log.close_log(log, "Success", http_status=200, data=frappe.as_json({"mp_item_nrs": units}))
		frappe.db.commit()
		return
	message = client.error_message(response)
	definite = _is_definite_refusal(response.status_code)
	carrier_log.close_log(
		log, "Failed" if definite else "Unknown", error=message, http_status=response.status_code
	)
	frappe.db.commit()
	raise (_OutOfStockFailed if definite else _OutOfStockUnknown)(message)


def _apply_out_of_stock_locally(
	account, marketplace_order_name, external_order_id, mp_item_nrs, sales_order, request_id, raw_order=None
):
	"""The local half: refresh the Marketplace Order from Noon, then run the Sales Order sync, whose planner
	removes the row through the native Update Items (``flags.marketplace_syncing_orders`` lets
	``noon_item_edit_guard`` pass it). Returns ``""`` when done, else a readable reason: the reservation then
	stays and Retry repeats only this step."""
	from marketplace_integrations.services.sales_order_sync import sync_sales_order

	try:
		if raw_order is None:
			raw_order = _read_noon_order(account, external_order_id, sales_order, request_id)
	except _OutOfStockUnknown as exc:
		return cstr(exc)
	if not all(_is_out_of_stock(raw_order, nr) for nr in mp_item_nrs):
		return _("Noon does not show the units as Out of Stock yet. Retry in a moment.")
	try:
		# commit right before each lock below: import_order() and the sync lock the order row first
		frappe.db.commit()
		import_order(account, raw_order, bump_trigger_source="Manual", echo_guard=False)
		frappe.db.commit()
		result = sync_sales_order(marketplace_order_name, trigger_source="Manual")
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Noon fulfillment: Out of Stock local step failed", message=frappe.get_traceback())
		return _("Noon confirmed, but the Sales Order could not be updated. See the Error Log.")
	if not result.get("ok"):
		return strip_html(cstr(result.get("error"))) or _("Noon confirmed, but the Sales Order was not updated.")
	return ""


def run_mark_out_of_stock(marketplace_order_name, mp_item_nrs, sales_order, request_id, check_first=False):
	"""Worker of Mark as Out of Stock. Nothing is held across a Noon call (commit before each call, lock
	after, ``_lock_and_load_order``).

	``check_first`` (a Retry: the units were reserved by an earlier press) asks Noon first (GetFbpiOrder); the
	units Noon already shows Out of Stock are not sent again, so UpdateOrder is never repeated. UpdateOrder
	itself is sent at most once per press and never resent automatically. The reservation
	(``noon_pending_oos_units``) is released on a definite refusal and when the local step is done; it stays
	after an Unknown answer and after a failed local step (Needs Attention lists it)."""
	frappe.set_user("Administrator")
	order = _lock_and_load_order(marketplace_order_name)
	if not set(mp_item_nrs) <= _pending_oos_units(order):
		frappe.db.commit()
		_oos_inflight_remove(marketplace_order_name, request_id)
		_set_result(sales_order, request_id, "failed", _("The Out of Stock reservation was released."))
		return
	# a Retry's reservation outlives an Unknown answer of this run too (nothing is sent blind)
	keep_reservation = bool(check_first)
	try:
		account, client = _account_and_client(order)
		external_order_id = order.external_order_id
		frappe.db.commit()  # release the order lock before the Noon calls below

		raw_order = None
		to_send = list(mp_item_nrs)
		if check_first:
			raw_order = _read_noon_order(account, external_order_id, sales_order, request_id)
			to_send = [nr for nr in mp_item_nrs if not _is_out_of_stock(raw_order, nr)]
		if to_send:
			order = _lock_and_load_order(marketplace_order_name)
			eligible = {row["mp_item_nr"] for row in get_shippable_units(order, include_pending_oos=True)}
			if not set(mp_item_nrs) <= _pending_oos_units(order) or not set(to_send) <= eligible:
				frappe.db.commit()
				keep_reservation = False
				_set_result(sales_order, request_id, "failed", _("Units are no longer eligible to mark Out of Stock."))
				return
			from marketplace_integrations.integrations.noon.delivery_note_shipment import has_unresolved_create

			if has_unresolved_create(order.name):
				frappe.db.commit()
				keep_reservation = False
				_set_result(sales_order, request_id, "failed", _unresolved_create_message())
				return
			frappe.db.commit()
			keep_reservation = True  # from the send on, Noon may have acted
			_send_out_of_stock(client, external_order_id, to_send, sales_order, request_id)
			order = _lock_and_load_order(marketplace_order_name)
			if set(mp_item_nrs) <= _pending_oos_units(order):
				# #364 expected-change marker; the own refresh passes echo_guard=False so it stays for a later poll
				record_out_of_stock(order.name, to_send)
			frappe.db.commit()
			raw_order = None  # the answer must be read after the send
		reason = _apply_out_of_stock_locally(
			account, marketplace_order_name, external_order_id, mp_item_nrs, sales_order, request_id, raw_order
		)
		if reason:
			keep_reservation = True
			_set_result(sales_order, request_id, "local_failed", reason)
		else:
			keep_reservation = False
			_set_result(sales_order, request_id, "done")
	except _OutOfStockFailed as exc:
		keep_reservation = False
		_set_result(sales_order, request_id, "failed", cstr(exc))
	except _OutOfStockUnknown as exc:
		_set_result(sales_order, request_id, "unknown", cstr(exc))
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Noon fulfillment: Out of Stock failed", message=frappe.get_traceback())
		_set_result(
			sales_order,
			request_id,
			"unknown" if keep_reservation else "failed",
			_("Marking Out of Stock failed unexpectedly. See the Error Log."),
		)
	finally:
		_oos_inflight_remove(marketplace_order_name, request_id)
		if not keep_reservation:
			_release_pending_oos_units(marketplace_order_name, mp_item_nrs)


# ---------------------------------------------------------------------------
# Order invoice (UploadFbpiOrderInvoice)
# ---------------------------------------------------------------------------
# The shipment invoice (UploadShipmentInvoice) is sent from the Delivery Note
# (``delivery_note_shipment.upload_invoice_for_note``, #416); it shares the file rules, the result the dialog
# polls and the logged Noon call defined here.


def _invoice_result_key(idempotency_key):
	return f"noon-invoice-result|{idempotency_key}"


def set_invoice_result(idempotency_key, state, message=""):
	"""What the invoice dialog reads (``invoice_result``): ``pending``, ``done`` or ``failed``. Display only:
	the Integration Request rows (and, for the order invoice, the Marketplace Order fields) are the record.
	The idempotency key is one per press and is reused by Retry, so a Retry starts ``pending`` again."""
	frappe.cache().set_value(
		_invoice_result_key(idempotency_key),
		{"state": state, "message": strip_html(cstr(message))[:500]},
		expires_in_sec=_INVOICE_RESULT_TTL,
	)


def _invoice_binding_key(idempotency_key):
	return f"noon-invoice-document|{idempotency_key}"


def bind_invoice_key(idempotency_key, doctype, name):
	"""Remember which document a press key belongs to, so the poll of one document cannot read the result of
	another's upload (a key is a secret-ish token, but the result text can name Noon's refusal)."""
	frappe.cache().set_value(_invoice_binding_key(idempotency_key), f"{doctype}|{name}", expires_in_sec=_INVOICE_RESULT_TTL)


def invoice_key_belongs_to(idempotency_key, doctype, name):
	"""False only when the key is bound to a different document (an unbound or expired key is not refused)."""
	bound = frappe.cache().get_value(_invoice_binding_key(idempotency_key))
	return not bound or bound == f"{doctype}|{name}"


def invoice_result(idempotency_key, doctype=None, name=None):
	"""``{"state", "message"}`` of one invoice press, ``pending`` until the job has written one. With a
	document given, a key bound to another document reads as ``pending``."""
	if doctype and not invoice_key_belongs_to(idempotency_key, doctype, name):
		return {"state": "pending", "message": ""}
	return frappe.cache().get_value(_invoice_result_key(idempotency_key)) or {"state": "pending", "message": ""}


def send_invoice(client, path, body, reference_doctype, reference_name, reference, metadata):
	"""One Noon invoice call, logged to the Integration Request through ``carrier_log`` (operation "Invoice",
	the key of the press as correlation id). The log holds ``metadata`` only (invoice number, issue date,
	order or shipment number), never the file bytes, a secret or customer data. Returns ``("done", "")`` on
	200, else ``("failed", Noon's text)``: a same-key Retry is safe at Noon (idempotency key), so an unknown
	answer is not told apart from a refusal."""
	log = carrier_log.open_log(
		SERVICE_NAME,
		reference_name,
		INVOICE_OPERATION,
		correlation_id=body["idempotency_key"],
		reference=reference,
		reference_doctype=reference_doctype,
	)
	frappe.db.commit()  # logged before the call: a worker that dies mid-call leaves the row
	try:
		response = client.request("POST", path, json=body)
	except Exception as exc:
		message = _("The Noon request failed ({0}).").format(strip_html(cstr(exc))[:200])
		carrier_log.close_log(log, "Unknown", error=message)
		frappe.db.commit()
		return "failed", message
	if response.status_code == 200:
		carrier_log.close_log(log, "Success", http_status=200, data=frappe.as_json(metadata))
		frappe.db.commit()
		return "done", ""
	message = client.error_message(response)
	carrier_log.close_log(
		log,
		"Failed" if _is_definite_refusal(response.status_code) else "Unknown",
		error=message,
		http_status=response.status_code,
	)
	frappe.db.commit()
	return "failed", message


def require_fbpi(account):
	"""Refuse (a clear message) unless the account's stored warehouse is FBPI (``catalog.fbpi_ready``)."""
	ready, reason = fbpi_ready(account)
	if not ready:
		frappe.throw(reason)


def _validate_invoice_file(file_url):
	"""The File must be readable, private, a real PDF and within Noon's 5 MiB decoded limit. Returns the File."""
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
	return file_doc


def attach_invoice_file(file_url, doctype, name):
	"""Validate the uploaded PDF (``_validate_invoice_file``), then make sure it is a native private File
	attached to ``doctype`` ``name`` (the document the button was used on): the dialog's Attach field uploads
	a File attached to nothing, so its content is saved again with ``file_manager.save_file``. Returns the
	``file_url`` to send. The caller authorized the user on that document. The loose upload is removed once
	the attached copy has its own file (a shared file is left alone)."""
	from frappe.utils.file_manager import save_file

	file_doc = _validate_invoice_file(file_url)
	if file_doc.attached_to_doctype == doctype and file_doc.attached_to_name == name:
		return file_doc.file_url
	content = file_doc.get_content()
	if isinstance(content, str):
		content = content.encode()
	# save_file inserts the File as the system; the caller checked write on the document it attaches to
	saved = save_file(file_doc.file_name, content, doctype, name, is_private=1)
	if (
		not file_doc.attached_to_doctype
		and not file_doc.attached_to_name
		and saved.name != file_doc.name
		and file_doc.owner == frappe.session.user
	):
		# Also when the attached copy shares the loose file's path (same content): Frappe's File removes the
		# disk file only when no other File row has the same content hash, so the attached copy keeps it.
		frappe.delete_doc("File", file_doc.name, ignore_permissions=True, force=True)
	return saved.file_url


def upload_order_invoice(marketplace_order_name, file_url, invoice_nr, issued_at, sales_order=None):
	"""Request path for the order-level invoice. Reads and validates the File here (never logs its
	bytes), generates a fresh idempotency key for this explicit upload action, and writes Pending
	invoice state before enqueuing the actual Noon call. Refused unless the account's warehouse is FBPI.
	Precondition: at least one Delivery Note with a Created Noon shipment. Re-uploading over an Uploaded
	order invoice is allowed (Noon supersedes the previous one; the dialog warns first).

	``sales_order`` (the document the button was used on; the API checked write on it) makes the PDF a
	private File attached to that Sales Order and the order row is written as the system (the user needs no
	Marketplace Order right); without it the File is attached to the Marketplace Order. Returns
	``{"queued", "idempotency_key"}``."""
	from marketplace_integrations.integrations.noon.delivery_note_shipment import has_created_note

	order = _lock_and_load_order(marketplace_order_name, as_system=bool(sales_order))
	account, _client = _account_and_client(order)
	require_fbpi(account)
	if not has_created_note(order.name):
		frappe.throw(_("The order needs at least one Created shipment before uploading an order invoice."))
	if order.noon_order_invoice_status == "Pending":
		frappe.throw(_("An order invoice upload is already pending."))
	invoice_nr = cstr(invoice_nr).strip()
	if not invoice_nr or not issued_at:
		frappe.throw(_("Invoice Nr and Issued At are required."))
	attach_to = ("Sales Order", sales_order) if sales_order else ("Marketplace Order", order.name)
	file_url = attach_invoice_file(file_url, *attach_to)

	order.noon_order_invoice_file = file_url
	order.noon_order_invoice_nr = invoice_nr
	order.noon_order_invoice_issued_at = issued_at
	order.noon_order_invoice_idempotency_key = frappe.generate_hash(length=32)
	order.noon_order_invoice_status = "Pending"
	order.noon_order_invoice_last_error = None
	order.save()
	set_invoice_result(order.noon_order_invoice_idempotency_key, "pending")
	bind_invoice_key(order.noon_order_invoice_idempotency_key, *attach_to)
	frappe.db.commit()
	return _enqueue_order_invoice(order, sales_order)


def _enqueue_order_invoice(order, sales_order):
	key = order.noon_order_invoice_idempotency_key
	job = frappe.enqueue(
		run_upload_order_invoice,
		queue="long",
		timeout=300,
		job_id=_order_invoice_job_id(order.name, key),
		deduplicate=True,
		marketplace_order_name=order.name,
		invoice_idempotency_key=key,
		sales_order=sales_order,
	)
	return {"queued": job is not None, "idempotency_key": key}


def _set_order_invoice_error(order, message):
	order.noon_order_invoice_status = "Error"
	order.noon_order_invoice_last_error = message
	order.save()
	set_invoice_result(order.noon_order_invoice_idempotency_key, "failed", message)
	frappe.db.commit()


def run_upload_order_invoice(marketplace_order_name, invoice_idempotency_key, sales_order=None):
	"""Worker for ``upload_order_invoice()``. Releases the order lock around the File read and Noon
	call and re-checks ``invoice_idempotency_key`` on re-acquisition (a deliberate re-upload with a
	fresh key supersedes an in-flight call). Errors go to the dedicated
	``noon_order_invoice_last_error`` field and to the dialog's result. The Noon call is logged to the
	Integration Request on the Sales Order the button was used on (``sales_order``), else the order."""
	from marketplace_integrations.integrations.noon.delivery_note_shipment import has_created_note

	frappe.set_user("Administrator")
	order = _lock_and_load_order(marketplace_order_name)
	if (
		not order.noon_order_invoice_file
		or order.noon_order_invoice_idempotency_key != invoice_idempotency_key
		or order.noon_order_invoice_status not in ("Pending", "Error")
	):
		frappe.db.commit()
		return
	if not has_created_note(order.name):
		message = _("The order no longer has a Created shipment.")
		_set_order_invoice_error(order, message)
		return
	try:
		_account, client = _account_and_client(order)
	except Exception:
		frappe.log_error(title="Noon order invoice: account unusable", message=frappe.get_traceback())
		_set_order_invoice_error(order, _("The Noon account could not be used. See the Error Log."))
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
		outcome, remote_message = send_invoice(
			client,
			FBPI_ORDER_INVOICE_UPLOAD_PATH,
			{
				"fbpi_order_nr": fbpi_order_nr,
				"invoice": encoded,
				"invoice_nr": invoice_nr,
				"issued_at": issued_at,
				"idempotency_key": invoice_idempotency_key,
			},
			"Sales Order" if sales_order else "Marketplace Order",
			sales_order or order.name,
			fbpi_order_nr,
			request_metadata,
		)
	except Exception:
		order = _lock_and_load_order(marketplace_order_name)
		if order.noon_order_invoice_idempotency_key == invoice_idempotency_key:
			order.noon_order_invoice_status = "Error"
			order.noon_order_invoice_last_error = _("The invoice could not be sent. See the Error Log.")
			order.save()
			set_invoice_result(
				invoice_idempotency_key, "failed", _("The invoice could not be sent. See the Error Log.")
			)
		frappe.db.commit()
		frappe.log_error(title="Noon order invoice failed", message=frappe.get_traceback())
		return

	order = _lock_and_load_order(marketplace_order_name)
	if order.noon_order_invoice_idempotency_key != invoice_idempotency_key:
		# A deliberate re-upload (fresh key) superseded this call in flight.
		frappe.db.commit()
		return

	if outcome != "done":
		_set_order_invoice_error(order, remote_message[:500])
		return

	order.noon_order_invoice_status = "Uploaded"
	order.noon_order_invoice_last_error = None
	order.save()
	set_invoice_result(invoice_idempotency_key, "done")
	frappe.db.commit()


def retry_order_invoice(marketplace_order_name, sales_order=None):
	"""Re-enqueue a Pending/Error order invoice upload with its saved key (Noon treats a same-key call as
	the same upload). Same FBPI refusal as the first press. Returns ``{"queued", "idempotency_key"}``."""
	order = _lock_and_load_order(marketplace_order_name, as_system=bool(sales_order))
	account, _client = _account_and_client(order)
	require_fbpi(account)
	if order.noon_order_invoice_status not in ("Pending", "Error") or not order.noon_order_invoice_file:
		frappe.throw(_("The order invoice has nothing to retry."))
	set_invoice_result(order.noon_order_invoice_idempotency_key, "pending")
	bind_invoice_key(
		order.noon_order_invoice_idempotency_key,
		*(("Sales Order", sales_order) if sales_order else ("Marketplace Order", order.name)),
	)
	frappe.db.commit()
	return _enqueue_order_invoice(order, sales_order)
