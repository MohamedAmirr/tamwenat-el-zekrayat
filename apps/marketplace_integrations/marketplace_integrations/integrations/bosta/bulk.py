# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Create Bosta shipments for several Sales Orders at once (#305).

``get_bulk_create_plan`` checks each order locally (the only Bosta calls are the district lookups, which stop after the first failure) and sorts it into
skipped / ready to send / needs review. ``queue_bulk_create`` enqueues one ``long`` job
that calls ``deliveries.create_bosta_shipment`` per order, each isolated from the rest.
Progress and results sit in the cache under a job id only the requesting user can read
(the shape ``ShopifyCatalogPushStatus`` polls).
"""

import html
import re

import frappe
from frappe import _
from frappe.utils import cstr, flt, strip_html

from marketplace_integrations.integrations.bosta import deliveries

MAX_BULK_ORDERS = 50
RESULT_TTL = 60 * 60
# The address first line must be longer than this to be sent without review.
MIN_DIRECT_FIRST_LINE_LENGTH = 5
MAX_MESSAGE_LENGTH = 300
# Bosta's error code for a delivery created without a district.
DISTRICT_REQUIRED_ERROR_CODE = 777
_BOSTA_MESSAGE = re.compile(r'"message"\s*:\s*"([^"]+)"')
_DISTRICT_ERROR = re.compile(rf'errorCode"?\s*:\s*"?{DISTRICT_REQUIRED_ERROR_CODE}\b')

ORDER_FIELDS = (
	"receiver_name",
	"receiver_phone",
	"city",
	"district_id",
	"address_first_line",
)


def _cache_key(job_id):
	return f"marketplace_integrations:bosta:bulk_create_job:{job_id}"


def _check_batch_size(count):
	if count > MAX_BULK_ORDERS:
		frappe.throw(
			_("You can create at most {0} Bosta shipments at once. You selected {1}.").format(
				MAX_BULK_ORDERS, count
			)
		)


def get_bulk_create_plan(sales_orders):
	"""Sort the selected orders into ``skipped`` (with a reason), ``ready`` (sent without
	review) and ``review`` (the Create dialog opens). Every entry has ``sales_order`` and
	``defaults`` (the dialog's defaults) once it is not skipped. Nothing is sent to Bosta."""
	deliveries._require_creator_role()
	names = list(dict.fromkeys(cstr(name).strip() for name in frappe.parse_json(sales_orders) or []))
	names = [name for name in names if name]
	_check_batch_size(len(names))
	plan = {"skipped": [], "ready": [], "review": []}
	for name in names:
		reason, defaults = _check_order(name)
		if reason:
			plan["skipped"].append({"sales_order": name, "reason": reason})
			continue
		bucket = "ready" if _is_ready(defaults) else "review"
		plan[bucket].append({"sales_order": name, "defaults": defaults})
	return plan


def _check_order(name):
	"""``(reason, defaults)``: a one-line reason this order cannot be sent, else the dialog
	defaults. Nothing is created in Bosta."""
	try:
		deliveries._get_sales_order(name)
		active = deliveries._active_shipment(name)
		if active:
			return _("Bosta Shipment {0} already exists for this Sales Order.").format(active.name), None
		return "", deliveries.get_sales_order_defaults(name)
	except frappe.DoesNotExistError:
		frappe.clear_last_message()
		return _("Sales Order {0} does not exist.").format(name), None
	except frappe.PermissionError:
		frappe.clear_last_message()
		return _("You are not allowed to read this Sales Order."), None
	except frappe.ValidationError as exc:
		frappe.clear_last_message()
		return _plain(exc), None


def _is_ready(defaults):
	return bool(
		cstr(defaults.get("district_id")).strip()
		and cstr(defaults.get("city_name")).strip()
		and cstr(defaults.get("receiver_name")).strip()
		and cstr(defaults.get("receiver_phone")).strip()
		and len(cstr(defaults.get("address_first_line")).strip()) > MIN_DIRECT_FIRST_LINE_LENGTH
	)


def queue_bulk_create(orders, skipped=None):
	"""Enqueue one background job for ``orders`` (``[{sales_order, receiver_name,
	receiver_phone, city, district_id, address_first_line, expected_cod}]``). ``skipped``
	(``[{sales_order, reason}]``) only carries the pre-check and Skip rows into the results."""
	deliveries._require_creator_role()
	orders = [_clean_order(order) for order in frappe.parse_json(orders) or []]
	# The same order twice would only fail the second time: keep the first.
	first_by_name = {}
	for order in orders:
		first_by_name.setdefault(order["sales_order"], order)
	orders = list(first_by_name.values())
	skipped = [
		{
			"sales_order": cstr(row.get("sales_order")).strip(),
			"reason": cstr(row.get("reason"))[:MAX_MESSAGE_LENGTH],
		}
		for row in frappe.parse_json(skipped or "[]")
	]
	if not orders:
		frappe.throw(_("There are no Sales Orders to send to Bosta."))
	_check_batch_size(len(orders) + len(skipped))

	owner = frappe.session.user
	job_id = frappe.generate_hash(length=16)
	_set_status(job_id, {"owner": owner, "status": "queued"})
	frappe.enqueue(
		run_bulk_create,
		queue="long",
		timeout=3600,
		job_id_for_result=job_id,
		owner=owner,
		orders=orders,
		skipped=skipped,
	)
	return {"job_id": job_id}


def _clean_order(order):
	if not isinstance(order, dict) or not cstr(order.get("sales_order")).strip():
		frappe.throw(_("Each Sales Order to send needs a name."))
	cleaned = {"sales_order": cstr(order["sales_order"]).strip()}
	for field in ORDER_FIELDS:
		cleaned[field] = cstr(order.get(field)).strip()
	expected_cod = order.get("expected_cod")
	cleaned["expected_cod"] = None if expected_cod in (None, "") else flt(expected_cod, 2)
	return cleaned


def get_bulk_create_status(job_id):
	state = frappe.cache().get_value(_cache_key(job_id))
	if not state or state.get("owner") != frappe.session.user:
		frappe.throw(_("Bulk Bosta job not found."), frappe.PermissionError)
	return {key: value for key, value in state.items() if key != "owner"}


def run_bulk_create(job_id_for_result, owner, orders, skipped):
	"""Background job: create each order's Bosta shipment, one failure never stops the rest."""
	results = [
		{"mapping_name": row["sales_order"], "ok": False, "skipped": True, "detail": row["reason"]}
		for row in skipped
	]
	_set_status(job_id_for_result, {"owner": owner, "status": "running", "result": _payload(results)})
	try:
		for order in orders:
			results.append(_create_one(order))
			_set_status(job_id_for_result, {"owner": owner, "status": "running", "result": _payload(results)})
		_set_status(job_id_for_result, {"owner": owner, "status": "completed", "result": _payload(results)})
	except Exception as exc:
		_set_status(
			job_id_for_result,
			{"owner": owner, "status": "failed", "error": cstr(exc), "result": _payload(results)},
		)
		raise


def _create_one(order):
	args = {key: value for key, value in order.items() if key != "sales_order"}
	try:
		outcome = deliveries.create_bosta_shipment(
			order["sales_order"],
			args["receiver_name"],
			args["receiver_phone"],
			args["city"],
			args["district_id"],
			args["address_first_line"],
			args["expected_cod"],
		)
	except Exception as exc:
		# Drop anything the failed order left uncommitted before the next one starts.
		frappe.db.rollback()
		frappe.clear_last_message()
		return {
			"mapping_name": order["sales_order"],
			"ok": False,
			"skipped": False,
			"detail": _failure_line(order["sales_order"], exc),
		}
	return {
		"mapping_name": order["sales_order"],
		"ok": True,
		"skipped": False,
		"detail": _("Bosta shipment {0} created and Delivery Note {1} submitted.").format(
			outcome.get("tracking_number"), outcome.get("delivery_note")
		),
	}


def _failure_line(sales_order, exc):
	"""One plain-language line for a failed order."""
	shipment = frappe.db.get_value(
		"Bosta Shipment",
		{"sales_order": sales_order, "status": ["!=", "Canceled"]},
		["tracking_number", "status", "last_error"],
		as_dict=True,
	)
	if shipment and shipment.tracking_number and shipment.status in ("Delivery Note Failed", "Bosta Created"):
		return _(
			"Bosta accepted the delivery (tracking number {0}) but the Delivery Note could not be created. "
			"Retry creates only the Delivery Note."
		).format(shipment.tracking_number)

	raw = html.unescape(cstr(exc))
	if _DISTRICT_ERROR.search(raw):
		return _("Bosta needs a district for this address.")
	if isinstance(exc, frappe.ValidationError):
		# "Bosta request failed (HTTP 400): {...}": show the message Bosta gave when it has one.
		bosta_message = _BOSTA_MESSAGE.search(raw)
		return _plain(bosta_message.group(1) if bosta_message else exc)
	frappe.log_error(frappe.get_traceback(with_context=False), "Bosta bulk create failed")
	if shipment and shipment.last_error:
		return _plain(shipment.last_error)
	return _("The shipment could not be created. See Error Log for details.")


def _plain(message):
	return strip_html(html.unescape(cstr(message))).strip()[:MAX_MESSAGE_LENGTH]


def _payload(results):
	return {
		"results": results,
		"total": len(results),
		"succeeded": sum(1 for row in results if row["ok"]),
		"failed": sum(1 for row in results if not row["ok"] and not row.get("skipped")),
		"skipped_mappings": sum(1 for row in results if row.get("skipped")),
		"truncated": False,
	}


def _set_status(job_id, state):
	frappe.cache().set_value(_cache_key(job_id), state, expires_in_sec=RESULT_TTL)
