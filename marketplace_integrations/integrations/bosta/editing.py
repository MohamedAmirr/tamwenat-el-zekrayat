# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Edit a Bosta delivery's receiver phone and drop-off address from the Sales Order (#273).

Updates Bosta and the Bosta Shipment only; the Sales Order, its address and Shopify are not changed.
"""

import frappe
from frappe import _
from frappe.utils import cstr, escape_html

from marketplace_integrations.integrations.bosta import locations
from marketplace_integrations.integrations.bosta.client import BostaClient, is_success_response
from marketplace_integrations.integrations.bosta.deliveries import (
	CREATOR_ROLES,
	lock_and_load_sales_order,
)
from marketplace_integrations.marketplace_integrations.doctype.bosta_shipment.bosta_shipment import (
	CANCELED,
	save_as_system,
)

# Bosta's documented states in which a delivery may be edited (OpenAPI, "Business update delivery").
EDITABLE_STATE_CODES = (10, 11, 20, 21, 22, 24, 30, 41, 47)

# Bosta's documented limit is "more than 5" characters; the upper bound matches Address.address_line1.
ADDRESS_LINE_MAX_LENGTH = 240


def _verify_hint():
	return _("Nothing was changed in ERPNext. Check the delivery in Bosta before trying again.")


def edit_bosta_shipment(shipment_name, receiver_phone, city_id, address_first_line):
	"""Send the new receiver phone and drop-off address to Bosta, then record them on the Bosta Shipment.

	Creator roles only. Refused, without calling Bosta, unless the shipment has a tracking
	number, is not Canceled and its stored state is one Bosta allows editing in. The body is
	exactly ``receiver.phone`` and ``dropOffAddress`` (city code, firstLine); no district,
	Bosta resolves it (#318). A Bosta refusal or a network error changes nothing locally.
	"""
	frappe.only_for(CREATOR_ROLES)
	phone, first_line = _clean(receiver_phone, address_first_line)
	# Before the lock: this may call Bosta (cached lists).
	city = locations.get_city(city_id)
	shipment = frappe.get_doc("Bosta Shipment", shipment_name)
	shipment.check_permission("read")
	lock_and_load_sales_order(shipment.sales_order)
	shipment.reload()
	_refuse_edit_locally(shipment)
	client = BostaClient()
	client.ensure_ready()
	# Release the Sales Order lock before the HTTP call.
	frappe.db.commit()

	payload = {
		"receiver": {"phone": phone},
		"dropOffAddress": {"city": city["code"], "firstLine": first_line},
	}
	try:
		response = client.update_delivery(shipment.tracking_number, payload)
	except Exception:
		frappe.db.rollback()
		# Replace the client's own "Failed to reach Bosta" popup with the clearer message.
		frappe.clear_messages()
		frappe.throw(_("Could not confirm that Bosta updated the delivery. {0}").format(_verify_hint()))
	if not is_success_response(response):
		frappe.throw(f"{escape_html(client.error_message(response))} {_verify_hint()}")
	_record_on_shipment(shipment, phone, city, first_line)


def _clean(receiver_phone, address_first_line):
	phone, first_line = (cstr(value).strip() for value in (receiver_phone, address_first_line))
	if not phone:
		frappe.throw(_("Enter the receiver phone."))
	if len(first_line) <= 5:
		frappe.throw(_("Address First Line must be more than 5 characters."))
	if len(first_line) > ADDRESS_LINE_MAX_LENGTH:
		frappe.throw(_("Address First Line must be at most {0} characters.").format(ADDRESS_LINE_MAX_LENGTH))
	return phone, first_line


def _refuse_edit_locally(shipment):
	if not shipment.tracking_number:
		frappe.throw(_("This Bosta Shipment has no tracking number, so there is nothing to edit in Bosta."))
	if shipment.status == CANCELED:
		frappe.throw(_("A canceled Bosta Shipment cannot be edited."))
	if not shipment.bosta_state_code:
		frappe.throw(_("This shipment has no Bosta state stored yet. Refresh from Bosta first."))
	if shipment.bosta_state_code not in EDITABLE_STATE_CODES:
		frappe.throw(
			_("Bosta does not allow editing a delivery in state {0} ({1}).").format(
				shipment.bosta_state_code, escape_html(cstr(shipment.bosta_state_value))
			)
		)


def _record_on_shipment(shipment, phone, city, first_line):
	"""Bosta accepted the change: record it on the Bosta Shipment only. The Sales Order, its
	address and any Marketplace Order are not changed."""
	lock_and_load_sales_order(shipment.sales_order)
	try:
		shipment.reload()
		canceled = shipment.status == CANCELED
		if not canceled:
			_write_edit(shipment, phone, city, first_line)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			frappe.get_traceback(with_context=False), f"Bosta edit write-back failed for {shipment.name}"
		)
		frappe.throw(
			_(
				"Bosta accepted the change, but ERPNext could not record it. Nothing was changed in "
				"ERPNext; enter the same receiver phone and address again or fix it by hand. See Error Log."
			)
		)
	if canceled:
		frappe.throw(
			_(
				"Bosta accepted the change, but this shipment was canceled in the meantime, "
				"so ERPNext did not record it."
			)
		)


def _write_edit(shipment, phone, city, first_line):
	shipment.receiver_phone = phone
	shipment.city = city["name"]
	shipment.city_id = city["id"]
	shipment.city_code = city["code"]
	# Bosta now resolves the district itself; a district stored by an older create is stale.
	shipment.district_id = ""
	shipment.district_name = ""
	shipment.address_first_line = first_line
	save_as_system(shipment)
