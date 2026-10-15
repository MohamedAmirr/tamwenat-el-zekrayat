# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Persist documented Bosta delivery states and their safe ERP side effects."""

import time

import frappe
from frappe import _
from frappe.utils import add_to_date, cstr, now_datetime

from marketplace_integrations.integrations.bosta.client import (
	BostaClient,
	is_success_response,
	response_body,
)
from marketplace_integrations.integrations.bosta.deliveries import (
	lock_and_load_sales_order,
	rollback_to_savepoint,
)
from marketplace_integrations.marketplace_integrations.doctype.bosta_shipment.bosta_shipment import (
	CANCELED,
	save_as_system,
)

# Terminal states: polling stops once Bosta reports Delivered (45) or Canceled (49).
# Every other state - including 46/48/60/100 - keeps being polled (until POLL_MAX_AGE_DAYS)
# rather than being guessed as terminal.
TERMINAL_STATE_CODES = (45, 49)

# Polling only covers shipments created within this many days, so a shipment whose
# terminal state we never observe cannot be polled forever.
POLL_MAX_AGE_DAYS = 60

CANCEL_SAVEPOINT = "bosta_cancel_delivery_note"

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
	"""Poll Bosta deliveries that have not reached a terminal state, and retry any
	Delivery Note cancellation that failed earlier."""
	retry_failed_cancellations()
	settings = frappe.get_single("Bosta Settings")
	if not settings.enabled:
		return 0

	shipments = frappe.get_all(
		"Bosta Shipment",
		filters={
			"tracking_number": ["!=", ""],
			"status": ["!=", CANCELED],
			"bosta_state_code": ["not in", list(TERMINAL_STATE_CODES)],
			"creation": [">=", add_to_date(now_datetime(), days=-POLL_MAX_AGE_DAYS)],
		},
		fields=["name", "tracking_number"],
		order_by="name asc",
	)
	updated = 0
	client = BostaClient(settings=settings)
	for shipment in shipments:
		try:
			# Taken before the request: the snapshot is no newer than this instant.
			fetched_at = int(time.time() * 1000)
			response = client.get_delivery(shipment.tracking_number)
			if not is_success_response(response):
				raise BostaStatusError(client.error_message(response))
			state = _polled_state(response_body(response))
			if (
				state
				and apply_state(
					shipment.tracking_number,
					state_code=state["code"],
					timestamp=fetched_at,
					snapshot=state["snapshot"],
					skip_if_unchanged=True,
				)
				== APPLIED
			):
				updated += 1
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				frappe.get_traceback(with_context=False),
				f"Bosta status polling failed for {shipment.name}",
			)
	return updated


def retry_failed_cancellations():
	"""Bosta cancelled the shipment but ERPNext could not cancel the Delivery Note:
	try again (local only; no Bosta call)."""
	for row in frappe.get_all(
		"Bosta Shipment",
		filters={
			"bosta_state_code": 49,
			"status": ["!=", CANCELED],
			"delivery_note_error": ["!=", ""],
		},
		fields=["name", "sales_order"],
	):
		try:
			lock_and_load_sales_order(row.sales_order)
			shipment = frappe.get_doc("Bosta Shipment", row.name)
			if shipment.status != CANCELED:
				_cancel_linked_delivery_note(shipment)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				frappe.get_traceback(with_context=False),
				f"Bosta cancellation retry failed for {row.name}",
			)


def _polled_state(body):
	"""The documented ``data.state`` of a delivery read, as ``{code, snapshot}`` or ``None``."""
	data = body.get("data")
	state = data.get("state") if isinstance(data, dict) else None
	if not isinstance(state, dict):
		return None
	code = _as_documented_integer(state.get("code"))
	if code is None:
		return None
	snapshot = {"code": state.get("code")}
	if state.get("value") is not None:
		snapshot["value"] = state.get("value")
	return {"code": code, "snapshot": snapshot}


def process_webhook_event(payload):
	"""Apply one authenticated Bosta webhook event.

	The public API endpoint only authenticates and parses JSON.  A malformed body is
	an error; anything well-formed but unusable (unknown tracking number, unrecognised
	state code or delivery type) is logged and skipped so Bosta gets a 200.
	"""
	event = _validated_webhook_event(payload)
	return apply_state(
		event["tracking_number"],
		state_code=event["state_code"],
		timestamp=event["timestamp"],
		delivery_id=event["delivery_id"],
		delivery_type=event["delivery_type"],
		exception_reason=event["exception_reason"],
		exception_code=event["exception_code"],
	)


APPLIED = "applied"
STALE = "stale"
SKIPPED = "skipped"
UNCHANGED = "unchanged"


def apply_state(
	tracking_number,
	*,
	state_code,
	timestamp,
	delivery_id=None,
	delivery_type="SEND",
	exception_reason=None,
	exception_code=None,
	snapshot=None,
	skip_if_unchanged=False,
):
	"""The one place a Bosta state (from a webhook or a poll) is applied.

	``timestamp`` is milliseconds since the epoch: the event's ``timeStamp`` for a
	webhook, the instant the read was started for a poll. Anything not newer than the
	stored one is ignored, so a stale poll cannot overwrite a newer webhook (and vice
	versa). A poll has no delivery type of its own; every shipment this app creates is
	a SEND (type 10). Returns APPLIED, STALE or SKIPPED.
	"""
	if state_code not in BOSTA_STATE_NAMES:
		return _skip("unrecognised state code", tracking_number, state_code)
	if delivery_type not in DOCUMENTED_DELIVERY_TYPES:
		return _skip("unrecognised delivery type", tracking_number, delivery_type)

	sales_order = frappe.db.get_value("Bosta Shipment", {"tracking_number": tracking_number}, "sales_order")
	if not sales_order:
		return _skip("unknown tracking number", tracking_number)
	# Sales Order first, then the shipment: the same order create_bosta_shipment locks in.
	lock_and_load_sales_order(sales_order)
	shipment_name = frappe.db.get_value(
		"Bosta Shipment", {"tracking_number": tracking_number}, "name", for_update=True
	)
	if not shipment_name:
		return _skip("unknown tracking number", tracking_number)
	shipment = frappe.get_doc("Bosta Shipment", shipment_name)
	if delivery_id and shipment.bosta_delivery_id and shipment.bosta_delivery_id != delivery_id:
		return _skip("delivery id mismatch", tracking_number, delivery_id)
	if _is_duplicate_or_older_event(shipment, timestamp):
		return STALE
	if skip_if_unchanged and shipment.bosta_state_code == state_code:
		return UNCHANGED

	shipment.bosta_state_code = state_code
	shipment.bosta_state_value = BOSTA_STATE_NAMES[state_code]
	shipment.last_bosta_event_timestamp = timestamp
	if exception_reason is not None:
		shipment.bosta_exception_reason = exception_reason
		shipment.bosta_exception_code = exception_code or 0
	if snapshot is not None:
		shipment.bosta_status_snapshot = frappe.as_json(snapshot)
	shipment.last_bosta_status_on = now_datetime()
	if shipment.status not in ("Bosta Failed", "Delivery Note Failed") and not shipment.delivery_note_error:
		shipment.last_error = ""
	save_as_system(shipment)

	# Only the standard outbound delivery (SEND) is cancelled; state 46 (Returned to
	# business) is documented for other types and has no Delivery Note side effect.
	if state_code == 49 and delivery_type == "SEND":
		_cancel_linked_delivery_note(shipment)
	frappe.db.commit()
	return APPLIED


def _skip(reason, tracking_number, detail=None):
	frappe.logger("bosta").info(
		f"Bosta event skipped ({reason}): tracking {cstr(tracking_number)[:60]} {cstr(detail)[:60]}"
	)
	return SKIPPED


def _validated_webhook_event(payload):
	"""Reject only a malformed body; unusable-but-well-formed events are skipped later."""
	if not isinstance(payload, dict):
		_invalid_webhook_event()

	delivery_id = cstr(payload.get("_id")).strip()
	tracking_number = cstr(payload.get("trackingNumber")).strip()
	state_code = _as_documented_integer(payload.get("state"))
	timestamp = _as_documented_integer(payload.get("timeStamp"))
	if not delivery_id or not tracking_number or state_code is None or timestamp is None:
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
		"delivery_type": cstr(payload.get("type")).strip(),
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
	"""Cancel the linked Delivery Note (inside a savepoint) and mark the shipment Canceled.

	On failure the shipment is left active - so no second shipment can be created while
	the goods are still booked out - with ``delivery_note_error`` set for Needs Attention.
	"""
	if (
		shipment.delivery_note
		and frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus") == 1
	):
		frappe.db.savepoint(CANCEL_SAVEPOINT)
		try:
			delivery_note = frappe.get_doc("Delivery Note", shipment.delivery_note)
			delivery_note.flags.ignore_permissions = True
			delivery_note.cancel()
		except Exception as exc:
			rollback_to_savepoint(CANCEL_SAVEPOINT)
			error = _("Delivery Note {0} could not be cancelled: {1}").format(
				shipment.delivery_note, frappe.utils.strip_html(cstr(exc))[:300]
			)
			if shipment.delivery_note_error == error:
				# Same failure as the last attempt: no new log entry, no write.
				return False
			frappe.log_error(
				frappe.get_traceback(with_context=False),
				f"Bosta cancellation could not cancel Delivery Note {shipment.delivery_note}",
			)
			shipment.last_error = _(
				"Bosta cancelled this shipment, but ERPNext could not cancel Delivery Note {0}. See Error Log."
			).format(shipment.delivery_note)
			shipment.delivery_note_error = error
			save_as_system(shipment)
			return False
		frappe.db.release_savepoint(CANCEL_SAVEPOINT)

	shipment.status = CANCELED
	shipment.last_error = ""
	shipment.delivery_note_error = ""
	save_as_system(shipment)
	return True


def _invalid_webhook_event():
	frappe.throw(_("Invalid Bosta webhook event."), frappe.ValidationError)


class BostaStatusError(Exception):
	"""Expected remote status failure isolated to one scheduled shipment."""
