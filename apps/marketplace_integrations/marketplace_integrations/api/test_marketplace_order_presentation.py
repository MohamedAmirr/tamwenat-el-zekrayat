# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.marketplace_order_presentation import get_item_presentation
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order


class IntegrationTestMarketplaceOrderPresentation(IntegrationTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Presentation Connection {suffix}",
				"client_id": "test-client-id",
				"client_secret": "test-client-secret",
				"chain_id": "test-chain-id",
				"environment": "Sandbox",
			}
		).insert()
		self.account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Presentation Account {suffix}",
				"platform": "Talabat",
				"external_account_id": f"presentation-vendor-{suffix}",
				"connection_doctype": "Talabat Connection",
				"connection": self.connection.name,
			}
		).insert()
		self.order = upsert_marketplace_order(
			self.account.name,
			{
				"external_order_id": f"presentation-order-{suffix}",
				"latest_payload": {},
				"items": [
					{"external_item_id": "line-1", "external_sku": "mapped-sku"},
					{"external_item_id": "line-2", "external_sku": "unmapped-sku"},
				],
			},
		)

	@patch("marketplace_integrations.api.marketplace_order_presentation.resolve_item")
	def test_returns_live_mapping_status_without_a_price_fallback(self, resolve_item):
		resolve_item.side_effect = ["ITEM-MAPPED", None]

		result = get_item_presentation(self.order.name)

		self.assertFalse(result["price_comparison_available"])
		self.assertEqual(
			result["items"],
			[
				{"external_item_id": "line-1", "external_sku": "mapped-sku", "mapped_item": "ITEM-MAPPED"},
				{"external_item_id": "line-2", "external_sku": "unmapped-sku", "mapped_item": None},
			],
		)
		resolve_item.assert_any_call(self.account.name, "mapped-sku")
		resolve_item.assert_any_call(self.account.name, "unmapped-sku")

	def test_requires_read_permission_on_the_specific_order(self):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"presentation-{frappe.generate_hash(length=8)}@example.com",
				"first_name": "Presentation Reader",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()

		with self.set_user(user.name), self.assertRaises(frappe.PermissionError):
			get_item_presentation(self.order.name)

	@patch("marketplace_integrations.api.marketplace_order_presentation.frappe.has_permission", return_value=False)
	def test_requires_mapping_read_permission(self, has_permission):
		with self.assertRaises(frappe.PermissionError):
			get_item_presentation(self.order.name)
		has_permission.assert_called_once_with("Marketplace Product Mapping", ptype="read")
