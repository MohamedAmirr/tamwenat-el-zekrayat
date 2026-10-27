# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Mark shipments that already store a delivery fee as having received one.

A Currency column cannot tell an unset fee from a real 0, so the fee is shown (and a Delivered
shipment stops being polled for it) only once ``shipment_fee_received_on`` is set. A stored fee
above 0 is certainly a received one; 0 stays unset and is stamped by the next poll or Refresh.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Bosta Shipment") or not frappe.db.has_column(
		"Bosta Shipment", "shipment_fee_received_on"
	):
		return
	for shipment in frappe.get_all(
		"Bosta Shipment",
		filters={"shipment_fee": [">", 0], "shipment_fee_received_on": ["is", "not set"]},
		fields=["name", "last_bosta_status_on", "modified"],
		limit_page_length=0,
	):
		frappe.db.set_value(
			"Bosta Shipment",
			shipment.name,
			"shipment_fee_received_on",
			shipment.last_bosta_status_on or shipment.modified,
			update_modified=False,
		)
