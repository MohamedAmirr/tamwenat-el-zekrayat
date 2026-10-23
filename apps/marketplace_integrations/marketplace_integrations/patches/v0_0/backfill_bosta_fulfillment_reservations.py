# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Add fulfillment identities and conservative links for existing Bosta shipments."""

import hashlib
import re

import frappe
from frappe.utils import flt

from marketplace_integrations.marketplace_integrations.doctype.bosta_shipment.bosta_shipment import (
	save_as_system,
)


def execute():
	if not frappe.db.table_exists("Bosta Shipment"):
		return
	_drop_legacy_active_sales_order_unique_index()

	shipments = frappe.get_all(
		"Bosta Shipment",
		fields=["name", "sales_order", "delivery_note", "status", "bosta_delivery_id"],
		order_by="name asc",
	)
	id_counts = {}
	for shipment in shipments:
		delivery_id = str(shipment.bosta_delivery_id or "").strip()
		if delivery_id:
			id_counts[delivery_id] = id_counts.get(delivery_id, 0) + 1

	delivery_note_counts = {}
	for shipment in shipments:
		if shipment.delivery_note:
			delivery_note_counts[shipment.delivery_note] = delivery_note_counts.get(shipment.delivery_note, 0) + 1

	for row in shipments:
		shipment = frappe.get_doc("Bosta Shipment", row.name)
		if not shipment.company and shipment.sales_order:
			shipment.company = frappe.db.get_value("Sales Order", shipment.sales_order, "company")
		if not shipment.fulfillment_key:
			shipment.fulfillment_key = frappe.generate_hash(length=40)
		delivery_id = str(shipment.bosta_delivery_id or "").strip()
		if delivery_id and id_counts.get(delivery_id) == 1:
			shipment.external_delivery_identity_key = hashlib.sha256(
				frappe.as_json(["Bosta", delivery_id]).encode()
			).hexdigest()
		elif delivery_id:
			# Leave conflicting historical identities without a unique key. Do not alter
			# or choose between the source records; production review remains manual.
			shipment.external_delivery_identity_key = None

		if shipment.status == "Canceled":
			shipment.reservation_status = "Released"
		elif shipment.delivery_note and _submitted_delivery_note(shipment.delivery_note):
			shipment.reservation_status = "Fulfilled"
		else:
			# In particular, an old Bosta Failed row may represent an unknown remote
			# outcome. Keep it reserved until Check Bosta resolves that operation.
			shipment.reservation_status = "Reserved"

		if (
			shipment.delivery_note
			and delivery_note_counts.get(shipment.delivery_note) == 1
			and not shipment.shipment_items
		):
			_delivery_note_items_to_shipment(shipment)

		save_as_system(shipment)


def _drop_legacy_active_sales_order_unique_index():
	"""The old unique index conflicts with the shipment-item reservation model."""
	indexes = frappe.db.sql(
		"SHOW INDEX FROM `tabBosta Shipment` WHERE Column_name = %s",
		("active_sales_order",),
		as_dict=True,
	)
	unique_indexes = {
		row.Key_name
		for row in indexes
		if row.Non_unique == 0 and row.Key_name != "PRIMARY" and re.fullmatch(r"[A-Za-z0-9_]+", row.Key_name)
	}
	for index_name in unique_indexes:
		frappe.db.sql(f"ALTER TABLE `tabBosta Shipment` DROP INDEX `{index_name}`")


def _submitted_delivery_note(name):
	return frappe.db.get_value("Delivery Note", name, "docstatus") == 1


def _delivery_note_items_to_shipment(shipment):
	"""Copy only explicit SO-row links from the already-linked Delivery Note."""
	delivery_note = frappe.get_doc("Delivery Note", shipment.delivery_note)
	if delivery_note.is_return:
		return
	for item in delivery_note.items:
		if not item.so_detail or not item.item_code:
			continue
		so_parent, so_item_code = frappe.db.get_value(
			"Sales Order Item", item.so_detail, ["parent", "item_code"]
		) or (None, None)
		if so_parent != shipment.sales_order or so_item_code != item.item_code:
			continue
		shipment.append(
			"shipment_items",
			{
				"so_detail": item.so_detail,
				"item_code": item.item_code,
				"warehouse": item.warehouse,
				"qty": flt(item.qty),
				"uom": item.uom,
				"conversion_factor": flt(item.conversion_factor),
				"stock_qty": flt(item.stock_qty),
				"fulfilled_qty": flt(item.stock_qty) if delivery_note.docstatus == 1 else 0,
				"batch_no": item.batch_no,
				"serial_no": item.serial_no,
				"delivery_note_item": item.name,
			},
		)
