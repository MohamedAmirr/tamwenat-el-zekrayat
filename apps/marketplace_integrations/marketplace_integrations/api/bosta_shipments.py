# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Desk boundary for manual Bosta carrier delivery creation."""

import frappe
from frappe.utils import cint

from marketplace_integrations.integrations.bosta import locations
from marketplace_integrations.integrations.bosta.deliveries import (
	CREATOR_ROLES,
	get_sales_order_defaults,
)
from marketplace_integrations.integrations.bosta.deliveries import (
	check_bosta_shipment as _check_bosta_shipment,
)
from marketplace_integrations.integrations.bosta.deliveries import (
	create_bosta_shipment as _create_bosta_shipment,
)
from marketplace_integrations.integrations.bosta.deliveries import (
	get_sales_order_shipment as _get_sales_order_shipment,
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
def get_districts(city_id: str, refresh: int = 0):
	"""A Bosta city's drop-off-capable districts (cached 24h); ``refresh`` refetches them."""
	frappe.only_for(CREATOR_ROLES)
	return locations.get_districts(city_id, refresh=bool(cint(refresh)))


@frappe.whitelist(methods=["POST"])
def create_shipment(
	sales_order: str,
	receiver_name: str,
	receiver_phone: str,
	city: str,
	district_id: str = "",
	address_first_line: str = "",
	expected_cod: float | None = None,
):
	return _create_bosta_shipment(
		sales_order,
		receiver_name,
		receiver_phone,
		city,
		district_id,
		address_first_line,
		expected_cod,
	)


@frappe.whitelist(methods=["POST"])
def check_shipment(shipment: str):
	"""Resolve a stuck Bosta Shipment by searching Bosta for its business reference."""
	return _check_bosta_shipment(shipment)


@frappe.whitelist(methods=["POST"])
def refresh_shipment(shipment: str):
	"""Read the shipment's current state from Bosta and apply it (creator roles only)."""
	return _refresh_shipment_state(shipment)


@frappe.whitelist(methods=["POST"])
def cancel_shipment(shipment: str):
	"""Terminate the delivery in Bosta and cancel its Delivery Note (creator roles only)."""
	return _cancel_bosta_shipment(shipment)


@frappe.whitelist(methods=["POST"])
def print_a4_arabic_label(shipment: str):
	"""Return a private A4 Arabic Bosta label File for an authorized reader."""
	return create_a4_arabic_label(shipment)
