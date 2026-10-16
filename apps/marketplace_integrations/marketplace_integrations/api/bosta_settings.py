# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Desk API for the Bosta Settings form (#276)."""

import frappe

from marketplace_integrations.integrations.bosta import locations


@frappe.whitelist(methods=["POST"])
def get_pickup_locations():
	"""The business's Bosta pickup locations for the Business Location ID picker.

	Read-only against Bosta; nothing is stored. Uses the API key saved on Bosta Settings
	and fails before any request when there is none."""
	frappe.only_for("System Manager")
	return locations.get_pickup_locations()
