# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Persist documented Bosta delivery states and their safe ERP side effects."""

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from marketplace_integrations.integrations.bosta.client import BostaClient

# ``Delivered`` is the only terminal delivery state explicitly shown in the
# supplied Bosta read-response contract. All other (including unknown) states
# remain eligible for polling rather than being guessed as terminal.
DOCUMENTED_COMPLETE_STATE_VALUES = {"Delivered"}

# Names are deliberately from Bosta's supplied webhook guide.  We do not
# derive a state name from an unknown numeric code or trust a free-text state
# value sent by a callback.
BOSTA_STATE_NAMES = {
	10: "Pickup requested",
	11: "Waiting for route",
	20: "Route Assigned",
	21: "Picked up from business",
	22: "Picking up from consignee",
	23: "Picked up from consignee",
	24: "Received at warehouse",
	25: "Fulfilled",
	30: "In transit between Hubs",
	40: "Picking up",
	41: "Picked up",
	45: "Delivered",
	46: "Returned to business",
	47: "Exception",
	48: "Terminated",
	49: "Canceled",
	60: "Returned to stock",
	100: "Lost",
	101: "Damaged",
	102: "Investigation",
	103: "Awaiting your action",
	104: "Archived",
	105: "On hold",
}

DOCUMENTED_DELIVERY_TYPES = {
	"SEND",
	"EXCHANGE",
	"CUSTOMER_RETURN_PICKUP",
	"RTO",
	"SIGN_AND_RETURN",
	"FXF_SEND",
}


def poll_incomplete_shipments():
	"""Poll Bosta deliveries that do not yet have its documented Delivered state."""
	settings = frappe.get_single("Bosta Settings")
	if not settings.enabled:
		return 0

	shipment_names = frappe.get_all(
		"Bosta Shipment",
		filters={"tracking_number": ["!=", ""]},
		pluck="name",
		order_by="name asc",
	)
	updated = 0
	client = BostaClient(settings=settings)
	for shipment_name in shipment_names:
		shipment = frappe.get_doc("Bosta Shipment", shipment_name)
		if is_bosta_delivery_complete(shipment.bosta_state_value):
			continue
		try:
			response = client.get_delivery(shipment.tracking_number)
			if not _is_success_response(response):
				raise BostaStatusError(client.error_message(response))
			if update_shipment_status(shipment, _response_body(response)):
				updated += 1
		except Exception:
			frappe.log_error(
				frappe.get_traceback(with_context=False),
				f"Bosta status polling failed for {shipment.name}",
			)
	return updated


def update_shipment_status(shipment, response_body):
	"""Persist only the documented ``state`` snapshot from a Bosta response.

	Malformed or schema-incomplete responses are intentionally ignored. This
	keeps an undocumented payload from becoming a business-state transition.
	"""
	data = response_body.get("data") if isinstance(response_body, dict) else None
	state = data.get("state") if isinstance(data, dict) else None
	if not isinstance(state, dict):
		return False

	code = state.get("code")
	value = state.get("value")
	if code is None and value is None:
		return False

	snapshot = {}
	if code is not None:
		snapshot["code"] = code
	if value is not None:
		snapshot["value"] = value
	numeric_code = _as_documented_integer(code)
	shipment.bosta_state_code = numeric_code if numeric_code is not None else 0
	shipment.bosta_state_value = (
		BOSTA_STATE_NAMES.get(numeric_code, cstr(value)) if code is not None else cstr(value)
	)
	shipment.bosta_status_snapshot = frappe.as_json(snapshot)
	shipment.last_bosta_status_on = now_datetime()
	shipment.save(ignore_permissions=True)
	return True


def process_webhook_event(payload):
	"""Apply one authenticated, documented Bosta webhook event.

	The public API endpoint only authenticates and parses JSON.  Keeping all
	document updates here makes this service callable and testable without the
	HTTP boundary, while preserving the same validation for each event.
	"""
	event = _validated_webhook_event(payload)
	shipment_name = frappe.db.get_value(
		"Bosta Shipment", {"tracking_number": event["tracking_number"]}, "name", for_update=True
	)
	if not shipment_name:
		_invalid_webhook_event()

	shipment = frappe.get_doc("Bosta Shipment", shipment_name)
	if shipment.bosta_delivery_id and shipment.bosta_delivery_id != event["delivery_id"]:
		_invalid_webhook_event()

	if _is_duplicate_or_older_event(shipment, event["timestamp"]):
		return False

	shipment.bosta_state_code = event["state_code"]
	shipment.bosta_state_value = BOSTA_STATE_NAMES[event["state_code"]]
	shipment.last_bosta_event_timestamp = event["timestamp"]
	shipment.bosta_exception_reason = event["exception_reason"]
	shipment.bosta_exception_code = event["exception_code"]
	shipment.last_bosta_status_on = now_datetime()
	shipment.save(ignore_permissions=True)

	# A Bosta Shipment is created only for the standard outbound delivery
	# workflow.  Its documented type is SEND.  State 46 deliberately has no
	# Delivery Note side effect: Bosta documents it only for non-SEND types.
	if event["state_code"] == 49 and event["delivery_type"] == "SEND":
		_cancel_linked_delivery_note(shipment)
	return True


def _validated_webhook_event(payload):
	if not isinstance(payload, dict):
		_invalid_webhook_event()

	delivery_id = cstr(payload.get("_id")).strip()
	tracking_number = cstr(payload.get("trackingNumber")).strip()
	delivery_type = cstr(payload.get("type")).strip()
	state_code = _as_documented_integer(payload.get("state"))
	timestamp = _as_documented_integer(payload.get("timeStamp"))
	if (
		not delivery_id
		or not tracking_number
		or delivery_type not in DOCUMENTED_DELIVERY_TYPES
		or state_code not in BOSTA_STATE_NAMES
		or timestamp is None
	):
		_invalid_webhook_event()

	exception_reason = payload.get("exceptionReason")
	exception_code = payload.get("exceptionCode")
	if exception_reason is not None and not isinstance(exception_reason, str):
		_invalid_webhook_event()
	if exception_code is not None:
		exception_code = _as_documented_integer(exception_code)
		if exception_code is None:
			_invalid_webhook_event()

	return {
		"delivery_id": delivery_id,
		"tracking_number": tracking_number,
		"delivery_type": delivery_type,
		"state_code": state_code,
		"timestamp": timestamp,
		"exception_reason": cstr(exception_reason).strip() if exception_reason is not None else "",
		"exception_code": exception_code or 0,
	}


def _as_documented_integer(value):
	if isinstance(value, bool):
		return None
	try:
		integer_value = int(value)
	except TypeError, ValueError:
		return None
	return integer_value if cstr(value).strip() == cstr(integer_value) else None


def _is_duplicate_or_older_event(shipment, timestamp):
	stored_timestamp = _as_documented_integer(shipment.last_bosta_event_timestamp)
	return stored_timestamp is not None and timestamp <= stored_timestamp


def _cancel_linked_delivery_note(shipment):
	if not shipment.delivery_note:
		return
	delivery_note = frappe.get_doc("Delivery Note", shipment.delivery_note)
	if delivery_note.docstatus == 1:
		try:
			delivery_note.cancel()
		except Exception:
			frappe.log_error(
				frappe.get_traceback(with_context=False),
				f"Bosta cancellation could not cancel Delivery Note {delivery_note.name}",
			)
			shipment.last_error = _(
				"Bosta cancelled this shipment, but ERPNext could not cancel Delivery Note {0}. See Error Log."
			).format(delivery_note.name)
			shipment.save(ignore_permissions=True)


def _invalid_webhook_event():
	frappe.throw(_("Invalid Bosta webhook event."), frappe.ValidationError)


def is_bosta_delivery_complete(state_value):
	return cstr(state_value).strip() in DOCUMENTED_COMPLETE_STATE_VALUES


class BostaStatusError(Exception):
	"""Expected remote status failure isolated to one scheduled shipment."""


def _is_success_response(response):
	return 200 <= response.status_code < 300 and _response_body(response).get("success") is not False


def _response_body(response):
	try:
		body = response.json()
	except ValueError:
		return {}
	return body if isinstance(body, dict) else {}
