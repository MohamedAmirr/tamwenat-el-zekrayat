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

	Bulk-fetches mapping rows and loads each distinct Marketplace Account
	document at most once (review finding #1), rather than the previous
	per-mapping ``frappe.get_doc()`` shape - which not only issued one query
	per mapping for the mapping itself, but also re-loaded the SAME Account
	document once per mapping that pointed at it (e.g. loading one Account
	3,000 times for 3,000 mappings on it). Permission checks use
	``frappe.has_permission(doctype, "write", doc=name)`` rather than a full
	``Document.check_permission()`` - Frappe's own permission layer
	(``frappe.permissions.has_permission()``) already avoids loading child
	tables for this (``frappe.get_lazy_doc()``), so passing a bare name is
	strictly cheaper than loading a full document just to check permission
	on it.
	"""
	mapping_names = list(mapping_names)
	mapping_rows = frappe.get_all(
		"Marketplace Product Mapping",
		filters={"name": ["in", mapping_names]},
		fields=["name", "marketplace_account"],
	)
	rows_by_name = {row.name: row for row in mapping_rows}
	missing = [name for name in mapping_names if name not in rows_by_name]
	if missing:
		frappe.throw(
			frappe._("Marketplace Product Mapping {0} does not exist.").format(missing[0]),
			frappe.DoesNotExistError,
		)

	for mapping_name in mapping_names:
		if not frappe.has_permission("Marketplace Product Mapping", "write", doc=mapping_name):
			frappe.throw(
				frappe._("Not permitted to modify Marketplace Product Mapping {0}").format(mapping_name),
				frappe.PermissionError,
			)

	account_names = {row.marketplace_account for row in mapping_rows if row.marketplace_account}
	account_docs = {}
	for account_name in account_names:
		if not frappe.has_permission("Marketplace Account", "write", doc=account_name):
			frappe.throw(
				frappe._("Not permitted to modify Marketplace Account {0}").format(account_name),
				frappe.PermissionError,
			)
		account_docs[account_name] = frappe.get_doc("Marketplace Account", account_name)

	accounts = {}
	for mapping_name in mapping_names:
		row = rows_by_name[mapping_name]
		account_doc = account_docs[row.marketplace_account]
		accounts.setdefault(account_doc.name, {"account": account_doc, "mapping_names": []})[
			"mapping_names"
		].append(mapping_name)
	return accounts
