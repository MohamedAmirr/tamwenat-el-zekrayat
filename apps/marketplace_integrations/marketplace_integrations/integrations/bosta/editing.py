# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Edit the receiver phone and drop-off address of a created Bosta delivery (epic #413, #410).

Bosta's ``PUT /deliveries/business/{trackingNumber}`` (docs.yaml, "Business update delivery") is allowed only in
``EDITABLE_STATE_CODES``. The body holds only what changed against the request snapshot
(``custom_carrier_request_snapshot``, the payload that was sent): ``receiver.phone``, and ``dropOffAddress``
(city code and first line, sent as the one object the proven #273 call used; no district, Bosta resolves it).

On a 2xx the note's ``contact_mobile`` and Bosta city fields and the snapshot are written together with
``db_set`` (a system write-back that skips ``validate``: the person's own save cannot change a started
shipment). The shared Address, the Sales Order and Shopify are not changed (open question D12). No lock is
held across the HTTP call. The Integration Request log operation is "Edit".
"""

import html
import json

import frappe
from frappe import _
from frappe.utils import cstr, escape_html, flt, now_datetime, strip_html_tags

from marketplace_integrations.integrations.bosta import delivery_note_shipment as shipment
from marketplace_integrations.integrations.bosta import locations, statuses
from marketplace_integrations.integrations.bosta.client import SERVICE_NAME, BostaClient, is_success_response
from marketplace_integrations.services import carrier_log

#: Bosta's documented states in which a delivery may be edited (docs.yaml, "Business update delivery").
EDITABLE_STATE_CODES = (10, 11, 20, 21, 22, 24, 30, 41, 47)
#: Bosta's documented lower bound is "more than 5" characters; the upper bound matches Address.address_line1.
ADDRESS_LINE_MIN_LENGTH = 6
ADDRESS_LINE_MAX_LENGTH = 240


def _hint():
	return _("Nothing was changed in ERPNext. Check the delivery in Bosta before trying again.")


def _snapshot(raw):
	try:
		data = frappe.parse_json(raw) if raw else {}
	except Exception:
		return {}
	return data if isinstance(data, dict) else {}


def _baseline(snapshot):
	"""What Bosta holds, as the snapshot recorded it (empty strings when the note has no snapshot, for
	example a note migrated from the old flow: everything entered is then sent)."""
	receiver = snapshot.get("receiver") if isinstance(snapshot.get("receiver"), dict) else {}
	address = snapshot.get("dropOffAddress") if isinstance(snapshot.get("dropOffAddress"), dict) else {}
	return {
		"phone": cstr(receiver.get("phone")).strip(),
		"city": cstr(address.get("city")).strip(),
		"first_line": cstr(address.get("firstLine")).strip(),
	}


def get_edit_values(delivery_note):
	"""The values the edit dialog starts from: the snapshot's phone, city code and first line, plus the
	Bosta city id for the picker (``None`` when the city lists cannot be read)."""
	values = frappe.db.get_value(
		"Delivery Note",
		delivery_note,
		["custom_carrier_request_snapshot", "custom_bosta_city", "custom_bosta_city_name"],
		as_dict=True,
	)
	base = _baseline(_snapshot(values.custom_carrier_request_snapshot))
	code = base["city"] or cstr(values.custom_bosta_city).strip()
	city_id = None
	try:
		city_id = (
			next((c["id"] for c in locations.get_directory() if c["code"] == code), None) if code else None
		)
	except locations.BostaLookupUnavailable:
		city_id = None
	return {
		"phone": base["phone"],
		"first_line": base["first_line"],
		"city_id": city_id,
		"city_name": cstr(values.custom_bosta_city_name),
	}


def _clean(receiver_phone, address_first_line):
	phone, first_line = (cstr(value).strip() for value in (receiver_phone, address_first_line))
	if not phone:
		frappe.throw(_("Enter the receiver phone."))
	if len(first_line) < ADDRESS_LINE_MIN_LENGTH:
		frappe.throw(_("Address First Line must be more than 5 characters."))
	if len(first_line) > ADDRESS_LINE_MAX_LENGTH:
		frappe.throw(_("Address First Line must be at most {0} characters.").format(ADDRESS_LINE_MAX_LENGTH))
	return phone, first_line


def _status(value):
	return cstr(value) or shipment.NOT_CREATED


def _state_code(name):
	return next((code for code, state in statuses.BOSTA_STATE_NAMES.items() if state == name), None)


def _refuse_edit_locally(values):
	if _status(values.custom_integration_status) != shipment.CREATED:
		frappe.throw(_("Only a note with a created Bosta shipment can be edited."))
	if not cstr(values.lr_no).strip():
		frappe.throw(_("This Delivery Note has no Bosta tracking number."))
	if not values.custom_shipping_status:
		frappe.throw(_("This shipment has no Bosta state stored yet. Refresh from Bosta first."))
	if _state_code(values.custom_shipping_status) not in EDITABLE_STATE_CODES:
		frappe.throw(
			_("Bosta does not allow editing a delivery in state {0}.").format(
				escape_html(_(values.custom_shipping_status))
			)
		)


def edit_shipment(delivery_note, receiver_phone, city_id, address_first_line):
	"""Send the changed receiver phone and drop-off address to Bosta, then record them on the note (the API
	checked the write permission). Refused, without calling Bosta, unless the note is Created and in an
	editable Bosta state, and when nothing differs from the snapshot. A Bosta refusal or a network error
	changes nothing locally."""
	phone, first_line = _clean(receiver_phone, address_first_line)
	# Before the lock: this may call Bosta (cached lists).
	city = locations.get_city(city_id)
	BostaClient().ensure_ready()
	frappe.db.commit()
	values = frappe.db.get_value(
		"Delivery Note",
		delivery_note,
		[
			"custom_integration_status",
			"custom_shipping_status",
			"lr_no",
			"docstatus",
			"custom_carrier_reference",
			"custom_carrier_request_snapshot",
		],
		as_dict=True,
		for_update=True,
	)
	try:
		if values.docstatus == 2:
			frappe.throw(_("A cancelled Delivery Note cannot be edited."))
		_refuse_edit_locally(values)
		snapshot = _snapshot(values.custom_carrier_request_snapshot)
		base = _baseline(snapshot)
		payload = {}
		if phone != base["phone"]:
			payload["receiver"] = {"phone": phone}
		if city["code"] != base["city"] or first_line != base["first_line"]:
			payload["dropOffAddress"] = {"city": city["code"], "firstLine": first_line}
		if not payload:
			frappe.throw(_("Nothing was changed."))
	finally:
		# Release the row lock before the HTTP call (also when a refusal was thrown).
		frappe.db.commit()
	tracking = cstr(values.lr_no).strip()
	log = carrier_log.open_log(
		SERVICE_NAME, delivery_note, "Edit", reference=cstr(values.custom_carrier_reference) or delivery_note
	)
	frappe.db.commit()

	client = BostaClient()
	try:
		response = client.update_delivery(tracking, payload)
	except Exception:
		frappe.db.rollback()
		carrier_log.close_log(
			log, "Unknown", error=_("The Bosta request failed. It may or may not have reached Bosta.")
		)
		frappe.db.commit()
		# Replace the client's own "Failed to reach Bosta" popup with the clearer message.
		frappe.clear_messages()
		frappe.throw(_("Could not confirm that Bosta updated the delivery. {0}").format(_hint()))
	if not is_success_response(response):
		message = client.error_message(response)
		carrier_log.close_log(log, "Failed", error=message, http_status=response.status_code)
		frappe.db.commit()
		frappe.throw(f"{escape_html(message)} {_hint()}")
	carrier_log.close_log(log, "Success", http_status=response.status_code)
	return _record(delivery_note, payload, snapshot, phone, city)


# ---------------------------------------------------------------------------
# COD update (#501)
# ---------------------------------------------------------------------------
#
# Bosta can update the COD of a started delivery (``cod`` of ``DeliveryEditBody``, docs.yaml) but not its items, the
# parcel description or the label. ``custom_cod_amount`` is what ERPNext wants Bosta to collect; the snapshot's
# ``cod`` is the COD Bosta was LAST SENT (written BEFORE the call and put back only on a definite refusal, so an
# unknown answer or a crash leaves a visible difference). A difference between the two is the Needs Attention row
# "Bosta COD Differs" (``cod_differs``); its Retry is ``sync_cod``, which sends ``custom_cod_amount`` again. A PUT of
# the same COD is harmless, so a repeat is also the "check" of an unknown answer: no separate read is needed.
# A stored label is requested again after every successful update (it prints the COD).


def snapshot_cod(raw_snapshot):
	"""The COD Bosta was last sent, from the request snapshot; ``None`` when the snapshot has none (a note from
	before the snapshot existed: nothing to compare)."""
	snapshot = _snapshot(raw_snapshot)
	return flt(snapshot["cod"], 2) if snapshot.get("cod") is not None else None


def cod_differs(custom_cod_amount, raw_snapshot):
	"""Whether the COD Bosta was last sent is not the COD ERPNext wants it to collect."""
	sent = snapshot_cod(raw_snapshot)
	return sent is not None and sent != flt(custom_cod_amount, 2)


def cod_edit_blocker(note):
	"""Why Bosta cannot be sent a COD update for this Created note now (its status is not stored yet, no tracking
	number, a state Bosta does not allow editing in), as plain text, else ``None``. Same rules as ``update_cod``,
	asked before anything is changed. ``note`` is a Delivery Note or its values."""
	try:
		_refuse_edit_locally(note)
	except frappe.ValidationError as exc:
		frappe.clear_messages()
		# plain text: the client escapes it where it shows it, so the entities of the message are undone here
		return html.unescape(strip_html_tags(cstr(exc))).strip()
	return None


def _store_snapshot_cod(delivery_note, snapshot, cod):
	new = dict(snapshot)
	if cod is None:
		# restoring a snapshot that had no COD: leave none (a stored value would show a false "Bosta COD Differs")
		new.pop("cod", None)
	else:
		new["cod"] = cod
	frappe.db.set_value(
		"Delivery Note", delivery_note, "custom_carrier_request_snapshot", json.dumps(new), update_modified=False
	)


def update_cod(delivery_note, cod):
	"""Send ``{"cod": cod}`` for the note's Bosta delivery. Returns ``_dict(outcome, message)``: ``updated``
	(Bosta answered 2xx; a stored label is requested again), ``failed`` (a definite 4xx refusal; nothing changed)
	or ``unknown`` (timeout, 5xx, 429: Bosta may hold the new COD, so the snapshot keeps it and
	``cod_differs`` shows the note). ``message`` is Bosta's raw text (the caller escapes it).

	Raises, changing nothing, when the note cannot be edited at Bosta (not Created, no tracking number, a Bosta
	state that does not allow editing, a COD over Bosta's limit). Commits; no lock is held across the call."""
	cod = flt(cod, 2)
	shipment._check_cod(cod)
	BostaClient().ensure_ready()
	frappe.db.commit()
	values = frappe.db.get_value(
		"Delivery Note",
		delivery_note,
		[
			"custom_integration_status",
			"custom_shipping_status",
			"lr_no",
			"docstatus",
			"custom_carrier_reference",
			"custom_carrier_request_snapshot",
		],
		as_dict=True,
		for_update=True,
	)
	try:
		if values.docstatus == 2:
			frappe.throw(_("A cancelled Delivery Note cannot be edited."))
		_refuse_edit_locally(values)
		snapshot = _snapshot(values.custom_carrier_request_snapshot)
		previous = snapshot.get("cod")
		# Written before the call: if the request is lost or the process dies, the difference stays visible.
		_store_snapshot_cod(delivery_note, snapshot, cod)
	finally:
		frappe.db.commit()
	tracking = cstr(values.lr_no).strip()
	log = carrier_log.open_log(
		SERVICE_NAME, delivery_note, "Edit", reference=cstr(values.custom_carrier_reference) or delivery_note
	)
	frappe.db.commit()

	client = BostaClient()
	try:
		response = client.update_delivery(tracking, {"cod": cod})
	except Exception:
		frappe.db.rollback()
		message = _("The Bosta request failed. It may or may not have reached Bosta.")
		carrier_log.close_log(log, "Unknown", error=message)
		frappe.db.commit()
		frappe.clear_messages()
		return frappe._dict(outcome="unknown", message=message)
	if not is_success_response(response):
		message = client.error_message(response)
		if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
			# A definite refusal: Bosta holds what it held before.
			_store_snapshot_cod(delivery_note, snapshot, previous)
			carrier_log.close_log(log, "Failed", error=message, http_status=response.status_code)
			frappe.db.commit()
			return frappe._dict(outcome="failed", message=message)
		carrier_log.close_log(log, "Unknown", error=message, http_status=response.status_code)
		frappe.db.commit()
		return frappe._dict(outcome="unknown", message=message)
	carrier_log.close_log(log, "Success", http_status=response.status_code)
	shipment.queue_label_refresh(delivery_note)
	frappe.db.commit()
	return frappe._dict(outcome="updated", message="")


def _order_with_open_edit(delivery_note):
	"""The Marketplace Order of the note's Sales Order that still has an item-edit marker (``outbound_edit_state`` set:
	in flight or completed but not re-baselined), else ``None``."""
	sales_orders = frappe.get_all(
		"Delivery Note Item",
		filters={"parent": delivery_note, "parenttype": "Delivery Note"},
		pluck="against_sales_order",
		distinct=True,
		parent_doctype="Delivery Note",
		limit_page_length=0,
	)
	for sales_order in filter(None, sales_orders):
		order = frappe.db.get_value("Sales Order", sales_order, "custom_marketplace_order")
		if order and frappe.db.get_value("Marketplace Order", order, "outbound_edit_state"):
			return order
	return None


def sync_cod(delivery_note):
	"""Needs Attention "Bosta COD Differs" Retry: send ``custom_cod_amount`` to Bosta again. Returns
	``{"status": "in_sync" | "updated"}``; raises with Bosta's reason (escaped) when Bosta refused or did not
	answer, and the row stays."""
	values = frappe.db.get_value(
		"Delivery Note", delivery_note, ["custom_cod_amount", "custom_carrier_request_snapshot"], as_dict=True
	)
	if not values or not cod_differs(values.custom_cod_amount, values.custom_carrier_request_snapshot):
		return {"status": "in_sync"}
	order = _order_with_open_edit(delivery_note)
	if order:
		# An Edit Order sent the new COD to Bosta, and Shopify's answer is not settled: ``custom_cod_amount`` may still be
		# the OLD value, which a retry would push back over the right COD.
		frappe.throw(
			_(
				"An item edit of order {0} is not settled with Shopify yet, so the COD is not sent again. Check the order "
				"in Shopify, clear the edit marker on the Marketplace Order, then retry."
			).format(order)
		)
	result = update_cod(delivery_note, values.custom_cod_amount)
	if result.outcome != "updated":
		frappe.throw(f"{escape_html(result.message)} {_hint()}")
	return {"status": "updated"}


def _record(delivery_note, payload, snapshot, phone, city):
	"""Bosta accepted the change: record it on the note and the snapshot together, if the note is still the
	Created shipment it was."""
	frappe.db.commit()
	current = frappe.db.get_value(
		"Delivery Note",
		delivery_note,
		["custom_integration_status", "docstatus"],
		as_dict=True,
		for_update=True,
	)
	if current.docstatus == 2 or _status(current.custom_integration_status) != shipment.CREATED:
		frappe.db.commit()
		frappe.throw(
			_(
				"Bosta accepted the change, but this shipment was cancelled in the meantime, "
				"so ERPNext did not record it."
			)
		)
	new_snapshot = dict(snapshot)
	changes = {"custom_last_sync_at": now_datetime()}
	if "receiver" in payload:
		new_snapshot["receiver"] = {**(snapshot.get("receiver") or {}), **payload["receiver"]}
		changes["contact_mobile"] = phone
	if "dropOffAddress" in payload:
		new_snapshot["dropOffAddress"] = {
			**(snapshot.get("dropOffAddress") or {}),
			**payload["dropOffAddress"],
		}
		changes["custom_bosta_city"] = city["code"]
		changes["custom_bosta_city_name"] = city["name"]
	changes["custom_carrier_request_snapshot"] = json.dumps(new_snapshot)
	frappe.get_doc("Delivery Note", delivery_note).db_set(
		changes, update_modified=True, notify=True, commit=True
	)
	return {"status": shipment.CREATED}
