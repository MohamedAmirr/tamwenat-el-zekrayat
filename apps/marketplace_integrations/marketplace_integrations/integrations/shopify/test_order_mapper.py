# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from decimal import Decimal

from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.shopify.order_mapper import normalize_order


class UnitTestShopifyOrderMapper(UnitTestCase):
	def order(self, **overrides):
		value = {
			"id": "gid://shopify/Order/100",
			"name": "#1001",
			"createdAt": "2026-09-27T10:00:00Z",
			"updatedAt": "2026-09-27T10:05:00Z",
			"displayFinancialStatus": "PAID",
			"displayFulfillmentStatus": "UNFULFILLED",
			"currencyCode": "EGP",
			"presentmentCurrencyCode": "EGP",
			"currentSubtotalPriceSet": {"shopMoney": {"amount": "100.00"}},
			"currentTotalDiscountsSet": {"shopMoney": {"amount": "10.00"}},
			"currentTotalTaxSet": {"shopMoney": {"amount": "14.00"}},
			"currentShippingPriceSet": {"shopMoney": {"amount": "5.00"}},
			"currentTotalDutiesSet": {"shopMoney": {"amount": "2.00"}},
			"currentTotalPriceSet": {"shopMoney": {"amount": "111.00"}},
			"transactions": [],
			"refunds": [],
			"lineItems": {
				"pageInfo": {"hasNextPage": False},
				"nodes": [
					{
						"id": "gid://shopify/LineItem/1",
						"name": "Coffee",
						"sku": "COFFEE-1",
						"quantity": 3,
						"currentQuantity": 2,
						"variant": {"id": "gid://shopify/ProductVariant/1"},
						"originalUnitPriceSet": {"shopMoney": {"amount": "20.00"}},
						"discountAllocations": [
							{"allocatedAmountSet": {"shopMoney": {"amount": "3.50"}}}
						],
					}
				],
			},
		}
		value.update(overrides)
		return value

	def test_maps_gid_status_totals_and_variant_identity(self):
		result = normalize_order(self.order())

		self.assertEqual(result["external_order_id"], "gid://shopify/Order/100")
		self.assertEqual(result["platform_order_reference"], "#1001")
		self.assertEqual(result["external_financial_status"], "PAID")
		self.assertIsNone(result["external_status"])
		self.assertEqual(result["shipping_total"], "5.00")
		self.assertEqual(result["duties_total"], "2.00")
		self.assertEqual(result["items"][0]["external_variant_id"], "gid://shopify/ProductVariant/1")
		self.assertEqual(result["items"][0]["current_quantity"], 2)
		self.assertEqual(result["items"][0]["net_amount"], Decimal("36.50"))

	def test_null_variant_is_supported(self):
		order = self.order()
		order["lineItems"]["nodes"][0]["variant"] = None
		result = normalize_order(order)
		self.assertIsNone(result["items"][0]["external_variant_id"])

	def test_transaction_amount_distinguishes_missing_from_real_zero(self):
		order = self.order()
		line = order["lineItems"]["nodes"][0]
		line["priceAfterAllDiscountsBeforeTaxesSet"] = None
		missing = normalize_order(order)["items"][0]
		self.assertEqual(missing["transaction_amount_missing"], 1)

		line["priceAfterAllDiscountsBeforeTaxesSet"] = {"shopMoney": {"amount": "0.00"}}
		zero = normalize_order(order)["items"][0]
		self.assertEqual(zero["transaction_amount"], "0.00")
		self.assertEqual(zero["transaction_amount_missing"], 0)

	def test_rejects_unbounded_line_items(self):
		order = self.order()
		order["lineItems"]["pageInfo"]["hasNextPage"] = True
		with self.assertRaisesRegex(Exception, "exceeds the supported lineItems"):
			normalize_order(order)
