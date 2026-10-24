# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api import shopify_payment_gateways as gateway_api
from marketplace_integrations.api.shopify_payment_gateways import eligible_gateway_counts


class TestShopifyPaymentGatewayPicker(IntegrationTestCase):
	def test_endpoint_is_account_scoped_and_marks_existing_mapping(self):
		account = frappe._dict(
			name="MKTACC-SHOPIFY",
			platform="Shopify",
			connection_doctype="Shopify Connection",
			shopify_payment_gateways=[frappe._dict(shopify_gateway="Kashier")],
			check_permission=lambda _ptype: None,
		)
		orders = [
			frappe._dict(
				payment_transactions=[
					{"kind": "SALE", "status": "SUCCESS", "gateway": " Kashier "},
					{"kind": "CAPTURE", "status": "SUCCESS", "gateway": "New Gateway"},
				]
			)
		]
		with (
			patch.object(gateway_api.frappe, "get_doc", return_value=account),
			patch.object(gateway_api.frappe, "has_permission", return_value=True),
			patch.object(gateway_api.frappe, "get_list", return_value=orders) as get_list,
		):
			result = gateway_api.list_recent_successful_gateways("MKTACC-SHOPIFY")
		self.assertEqual(
			result,
			[
				{"gateway": "Kashier", "orders_count": 1, "mapped": True},
				{"gateway": "New Gateway", "orders_count": 1, "mapped": False},
			],
		)
		self.assertEqual(get_list.call_args.args[0], "Marketplace Order")
		self.assertEqual(get_list.call_args.kwargs["limit_page_length"], 500)
		self.assertNotIn("start", get_list.call_args.kwargs)
		get_list.assert_called_once()
		self.assertEqual(get_list.call_args.kwargs["filters"]["marketplace_account"], account.name)
		self.assertNotIn("payment_transactions", get_list.call_args.kwargs["filters"])

	def test_endpoint_rejects_missing_marketplace_order_read_permission(self):
		account = frappe._dict(
			name="MKTACC-SHOPIFY",
			platform="Shopify",
			connection_doctype="Shopify Connection",
			shopify_payment_gateways=[],
			check_permission=lambda _ptype: None,
		)
		with (
			patch.object(gateway_api.frappe, "get_doc", return_value=account),
			patch.object(gateway_api.frappe, "has_permission", return_value=False),
			patch.object(gateway_api.frappe, "get_list", wraps=frappe.get_list) as get_list,
			self.assertRaises(frappe.PermissionError),
		):
			gateway_api.list_recent_successful_gateways(account.name)
		self.assertFalse(
			any(call.args and call.args[0] == "Marketplace Order" for call in get_list.call_args_list)
		)

	def test_only_successful_sale_and_capture_exact_trimmed_gateways_are_returned(self):
		orders = [
			{
				"payment_transactions": [
					{"kind": "SALE", "status": "SUCCESS", "gateway": " Kashier "},
					{"kind": "CAPTURE", "status": "SUCCESS", "gateway": "Paymob"},
					{"kind": "AUTHORIZATION", "status": "SUCCESS", "gateway": "unused"},
					{"kind": "SALE", "status": "PENDING", "gateway": "pending"},
					{"kind": "CAPTURE", "status": "SUCCESS", "gateway": "   "},
				],
			},
		]
		self.assertEqual(eligible_gateway_counts(orders), {"Kashier": 1, "Paymob": 1})

	def test_accepts_json_text_and_graphql_nodes_shape(self):
		orders = [
			{
				"payment_transactions": (
					'{"nodes":[{"kind":"SALE","status":"SUCCESS","gateway":"Kashier"}]}'
				)
			}
		]
		self.assertEqual(eligible_gateway_counts(orders), {"Kashier": 1})
