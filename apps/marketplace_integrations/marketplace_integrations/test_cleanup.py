import unittest

import frappe

from marketplace_integrations.testing_utils import CLEANUP_DOCTYPES, CleanupTestCase, snapshot_names


class TestCleanupTestCase(CleanupTestCase):
	def test_committed_rows_do_not_survive_the_test(self):
		before = snapshot_names()

		class Inner(CleanupTestCase):
			def test_leaky(self):
				frappe.get_doc(
					{
						"doctype": "Price List",
						"price_list_name": f"Leak Guard {frappe.generate_hash(length=8)}",
						"currency": "EGP",
						"selling": 1,
					}
				).insert()
				frappe.db.commit()  # what production code under test does

		result = unittest.TextTestRunner(stream=open("/dev/null", "w")).run(
			unittest.defaultTestLoader.loadTestsFromTestCase(Inner)
		)

		self.assertTrue(result.wasSuccessful())
		self.assertEqual(snapshot_names(), before, f"leaked rows in {CLEANUP_DOCTYPES}")
