# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized Desk entry for Noon stock synchronization."""

import frappe

from marketplace_integrations.integrations.noon import inventory


@frappe.whitelist(methods=["POST"])
def queue_stock_push(account: str, mapping_names=None):
	return inventory.queue_stock_push(account, mapping_names)
