# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bosta delivery states on the Delivery Note: webhooks, nightly reconciliation, Refresh (#409).

A webhook is recorded as an Integration Log row (``Integration Request``, operation Webhook) before
anything is applied; the unique ``custom_dedupe_key`` makes a repeated event a no-op without a
check-then-insert race. ``apply_event`` finds the note by the Bosta delivery id, else the tracking
number (exact, normalized match), ignores older events, asks Bosta when the event contradicts the
note, and writes the state with ``db_set``. No lock is held across an HTTP call. Nothing here runs from
``doc_events``.
"""

import hashlib
import json
import math
from numbers import Real

import frappe
from frappe import _
from frappe.utils import add_days, cstr, escape_html, now_datetime

from marketplace_integrations.integrations.bosta import delivery_note_shipment as shipment
from marketplace_integrations.integrations.bosta.client import BostaClient, is_success_response, response_body

# Bosta's own terminal states: a Delivered (45) or Canceled (49) delivery is not polled again.
TERMINAL_STATE_CODES = (45, 49)
#: Reconciliation only covers notes created within this many days, so a delivery whose terminal state
#: is never observed cannot be polled forever.
POLL_MAX_AGE_DAYS = 60
#: A webhook that no note could take yet (a create still in flight) is kept this long, then ignored.
PENDING_EVENT_DAYS = 3
#: Bosta states that mean the delivery was cancelled or terminated by Bosta.
CANCELLED_STATE_CODES = (48, 49)
#: Bosta states that mean the shipment came back to the merchant ("Returned to business", "Returned to
#: stock"): the same pair the Needs Attention "Bosta Return" row uses. One draft Sales Return per note
#: (``returns.create_returned_draft``) is made for them. Open question D13: which of the two is the
#: "customer never received it" state; the draft is idempotent, so both is safe.
RETURNED_STATE_CODES = (46, 60)

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


class RateLimited(Exception):
	"""Bosta answered 429; a reconciliation run stops early."""


def terminal_state_names():
	return [BOSTA_STATE_NAMES[code] for code in TERMINAL_STATE_CODES]


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def process_webhook_event(payload):
	"""Record one authenticated Bosta webhook event and queue its apply (the API endpoint only
	authenticates and parses JSON). A malformed body is an error; a repeated event is answered
	"duplicate"."""
	event = _validated_webhook_event(payload)
	log = record_webhook_event(event)
	if not log:
		return "duplicate"
	frappe.db.commit()
	frappe.enqueue(
		"marketplace_integrations.integrations.bosta.statuses.apply_event",
		queue="short",
		timeout=120,
		job_id=f"bosta-event|{log}",
		enqueue_after_commit=True,
		log=log,
	)
	return "queued"


def record_webhook_event(event):
	"""Insert the Webhook log row; ``None`` when the same event was recorded before."""
	key = hashlib.sha256(
		f"{event['tracking_number']}|{event['state_code']}|{event['timestamp']}".encode()
	).hexdigest()
	minimal = {
		"_id": event["delivery_id"],
		"trackingNumber": event["tracking_number"],
		"state": event["state_code"],
		"timeStamp": event["timestamp"],
		"type": event["delivery_type"],
		"exceptionCode": event["exception_code"],
	}
	log = frappe.get_doc(
		{
			"doctype": "Integration Request",
			"integration_request_service": "Bosta",
			"is_remote_request": 1,
			"status": "Queued",
			"custom_operation_type": "Webhook",
			"custom_delivery_id": event["delivery_id"],
			"custom_dedupe_key": key,
			"data": json.dumps(minimal),
		}
	)
	try:
		log.insert(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		frappe.db.rollback()
		return None
	return log.name


def _note_for(delivery_id, tracking_number):
	"""The Delivery Note of a Bosta delivery: by delivery id, else tracking number. Exact comparison of
	the normalized value (MariaDB's case-insensitive match must not pick a different row)."""
	for fieldname, value in (("custom_carrier_delivery_id", delivery_id), ("lr_no", tracking_number)):
		value = cstr(value).strip()
		if not value:
			continue
		rows = frappe.get_all(
			"Delivery Note",
			filters={fieldname: value, "docstatus": ["<", 2], "is_return": 0},
			fields=["name", fieldname],
			limit_page_length=5,
		)
		rows = [row for row in rows if cstr(row[fieldname]).strip() == value]
		if len(rows) == 1:
			return rows[0].name
	return None


def apply_event(log):
	"""Apply one recorded webhook event to its Delivery Note.

	The log stays ``Queued`` ("pending") while the note cannot take the event yet: a note still being
	created, or an event for a delivery no note has claimed while a create is in flight. It is applied
	when the shipment is Created (``replay_pending_events``), the nightly reconciliation retries it, and
	it is closed as ignored after ``PENDING_EVENT_DAYS``."""
	frappe.set_user("Administrator")
	return _apply_event(log)


def _apply_event(log):
	data = frappe.db.get_value("Integration Request", log, "data")
	if not data or frappe.db.get_value("Integration Request", log, "status") != "Queued":
		return
	event = json.loads(data)
	code = _as_documented_integer(event.get("state"))
	ts = _as_documented_integer(event.get("timeStamp"))
	if code not in BOSTA_STATE_NAMES:
		return _ignore(log, "unrecognised state")
	note = _note_for(event.get("_id"), event.get("trackingNumber"))
	if not note:
		if _a_create_is_in_flight():
			frappe.db.commit()
			return "pending"
		return _ignore(log, "no matching Delivery Note")
	outcome = _write_state(note, code, ts, event=True)
	if outcome == "pending":
		return outcome
	if outcome == "older":
		return _ignore(log, "older or repeated event")
	if outcome == "applied":
		shipment.close_log(log, "Success")
		frappe.db.commit()
		return outcome
	# The event contradicts what the note says: ask Bosta and apply its answer. The log is closed after
	# that answer, so a failed refresh is a failed event, never a successful one.
	try:
		refreshed = refresh_note(note)
	except Exception as exc:
		frappe.db.rollback()
		shipment.close_log(log, "Failed", error=f"conflict, refresh from Bosta failed: {cstr(exc)[:300]}")
		frappe.db.commit()
		return "failed"
	if refreshed is None:
		shipment.close_log(log, "Failed", error="conflict, the note has no tracking number to refresh from")
		frappe.db.commit()
		return "failed"
	shipment.close_log(log, "Success", output="conflict: refreshed from Bosta")
	frappe.db.commit()
	return "refreshed"


def _a_create_is_in_flight():
	return bool(
		frappe.db.exists(
			"Delivery Note",
			{"custom_integration_status": ["in", [shipment.CREATING, shipment.UNKNOWN]], "docstatus": 0},
		)
	)


def _ignore(log, reason):
	shipment.close_log(log, "Success", output=f"ignored: {reason}")
	frappe.db.commit()
	return "ignored"


def replay_pending_events(delivery_id=None):
	"""Apply the Webhook logs that stayed ``Queued`` (oldest event first), for one delivery or for all of
	them. Events older than ``PENDING_EVENT_DAYS`` are closed as ignored."""
	filters = {"integration_request_service": "Bosta", "custom_operation_type": "Webhook", "status": "Queued"}
	if delivery_id:
		filters["custom_delivery_id"] = delivery_id
	logs = frappe.get_all(
		"Integration Request", filters=filters, fields=["name", "creation", "data"], order_by="creation asc"
	)

	def stamp(row):
		try:
			return _as_documented_integer(json.loads(row.data or "{}").get("timeStamp")) or 0
		except ValueError:
			return 0

	cutoff = add_days(now_datetime(), -PENDING_EVENT_DAYS)
	for row in sorted(logs, key=stamp):
		if row.creation < cutoff:
			_ignore(row.name, f"no Delivery Note could take it within {PENDING_EVENT_DAYS} days")
			continue
		_apply_event(row.name)


def _write_state(note, code, timestamp=None, fee=None, event=False):
	"""Write a Bosta state onto a note: commit, take the row lock as the next statement, compare and write
	inside that one lock (a late event cannot overwrite a newer one). Created + Canceled/Terminated by
	Bosta becomes Cancelled.

	``event=True`` is a webhook: it writes nothing and returns ``"older"`` when the stored event time is
	not older than ``timestamp``, ``"pending"`` while the note is Creating, ``"conflict"`` when the note's
	status contradicts the event. A refresh (``event=False``) is Bosta's own answer and always writes.
	Returns ``"applied"`` when written."""
	frappe.db.commit()
	current = frappe.db.get_value(
		"Delivery Note",
		note,
		["custom_integration_status", "custom_bosta_last_event_at", "docstatus"],
		as_dict=True,
		for_update=True,
	)
	status = cstr(current.custom_integration_status)
	stored = _as_documented_integer(current.custom_bosta_last_event_at)
	if event:
		outcome = None
		if stored is not None and timestamp is not None and timestamp <= stored:
			outcome = "older"
		elif status == shipment.CREATING:
			outcome = "pending"
		elif (
			status not in (shipment.CREATED, shipment.CANCELLATION_UNKNOWN)
			and not (
				status in (shipment.CANCELLED, shipment.CANCELLATION_PENDING) and code in CANCELLED_STATE_CODES
			)
		):
			outcome = "conflict"
		if outcome:
			frappe.db.commit()
			return outcome
	values = {"custom_shipping_status": BOSTA_STATE_NAMES[code], "custom_last_sync_at": now_datetime()}
	if timestamp is not None and (stored is None or timestamp > stored):
		values["custom_bosta_last_event_at"] = str(timestamp)
	if fee is not None:
		values["custom_actual_shipping_cost"] = fee
		# A Currency column cannot tell a real 0 from "not received yet": this timestamp can.
		values["custom_actual_cost_received_on"] = now_datetime()
	if code in CANCELLED_STATE_CODES and status in (
		shipment.CREATED,
		shipment.CANCELLATION_PENDING,
		shipment.CANCELLATION_UNKNOWN,
	):
		# Bosta shows the delivery cancelled: a cancel this app started (Pending / Unknown) is resolved too.
		values["custom_integration_status"] = shipment.CANCELLED
	elif code not in CANCELLED_STATE_CODES and status == shipment.CANCELLATION_UNKNOWN:
		# The cancel never took effect (Bosta shows a live state): back to Created, the stale "unknown"
		# text is cleared. Pending is not touched: its terminate call is still in flight.
		values["custom_integration_status"] = shipment.CREATED
		values["custom_last_integration_error"] = None
	# A draft note may be open in a form about to be submitted: a bumped ``modified`` would make that
	# native submit fail with "modified after you opened it", and the person changed nothing. ``notify``
	# still refreshes an open form. A submitted note keeps the bump (nothing can be submitted from it).
	frappe.get_doc("Delivery Note", note).db_set(
		values, update_modified=current.docstatus != 0, notify=True, commit=True
	)
	if code in RETURNED_STATE_CODES and current.docstatus == 1:
		from marketplace_integrations.integrations.bosta import returns

		returns.queue_returned_draft(note)
		frappe.db.commit()
	update_sales_order_summary_of(note)
	return "applied"


# ---------------------------------------------------------------------------
# Refresh (GET the delivery) and reconciliation
# ---------------------------------------------------------------------------


def refresh_note(delivery_note):
	"""Read the note's delivery from Bosta and apply its state. The HTTP call comes first; the lock and
	the write after it. Raises ``RateLimited`` on HTTP 429. Returns the Bosta state name or ``None``."""
	tracking = cstr(frappe.db.get_value("Delivery Note", delivery_note, "lr_no")).strip()
	if not tracking:
		return None
	frappe.db.commit()
	client = BostaClient()
	response = client.get_delivery(tracking)
	if response.status_code == 429:
		raise RateLimited()
	log = shipment.open_log(delivery_note, "Refresh")
	if not is_success_response(response):
		message = client.error_message(response)
		shipment.close_log(log, "Failed", error=message, http_status=response.status_code)
		frappe.db.commit()
		frappe.throw(escape_html(message))
	body = response_body(response)
	state = _polled_state(body)
	if state is None or state["code"] not in BOSTA_STATE_NAMES:
		message = _("Bosta returned a state this app does not recognise.")
		shipment.close_log(log, "Failed", error=message)
		frappe.db.commit()
		frappe.throw(message)
	_write_state(delivery_note, state["code"], fee=_shipment_fee(body))
	shipment.close_log(log, "Success", http_status=response.status_code)
	frappe.db.commit()
	return BOSTA_STATE_NAMES[state["code"]]


def reconcile_open_notes():
	"""Nightly (``59 23 * * *``): queue the one reconciliation job (deduplicated)."""
	frappe.enqueue(
		"marketplace_integrations.integrations.bosta.statuses.run_reconcile",
		queue="long",
		timeout=1800,
		job_id="bosta-reconcile",
		deduplicate=True,
	)


def _open_notes():
	cutoff = add_days(now_datetime(), -POLL_MAX_AGE_DAYS)
	base = {"docstatus": ["<", 2], "creation": [">=", cutoff]}
	notes = []
	for status in (shipment.CREATING, shipment.UNKNOWN, "Cancellation Pending", "Cancellation Unknown"):
		notes += frappe.get_all(
			"Delivery Note", filters={**base, "custom_integration_status": status}, pluck="name"
		)
	created = frappe.get_all(
		"Delivery Note",
		filters={**base, "custom_integration_status": shipment.CREATED},
		fields=["name", "custom_shipping_status"],
	)
	notes += [row.name for row in created if row.custom_shipping_status not in terminal_state_names()]
	return notes


def run_reconcile():
	"""Resolve every open note: a create with an unknown result by lookup only (never create), the rest by
	reading Bosta's state. One note's failure does not stop the run; a 429 does."""
	frappe.set_user("Administrator")
	done = 0
	try:
		replay_pending_events()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Bosta reconciliation: pending events failed", message=frappe.get_traceback())
	for note in _open_notes():
		try:
			status = shipment.status_of(note)
			if status == shipment.CREATING and not shipment.is_stale_creating(note):
				continue
			if status in (shipment.CREATING, shipment.UNKNOWN):
				shipment.check_with_bosta(note)
			else:
				refresh_note(note)
			done += 1
		except RateLimited:
			frappe.log_error(title="Bosta reconciliation stopped: rate limited (429)", message=note)
			break
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="Bosta reconciliation: note failed", message=f"{note}\n{frappe.get_traceback()}")
		frappe.db.commit()
	return done


# ---------------------------------------------------------------------------
# Sales Order summary
# ---------------------------------------------------------------------------


def update_sales_order_summary_of(delivery_note):
	order = frappe.db.get_value("Delivery Note Item", {"parent": delivery_note, "parenttype": "Delivery Note"}, "against_sales_order")
	if order:
		update_sales_order_summary(order)


def keep_stored_summary(doc, method=None):
	"""Sales Order ``validate`` and ``before_update_after_submit``: the summary is written with
	``db_set``/``set_value`` by the app only, so a form copy loaded before such a write must not put its old
	value back. Restore the stored value (empty on a new or copied order)."""
	before = doc.get_doc_before_save()
	doc.custom_shipping_status_summary = before.get("custom_shipping_status_summary") if before else None


def update_sales_order_summary(sales_order):
	"""Store ``{"Delivered": 2, "In transit between Hubs": 1}`` on the Sales Order: its Bosta Delivery Notes
	(docstatus < 2, a shipment started) grouped by Shipping Status; a Created note without a state counts
	as Created. Two workers finishing different notes of one order would each count before the other
	wrote, so commit, lock the Sales Order row as the next statement, then count and write."""
	frappe.db.commit()
	frappe.db.get_value("Sales Order", sales_order, "name", for_update=True)
	parents = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": sales_order, "parenttype": "Delivery Note"},
		pluck="parent",
		distinct=True,
		parent_doctype="Delivery Note",
	)
	counts = {}
	supplier = shipment.bosta_supplier()
	if parents and supplier:
		for row in frappe.get_all(
			"Delivery Note",
			filters={
				"name": ["in", parents],
				"docstatus": ["<", 2],
				"transporter": supplier,
				"custom_carrier_delivery_id": ["!=", ""],
			},
			fields=["custom_shipping_status", "custom_integration_status"],
		):
			label = row.custom_shipping_status or row.custom_integration_status or shipment.CREATED
			counts[label] = counts.get(label, 0) + 1
	frappe.db.set_value(
		"Sales Order",
		sales_order,
		"custom_shipping_status_summary",
		json.dumps(counts) if counts else None,
		update_modified=False,
	)
	frappe.db.commit()


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


def _shipment_fee(body):
	"""Return Bosta's numeric, nonnegative delivery fee; missing or invalid data is ignored."""
	data = body.get("data") if isinstance(body, dict) else None
	value = data.get("shipmentFees") if isinstance(data, dict) else None
	if (
		isinstance(value, bool)
		or not isinstance(value, Real)
		or not math.isfinite(value)
		or value < 0
	):
		return None
	return value


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


def _invalid_webhook_event():
	frappe.throw(_("Invalid Bosta webhook event."), frappe.ValidationError)
