# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.test_marketplace_account import (
	mock_shopify_verification,
)
from marketplace_integrations.patches.v0_0.backfill_marketplace_order_platform import execute
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order


class IntegrationTestBackfillMarketplaceOrderPlatform(IntegrationTestCase):
	def make_account(self, platform="Talabat"):
		suffix = frappe.generate_hash(length=8)
		connection_doctype = "Talabat Connection" if platform == "Talabat" else "Shopify Connection"
		if platform == "Talabat":
			connection = frappe.get_doc(
				{
					"doctype": "Talabat Connection",
					"connection_name": f"Platform Backfill Connection {suffix}",
					"client_id": f"platform-backfill-client-{suffix}",
					"client_secret": "platform-backfill-secret",
					"chain_id": f"platform-backfill-chain-{suffix}",
				}
			).insert()
		else:
			connection = frappe.get_doc(
				{
					"doctype": "Shopify Connection",
					"connection_name": f"Platform Backfill Connection {suffix}",
					"shop_domain": f"platform-backfill-{suffix}.myshopify.com",
					"client_id": f"platform-backfill-client-{suffix}",
					"client_secret": "platform-backfill-secret",
				}
			).insert()
		account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Platform Backfill Account {suffix}",
				"platform": platform,
				"external_account_id": f"platform-backfill-external-{suffix}",
				"connection_doctype": connection_doctype,
				"connection": connection.name,
			}
		)
		if platform == "Shopify":
			# validate_shopify_account() makes a live Admin GraphQL call
			# (fetch_shop_and_active_locations()) - never make that call for
			# real from this test, same pattern test_marketplace_account.py
			# uses for every other Shopify-platform test.
			with mock_shopify_verification():
				account.insert()
		else:
			account.insert()
		return account

	def make_order(self, account, order_id):
		normalized_order = {
			"external_order_id": order_id,
			"external_status": "RECEIVED",
			"latest_payload": {"order_id": order_id},
			"items": [
				{
					"external_item_id": "item-001",
					"external_sku": "SKU-001",
					"fulfilled_quantity": 1,
					"fulfilled_total_price": 10.0,
				}
			],
		}
		return upsert_marketplace_order(account.name, normalized_order)

	def test_backfills_blank_platform_from_linked_account(self):
		account = self.make_account("Talabat")
		order = self.make_order(account, f"order-{frappe.generate_hash(length=8)}")
		# Simulate a pre-#108 row: platform column exists (post schema-sync)
		# but was never populated for this historical row.
		frappe.db.set_value("Marketplace Order", order.name, "platform", "", update_modified=False)
		frappe.clear_document_cache("Marketplace Order", order.name)

		execute()

		self.assertEqual(frappe.db.get_value("Marketplace Order", order.name, "platform"), "Talabat")

	def test_backfills_blank_platform_for_a_shopify_linked_order(self):
		"""The backfill is not Talabat-specific - it must correctly fill
		"Shopify" too, from a Shopify-linked Marketplace Account, not just
		"Talabat"."""
		account = self.make_account("Shopify")
		order = self.make_order(account, f"order-{frappe.generate_hash(length=8)}")
		frappe.db.set_value("Marketplace Order", order.name, "platform", "", update_modified=False)
		frappe.clear_document_cache("Marketplace Order", order.name)

		execute()

		self.assertEqual(frappe.db.get_value("Marketplace Order", order.name, "platform"), "Shopify")

	def test_does_not_overwrite_an_already_set_platform(self):
		"""A row whose platform already disagrees with its current account
		(e.g. the account's platform was switched after the order existed,
		the known limitation decision 1 accepts) must not be silently
		overwritten by this bulk backfill - it only fills genuinely blank
		values."""
		account = self.make_account("Talabat")
		order = self.make_order(account, f"order-{frappe.generate_hash(length=8)}")
		frappe.db.set_value("Marketplace Order", order.name, "platform", "Shopify", update_modified=False)
		frappe.clear_document_cache("Marketplace Order", order.name)

		execute()

		self.assertEqual(frappe.db.get_value("Marketplace Order", order.name, "platform"), "Shopify")

	def test_backfill_is_idempotent(self):
		"""Re-running the patch after it already backfilled a row must not
		touch that row again: the WHERE clause excludes already-set rows, so
		the second run's own UPDATE affects zero rows, and the already-correct
		value survives untouched."""
		account = self.make_account("Talabat")
		order = self.make_order(account, f"order-{frappe.generate_hash(length=8)}")
		frappe.db.set_value("Marketplace Order", order.name, "platform", "", update_modified=False)
		frappe.clear_document_cache("Marketplace Order", order.name)

		execute()
		self.assertEqual(frappe.db.get_value("Marketplace Order", order.name, "platform"), "Talabat")

		execute()
		self.assertEqual(frappe.db._cursor.rowcount, 0)
		self.assertEqual(frappe.db.get_value("Marketplace Order", order.name, "platform"), "Talabat")

	def test_never_touches_order_identity_key(self):
		account = self.make_account("Talabat")
		order = self.make_order(account, f"order-{frappe.generate_hash(length=8)}")
		frappe.db.set_value("Marketplace Order", order.name, "platform", "", update_modified=False)
		identity_key_before = frappe.db.get_value("Marketplace Order", order.name, "order_identity_key")

		execute()

		self.assertEqual(
			frappe.db.get_value("Marketplace Order", order.name, "order_identity_key"), identity_key_before
		)
