# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Keep historical Bosta rows out of automatic stock posting pending review."""

import frappe


def execute():
	if not frappe.db.table_exists("Bosta Shipment"):
		return
	# Historic carrier status and Delivery Note links do not prove who physically
	# handed the parcel over. Do not create, submit, cancel, or otherwise alter stock.
	frappe.db.sql(
		"""
		UPDATE `tabBosta Shipment`
		SET handover_status = 'Review Required', stock_posting_status = 'Review Required'
		WHERE IFNULL(handover_status, '') = '' OR handover_status = 'Awaiting Handover'
		"""
	)
