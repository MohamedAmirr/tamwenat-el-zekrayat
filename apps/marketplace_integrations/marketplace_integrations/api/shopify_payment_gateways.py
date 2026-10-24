# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Shopify payment gateway picker data (#347)."""

import json

import frappe
from frappe import _
from frappe.utils import cstr


@frappe.whitelist(methods=["POST"])
def list_recent_successful_gateways(account: str):
	"""Return exact gateway names used by successful SALE/CAPTURE transactions.

	The search is deliberately limited to the newest 500 readable local Shopify
	orders for this account. The caller still types freely in the child table.
	"""
	account = frappe.get_doc("Marketplace Account", account)
	account.check_permission("write")
	if not frappe.has_permission("Marketplace Order", ptype="read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if account.platform != "Shopify" or account.connection_doctype != "Shopify Connection":
		frappe.throw(_("Choose a Shopify Marketplace Account."))

	orders = frappe.get_list(
		"Marketplace Order",
		filters={
			"marketplace_account": account.name,
			"platform": "Shopify",
		},
		fields=["payment_transactions"],
		order_by="modified desc",
		limit_page_length=500,
	)
	orders = [row for row in orders if _has_transactions(row.payment_transactions)]
	mapped = {
		cstr(row.shopify_gateway).strip()
		for row in account.get("shopify_payment_gateways") or []
		if cstr(row.shopify_gateway).strip()
	}
	return [
		{"gateway": gateway, "orders_count": count, "mapped": gateway in mapped}
		for gateway, count in eligible_gateway_counts(orders).items()
	]


def eligible_gateway_counts(orders):
	"""Count orders carrying each exact, successful transaction gateway."""
	counts = {}
	for order in orders:
		transactions = (
			order.get("payment_transactions")
			if isinstance(order, dict)
			else getattr(order, "payment_transactions", None)
		)
		if isinstance(transactions, str):
			try:
				transactions = json.loads(transactions)
			except (TypeError, ValueError):
				continue
		if isinstance(transactions, dict):
			transactions = transactions.get("nodes") or []
		if not isinstance(transactions, list):
			continue
		order_gateways = set()
		for transaction in transactions:
			if not isinstance(transaction, dict):
				continue
			if transaction.get("kind") not in {"SALE", "CAPTURE"} or transaction.get("status") != "SUCCESS":
				continue
			gateway = cstr(transaction.get("gateway")).strip()
			if gateway:
				order_gateways.add(gateway)
		for gateway in order_gateways:
			counts[gateway] = counts.get(gateway, 0) + 1
	return dict(sorted(counts.items(), key=lambda item: item[0].casefold()))


def _has_transactions(value):
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except (TypeError, ValueError):
			return False
	if isinstance(value, dict):
		return bool(value.get("nodes"))
	return isinstance(value, list) and bool(value)
