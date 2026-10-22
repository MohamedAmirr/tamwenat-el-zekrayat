# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Reconcile Custom DocPerm shadow rows with the permission rows shipped in the doctype JSON (#322).

A doctype with at least one Custom DocPerm row (Role Permission Manager edits, or the copy made
by ``frappe.permissions.add_permission``) uses ONLY those rows and ignores the permission rows in
its JSON, so the platform-role rows added by #322 would never apply on such a site. For every
top-level app doctype that has Custom DocPerm rows, and each permission row shipped in its JSON:

- no Custom DocPerm row for that role + permlevel: insert one copying the shipped ptypes;
- one exists: set the shipped ptypes to 1 (it only raises ptypes, never lowers them).

It never deletes a row, never touches a role absent from the shipped JSON (ERPNext roles,
site-specific roles), and keeps ptypes already set on the site. A doctype without Custom DocPerm
rows is left alone (it keeps using the JSON). A second run changes nothing. The doctype cache is
cleared for each doctype that changed.

Rollback: the patch prints every row it inserts or raises. To revert, delete the inserted Custom
DocPerm rows and reset the raised ptypes in the Role Permission Manager. Reverting the commit
alone does not undo database rows.
"""

import json

import frappe

MODULE = "Marketplace Integrations"
APP = "marketplace_integrations"
PTYPES = (
	"read",
	"write",
	"create",
	"delete",
	"submit",
	"cancel",
	"amend",
	"print",
	"email",
	"report",
	"import",
	"export",
	"share",
)


def execute():
	doctypes = frappe.get_all("DocType", filters={"module": MODULE, "istable": 0, "custom": 0}, pluck="name")
	reconcile(doctypes)


def reconcile(doctypes):
	"""Reconcile ``doctypes`` (names); returns ``{doctype: [change description, ...]}``
	for the doctypes that changed."""
	changed = {}
	for doctype in doctypes:
		if not frappe.db.exists("Custom DocPerm", {"parent": doctype}):
			continue
		changes = _reconcile_doctype(doctype)
		if changes:
			frappe.clear_cache(doctype=doctype)
			changed[doctype] = changes
			for change in changes:
				print(f"reconcile_platform_custom_docperms: {doctype}: {change}")
	return changed


def _shipped_permissions(doctype):
	scrubbed = frappe.scrub(doctype)
	path = frappe.get_app_path(APP, "marketplace_integrations", "doctype", scrubbed, f"{scrubbed}.json")
	with open(path) as handle:
		return json.load(handle).get("permissions", [])


def _reconcile_doctype(doctype):
	changes = []
	for shipped in _shipped_permissions(doctype):
		role = shipped.get("role")
		permlevel = shipped.get("permlevel") or 0
		wanted = [ptype for ptype in PTYPES if shipped.get(ptype)]
		if not role or not wanted:
			continue

		existing = frappe.get_all(
			"Custom DocPerm",
			filters={"parent": doctype, "role": role, "permlevel": permlevel},
			fields=["name", *PTYPES],
		)
		if not existing:
			# Patch-time repair of a permission table: Custom DocPerm needs no user permission
			# check, and the role may not exist yet (the Role fixture can sync after this
			# patch), so link validation is skipped.
			doc = frappe.get_doc(
				{
					"doctype": "Custom DocPerm",
					"parent": doctype,
					"parenttype": "DocType",
					"parentfield": "permissions",
					"role": role,
					"permlevel": permlevel,
					**{ptype: 1 for ptype in wanted},
				}
			)
			doc.flags.ignore_links = True
			doc.insert(ignore_permissions=True)
			changes.append(f"inserted {role} permlevel {permlevel} ({', '.join(wanted)})")
			continue

		for row in existing:
			missing = [ptype for ptype in wanted if not row.get(ptype)]
			if not missing:
				continue
			# Raising ptypes on an existing permission row; a document save would only re-run
			# the same Custom DocPerm validation without adding anything for a 0 -> 1 change.
			frappe.db.set_value("Custom DocPerm", row.name, {ptype: 1 for ptype in missing})
			changes.append(f"raised {role} permlevel {permlevel} ({', '.join(missing)})")
	return changes
