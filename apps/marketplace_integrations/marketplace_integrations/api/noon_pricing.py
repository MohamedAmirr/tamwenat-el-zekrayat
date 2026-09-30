# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized Desk entry for Noon pricing synchronization."""

import frappe

from marketplace_integrations.integrations.noon import pricing


@frappe.whitelist(methods=["POST"])
def queue_pricing_push(account: str, mapping_names=None):
	return pricing.queue_pricing_push(account, mapping_names)
