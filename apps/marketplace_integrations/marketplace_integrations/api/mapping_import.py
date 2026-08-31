# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Whitelisted boundary for Marketplace Product Mapping XLSX workflows."""

import frappe

from marketplace_integrations.services import mapping_import


@frappe.whitelist()
def export_mappings_xlsx():
	"""Export mappings the caller may read as an XLSX workbook."""
	return mapping_import.export_mappings_xlsx()


@frappe.whitelist(methods=["POST"])
def preview_mappings_import(file_url: str):
	"""Parse and validate a workbook without making any writes."""
	return mapping_import.preview_mappings_import(file_url)


@frappe.whitelist(methods=["POST"])
def apply_mappings_import(file_url: str):
	"""Re-parse, re-validate, and apply valid mapping rows."""
	return mapping_import.apply_mappings_import(file_url)
