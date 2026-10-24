# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Add the shipped Bosta Shipment read permission to Stock Manager where needed."""

import frappe

from marketplace_integrations.patches.v0_0.reconcile_platform_custom_docperms import (
	_reconcile_doctype,
)


def execute():
	if not frappe.db.exists("DocType", "Bosta Shipment"):
		return
	# With no Custom DocPerm rows, the DocType JSON permissions already apply. Calling
	# _reconcile_doctype would create a partial override and hide the remaining JSON roles.
	if not frappe.db.exists("Custom DocPerm", {"parent": "Bosta Shipment"}):
		return
	# Existing sites with Custom DocPerm rows ignore DocType JSON permissions.
	# The reconciler only raises/inserts the roles explicitly listed in that JSON.
	for change in _reconcile_doctype("Bosta Shipment"):
		print(f"reconcile_bosta_handover_permissions: Bosta Shipment: {change}")
