# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized API boundary for the Marketplace Talabat catalog page."""

import frappe

from marketplace_integrations.integrations.talabat import catalog


@frappe.whitelist()
def get_catalog_page(marketplace_account, page=1, page_size=20):
	return catalog.get_catalog_page(marketplace_account, page, page_size)


@frappe.whitelist()
def get_mapped_items(marketplace_account):
	return catalog.get_mapped_items(marketplace_account)


@frappe.whitelist()
def upsert_mapping(marketplace_account, external_reference, item):
	return catalog.upsert_mapping(marketplace_account, external_reference, item)


@frappe.whitelist()
def disable_mapping(marketplace_account, external_reference):
	return catalog.disable_mapping(marketplace_account, external_reference)
