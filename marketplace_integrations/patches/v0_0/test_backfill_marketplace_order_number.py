# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.backfill_marketplace_order_number import execute

# Same structural stand-in connection used by the other backfill tests.
STAND_IN_CONNECTION_DOCTYPE = "UOM"
STAND_IN_CONNECTION = "Nos"


class IntegrationTestBackfillMarketplaceOrderNumber(IntegrationTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Order Number Backfill {suffix}",
				"platform": "Talabat",
				"external_account_id": f"order-number-backfill-{suffix}",
				"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
				"connection": STAND_IN_CONNECTION,
			}
		).insert()
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		frappe.db.delete("Marketplace Order", {"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, force=True)

	def make_order(self, code):
		order = frappe.get_doc(
			{
				"doctype": "Marketplace Order",
				"marketplace_account": self.account.name,
				"external_order_id": f"num-backfill-{frappe.generate_hash(length=8)}",
				"order_code": code,
			}
		)
		order.flags.skip_sync_status_update = True
		order.insert()
		return order

	def number(self, order):
		return frappe.db.get_value("Marketplace Order", order.name, "order_number")

	def test_fills_missing_number_and_skips_non_numeric_codes(self):
		hashed = self.make_order("#25233")
		other = self.make_order("PICKUP-001")
		frappe.db.set_value("Marketplace Order", hashed.name, "order_number", 0, update_modified=False)

		execute()

		self.assertEqual(self.number(hashed), 25233)
		self.assertFalse(self.number(other))

	def test_second_run_is_a_no_op_and_keeps_modified(self):
		order = self.make_order("#77")
		frappe.db.set_value("Marketplace Order", order.name, "order_number", 0, update_modified=False)
		execute()
		modified = frappe.db.get_value("Marketplace Order", order.name, "modified")

		execute()

		self.assertEqual(self.number(order), 77)
		self.assertEqual(frappe.db.get_value("Marketplace Order", order.name, "modified"), modified)
