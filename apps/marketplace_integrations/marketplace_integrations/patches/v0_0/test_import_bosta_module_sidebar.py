# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.import_bosta_module_sidebar import execute

SIDEBAR = "Bosta"


class IntegrationTestImportBostaModuleSidebar(IntegrationTestCase):
	"""Puts Sidebar Bosta on the wrong module, and deletes it, then runs the patch."""

	def setUp(self):
		self.existed = frappe.db.exists("Sidebar", SIDEBAR)
		self.addCleanup(self.restore)

	def restore(self):
		# the patch is the shipped source of truth: running it restores the row
		execute()

	def test_moves_a_sidebar_on_the_old_module_to_bosta(self):
		frappe.db.set_value("Sidebar", SIDEBAR, "module", "Marketplace Integrations")
		execute()
		self.assertEqual(frappe.db.get_value("Sidebar", SIDEBAR, "module"), "Bosta")

	def test_recreates_a_deleted_sidebar_and_is_idempotent(self):
		if frappe.db.exists("Sidebar", SIDEBAR):
			frappe.delete_doc("Sidebar", SIDEBAR, force=True, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Sidebar", SIDEBAR))
		execute()
		execute()
		self.assertEqual(frappe.db.get_value("Sidebar", SIDEBAR, "module"), "Bosta")
		self.assertEqual(frappe.db.count("Sidebar", {"name": SIDEBAR}), 1)
