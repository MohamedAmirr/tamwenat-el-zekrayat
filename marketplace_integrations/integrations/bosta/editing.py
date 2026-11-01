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

import json

import frappe
from frappe import _
from frappe.utils import cstr, escape_html, now_datetime

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
