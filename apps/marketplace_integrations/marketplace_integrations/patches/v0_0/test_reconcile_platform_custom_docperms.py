# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.reconcile_platform_custom_docperms import reconcile
from marketplace_integrations.tests.utils import make_role_user

SEEDED = "Marketplace Account"
UNTOUCHED = "Noon Return"
PLATFORM_ROLES = ("Talabat User", "Noon User", "Shopify User")


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
	def setUp(self):
		if frappe.db.exists("Custom DocPerm", {"parent": UNTOUCHED}):
			self.skipTest(f"{UNTOUCHED} already has Custom DocPerm rows on this site")
		# make_role_user copies the standard rows into Custom DocPerm and removes everything it
		# caused on cleanup (unless the site already had rows, which we do not own).
		self.assertFalse(
			frappe.db.exists("Custom DocPerm", {"parent": SEEDED}),
			f"{SEEDED} already has Custom DocPerm rows on this site; cannot seed",
		)
		_email, self.unrelated_role = make_role_user(self, "Reconcile Unrelated", {SEEDED: ["read"]})
		# The copy already contains the shipped platform rows; drop them so the patch has to add them.
		frappe.db.delete("Custom DocPerm", {"parent": SEEDED, "role": ["in", PLATFORM_ROLES]})
		frappe.clear_cache(doctype=SEEDED)

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
		# Nothing deleted, unrelated and pre-existing rows unchanged.
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
		unrelated = rows(SEEDED)[(self.unrelated_role, 0)]

		changed = reconcile([SEEDED])

		after = rows(SEEDED)
		self.assertIn(SEEDED, changed)
		self.assertEqual(after[("Talabat User", 0)].write, 1)
		self.assertEqual(after[("Talabat User", 0)].email, 1)
		self.assertEqual(after[(self.unrelated_role, 0)], unrelated)
