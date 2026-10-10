# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.patches.v0_0.clear_cancelled_shopify_sync_errors import execute
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order


class IntegrationTestClearCancelledShopifySyncErrors(IntegrationTestCase):
	def setUp(self):
		self.account = make_shopify_account(f"Clear Cancelled {frappe.generate_hash(length=8)}")

	def make_order(self, **fields):
		order = upsert_marketplace_order(
			self.account.name,
			{
				"external_order_id": f"clear-cancelled-{frappe.generate_hash(length=8)}",
				"latest_payload": {"id": "1"},
				"items": [],
			},
		)
		frappe.db.set_value("Marketplace Order", order.name, fields, update_modified=False)
		return order.name

	def values(self, name):
		return frappe.db.get_value(
			"Marketplace Order", name, ["erpnext_sync_status", "last_sync_error"], as_dict=True
		)

	def test_clears_a_cancelled_unlinked_sync_error_and_leaves_the_rest(self):
		stuck = self.make_order(
			erpnext_sync_status="Sync Error",
			last_sync_error="Cancelled Shopify orders cannot be synchronized.",
			cancelled_at=now_datetime(),
		)
		not_cancelled = self.make_order(erpnext_sync_status="Sync Error", last_sync_error="Some blocker.")
		other_status = self.make_order(
			erpnext_sync_status="Modified After Sync", last_sync_error="Changed.", cancelled_at=now_datetime()
		)

		execute()

		self.assertEqual(self.values(stuck).erpnext_sync_status, "Not Synced")
		self.assertFalse(self.values(stuck).last_sync_error)
		self.assertEqual(self.values(not_cancelled).erpnext_sync_status, "Sync Error")
		self.assertEqual(self.values(other_status).erpnext_sync_status, "Modified After Sync")

	def test_is_idempotent(self):
		stuck = self.make_order(
			erpnext_sync_status="Sync Error", last_sync_error="Blocked.", cancelled_at=now_datetime()
		)

		execute()
		execute()

		self.assertEqual(self.values(stuck).erpnext_sync_status, "Not Synced")
