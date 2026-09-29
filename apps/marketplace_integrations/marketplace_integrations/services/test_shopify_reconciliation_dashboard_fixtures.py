# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#142: one test per dashboard failure state, proving each surfaces correctly
in its corresponding Number Card filter / Page query.

Sections 2, 3, 5 are plain Number Cards (fixtures/number_card.json) - Frappe's
own Number Card widget evaluates `filters_json` via `frappe.get_list()`, so
proving "a fixture in this failure state is counted by the SAME filter the
Number Card fixture declares" is the correct test boundary here, without
reimplementing Frappe's Number Card rendering. Sections 1's cache and 4/6's
live queries are exercised directly via their own functions, already unit
tested in integrations/shopify/test_products.py, integrations/shopify/
test_orders.py and services/test_shopify_reconciliation.py - referenced here
again only to keep all 6 states enumerated in one place, per #142's
acceptance criteria."""

import json

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify.test_products import make_item, make_shopify_account
from marketplace_integrations.integrations.shopify.webhooks import MAX_WEBHOOK_DELIVERY_ATTEMPTS
from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)
from marketplace_integrations.services.shopify_reconciliation import (
	get_order_import_failures,
	get_refund_evidence_gaps,
)


def _number_card_filters(name):
	"""Read a shipped Number Card fixture's own filters_json and turn it into
	an ORM filter dict, so this test proves the fixture used by the real
	dashboard - not a hand-copied duplicate of its filter."""
	with open(
		frappe.get_app_path("marketplace_integrations", "fixtures", "number_card.json"), encoding="utf-8"
	) as f:
		cards = json.load(f)
	card = next(c for c in cards if c["name"] == name)
	filters = {}
	for _doctype, fieldname, operator, value, _invert in json.loads(card["filters_json"]):
		filters[fieldname] = value if operator == "=" else [operator, value]
	return card["document_type"], filters


class IntegrationTestDashboardFailureStatesSurfaceCorrectly(IntegrationTestCase):
	def setUp(self):
		self.account = make_shopify_account(f"Dashboard Fixture Account {frappe.generate_hash(length=8)}")
		self.item = make_item("Test Dashboard Fixture Item")

	def tearDown(self):
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc(
			"Shopify Connection", self.account.connection, ignore_permissions=True, force=True
		)

	def test_state_1_catalog_push_error_surfaces(self):
		mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": self.item,
				"external_reference": f"gid://shopify/ProductVariant/{frappe.generate_hash(length=8)}",
				"sync_status": "Error",
			}
		).insert()

		doctype, filters = _number_card_filters("Shopify Catalog Push Errors")
		names = [row.name for row in frappe.get_all(doctype, filters=filters)]
		self.assertIn(mapping.name, names)

	def test_state_2_inventory_sync_error_surfaces(self):
		mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": self.item,
				"external_reference": f"gid://shopify/ProductVariant/{frappe.generate_hash(length=8)}",
				"inventory_sync_status": "Error",
			}
		).insert()

		doctype, filters = _number_card_filters("Shopify Inventory Sync Errors")
		names = [row.name for row in frappe.get_all(doctype, filters=filters)]
		self.assertIn(mapping.name, names)

	def test_state_3_webhook_delivery_error_surfaces(self):
		delivery = frappe.get_doc(
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

		doctype, filters = _number_card_filters("Shopify Webhook Deliveries: Error")
		names = [row.name for row in frappe.get_all(doctype, filters=filters)]
		self.assertIn(delivery.name, names)

	def test_state_3b_webhook_delivery_exhausted_retries_surfaces(self):
		delivery = frappe.get_doc(
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

		doctype, filters = _number_card_filters("Shopify Webhook Deliveries: Exhausted Retries")
		names = [row.name for row in frappe.get_all(doctype, filters=filters)]
		self.assertIn(delivery.name, names)

	def test_state_4_sync_error_marketplace_order_surfaces(self):
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

		doctype, filters = _number_card_filters("Marketplace Orders: Sync Error")
		names = [row.name for row in frappe.get_all(doctype, filters=filters)]
		self.assertIn(order.name, names)

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

	def test_state_6_refund_evidence_gap_order_surfaces(self):
		order = frappe.get_doc(
			{
				"doctype": "Marketplace Order",
				"marketplace_account": self.account.name,
				"external_order_id": f"dashboard-fixture-refund-{frappe.generate_hash(length=8)}",
				"platform": "Shopify",
				"external_financial_status": "REFUNDED",
				"refunds": "[]",
			}
		)
		order.flags.skip_sync_status_update = True
		order.order_identity_key = compute_order_identity_key(self.account.name, order.external_order_id)
		order.insert()

		names = [row.name for row in get_refund_evidence_gaps()]
		self.assertIn(order.name, names)
