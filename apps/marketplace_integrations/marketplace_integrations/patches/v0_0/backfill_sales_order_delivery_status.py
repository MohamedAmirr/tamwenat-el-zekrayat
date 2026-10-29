# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Backfill ``Sales Order.custom_delivery_status`` for existing Noon and Bosta orders (#378).

Patches run before fixtures are synced on ``bench migrate``, so on the first migrate the Custom
Field does not exist yet; like ``backfill_sales_order_bosta_delivery_status`` this patch creates
it from the fixture definition itself.  Noon orders get ``Shipped <n>/<total>`` from their order
rows, Bosta orders the same state ``Bosta Shipment`` writes.  Idempotent: a value that is already
right is not written, and ``modified`` is never touched.
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

FIELD_NAME = "Sales Order-custom_delivery_status"
FIELDNAME = "custom_delivery_status"
_NOT_FIELD_PROPERTIES = ("name", "creation", "modified", "modified_by", "owner", "docstatus", "doctype", "idx", "module")


def _ensure_custom_field():
	if frappe.db.exists("Custom Field", FIELD_NAME):
		return
	with open(frappe.get_app_path("marketplace_integrations", "fixtures", "custom_field.json")) as fixture:
		definition = next(row for row in json.load(fixture) if row.get("name") == FIELD_NAME)
	field = {key: value for key, value in definition.items() if key not in _NOT_FIELD_PROPERTIES}
	create_custom_fields({field["dt"]: [field]})


def _set(sales_order, value):
	if (frappe.db.get_value("Sales Order", sales_order, FIELDNAME) or "") != value:
		frappe.db.set_value("Sales Order", sales_order, FIELDNAME, value, update_modified=False)


def execute():
	if not frappe.db.table_exists("Sales Order"):
		return
	_ensure_custom_field()
	_backfill_noon()
	_backfill_bosta()


def _backfill_noon():
	if not frappe.db.table_exists("Marketplace Order"):
		return
	from marketplace_integrations.integrations.noon.orders import delivery_status_text

	orders = frappe.get_all(
		"Marketplace Order",
		filters={"platform": "Noon", "erpnext_sales_order": ["is", "set"]},
		fields=["name", "erpnext_sales_order"],
		limit_page_length=0,
	)
	for order in orders:
		rows = frappe.get_all(
			"Marketplace Order Item",
			filters={"parenttype": "Marketplace Order", "parent": order.name},
			fields=["external_status", "integration_status", "current_quantity"],
		)
		_set(order.erpnext_sales_order, delivery_status_text(rows))


def _backfill_bosta():
	if not frappe.db.table_exists("Bosta Shipment"):
		return
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
		_set(sales_order, sales_order_delivery_status(shipments))
