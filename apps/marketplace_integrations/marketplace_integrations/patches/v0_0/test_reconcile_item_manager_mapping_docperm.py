# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.reconcile_item_manager_mapping_docperm import execute
from marketplace_integrations.patches.v0_0.reconcile_platform_custom_docperms import reconcile

MAPPING = "Marketplace Product Mapping"
UNTOUCHED = "Noon Return"
ROLE = "Item Manager"
# A role the shipped JSON does not mention for this patch; its rows must never change.
UNRELATED_ROLE = "Accounts User"


def rows(doctype):
	return {
		(row.role, row.permlevel): row
		for row in frappe.get_all(
			"Custom DocPerm",
			filters={"parent": doctype},
			fields=["name", "role", "permlevel", "read", "write", "create", "delete", "report", "email"],
		)
	}


class IntegrationTestReconcileItemManagerMappingDocperm(IntegrationTestCase):
	"""Builds its own Custom DocPerm state: setUp snapshots and clears the rows of both doctypes,
	the cleanup restores the snapshot (rows are recreated, names may differ)."""

	def setUp(self):
		self.snapshot = {}
		for doctype in (MAPPING, UNTOUCHED):
			self.snapshot[doctype] = frappe.get_all(
				"Custom DocPerm", filters={"parent": doctype}, fields=["*"], order_by="idx asc"
			)
			frappe.db.delete("Custom DocPerm", {"parent": doctype})
			frappe.clear_cache(doctype=doctype)
		self.addCleanup(self.restore)

		self.insert_row(MAPPING, UNRELATED_ROLE, read=1)

	def insert_row(self, doctype, role, **ptypes):
		doc = frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": doctype,
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": role,
				"permlevel": 0,
				**ptypes,
			}
		)
		doc.flags.ignore_links = True
		return doc.insert(ignore_permissions=True)

	def restore(self):
		for doctype, saved in self.snapshot.items():
			frappe.db.delete("Custom DocPerm", {"parent": doctype})
			for row in saved:
				data = {k: v for k, v in row.items() if k not in ("name", "doctype")}
				doc = frappe.get_doc({"doctype": "Custom DocPerm", **data})
				doc.flags.ignore_links = True
				doc.insert(ignore_permissions=True)
			frappe.clear_cache(doctype=doctype)

	def test_inserts_the_item_manager_row_without_delete_and_is_idempotent(self):
		before = rows(MAPPING)
		self.assertNotIn((ROLE, 0), before)

		execute()

		after = rows(MAPPING)
		row = after[(ROLE, 0)]
		self.assertEqual((row.read, row.write, row.create, row.report), (1, 1, 1, 1))
		self.assertEqual(row.delete, 0)
		# Other roles unchanged, nothing deleted, no platform role row added by this patch.
		self.assertEqual(after[(UNRELATED_ROLE, 0)], before[(UNRELATED_ROLE, 0)])
		self.assertEqual(set(after) - {(ROLE, 0)}, set(before))

		# Second run: no change.
		execute()
		self.assertEqual(rows(MAPPING), after)

	def test_doctype_without_custom_docperm_rows_is_skipped(self):
		self.assertEqual(reconcile([UNTOUCHED], roles=(ROLE,)), {})
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": UNTOUCHED}))

	def test_roles_parameter_limits_the_reconcile(self):
		changed = reconcile([MAPPING], roles=(ROLE,))

		self.assertEqual(set(changed), {MAPPING})
		after = rows(MAPPING)
		for role in ("Talabat User", "Noon User", "Shopify User"):
			self.assertNotIn((role, 0), after)

	def test_default_roles_do_not_include_item_manager(self):
		reconcile([MAPPING])

		after = rows(MAPPING)
		self.assertNotIn((ROLE, 0), after)
		self.assertIn(("Talabat User", 0), after)
