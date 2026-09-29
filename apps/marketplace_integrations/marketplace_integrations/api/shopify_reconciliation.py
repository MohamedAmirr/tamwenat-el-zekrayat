# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized Desk API boundary for the Shopify Reconciliation dashboard's
custom Page sections (#139). Thin delegation only - see
services/shopify_reconciliation.py for the actual permission checks and
queries."""

import frappe

from marketplace_integrations.services import shopify_reconciliation


@frappe.whitelist()
def get_order_import_failures():
	return shopify_reconciliation.get_order_import_failures()


@frappe.whitelist()
def get_refund_evidence_gaps():
	return shopify_reconciliation.get_refund_evidence_gaps()
