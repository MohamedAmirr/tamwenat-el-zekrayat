# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Hourly full re-push of enabled Noon account pricing and stock."""

import frappe

from marketplace_integrations.integrations.noon.inventory import push_stock
from marketplace_integrations.integrations.noon.sync import enqueue_push


def sweep_all_accounts():
	from marketplace_integrations.integrations.noon.pricing import push_pricing

	accounts = frappe.get_all(
		"Marketplace Account",
		filters={"platform": "Noon", "enabled": 1},
		pluck="name",
		order_by="name asc",
	)
	queued = 0
	for account_name in accounts:
		queued += bool(enqueue_push(push_pricing, "pricing", account_name)["queued"])
		queued += bool(enqueue_push(push_stock, "stock", account_name)["queued"])
	return queued
