# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#390: record the Delivery Note of an already handed-over Noon shipment on its unit rows.

Before #390 a shipment's Delivery Notes were stored on the shipment row (``delivery_note`` and the
``delivery_notes`` JSON list).  The note is now recorded per unit (``Marketplace Order Shipment
Unit.delivery_note``) and a unit counts as covered while that note is live.  For every shipment
with a live note in the old fields, this sets the shipment's first live note on the unit rows that
still have none, except the units listed as unresolved in ``skipped_units`` (those were never on
the note).  Without it, the next SHIPPED import would draft a second note for the same units.

Idempotent: only empty unit rows are written, so a second run changes nothing; a site with no
handover data finds no shipment to process.  Runs after the model sync (the new column exists).
"""

import frappe
from frappe.utils import cint, cstr

from marketplace_integrations.integrations.noon.handover_state import live_note, load_entries

SHIPMENT = "Marketplace Order Shipment"
UNIT = "Marketplace Order Shipment Unit"


def _legacy_notes(shipment):
	names = []
	raw = cstr(shipment.delivery_notes).strip()
	if raw:
		try:
			listed = frappe.parse_json(raw)
		except ValueError:
			listed = []
		names = [n for n in listed if isinstance(n, str)] if isinstance(listed, list) else []
	first = cstr(shipment.delivery_note).strip()
	if first and first not in names:
		names.insert(0, first)
	return names


def execute():
	if not frappe.db.table_exists(UNIT) or not frappe.db.has_column(UNIT, "delivery_note"):
		return
	shipments = frappe.get_all(
		SHIPMENT,
		or_filters={"delivery_note": ["!=", ""], "delivery_notes": ["!=", ""]},
		fields=["name", "parent", "integration_shipment_nr", "delivery_note", "delivery_notes", "skipped_units"],
		parent_doctype="Marketplace Order",
		limit_page_length=0,
	)
	for shipment in shipments:
		note = next((found for found in map(live_note, _legacy_notes(shipment)) if found), None)
		if not note:
			continue
		skipped = {}
		for entry in load_entries(shipment):
			sku = cstr(entry.get("external_sku")).strip()
			skipped[sku] = skipped.get(sku, 0) + cint(entry.get("qty"))
		units = frappe.get_all(
			UNIT,
			filters={
				"parent": shipment.parent,
				"parenttype": "Marketplace Order",
				"integration_shipment_nr": shipment.integration_shipment_nr,
			},
			fields=["name", "partner_sku", "delivery_note"],
			order_by="idx desc",
			limit_page_length=0,
		)
		for unit in units:  # last units first: the unresolved ones are the leftovers of a SKU
			if cstr(unit.delivery_note).strip():
				continue
			sku = cstr(unit.partner_sku).strip()
			if skipped.get(sku, 0) > 0:
				skipped[sku] -= 1
				continue
			frappe.db.set_value(UNIT, unit.name, "delivery_note", note, update_modified=False)
