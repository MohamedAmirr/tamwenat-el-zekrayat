# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.reconcile_platform_custom_docperms import reconcile

SEEDED = "Marketplace Account"
UNTOUCHED = "Noon Return"
PLATFORM_ROLES = ("Talabat User", "Noon User", "Shopify User")
# A role the shipped doctype JSON does not mention; the patch must never touch its rows.
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


class IntegrationTestReconcilePlatformCustomDocperms(IntegrationTestCase):
	"""The tests build their own Custom DocPerm state and do not depend on whether the migrate-time
	patch already ran on the site: setUp snapshots and clears every Custom DocPerm row of the two
	doctypes, the cleanup deletes what the test left and re-inserts the snapshot (rows are
	recreated, so their auto-generated names may differ from before)."""

	def setUp(self):
		self.snapshot = {}
		for doctype in (SEEDED, UNTOUCHED):
			self.snapshot[doctype] = frappe.get_all(
				"Custom DocPerm", filters={"parent": doctype}, fields=["*"], order_by="idx asc"
			)
			frappe.db.delete("Custom DocPerm", {"parent": doctype})
			frappe.clear_cache(doctype=doctype)
		self.addCleanup(self.restore)

		# Controlled state: one row for a role outside the shipped JSON, no platform rows.
		self.insert_row(SEEDED, UNRELATED_ROLE, read=1)

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

	def test_adds_platform_rows_keeps_everything_else_and_is_idempotent(self):
		before = rows(SEEDED)
		for role in PLATFORM_ROLES:
			self.assertNotIn((role, 0), before)

		changed = reconcile([SEEDED, UNTOUCHED])

		after = rows(SEEDED)
		self.assertEqual(set(changed), {SEEDED})
		for role in PLATFORM_ROLES:
			row = after[(role, 0)]
			self.assertEqual((row.read, row.write, row.report), (1, 1, 1))
			self.assertEqual((row.create, row.delete), (0, 0))
		# Nothing deleted, pre-existing rows unchanged.
		self.assertEqual(set(before) - set(after), set())
		for key, row in before.items():
			self.assertEqual(after[key], row)
		# A doctype without Custom DocPerm rows stays untouched.
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": UNTOUCHED}))

		# Second run: no change.
		self.assertEqual(reconcile([SEEDED, UNTOUCHED]), {})
		self.assertEqual(rows(SEEDED), after)

	def test_raises_missing_ptypes_but_never_lowers_or_touches_other_roles(self):
		reconcile([SEEDED])
		talabat = rows(SEEDED)[("Talabat User", 0)]
		# Site edits: drop a shipped ptype, add one the JSON does not have.
		frappe.db.set_value("Custom DocPerm", talabat.name, {"write": 0, "email": 1})
		unrelated = rows(SEEDED)[(UNRELATED_ROLE, 0)]

		changed = reconcile([SEEDED])

		after = rows(SEEDED)
		self.assertIn(SEEDED, changed)
		self.assertEqual(after[("Talabat User", 0)].write, 1)
		self.assertEqual(after[("Talabat User", 0)].email, 1)
		self.assertEqual(after[(UNRELATED_ROLE, 0)], unrelated)

	def test_no_custom_docperm_rows_is_a_no_op(self):
		self.assertEqual(reconcile([UNTOUCHED]), {})
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": UNTOUCHED}))

	def test_missing_doctype_json_is_skipped(self):
		# An orphan DocType row (no JSON shipped by this app version) must not abort migrate.
		before = rows(SEEDED)
		with patch("frappe.get_app_path", return_value="/nonexistent/doctype.json"):
			self.assertEqual(reconcile([SEEDED]), {})
		self.assertEqual(rows(SEEDED), before)
