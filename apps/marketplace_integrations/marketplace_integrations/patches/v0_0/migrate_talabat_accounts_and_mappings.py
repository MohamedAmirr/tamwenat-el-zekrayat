# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Migrate legacy Talabat vendors and product mappings into shared models.

This patch deliberately leaves every legacy document untouched.  It uses the
normal Document insert/save path for the shared records so their controller
identity keys are always recalculated.
"""

import frappe
from frappe import _
from frappe.utils import cstr

from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.marketplace_account import (
	compute_account_identity_key,
)
from marketplace_integrations.marketplace_integrations.doctype.marketplace_product_mapping.marketplace_product_mapping import (
	compute_mapping_identity_key,
)

LEGACY_VENDOR_DOCTYPE = "Talabat Vendor"
LEGACY_MAPPING_DOCTYPE = "Talabat Product Mapping"
MIGRATED_CONNECTION_SOURCE = "talabat_settings"
CONNECTION_DOCTYPE = "Talabat Connection"


def execute():
	"""Backfill shared records from installed legacy Talabat doctypes.

	No legacy rows (or warehouses) are created, modified, renamed, or deleted.
	A marker-tagged #37 connection is required only when there is source data.
	"""
	legacy_vendors = get_legacy_vendors()
	legacy_mappings = get_legacy_mappings()
	if not legacy_vendors and not legacy_mappings:
		return

	# Complete every source-data check before the first Marketplace Account or
	# Marketplace Product Mapping write. This keeps invalid legacy data from
	# leaving a partial backfill behind if execute() is called directly.
	validate_legacy_vendors(legacy_vendors)
	validate_legacy_mappings(legacy_mappings, {vendor.name for vendor in legacy_vendors})
	connection = get_migrated_connection()

	accounts_by_vendor = {
		vendor.name: create_or_update_account(vendor, connection) for vendor in legacy_vendors
	}
	for mapping in legacy_mappings:
		create_or_update_mapping(mapping, accounts_by_vendor[mapping.vendor])


def get_legacy_vendors():
	"""Return an empty list if the legacy vendor doctype is not installed."""
	if not frappe.db.table_exists(LEGACY_VENDOR_DOCTYPE):
		return []
	return frappe.get_all(
		LEGACY_VENDOR_DOCTYPE,
		fields=["name", "vendor_id", "vendor_name", "is_active"],
		order_by="name",
	)


def get_legacy_mappings():
	"""Return legacy mappings, accommodating an older source schema without
	sync metadata rather than assuming those columns exist. A missing legacy
	mapping doctype is simply an empty source list.
	"""
	if not frappe.db.table_exists(LEGACY_MAPPING_DOCTYPE):
		return []
	meta = frappe.get_meta(LEGACY_MAPPING_DOCTYPE)
	fields = ["name", "vendor", "item", "talabat_sku"]
	fields.extend(
		fieldname
		for fieldname in ("sync_status", "last_synced_on", "last_error", "enabled")
		if meta.has_field(fieldname)
	)
	return frappe.get_all(LEGACY_MAPPING_DOCTYPE, fields=fields, order_by="name")


def get_migrated_connection():
	connections = frappe.get_all(
		CONNECTION_DOCTYPE,
		filters={"legacy_migration_source": MIGRATED_CONNECTION_SOURCE},
		pluck="name",
	)
	if len(connections) != 1:
		frappe.throw(
			_(
				"Legacy Talabat data exists, but exactly one Talabat Connection marked with legacy migration source {0} is required before it can be migrated."
			).format(MIGRATED_CONNECTION_SOURCE)
		)
	return connections[0]


def validate_legacy_vendors(vendors):
	for vendor in vendors:
		if not cstr(vendor.vendor_id).strip():
			frappe.throw(
				_("Legacy Talabat Vendor {0} has no Vendor ID and cannot be migrated.").format(vendor.name)
			)


def validate_legacy_mappings(mappings, vendor_names):
	"""Reject source rows that cannot be represented faithfully before writes."""
	seen_vendor_skus = set()
	for mapping in mappings:
		sku = cstr(mapping.talabat_sku).strip()
		if not sku:
			frappe.throw(
				_("Legacy Talabat Product Mapping {0} has no Talabat SKU and cannot be migrated.").format(
					mapping.name
				)
			)
		if mapping.vendor not in vendor_names:
			frappe.throw(
				_("Legacy Talabat Product Mapping {0} references missing Talabat Vendor {1}.").format(
					mapping.name, mapping.vendor
				)
			)
		if not cstr(mapping.item).strip() or not frappe.db.exists("Item", mapping.item):
			frappe.throw(
				_(
					"Legacy Talabat Product Mapping {0} references missing Item {1} and cannot be migrated."
				).format(mapping.name, mapping.item or "(blank)")
			)
		identity = (mapping.vendor, sku)
		if identity in seen_vendor_skus:
			frappe.throw(
				_(
					"Legacy Talabat Product Mapping {0} duplicates Talabat SKU {1} for Talabat Vendor {2} and cannot be migrated."
				).format(mapping.name, sku, mapping.vendor)
			)
		seen_vendor_skus.add(identity)


def create_or_update_account(vendor, connection):
	vendor_id = cstr(vendor.vendor_id).strip()
	identity_key = compute_account_identity_key("Talabat", vendor_id)
	account_name = cstr(vendor.vendor_name).strip() or vendor_id
	account_name_by_identity = frappe.db.get_value(
		"Marketplace Account", {"account_identity_key": identity_key}
	)
	account = (
		frappe.get_doc("Marketplace Account", account_name_by_identity)
		if account_name_by_identity
		else frappe.get_doc({"doctype": "Marketplace Account"})
	)
	account.update(
		{
			"account_name": account_name,
			"platform": "Talabat",
			"external_account_id": vendor_id,
			"connection_doctype": CONNECTION_DOCTYPE,
			"connection": connection,
			"enabled": vendor.is_active,
		}
	)
	if account.is_new():
		account.insert(ignore_permissions=True)
	else:
		account.save(ignore_permissions=True)
	return account.name


def create_or_update_mapping(legacy_mapping, marketplace_account):
	external_reference = cstr(legacy_mapping.talabat_sku).strip()
	identity_key = compute_mapping_identity_key(marketplace_account, external_reference)
	mapping_name = frappe.db.get_value("Marketplace Product Mapping", {"mapping_identity_key": identity_key})
	mapping = (
		frappe.get_doc("Marketplace Product Mapping", mapping_name)
		if mapping_name
		else frappe.get_doc({"doctype": "Marketplace Product Mapping"})
	)
	mapping.update(
		{
			"marketplace_account": marketplace_account,
			"item": legacy_mapping.item,
			"external_reference": external_reference,
			"external_sku": external_reference,
			"enabled": legacy_mapping.get("enabled", 1),
		}
	)
	for fieldname in ("sync_status", "last_synced_on", "last_error"):
		if fieldname in legacy_mapping:
			mapping.set(fieldname, legacy_mapping.get(fieldname))
	if mapping.is_new():
		mapping.insert(ignore_permissions=True)
	else:
		mapping.save(ignore_permissions=True)
