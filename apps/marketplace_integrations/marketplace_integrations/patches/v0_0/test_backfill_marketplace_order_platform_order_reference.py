# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.backfill_marketplace_order_platform_order_reference import execute

# Same structural stand-in connection used by the other backfill tests.
STAND_IN_CONNECTION_DOCTYPE = "UOM"
STAND_IN_CONNECTION = "Nos"


class IntegrationTestBackfillMarketplaceOrderPlatformOrderReference(IntegrationTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Order Reference Backfill {suffix}",
				"platform": "Talabat",
				"external_account_id": f"order-reference-backfill-{suffix}",
				"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
				"connection": STAND_IN_CONNECTION,
			}
		).insert()
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		# Orders before the account (naming-series revert gotcha).
		frappe.db.delete("Marketplace Order", {"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, force=True)

	def make_order(self, reference):
		order = frappe.get_doc(
			{
				"doctype": "Marketplace Order",
				"marketplace_account": self.account.name,
				"external_order_id": f"ref-backfill-{frappe.generate_hash(length=8)}",
				"platform_order_reference": reference,
			}
		)
		order.flags.skip_sync_status_update = True
		order.insert()
		return order

	def set_reference(self, order, value):
		frappe.db.set_value(
			"Marketplace Order", order.name, "platform_order_reference", value, update_modified=False
		)
		frappe.clear_document_cache("Marketplace Order", order.name)

	def reference(self, order):
		return frappe.db.get_value("Marketplace Order", order.name, "platform_order_reference")

	def test_fills_empty_reference_from_external_order_id(self):
		order = self.make_order("x")
		self.set_reference(order, "")

		execute()

		self.assertEqual(self.reference(order), order.external_order_id)

	def test_does_not_overwrite_a_filled_reference_or_bump_modified(self):
		order = self.make_order("#1027")
		modified = frappe.db.get_value("Marketplace Order", order.name, "modified")

		execute()

		self.assertEqual(self.reference(order), "#1027")
		self.assertEqual(frappe.db.get_value("Marketplace Order", order.name, "modified"), modified)

	def test_second_run_is_a_no_op(self):
		order = self.make_order("x")
		self.set_reference(order, None)
		execute()
		modified = frappe.db.get_value("Marketplace Order", order.name, "modified")

		execute()

		self.assertEqual(self.reference(order), order.external_order_id)
		self.assertEqual(frappe.db.get_value("Marketplace Order", order.name, "modified"), modified)
