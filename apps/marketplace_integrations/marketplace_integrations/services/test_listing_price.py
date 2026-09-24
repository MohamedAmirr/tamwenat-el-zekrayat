# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the platform-neutral listing-price resolution extracted out of
integrations/talabat/catalog_sync.py (#106 decision 4). Talabat's own
integrations/talabat/test_catalog_sync.py already exercises this logic
end-to-end through Talabat's own validate_catalog_sync_account() and
get_listing_price() wrapper; these tests exercise the shared module directly,
including the platform-neutral entry point (resolve_listing_price()) Shopify
calls without any Talabat-specific account validation.
"""

from decimal import Decimal

import frappe
from frappe.utils import add_days, nowdate

from marketplace_integrations.services import listing_price
from marketplace_integrations.testing_utils import CleanupTestCase


class IntegrationTestListingPrice(CleanupTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.price_list = (
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": f"Listing Price Test {self.suffix}",
					"currency": "USD",
					"selling": 1,
					"enabled": 1,
				}
			)
			.insert()
			.name
		)

	def _make_item(self, marker, uom=None, conversion_factor=None):
		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"TEST-LISTING-PRICE-{marker}-{self.suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}
		)
		if uom:
			doc.append("uoms", {"uom": uom, "conversion_factor": conversion_factor})
		return doc.insert()

	def _make_price(self, item, rate, uom="Nos", **kwargs):
		doc = {
			"doctype": "Item Price",
			"price_list": self.price_list,
			"item_code": item.name,
			"uom": uom,
			"price_list_rate": rate,
			"selling": 1,
		}
		doc.update(kwargs)
		return frappe.get_doc(doc).insert()

	def test_resolve_listing_price_prefers_current_exact_uom_price(self):
		item = self._make_item("EXACT")
		self._make_price(item, 5, valid_from=add_days(nowdate(), -3), valid_upto=add_days(nowdate(), -1))
		self._make_price(item, 11, valid_from=add_days(nowdate(), -1))
		self._make_price(item, 12, valid_from=nowdate())

		price = listing_price.resolve_listing_price(self.price_list, item, "Nos")

		self.assertEqual(price, Decimal("12"))

	def test_resolve_listing_price_raises_when_uom_dependent_and_no_match(self):
		item = self._make_item("NO-MATCH")
		self._make_price(item, 10, uom="Nos")

		with self.assertRaises(listing_price.CatalogBuildError):
			listing_price.resolve_listing_price(self.price_list, item, "Box")

	def test_resolve_listing_price_falls_back_via_conversion_when_price_not_uom_dependent(self):
		uom = f"Listing Box {self.suffix}"
		frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert()
		item = self._make_item("FALLBACK", uom=uom, conversion_factor=4)
		self._make_price(item, 10, uom="Nos")
		frappe.db.set_value("Price List", self.price_list, "price_not_uom_dependent", 1)

		price = listing_price.resolve_listing_price(self.price_list, item, uom)

		self.assertEqual(price, Decimal("40"))

	def test_listing_uom_prefers_mapping_sales_uom_then_falls_back_to_stock_uom(self):
		item = self._make_item("UOM-FALLBACK")

		class _Mapping:
			sales_uom = None

		self.assertEqual(listing_price.listing_uom(_Mapping(), item), "Nos")

		_Mapping.sales_uom = " Box "
		self.assertEqual(listing_price.listing_uom(_Mapping(), item), "Box")

	def test_conversion_factor_is_one_for_stock_uom(self):
		item = self._make_item("CONVERSION-ONE")
		self.assertEqual(listing_price.conversion_factor(item, "Nos"), Decimal(1))

	def test_conversion_factor_requires_a_positive_configured_row(self):
		item = self._make_item("CONVERSION-MISSING")
		with self.assertRaises(listing_price.CatalogBuildError):
			listing_price.conversion_factor(item, "Box")

	def test_to_decimal_rejects_non_numeric_values(self):
		with self.assertRaises(listing_price.CatalogBuildError):
			listing_price.to_decimal("not-a-number")
