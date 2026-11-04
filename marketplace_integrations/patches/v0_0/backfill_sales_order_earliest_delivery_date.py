# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Fill ``custom_earliest_delivery_date`` of existing marketplace Sales Orders from their item delivery dates.

The countdown column reads the new field, which the sync sets only on newly built orders. For an order synced
before it existed the value is the earliest ``Sales Order Item`` delivery date, or the header ``delivery_date``
when no item has one. Only marketplace orders with an empty field are written, in batches and without touching
``modified``, so a second run changes nothing.

Patches in ``post_model_sync`` run before the fixtures are imported, so the Custom Field does not exist yet: the
patch creates it itself from ``fixtures/custom_field.json`` (the same entry the fixture import applies later).
"""

import json
import os

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

BATCH_SIZE = 500
FIELDNAME = "custom_earliest_delivery_date"


def execute():
	_ensure_custom_field()
	last_name = ""
	while True:
		# Paged by name so orders without any date (left empty) are not fetched again.
		rows = frappe.get_all(
			"Sales Order",
			filters={
				"custom_marketplace_order": ["is", "set"],
				FIELDNAME: ["is", "not set"],
				"name": [">", last_name],
			},
			fields=["name", "delivery_date"],
			order_by="name asc",
			limit=BATCH_SIZE,
		)
		if not rows:
			break
		last_name = rows[-1].name
		earliest_item_dates = _earliest_item_dates([row.name for row in rows])
		for row in rows:
			value = earliest_item_dates.get(row.name) or row.delivery_date
			if value:
				frappe.db.set_value("Sales Order", row.name, FIELDNAME, value, update_modified=False)


def _earliest_item_dates(names):
	rows = frappe.get_all(
		"Sales Order Item",
		filters={"parent": ["in", names], "parenttype": "Sales Order", "delivery_date": ["is", "set"]},
		fields=["parent", {"MIN": "delivery_date", "as": "earliest"}],
		group_by="parent",
	)
	return {row.parent: row.earliest for row in rows}


def _ensure_custom_field():
	path = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures", "custom_field.json")
	with open(path) as handle:
		entries = json.load(handle)
	fields = [
		{k: v for k, v in entry.items() if k not in ("doctype", "name", "modified", "module")}
		for entry in entries
		if entry["dt"] == "Sales Order" and entry["fieldname"] == FIELDNAME
	]
	create_custom_fields({"Sales Order": fields}, ignore_validate=True)
