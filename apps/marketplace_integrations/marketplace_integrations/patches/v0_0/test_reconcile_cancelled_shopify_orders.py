# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.patches.v0_0.reconcile_cancelled_shopify_orders import execute
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order


class IntegrationTestReconcileCancelledShopifyOrders(IntegrationTestCase):
	def setUp(self):
		self.account = make_shopify_account(f"Reconcile Cancelled {frappe.generate_hash(length=8)}")

	def make_order(self, *, financial_status, cancelled_at=None, **changes):
		fields = {
			"external_order_id": f"reconcile-cancelled-{frappe.generate_hash(length=8)}",
			"latest_payload": {"id": "shopify-order"},
			"external_financial_status": financial_status,
			"external_fulfillment_status": "UNFULFILLED",
			"cancelled_at": cancelled_at,
			"order_total": 0,
			"shipping_total": 0,
			"total_taxes": 0,
			"duties_total": 0,
			"items": [
				{
					"external_item_id": "line-1",
					"current_quantity": 0,
					"unfulfilled_quantity": 0,
				}
			],
		}
		fields.update(changes)
		order = upsert_marketplace_order(self.account.name, fields)
		frappe.db.set_value(
			"Marketplace Order",
			order.name,
			{
				"erpnext_sync_status": "Sync Error",
				"last_sync_error": "old cancellation blocker",
			},
			update_modified=False,
		)
		return order.name

	def state(self, name):
		return frappe.db.get_value(
			"Marketplace Order", name, ["erpnext_sync_status", "last_sync_error"], as_dict=True
		)

	def test_classifies_both_cancellation_evidence_paths_and_is_idempotent(self):
		cancelled_at = self.make_order(financial_status="PENDING", cancelled_at=now_datetime())
		fully_refunded = self.make_order(financial_status="REFUNDED", cancelled_at=None)
		paid = self.make_order(financial_status="PAID", cancelled_at=now_datetime())
		linked = self.make_order(financial_status="PENDING", cancelled_at=now_datetime())
		non_shopify = self.make_order(financial_status="PENDING", cancelled_at=now_datetime())
		frappe.db.set_value("Marketplace Order", linked, "erpnext_sales_order", "SO-EXISTING")
		frappe.db.set_value("Marketplace Order", non_shopify, "platform", "Noon")
		log_count = frappe.db.count(
			"Marketplace Sync Log",
			{"marketplace_order": ["in", [cancelled_at, fully_refunded, paid, linked, non_shopify]]},
		)

		# The shared test site may already contain eligible fixture rows from prior
		# tests, so the migration's total update count is intentionally not fixed.
		self.assertGreaterEqual(execute(), 3)
		self.assertEqual(self.state(cancelled_at).erpnext_sync_status, "Cancelled")
		self.assertFalse(self.state(cancelled_at).last_sync_error)
		self.assertEqual(self.state(fully_refunded).erpnext_sync_status, "Cancelled")
		self.assertFalse(self.state(fully_refunded).last_sync_error)
		self.assertEqual(self.state(paid).erpnext_sync_status, "Sync Error")
		self.assertEqual(
			self.state(paid).last_sync_error,
			"Cancelled in Shopify but the payment was not fully refunded; refund it in Shopify",
		)
		self.assertEqual(self.state(linked).last_sync_error, "old cancellation blocker")
		self.assertEqual(self.state(non_shopify).last_sync_error, "old cancellation blocker")
		self.assertEqual(
			frappe.db.count(
				"Marketplace Sync Log",
				{"marketplace_order": ["in", [cancelled_at, fully_refunded, paid, linked, non_shopify]]},
			),
			log_count,
		)
		self.assertEqual(execute(), 0)
