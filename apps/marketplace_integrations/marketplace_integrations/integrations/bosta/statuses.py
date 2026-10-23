# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Persist documented Bosta delivery states and their safe ERP side effects."""

import time

import frappe
from frappe import _
from frappe.utils import add_to_date, cstr, escape_html, now_datetime

from marketplace_integrations.integrations.bosta.client import (
	BostaClient,
	is_success_response,
	response_body,
)
from marketplace_integrations.integrations.bosta.deliveries import (
	CREATOR_ROLES,
	lock_and_load_sales_order,
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

# Bosta state code for Delivered; a delivered shipment cannot be cancelled from here.
DELIVERED_STATE_CODE = 45

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
	"""Poll Bosta deliveries that have not reached a terminal state, and finish any
	carrier-cancellation record that failed locally earlier."""
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
					delivery_type=state.get("delivery_type"),
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


def refresh_shipment_state(shipment_name):
	"""Read one shipment's delivery from Bosta and apply its state (a manual poll, #271).

	Creator roles only. One ``GET`` delivery, then the same ``apply_state`` as the poll and
	the webhook (so state 49 records carrier cancellation without reversing stock). A Bosta
	error, an unreadable state or a network failure raises without storing anything.
	Returns ``{"result": APPLIED | STALE}``.
	"""
	frappe.only_for(CREATOR_ROLES)
	shipment = frappe.get_doc("Bosta Shipment", shipment_name)
	shipment.check_permission("read")
	if not shipment.tracking_number:
		frappe.throw(_("This Bosta Shipment has no tracking number yet."))
	client = BostaClient()
	# Taken before the request: the snapshot is no newer than this instant.
	fetched_at = int(time.time() * 1000)
	response = client.get_delivery(shipment.tracking_number)
	if not is_success_response(response):
		frappe.throw(escape_html(client.error_message(response)))
	state = _polled_state(response_body(response))
	if not state:
		frappe.throw(_("Bosta returned no readable state for this delivery."))
	result = apply_state(
		shipment.tracking_number,
		state_code=state["code"],
		timestamp=fetched_at,
		delivery_type=state.get("delivery_type"),
		snapshot=state["snapshot"],
	)
	if result == SKIPPED:
		frappe.throw(_("Bosta returned a state this app does not recognise. Nothing was changed."))
	return {"result": result}


def cancel_bosta_shipment(shipment_name):
	"""Terminate the carrier delivery in Bosta and record its local status (#272).

	Creator roles only. Refused locally, without calling Bosta, only when there is no tracking
	number, the shipment is already Canceled, or the stored state is Delivered (45); every
	other state goes to Bosta and its
	error is shown escaped. A failed request leaves everything unchanged. A network error
	has an unknown outcome: nothing is changed, the delivery is read back, and only a
	reported 48/49 finishes the local carrier status update. Submitted Delivery Notes and
	posted stock are never canceled or reversed by this action.
	"""
	frappe.only_for(CREATOR_ROLES)
	shipment = frappe.get_doc("Bosta Shipment", shipment_name)
	shipment.check_permission("read")
	lock_and_load_sales_order(shipment.sales_order)
	shipment.reload()
	if shipment.status == CANCELED:
		# A repeated local action is harmless and must never issue another carrier request.
		return {"canceled": True, "delivery_note_error": shipment.delivery_note_error or ""}
	_refuse_cancel_locally(shipment)
	if shipment.bosta_terminated_on or shipment.bosta_state_code in (48, 49):
		# Bosta already terminated it (an earlier click or a reported state): do not send
		# the DELETE again, only finish the local side.
		return _finish_cancel_safely(shipment)
	client = BostaClient()
	client.ensure_ready()
	# Release the Sales Order lock before the HTTP call.
	frappe.db.commit()

	try:
		response = client.terminate_delivery(shipment.tracking_number)
	except Exception:
		# The recovery below reports the outcome; drop the client's own error popup.
		frappe.clear_messages()
		return _finish_after_unknown_outcome(shipment)
	if not is_success_response(response):
		frappe.throw(escape_html(client.error_message(response)))
	return _finish_cancel_safely(shipment)


def _refuse_cancel_locally(shipment):
	if not shipment.tracking_number:
		frappe.throw(_("This Bosta Shipment has no tracking number, so there is nothing to cancel in Bosta."))
	if shipment.status == CANCELED:
		frappe.throw(_("This Bosta Shipment is already canceled."))
	if shipment.bosta_state_code == DELIVERED_STATE_CODE:
		frappe.throw(_("Bosta reports this delivery as Delivered; it cannot be cancelled."))


def _finish_after_unknown_outcome(shipment):
	"""The terminate request failed without an answer: it may or may not have been applied."""
	frappe.db.rollback()
	try:
		refresh_shipment_state(shipment.name)
		shipment.reload()
	except Exception:
		frappe.db.rollback()
		shipment = None
	# Drop the client's own "Failed to reach Bosta" popups; the outcome is reported below.
	frappe.clear_messages()
	if shipment and shipment.status == CANCELED:
		return {"canceled": True, "delivery_note_error": ""}
	if shipment and shipment.bosta_state_code in (48, 49):
		return _finish_cancel_safely(shipment)
	frappe.throw(
		_(
			"Could not confirm that Bosta terminated the delivery. Nothing was changed here. "
			"Check the delivery in Bosta before trying again."
		)
	)


def _finish_cancel_safely(shipment):
	"""``_finish_cancel``, but Bosta has already terminated the delivery, so a failure here
	must not be lost: record it on the shipment so the poll's retry picks it up."""
	try:
		return _finish_cancel(shipment)
	except Exception as exc:
		frappe.db.rollback()
		frappe.log_error(
			frappe.get_traceback(with_context=False),
			f"Bosta cancellation could not be finished for {shipment.name}",
		)
		error = _(
			"Bosta terminated the delivery, but the local carrier status could not be recorded: {0}"
		).format(frappe.utils.strip_html(cstr(exc))[:300])
		try:
			frappe.db.set_value(
				"Bosta Shipment",
				shipment.name,
				{"bosta_terminated_on": now_datetime(), "delivery_note_error": error},
			)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				frappe.get_traceback(with_context=False),
				f"Bosta cancellation failure could not be recorded for {shipment.name}",
			)
		frappe.throw(
			_(
				"Bosta terminated the delivery, but ERPNext could not finish recording the status. "
				"The carrier status will be retried automatically."
			)
		)


def _finish_cancel(shipment):
	lock_and_load_sales_order(shipment.sales_order)
	shipment.reload()
	if not shipment.bosta_terminated_on:
		shipment.bosta_terminated_on = now_datetime()
		save_as_system(shipment)
	canceled = shipment.status == CANCELED or _cancel_linked_delivery_note(shipment)
	frappe.db.commit()
	return {"canceled": bool(canceled), "delivery_note_error": shipment.delivery_note_error or ""}


def retry_failed_cancellations():
	"""Finish recording Bosta cancellation locally after a transient save failure; no stock is reversed."""
	for row in frappe.get_all(
		"Bosta Shipment",
		filters={
			"status": ["!=", CANCELED],
			"delivery_note_error": ["!=", ""],
		},
		# Bosta cancelled it (state 49) or accepted our terminate request.
		or_filters=[["bosta_state_code", "=", 49], ["bosta_terminated_on", "is", "set"]],
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
	result = {"code": code, "snapshot": snapshot}
	if isinstance(data, dict) and isinstance(data.get("type"), str):
		result["delivery_type"] = data["type"].strip()
	return result


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
	delivery_type=None,
	exception_reason=None,
	exception_code=None,
	snapshot=None,
	skip_if_unchanged=False,
):
	"""The one place a Bosta state (from a webhook or a poll) is applied.

	``timestamp`` is milliseconds since the epoch: the event's ``timeStamp`` for a
	webhook, the instant the read was started for a poll. Anything not newer than the
	stored one is ignored, so a stale poll cannot overwrite a newer webhook (and vice
	versa). A poll may not identify a delivery type; such a read updates carrier state
	without establishing SEND handover. Returns APPLIED, STALE or SKIPPED.
	"""
	if state_code not in BOSTA_STATE_NAMES:
		return _skip("unrecognised state code", tracking_number, state_code)
	if delivery_type is not None and delivery_type not in DOCUMENTED_DELIVERY_TYPES:
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
	if delivery_type is not None:
		shipment.bosta_delivery_type = delivery_type
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
	# business) is documented for other types. Handover at SEND/21 is the stock trigger.
	if (
		state_code == 21
		and shipment.bosta_delivery_type == "SEND"
		and shipment.handover_status == "Awaiting Handover"
	):
		shipment.handover_status = "Confirmed"
		shipment.handover_confirmed_by = "Administrator"
		shipment.handover_confirmed_on = now_datetime()
		save_as_system(shipment)
		frappe.db.commit()
		from marketplace_integrations.integrations.bosta.deliveries import _create_delivery_note

		_create_delivery_note(shipment, sales_order)

	# Canceled/terminated carrier status never reverses an ERPNext stock posting.
	if state_code == 49 and shipment.bosta_delivery_type == "SEND":
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
	except (TypeError, ValueError):
		return None
	return integer_value if cstr(value).strip() == cstr(integer_value) else None


def _is_duplicate_or_older_event(shipment, timestamp):
	stored_timestamp = _as_documented_integer(shipment.last_bosta_event_timestamp)
	return stored_timestamp is not None and timestamp <= stored_timestamp


def _cancel_linked_delivery_note(shipment):
	"""Record carrier cancellation without reversing stock already posted in ERPNext."""
	was_posted = bool(shipment.delivery_note and frappe.db.get_value(
		"Delivery Note", shipment.delivery_note, "docstatus"
	) == 1)

	shipment.status = CANCELED
	if not was_posted:
		shipment.reservation_status = "Released"
	shipment.last_error = ""
	shipment.delivery_note_error = ""
	save_as_system(shipment)
	return True


def _invalid_webhook_event():
	frappe.throw(_("Invalid Bosta webhook event."), frappe.ValidationError)


class BostaStatusError(Exception):
	"""Expected remote status failure isolated to one scheduled shipment."""
