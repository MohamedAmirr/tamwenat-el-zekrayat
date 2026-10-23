# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Backfill each Sales Order from the newest Bosta shipment, idempotently."""

import frappe


def execute():
	if not frappe.db.table_exists("Bosta Shipment") or not frappe.db.table_exists("Sales Order"):
		return
	if not frappe.db.exists("Custom Field", "Sales Order-custom_bosta_delivery_status"):
		return
	selected = {}
	for shipment in frappe.get_all(
		"Bosta Shipment",
		fields=["name", "sales_order", "status", "bosta_state_code"],
		order_by="creation desc, name desc",
		limit_page_length=0,
	):
		if not shipment.sales_order:
			continue
		current = selected.get(shipment.sales_order)
		if current is None or (current.status == "Canceled" and shipment.status != "Canceled"):
			selected[shipment.sales_order] = shipment
	from marketplace_integrations.integrations.bosta.statuses import BOSTA_STATE_NAMES

	for sales_order, shipment in selected.items():
		value = BOSTA_STATE_NAMES.get(shipment.bosta_state_code) or shipment.status or ""
		if frappe.db.get_value("Sales Order", sales_order, "custom_bosta_delivery_status") != value:
			frappe.db.set_value(
				"Sales Order", sales_order, "custom_bosta_delivery_status", value, update_modified=False
			)
