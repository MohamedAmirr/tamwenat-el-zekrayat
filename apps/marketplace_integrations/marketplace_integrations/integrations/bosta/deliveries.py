# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bosta delivery creation and the corresponding ERPNext Delivery Note."""

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from frappe import _
from frappe.utils import cstr

from marketplace_integrations.integrations.bosta.client import BostaClient

BOSTA_DELIVERY_TYPE = 10


def get_sales_order_defaults(sales_order_name):
	"""Return only fields that can safely prefill the operator's dialog."""
	order = _get_submitted_sales_order(sales_order_name)
	return {
		"receiver_name": cstr(order.customer_name).strip(),
		"receiver_phone": cstr(order.contact_mobile or order.contact_phone).strip(),
	}


def create_bosta_shipment(
	sales_order_name,
	receiver_name,
	receiver_phone,
	city,
	district_id,
	city_id,
	district_name,
	address_first_line,
):
	"""Create Bosta's delivery, then the full remaining ERPNext Delivery Note.

	The local row is persisted before and after the remote call.  Thus a Bosta
	acceptance followed by an ERPNext Delivery Note failure is visible and can be
	resumed without ever submitting a second Bosta delivery.
	"""
	order = _get_submitted_sales_order(sales_order_name)
	frappe.db.get_value("Sales Order", order.name, "name", for_update=True)
	existing_name = frappe.db.get_value("Bosta Shipment", {"sales_order": order.name}, "name")
	if existing_name:
		existing = frappe.get_doc("Bosta Shipment", existing_name)
		if existing.status == "Delivery Note Failed" and existing.bosta_delivery_id:
			return _create_delivery_note(existing, order)
		frappe.throw(_("Bosta Shipment {0} already exists for this Sales Order.").format(existing.name))

	shipment = frappe.get_doc(
		{
			"doctype": "Bosta Shipment",
			"sales_order": order.name,
			"receiver_name": receiver_name,
			"receiver_phone": receiver_phone,
			"city": city,
			"district_id": district_id,
			"city_id": city_id,
			"district_name": district_name,
			"address_first_line": address_first_line,
		}
	)
	shipment.insert()
	shipment.unique_business_reference = f"BOSTA-{shipment.name}"
	shipment.save()
	# The unique local row must survive a network/remote failure, preventing a
	# second request that could duplicate a delivery after an ambiguous timeout.
	frappe.db.commit()
	if order.grand_total <= 0:
		frappe.msgprint(
			_(
				"This Sales Order has a non-positive COD amount. Bosta may reject a Deliver shipment, but it will be submitted unchanged."
			)
		)

	payload = _build_delivery_payload(shipment, order)
	response = BostaClient().create_delivery(payload)
	if not _is_accepted_response(response):
		shipment.status = "Bosta Failed"
		shipment.last_error = BostaClient().error_message(response)
		shipment.save()
		frappe.db.commit()
		frappe.throw(shipment.last_error)

	body = _response_body(response)
	data = body.get("data") if isinstance(body.get("data"), dict) else body
	delivery_id = cstr(data.get("_id") or data.get("id")).strip()
	tracking_number = cstr(data.get("trackingNumber")).strip()
	if not delivery_id or not tracking_number:
		shipment.status = "Bosta Failed"
		shipment.last_error = _("Bosta accepted the request without a delivery ID and tracking number.")
		shipment.save()
		frappe.db.commit()
		frappe.throw(shipment.last_error)

	shipment.bosta_delivery_id = delivery_id
	shipment.tracking_number = tracking_number
	shipment.status = "Bosta Created"
	shipment.last_error = ""
	shipment.save()
	frappe.db.commit()
	return _create_delivery_note(shipment, order)


def _get_submitted_sales_order(sales_order_name):
	order = frappe.get_doc("Sales Order", sales_order_name)
	order.check_permission("submit")
	if order.docstatus != 1:
		frappe.throw(_("Bosta Shipment can only be created from a submitted Sales Order."))
	return order


def _build_delivery_payload(shipment, order):
	"""The MVP sends only Bosta's required fields and approved exceptions."""
	settings = frappe.get_single("Bosta Settings")
	drop_off_address = {"city": shipment.city, "firstLine": shipment.address_first_line}
	if cstr(shipment.district_id).strip():
		drop_off_address["districtId"] = shipment.district_id
	else:
		drop_off_address.update({"cityId": shipment.city_id, "districtName": shipment.district_name})

	return {
		"type": BOSTA_DELIVERY_TYPE,
		"cod": order.grand_total,
		"receiver": {"firstName": shipment.receiver_name, "phone": shipment.receiver_phone},
		"dropOffAddress": drop_off_address,
		"businessLocationId": settings.business_location_id,
		"uniqueBusinessReference": shipment.unique_business_reference,
	}


def _is_accepted_response(response):
	return 200 <= response.status_code < 300 and _response_body(response).get("success") is not False


def _response_body(response):
	try:
		body = response.json()
	except ValueError:
		return {}
	return body if isinstance(body, dict) else {}


def _create_delivery_note(shipment, order):
	try:
		delivery_note = make_delivery_note(order.name)
		if not delivery_note.items:
			frappe.throw(_("The Sales Order has no remaining quantity to deliver."))
		delivery_note.insert()
		delivery_note.submit()
	except Exception:
		frappe.log_error(frappe.get_traceback(with_context=False), "Bosta Delivery Note creation failed")
		shipment.status = "Delivery Note Failed"
		shipment.last_error = _("Delivery Note creation failed. See Error Log for details.")
		shipment.save()
		frappe.db.commit()
		raise

	shipment.delivery_note = delivery_note.name
	shipment.status = "Completed"
	shipment.last_error = ""
	shipment.save()
	return {
		"shipment": shipment.name,
		"delivery_note": delivery_note.name,
		"tracking_number": shipment.tracking_number,
	}
