# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Desk boundary for manual Bosta carrier delivery creation."""

import frappe
from frappe.utils import cint

from marketplace_integrations.integrations.bosta import bulk, locations
from marketplace_integrations.integrations.bosta.deliveries import (
	CREATOR_ROLES,
	get_sales_order_defaults,
)
from marketplace_integrations.integrations.bosta.deliveries import (
	check_bosta_shipment as _check_bosta_shipment,
)
from marketplace_integrations.integrations.bosta.deliveries import (
	confirm_physical_handover as _confirm_physical_handover,
)
from marketplace_integrations.integrations.bosta.deliveries import (
	create_bosta_shipment as _create_bosta_shipment,
)
from marketplace_integrations.integrations.bosta.deliveries import (
	get_sales_order_shipment as _get_sales_order_shipment,
)
from marketplace_integrations.integrations.bosta.editing import (
	edit_bosta_shipment as _edit_bosta_shipment,
)
from marketplace_integrations.integrations.bosta.labels import create_a4_arabic_label
from marketplace_integrations.integrations.bosta.statuses import (
	cancel_bosta_shipment as _cancel_bosta_shipment,
)
from marketplace_integrations.integrations.bosta.statuses import (
	refresh_shipment_state as _refresh_shipment_state,
)


@frappe.whitelist(methods=["POST"])
def get_create_defaults(sales_order: str):
	return get_sales_order_defaults(sales_order)


@frappe.whitelist(methods=["POST"])
def get_sales_order_shipment(sales_order: str):
	"""The order's current Bosta shipment summary for the Sales Order form (#246)."""
	return _get_sales_order_shipment(sales_order)


@frappe.whitelist(methods=["POST"])
def get_cities(refresh: int = 0):
	"""Bosta's cities for the create dialog (cached 24h); ``refresh`` refetches them."""
	frappe.only_for(CREATOR_ROLES)
	return locations.get_cities(refresh=bool(cint(refresh)))


@frappe.whitelist(methods=["POST"])
def create_shipment(
	sales_order: str,
	receiver_name: str,
	receiver_phone: str,
	city_id: str,
	address_first_line: str = "",
	expected_cod: float | None = None,
):
	return _create_bosta_shipment(
		sales_order,
		receiver_name,
		receiver_phone,
		city_id,
		address_first_line,
		expected_cod,
	)


@frappe.whitelist(methods=["POST"])
def get_bulk_create_plan(sales_orders):
	"""Sort the selected Sales Orders into skipped / ready / needs review, without calling
	Bosta (creator roles only, at most 50 orders)."""
	return bulk.get_bulk_create_plan(sales_orders)


@frappe.whitelist(methods=["POST"])
def queue_bulk_create(orders, skipped=None):
	"""Queue the background job that creates the given orders' Bosta shipments."""
	return bulk.queue_bulk_create(orders, skipped)


@frappe.whitelist()
def get_bulk_create_status(job_id: str):
	"""Poll a bulk create job. Only the user who queued it can read it."""
	return bulk.get_bulk_create_status(job_id)


@frappe.whitelist(methods=["POST"])
def check_shipment(shipment: str):
	"""Resolve a stuck Bosta Shipment by searching Bosta for its business reference."""
	return _check_bosta_shipment(shipment)


@frappe.whitelist(methods=["POST"])
def confirm_physical_handover(shipment: str):
	"""Record authorized physical handover and post the reserved Delivery Note."""
	return _confirm_physical_handover(shipment)


@frappe.whitelist(methods=["POST"])
def refresh_shipment(shipment: str):
	"""Read the shipment's current state from Bosta and apply it (creator roles only)."""
	return _refresh_shipment_state(shipment)


@frappe.whitelist(methods=["POST"])
def cancel_shipment(shipment: str):
	"""Terminate the delivery in Bosta and cancel its Delivery Note (creator roles only)."""
	return _cancel_bosta_shipment(shipment)


@frappe.whitelist(methods=["POST"])
def edit_shipment(
	shipment: str,
	receiver_phone: str,
	city_id: str,
	address_first_line: str,
):
	"""Update the delivery's receiver phone and drop-off address in Bosta and ERPNext."""
	return _edit_bosta_shipment(shipment, receiver_phone, city_id, address_first_line)


@frappe.whitelist(methods=["POST"])
def print_a4_arabic_label(shipment: str):
	"""Return a private A4 Arabic Bosta label File for an authorized reader."""
	return create_a4_arabic_label(shipment)
