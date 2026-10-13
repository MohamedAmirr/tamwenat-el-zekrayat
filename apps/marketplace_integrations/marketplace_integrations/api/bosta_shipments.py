# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Desk boundary for manual Bosta carrier delivery creation."""

import frappe

from marketplace_integrations.integrations.bosta.deliveries import (
	create_bosta_shipment as _create_bosta_shipment,
)
from marketplace_integrations.integrations.bosta.deliveries import (
	get_sales_order_defaults,
)


@frappe.whitelist(methods=["POST"])
def get_create_defaults(sales_order: str):
	return get_sales_order_defaults(sales_order)


@frappe.whitelist(methods=["POST"])
def create_shipment(
	sales_order: str,
	receiver_name: str,
	receiver_phone: str,
	city: str,
	district_id: str,
	city_id: str,
	district_name: str,
	address_first_line: str,
):
	return _create_bosta_shipment(
		sales_order,
		receiver_name,
		receiver_phone,
		city,
		district_id,
		city_id,
		district_name,
		address_first_line,
	)
