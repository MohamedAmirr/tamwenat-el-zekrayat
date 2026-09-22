# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Shared validate-and-group-by-account logic for the manual catalog push flow.

Extracted out of ``api/talabat_catalog.py``'s ``_parse_mapping_names()``/
``_queue_mapping_catalog_syncs()`` (#106 decision 8) - a pure extraction, no
behavior change to the existing Talabat push flow. Talabat's own account
validation (``integrations/talabat/catalog_sync.py``'s
``validate_catalog_sync_account()``) and its fire-and-forget one-job-per-
account enqueue shape stay in ``api/talabat_catalog.py``; Shopify's own
account validation (``integrations/shopify/catalog_push.py``'s
``validate_push_account()``) and its job-status enqueue shape stay in
``api/shopify_catalog_push.py`` - this module only parses and groups, it
never validates a specific platform's account requirements and never
enqueues anything itself.
"""

import frappe


def parse_mapping_names(mapping_names):
	"""Return a de-duplicated list of stripped Marketplace Product Mapping names.

	Raises if the input isn't valid JSON, isn't a non-empty list, or contains
	anything other than non-blank strings.
	"""
	try:
		mapping_names = frappe.parse_json(mapping_names) if isinstance(mapping_names, str) else mapping_names
	except (TypeError, ValueError):
		frappe.throw(frappe._("Marketplace Product Mappings must be valid JSON."))
	if not isinstance(mapping_names, list) or not mapping_names:
		frappe.throw(frappe._("Select at least one Marketplace Product Mapping."))
	if any(not isinstance(name, str) or not name.strip() for name in mapping_names):
		frappe.throw(frappe._("Marketplace Product Mappings must be valid names."))
	return list(dict.fromkeys(name.strip() for name in mapping_names))


def group_mappings_by_account(mapping_names):
	"""Check write permission on every selected Mapping and its Marketplace
	Account, then group mapping names by account.

	Returns ``{account_name: {"account": account_doc, "mapping_names": [...]}}``.
	Does not validate any platform-specific account requirement (Warehouse,
	Price List, Connection, currency, ...) - that stays the caller's own
	responsibility, since it differs per platform.
	"""
	accounts = {}
	for mapping_name in mapping_names:
		mapping_doc = frappe.get_doc("Marketplace Product Mapping", mapping_name)
		mapping_doc.check_permission("write")
		account_doc = frappe.get_doc("Marketplace Account", mapping_doc.marketplace_account)
		account_doc.check_permission("write")
		accounts.setdefault(account_doc.name, {"account": account_doc, "mapping_names": []})[
			"mapping_names"
		].append(mapping_doc.name)
	return accounts
