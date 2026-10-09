# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#142: one test per dashboard failure state, proving each surfaces correctly
in its corresponding count function / Script Report query.

States 1-4 used to be plain Number Cards (fixtures/number_card.json) -
Frappe's own Number Card widget evaluated `filters_json` via
`frappe.get_list()`. #139 superseded those 5 Number Cards with the "Shopify
Reconciliation Summary" Script Report, whose first 5 metric rows reproduce
the exact same filter logic via dedicated count functions in
services/shopify_reconciliation.py (get_catalog_push_error_count() etc.), so
these tests now call those functions directly instead of reading the
(removed) number_card.json fixture. State 5's live query is exercised
directly via its own function, already unit tested in
integrations/shopify/test_products.py, integrations/shopify/test_orders.py,
services/test_shopify_reconciliation.py, and each remaining Script Report's
own test_*.py (marketplace_integrations/report/shopify_order_import_failures/,
.../shopify_reconciliation_summary/) - referenced here again only to keep
states enumerated in one place, per #142's acceptance criteria. State 6
(refund evidence gaps) and its backing get_refund_evidence_gaps() were
removed entirely in favor of a Marketplace Order list view filter - see
docs/marketplace-integrations/shopify-reconciliation-dashboard.md."""

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify.test_products import make_item, make_shopify_account
from marketplace_integrations.integrations.shopify.webhooks import MAX_WEBHOOK_DELIVERY_ATTEMPTS
from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)
from marketplace_integrations.services.shopify_reconciliation import (
	get_catalog_push_error_count,
	get_inventory_sync_error_count,
	get_order_import_failures,
	get_order_sync_error_count,
	get_webhook_delivery_error_count,
	get_webhook_delivery_exhausted_retry_count,
)


class IntegrationTestDashboardFailureStatesSurfaceCorrectly(IntegrationTestCase):
	def setUp(self):
		self.account = make_shopify_account(f"Dashboard Fixture Account {frappe.generate_hash(length=8)}")
		self.item = make_item("Test Dashboard Fixture Item")

	def tearDown(self):
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Shopify Connection", self.account.connection, ignore_permissions=True, force=True)

	def test_state_1_catalog_push_error_surfaces(self):
		before = get_catalog_push_error_count()
		frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": self.item,
				"external_reference": f"gid://shopify/ProductVariant/{frappe.generate_hash(length=8)}",
				"sync_status": "Error",
			}
		).insert()

		self.assertEqual(get_catalog_push_error_count(), before + 1)

	def test_state_2_inventory_sync_error_surfaces(self):
		before = get_inventory_sync_error_count()
		frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": self.item,
				"external_reference": f"gid://shopify/ProductVariant/{frappe.generate_hash(length=8)}",
				"inventory_sync_status": "Error",
			}
		).insert()

		self.assertEqual(get_inventory_sync_error_count(), before + 1)

	def test_state_3_webhook_delivery_error_surfaces(self):
		before = get_webhook_delivery_error_count()
		frappe.get_doc(
			{
				"doctype": "Shopify Webhook Delivery",
				"connection": self.account.connection,
				"shop_domain": "test-dashboard-fixture.myshopify.com",
				"topic": "ORDERS_UPDATED",
				"webhook_id": frappe.generate_hash(length=16),
				"status": "Error",
				"attempt_count": 1,
				"received_at": frappe.utils.now_datetime(),
			}
		).insert()

		self.assertEqual(get_webhook_delivery_error_count(), before + 1)

	def test_state_3b_webhook_delivery_exhausted_retries_surfaces(self):
		before = get_webhook_delivery_exhausted_retry_count()
		frappe.get_doc(
			{
				"doctype": "Shopify Webhook Delivery",
				"connection": self.account.connection,
				"shop_domain": "test-dashboard-fixture.myshopify.com",
				"topic": "ORDERS_UPDATED",
				"webhook_id": frappe.generate_hash(length=16),
				"status": "Error",
				"attempt_count": MAX_WEBHOOK_DELIVERY_ATTEMPTS,
				"received_at": frappe.utils.now_datetime(),
			}
		).insert()

		self.assertEqual(get_webhook_delivery_exhausted_retry_count(), before + 1)

	def test_state_4_sync_error_marketplace_order_surfaces(self):
		before = get_order_sync_error_count()
		order = frappe.get_doc(
			{
				"doctype": "Marketplace Order",
				"marketplace_account": self.account.name,
				"external_order_id": f"dashboard-fixture-{frappe.generate_hash(length=8)}",
				"platform": "Shopify",
			}
		)
		order.flags.skip_sync_status_update = True
		order.order_identity_key = compute_order_identity_key(self.account.name, order.external_order_id)
		order.insert()
		order.db_set("erpnext_sync_status", "Sync Error", update_modified=False)

		self.assertEqual(get_order_sync_error_count(), before + 1)

	def test_state_5_order_import_failure_log_row_surfaces(self):
		log = frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"status": "Error",
				"attempted_at": frappe.utils.now_datetime(),
				"traceback": "dashboard fixture failure",
			}
		).insert()

		names = [row.name for row in get_order_import_failures()]
		self.assertIn(log.name, names)
