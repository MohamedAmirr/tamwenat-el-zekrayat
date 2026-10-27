# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Backfill each Sales Order's Bosta delivery status from its shipments, idempotently.

Patches run before fixtures are synced on ``bench migrate`` (frappe/migrate.py), so on the
first migrate the Custom Field does not exist yet; this patch creates it from the fixture
definition itself, otherwise the backfill would be skipped and never run again.
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

FIELD_NAME = "Sales Order-custom_bosta_delivery_status"
_NOT_FIELD_PROPERTIES = ("name", "creation", "modified", "modified_by", "owner", "docstatus", "doctype", "idx", "module")


def _ensure_custom_field():
	if frappe.db.exists("Custom Field", FIELD_NAME):
		return
	with open(frappe.get_app_path("marketplace_integrations", "fixtures", "custom_field.json")) as fixture:
		definition = next(row for row in json.load(fixture) if row.get("name") == FIELD_NAME)
	field = {key: value for key, value in definition.items() if key not in _NOT_FIELD_PROPERTIES}
	create_custom_fields({field["dt"]: [field]})


def execute():
	if not frappe.db.table_exists("Bosta Shipment") or not frappe.db.table_exists("Sales Order"):
		return
	_ensure_custom_field()

	from marketplace_integrations.marketplace_integrations.doctype.bosta_shipment.bosta_shipment import (
		sales_order_delivery_status,
	)

	by_sales_order = {}
	for shipment in frappe.get_all(
		"Bosta Shipment",
		fields=["name", "sales_order", "status", "bosta_state_code"],
		order_by="creation desc, name desc",
		limit_page_length=0,
	):
		if shipment.sales_order:
			by_sales_order.setdefault(shipment.sales_order, []).append(shipment)

	for sales_order, shipments in by_sales_order.items():
		value = sales_order_delivery_status(shipments)
		if frappe.db.get_value("Sales Order", sales_order, "custom_bosta_delivery_status") != value:
			frappe.db.set_value(
				"Sales Order", sales_order, "custom_bosta_delivery_status", value, update_modified=False
			)
