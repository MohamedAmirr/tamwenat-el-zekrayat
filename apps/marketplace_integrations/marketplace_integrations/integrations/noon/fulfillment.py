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
import json

import frappe
from frappe import _
from frappe.utils import cstr, get_datetime, now_datetime

from marketplace_integrations.integrations.noon.catalog import get_account, validate_connection_enabled
from marketplace_integrations.integrations.noon.client import NoonClient
from marketplace_integrations.integrations.noon.constants import (
	FBPI_ORDER_INVOICE_UPLOAD_PATH,
	FBPI_ORDER_UPDATE_PATH,
	SHIPMENT_INVOICE_MAX_BYTES,
)
from marketplace_integrations.integrations.noon.expected_change import record_out_of_stock
from marketplace_integrations.integrations.noon.orders import get_order as get_fbpi_order
from marketplace_integrations.integrations.noon.orders import import_order
from marketplace_integrations.services.mappings import resolve_mapping_and_item

RQ_JOB_PREFIX = "marketplace-noon-fulfillment"
# The only courier: the value Noon documents for AWBs it generated.
NOON_COURIER = "noon"

MP_STATUS_CONFIRMED = "MP_ITEM_STATUS_CONFIRMED"
INTEGRATION_STATUS_ACKNOWLEDGED = "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED"
UPDATE_ORDER_ITEM_STATUS_OUT_OF_STOCK = "UPDATE_ORDER_REQUEST_ITEM_STATUS_OUT_OF_STOCK"


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
	from marketplace_integrations.integrations.noon.delivery_note_shipment import has_created_note

	actions = []
	if get_shippable_units(order):
		actions.append("mark_out_of_stock")
	created = has_created_note(order.name)
	if created and order.noon_order_invoice_status != "Pending":
		actions.append("upload_order_invoice")
	if created and order.noon_order_invoice_status in ("Pending", "Error"):
		actions.append("retry_order_invoice")
	if _pending_oos_units(order):
		actions.append("release_stuck_oos_reservation")

	items = [item for item in _latest_payload_items(order) if isinstance(item, dict)]
	cancelled_on_noon = (
		not actions and bool(items) and not any(item.get("mp_status") == MP_STATUS_CONFIRMED for item in items)
	)
	return {"actions": actions, "cancelled_on_noon": cancelled_on_noon}


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


def release_stuck_oos_reservation(marketplace_order_name):
	"""Manually clear every Pending Noon Out of Stock unit reservation on
	this order - recovers from a hard-killed worker process that never
	reached ``run_mark_out_of_stock()``'s ``finally`` release (#146 review
	finding #2). This reservation lives on the order itself
	(``noon_pending_oos_units``). A legitimate in-flight job's own ``finally``
	simply finds nothing left to release once this has already cleared it."""
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
# Mark Out of Stock (UpdateOrder)
# ---------------------------------------------------------------------------


def mark_out_of_stock(marketplace_order_name, mp_item_nrs):
	"""Request path (#146 decision 18): only units that are still shippable (not started by a Noon
	Delivery Note). The submitted-Sales-Order warning is client-side only (decision 8) - this
	function does not block on it."""
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
# Order invoice (UploadFbpiOrderInvoice)
# ---------------------------------------------------------------------------
# The shipment invoice (UploadShipmentInvoice) lived on the removed Marketplace Order shipment rows; sub-issue
# #416 brings it back on the Delivery Note.


def _validate_invoice_file(file_url):
	"""The File must be readable, private, a real PDF and within Noon's 5 MiB decoded limit."""
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


def upload_order_invoice(marketplace_order_name, file_url, invoice_nr, issued_at):
	"""Request path for the order-level invoice. Reads and validates the File here (never logs its
	bytes), generates a fresh idempotency key for this explicit upload action, and writes Pending
	invoice state before enqueuing the actual Noon call. Precondition: at least one Delivery Note
	with a Created Noon shipment. Re-uploading over an Uploaded order invoice is allowed (Noon
	supersedes the previous one)."""
	from marketplace_integrations.integrations.noon.delivery_note_shipment import has_created_note

	order = _lock_and_load_order(marketplace_order_name)
	_account_and_client(order)
	if not has_created_note(order.name):
		frappe.throw(_("The order needs at least one Created shipment before uploading an order invoice."))
	if order.noon_order_invoice_status == "Pending":
		frappe.throw(_("An order invoice upload is already pending."))
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
	"""Worker for ``upload_order_invoice()``. Releases the order lock around the File read and Noon
	call and re-checks ``invoice_idempotency_key`` on re-acquisition (a deliberate re-upload with a
	fresh key supersedes an in-flight call). Errors go to the dedicated
	``noon_order_invoice_last_error`` field."""
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
