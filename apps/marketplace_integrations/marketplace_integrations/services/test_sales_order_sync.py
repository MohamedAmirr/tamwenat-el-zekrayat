# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate

from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.services.sales_order_sync import (
	SyncBlocker,
	_build_sales_order_lines,
	_validate_platform_supported,
	build_sales_order_snapshot,
	get_current_sync_status,
	sync_sales_order,
)

_DEFAULT_VARIANT = object()


class IntegrationTestSalesOrderSync(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.company = frappe.defaults.get_global_default("company")
		self.price_list = frappe.get_all(
			"Price List", filters={"selling": 1, "enabled": 1}, pluck="name", limit=1
		)[0]
		self.warehouse = frappe.get_all(
			"Warehouse",
			filters={"disabled": 0, "is_group": 0, "company": self.company},
			pluck="name",
			limit=1,
		)[0]
		self.item = self._make_item()
		self.account = self._make_account()
		self._make_mapping("SKU-UNIT", self.item.name)

	def _make_item(self):
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"TEST-SO-SYNC-{self.suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"price_list": self.price_list,
				"item_code": item.name,
				"price_list_rate": 17,
				"selling": 1,
			}
		).insert()
		return item

	def _make_account(self):
		connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Sales Order Sync {self.suffix}",
				"client_id": f"sales-order-sync-client-{self.suffix}",
				"client_secret": "test-secret",
				"chain_id": f"sales-order-sync-chain-{self.suffix}",
			}
		).insert()
		return frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Sales Order Sync Account {self.suffix}",
				"platform": "Talabat",
				"external_account_id": f"sales-order-sync-vendor-{self.suffix}",
				"connection_doctype": "Talabat Connection",
				"connection": connection.name,
				"erpnext_company": self.company,
				"erpnext_selling_price_list": self.price_list,
				"warehouse": self.warehouse,
			}
		).insert()

	def _make_mapping(self, sku, item):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": item,
				"external_reference": sku,
				"sales_uom": "Nos",
			}
		).insert()

	def _make_shopify_account(self):
		connection = frappe.get_doc(
			{
				"doctype": "Shopify Connection",
				"connection_name": f"Shopify SO Sync {self.suffix}",
				"shop_domain": f"shop-so-{self.suffix}.myshopify.com",
				"client_id": f"shopify-so-client-{self.suffix}",
				"client_secret": "test-secret",
			}
		).insert()
		with patch(
			"marketplace_integrations.marketplace_integrations.doctype.marketplace_account.marketplace_account.fetch_shop_and_active_locations",
			return_value={"shop_id": f"gid://shopify/Shop/{self.suffix}", "active_locations": []},
		):
			account = frappe.get_doc(
				{
					"doctype": "Marketplace Account",
					"account_name": f"Shopify SO Sync Account {self.suffix}",
					"platform": "Shopify",
					"external_account_id": f"gid://shopify/Shop/{self.suffix}",
					"connection": connection.name,
					"erpnext_company": self.company,
					"erpnext_selling_price_list": self.price_list,
					"warehouse": self.warehouse,
				}
			).insert()
		return account

	def _make_shopify_order(
		self,
		account,
		*,
		transaction_amount=36.0,
		current_quantity=2,
		variant_gid=_DEFAULT_VARIANT,
		financial_status="PAID",
		cancelled_at=None,
		transaction_amount_missing=0,
		create_mapping=True,
		original_unit_price=None,
		customer_email=None,
		customer_first_name=None,
		customer_last_name=None,
		customer_phone_number=None,
		shipping_address=None,
		billing_address=None,
	):
		if variant_gid is _DEFAULT_VARIANT:
			variant_gid = f"gid://shopify/ProductVariant/{frappe.generate_hash(length=12)}"
		mapping = None
		if create_mapping:
			mapping = frappe.get_doc(
				{
					"doctype": "Marketplace Product Mapping",
					"marketplace_account": account.name,
					"item": self.item.name,
					"external_reference": variant_gid,
					"external_variant_id": variant_gid,
					"sales_uom": "Nos",
				}
			).insert()
		order = upsert_marketplace_order(
			account.name,
			{
				"external_order_id": f"gid://shopify/Order/{self.suffix}-{frappe.generate_hash(length=4)}",
				"processed_on": nowdate(),
				"cancelled_at": cancelled_at,
				"external_financial_status": financial_status,
				"customer_email": customer_email,
				"customer_first_name": customer_first_name,
				"customer_last_name": customer_last_name,
				"customer_phone_number": customer_phone_number,
				"shipping_address": shipping_address,
				"billing_address": billing_address,
				"latest_payload": {"updatedAt": "2026-09-28T10:00:00Z"},
				"items": [
					{
						"external_item_id": f"gid://shopify/LineItem/{self.suffix}",
						"external_variant_id": variant_gid,
						"current_quantity": current_quantity,
						"transaction_amount": transaction_amount,
						"transaction_amount_missing": transaction_amount_missing,
						**(
							{"original_unit_price": original_unit_price}
							if original_unit_price is not None
							else {}
						),
					}
				],
			},
		)
		return order, mapping

	def _make_pricing_rule(self):
		return frappe.get_doc(
			{
				"doctype": "Pricing Rule",
				"title": f"SO Sync Test Rule {self.suffix}",
				"apply_on": "Item Code",
				"items": [{"item_code": self.item.name}],
				"selling": 1,
				"company": self.company,
				"currency": frappe.get_cached_value("Company", self.company, "default_currency"),
				"price_or_product_discount": "Price",
				"rate_or_discount": "Discount Percentage",
				"discount_percentage": 50,
			}
		).insert()

	def _make_default_sales_taxes_template(self):
		tax_account = frappe.get_all(
			"Account",
			filters={"account_type": "Tax", "company": self.company, "is_group": 0},
			pluck="name",
			limit=1,
		)[0]
		return frappe.get_doc(
			{
				"doctype": "Sales Taxes and Charges Template",
				"title": f"SO Sync Test Tax {self.suffix}",
				"company": self.company,
				"is_default": 1,
				"taxes": [
					{
						"charge_type": "On Net Total",
						"account_head": tax_account,
						"description": "Test Tax",
						"rate": 14,
					}
				],
			}
		).insert()

	def _make_order(
		self, sku="SKU-UNIT", pricing_type="UNIT", fulfilled_quantity=2, fulfilled_total_price=999
	):
		return upsert_marketplace_order(
			self.account.name,
			{
				"external_order_id": f"sales-order-sync-order-{self.suffix}-{frappe.generate_hash(length=4)}",
				"latest_payload": {"order_id": "external-order"},
				"received_on": nowdate(),
				"promised_for": add_days(nowdate(), 1),
				"customer_first_name": "Talabat Customer",
				"customer_phone_number": "masked",
				"order_total": 1200,
				"items": [
					{
						"external_item_id": "line-1",
						"external_sku": sku,
						"external_status": "IN_CART",
						"pricing_type": pricing_type,
						"fulfilled_quantity": fulfilled_quantity,
						"fulfilled_weight": 1.25 if pricing_type == "KG" else None,
						"fulfilled_unit_price": (fulfilled_total_price / fulfilled_quantity)
						if fulfilled_quantity
						else None,
						"fulfilled_total_price": fulfilled_total_price,
					}
				],
			},
		)

	def test_creates_one_draft_sales_order_with_talabat_transaction_pricing(self):
		order = self._make_order()

		result = sync_sales_order(order.name)
		order.reload()
		self.account.reload()
		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)

		self.assertTrue(result["ok"])
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(sales_order.docstatus, 0)
		self.assertEqual(sales_order.company, self.company)
		self.assertEqual(sales_order.selling_price_list, self.price_list)
		self.assertEqual(sales_order.transaction_date, getdate(order.received_on))
		self.assertEqual(sales_order.delivery_date, getdate(order.promised_for))
		self.assertEqual(sales_order.customer, self.account.erpnext_customer)
		self.assertEqual(sales_order.ignore_pricing_rule, 1)
		self.assertEqual(sales_order.items[0].item_code, self.item.name)
		self.assertEqual(sales_order.items[0].qty, 2)
		self.assertEqual(sales_order.items[0].warehouse, self.warehouse)
		# effective_rate = fulfilled_total_price / fulfilled_quantity, not the
		# configured Item Price of 17.
		self.assertEqual(sales_order.items[0].rate, 499.5)
		self.assertEqual(sales_order.items[0].price_list_rate, 499.5)
		self.assertEqual(sales_order.items[0].discount_percentage, 0)
		self.assertEqual(sales_order.items[0].amount, 999)
		self.assertEqual(sales_order.grand_total, 999)
		self.assertNotEqual(sales_order.grand_total, order.order_total)
		self.assertFalse(frappe.db.get_value("Customer", self.account.erpnext_customer, "mobile_no"))
		self.assertEqual(
			frappe.db.get_value("Customer", self.account.erpnext_customer, "customer_type"), "Company"
		)

	def test_zero_value_line_remains_zero_and_is_not_price_list_filled(self):
		"""Zero fulfilled_total_price (with a matching zero fulfilled_unit_price,
		see `_make_order()`) is a valid, final, free-item amount - not
		treated as missing data (that distinction is #85 review's finding
		around Fix B: only a zero total paired with a *nonzero* unit price is
		suspicious - see test_zero_total_with_nonzero_unit_price_blocks_sync)."""
		order = self._make_order(fulfilled_total_price=0)

		result = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])

		self.assertTrue(result["ok"])
		self.assertEqual(sales_order.items[0].rate, 0)
		self.assertEqual(sales_order.items[0].price_list_rate, 0)
		self.assertEqual(sales_order.items[0].amount, 0)
		self.assertEqual(sales_order.items[0].is_free_item, 1)
		order.reload()
		self.assertEqual(order.erpnext_sync_status, "Synced")

	def test_pricing_rule_cannot_change_the_line_amount(self):
		self._make_pricing_rule()
		order = self._make_order()

		result = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])

		self.assertTrue(result["ok"])
		self.assertEqual(sales_order.items[0].rate, 499.5)
		self.assertEqual(sales_order.items[0].amount, 999)

	def test_automatic_tax_default_does_not_change_the_merchandise_amount(self):
		self._make_default_sales_taxes_template()
		order = self._make_order()

		result = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])

		self.assertTrue(result["ok"])
		self.assertEqual(sales_order.items[0].rate, 499.5)
		self.assertEqual(sales_order.items[0].amount, 999)
		self.assertEqual(sales_order.net_total, 999)

	def test_item_price_change_after_sync_does_not_affect_the_sales_order_or_hash(self):
		order = self._make_order()
		sync_sales_order(order.name)
		order.reload()

		frappe.db.set_value(
			"Item Price",
			{"item_code": self.item.name, "price_list": self.price_list, "selling": 1},
			"price_list_rate",
			999,
		)

		self.assertEqual(get_current_sync_status(order.name), "Synced")
		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
		self.assertEqual(sales_order.items[0].rate, 499.5)

	def test_fulfilled_total_price_change_dirties_hash_and_updates_the_sales_order(self):
		order = self._make_order()
		first = sync_sales_order(order.name)
		order.reload()
		order.items[0].fulfilled_total_price = 500
		order.save()
		order.reload()

		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		second = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", first["sales_order"])
		order.reload()

		self.assertTrue(second["ok"])
		self.assertEqual(second["sales_order"], first["sales_order"])
		self.assertEqual(sales_order.items[0].rate, 250)
		self.assertEqual(sales_order.items[0].amount, 500)
		self.assertEqual(order.erpnext_sync_status, "Synced")

	def test_retry_does_not_create_a_second_sales_order(self):
		order = self._make_order()
		first = sync_sales_order(order.name)
		second = sync_sales_order(order.name)

		self.assertTrue(first["ok"])
		self.assertTrue(second["ok"])
		self.assertEqual(first["sales_order"], second["sales_order"])
		self.assertEqual(frappe.db.count("Sales Order", {"name": first["sales_order"]}), 1)

	def test_changed_fulfilled_quantity_updates_the_same_draft_sales_order(self):
		order = self._make_order()
		first = sync_sales_order(order.name)
		order.reload()
		order.items[0].fulfilled_quantity = 3
		order.items[0].fulfilled_total_price = 1500
		order.save()
		order.reload()

		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		second = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", first["sales_order"])
		order.reload()

		self.assertTrue(second["ok"])
		self.assertEqual(second["sales_order"], first["sales_order"])
		self.assertEqual(sales_order.items[0].qty, 3)
		self.assertEqual(sales_order.items[0].rate, 500)
		self.assertEqual(order.erpnext_sync_status, "Synced")

	def test_unmapped_final_line_blocks_without_creating_a_sales_order(self):
		order = self._make_order(sku="UNMAPPED-SKU")

		result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertIn("UNMAPPED-SKU", result["error"])
		self.assertFalse(order.erpnext_sales_order)
		self.assertEqual(order.erpnext_sync_status, "Sync Error")

	def test_kg_line_blocks_until_a_uom_policy_is_configured(self):
		self._make_mapping("SKU-KG", self.item.name)
		order = self._make_order(sku="SKU-KG", pricing_type="KG")

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertIn("KG", result["error"])

	def test_submitted_sales_order_remains_modified_after_sync(self):
		order = self._make_order()
		first = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", first["sales_order"])
		sales_order.submit()
		order.reload()
		order.items[0].fulfilled_quantity = 3
		order.items[0].fulfilled_total_price = 1500
		order.save()

		result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertIn("submitted", result["error"])

	def test_talabat_only_changes_do_not_dirty_the_sales_order_snapshot(self):
		order = self._make_order()
		sync_sales_order(order.name)
		order.reload()
		order.customer_phone_number = "new masked value"
		order.external_status = "READY_FOR_PICKUP"
		order.save()
		order.reload()

		self.assertEqual(order.erpnext_sync_status, "Synced")

	def test_missing_account_warehouse_blocks_sync(self):
		self.account.reload()
		self.account.warehouse = None
		self.account.save()
		order = self._make_order()

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertIn("Warehouse", result["error"])

	def test_warehouse_belonging_to_a_different_company_blocks_sync(self):
		"""#85 review finding #5: Marketplace Account.warehouse has no company
		filter and erpnext_company is independently editable - if they diverge,
		this must be an actionable SyncBlocker, not an unhandled ERPNext throw.
		"""
		other_company = frappe.get_all(
			"Company", filters={"name": ["!=", self.company]}, pluck="name", limit=1
		)
		if not other_company:
			other_company = [
				frappe.get_doc(
					{
						"doctype": "Company",
						"company_name": f"SO Sync Other Co {self.suffix}",
						"default_currency": frappe.get_cached_value(
							"Company", self.company, "default_currency"
						),
						"country": frappe.get_cached_value("Company", self.company, "country"),
					}
				)
				.insert()
				.name
			]
		other_warehouse = frappe.get_all(
			"Warehouse",
			filters={"disabled": 0, "is_group": 0, "company": other_company[0]},
			pluck="name",
			limit=1,
		)[0]

		self.account.reload()
		self.account.warehouse = other_warehouse
		self.account.save()
		order = self._make_order()

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertIn("Company", result["error"])

	def test_disabled_warehouse_blocks_sync(self):
		"""#85 review finding #5: a disabled Warehouse throws the same way a
		company mismatch does; must be an actionable SyncBlocker."""
		disabled_warehouse = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": f"SO Sync Disabled WH {self.suffix}",
				"company": self.company,
				"disabled": 1,
			}
		).insert()
		self.account.reload()
		self.account.warehouse = disabled_warehouse.name
		self.account.save()
		order = self._make_order()

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertIn("disabled", result["error"].lower())

	def test_price_list_currency_mismatch_blocks_sync(self):
		"""#85 review finding #9: V1 assumes single-currency operation - a
		Selling Price List whose currency differs from the account's Company
		default currency must block sync rather than silently copying a
		Talabat amount as a raw rate into the wrong currency."""
		company_currency = frappe.get_cached_value("Company", self.company, "default_currency")
		other_currency = "USD" if company_currency != "USD" else "EUR"
		other_price_list = frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": f"SO Sync Other Currency {self.suffix}",
				"currency": other_currency,
				"selling": 1,
				"enabled": 1,
			}
		).insert()

		self.account.reload()
		self.account.erpnext_selling_price_list = other_price_list.name
		self.account.save()
		order = self._make_order()

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertIn("currency", result["error"].lower())

	def test_resync_resets_header_discount_tax_and_shipping(self):
		"""#85 review finding #3: a header-level discount/tax/shipping added by
		hand to the Draft between syncs must not survive a re-sync and silently
		cut the merchandise total below the Talabat transaction total."""
		order = self._make_order()
		first = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", first["sales_order"])
		sales_order.apply_discount_on = "Grand Total"
		sales_order.discount_amount = 500
		sales_order.save()
		sales_order.reload()
		self.assertLess(sales_order.grand_total, 999)

		order.reload()
		order.items[0].fulfilled_total_price = 1000
		order.items[0].fulfilled_unit_price = 500
		order.save()

		result = sync_sales_order(order.name)
		sales_order.reload()

		self.assertTrue(result["ok"])
		self.assertEqual(sales_order.discount_amount, 0)
		self.assertEqual(sales_order.items[0].amount, 1000)
		self.assertEqual(sales_order.grand_total, 1000)

	def test_null_fulfilled_total_price_blocks_in_the_line_builder(self):
		"""`fulfilled_total_price` is stored as a `NOT NULL Float` column, so a
		`None` value never survives a real Marketplace Order save/reload - it is
		silently coerced to `0.0`, a valid fulfilled amount (confirmed directly
		against this app's own schema). `_build_sales_order_lines()` therefore
		checks the separate `fulfilled_total_price_missing` flag (#85 review
		finding #1), not `fulfilled_total_price is None` - this exercises that
		guard directly, in memory, as defense-in-depth for a future in-memory
		caller that has not gone through a real Marketplace Order save/reload.
		"""
		order = SimpleNamespace(
			items=[
				frappe._dict(
					external_status="IN_CART",
					pricing_type="UNIT",
					external_item_id="line-1",
					external_sku="SKU-UNIT",
					fulfilled_quantity=2,
					fulfilled_total_price=None,
					fulfilled_total_price_missing=1,
				)
			]
		)

		with self.assertRaises(SyncBlocker):
			_build_sales_order_lines(order, self.account)

	def test_missing_raw_total_price_is_captured_as_missing_not_zero(self):
		"""A Talabat payload with `pricing.total_price` entirely absent must not
		silently become a synced free line: `integrations/talabat/order_mapper.py`
		must capture that absence as `fulfilled_total_price_missing` at parse
		time, before the value is coerced to `0.0` by the (NOT NULL) Float
		column, and `sync_sales_order()` must still block on it through the
		real DB-backed path (#85 review finding #1).
		"""
		from marketplace_integrations.integrations.talabat.order_mapper import normalize_order

		raw_order = {
			"order_id": f"raw-order-{self.suffix}",
			"sys": {"created_at": nowdate()},
			"promised_for": add_days(nowdate(), 1),
			"customer": {"first_name": "Talabat Customer", "phone_number": "masked"},
			"payment": {"order_total": 100},
			"items": [
				{
					"_id": "line-1",
					"sku": "SKU-UNIT",
					"status": "IN_CART",
					"original_pricing": {"pricing_type": "UNIT", "quantity": 2},
					"pricing": {"quantity": 2},
				}
			],
		}
		normalized = normalize_order(raw_order)
		order = upsert_marketplace_order(self.account.name, normalized)
		order.reload()  # the NOT NULL Float coercion is a DB-layer effect, not visible in-memory pre-reload

		self.assertEqual(order.items[0].fulfilled_total_price, 0)
		self.assertEqual(order.items[0].fulfilled_total_price_missing, 1)

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertIn("no fulfilled total price", result["error"].lower())

	def test_negative_fulfilled_total_price_blocks_sync(self):
		"""A negative fulfilled_total_price is never valid (#85 review finding #12)."""
		order = self._make_order(fulfilled_total_price=-1)

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertIn("negative fulfilled total price", result["error"].lower())

	def test_fulfilled_quantity_must_be_greater_than_zero(self):
		"""fulfilled_quantity <= 0 blocks sync."""
		order = self._make_order(fulfilled_quantity=0)
		order.reload()
		order.items[0].fulfilled_quantity = 0
		order.items[0].fulfilled_total_price = 0
		order.flags.ignore_mandatory = True
		order.save()

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertIn("positive fulfilled quantity", result["error"].lower())

	def test_negative_fulfilled_quantity_blocks_sync(self):
		"""Negative fulfilled_quantity blocks sync."""
		order = self._make_order(fulfilled_quantity=-1)
		order.reload()
		order.items[0].fulfilled_quantity = -1
		order.flags.ignore_mandatory = True
		order.save()

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertIn("positive fulfilled quantity", result["error"].lower())

	def test_only_final_status_lines_are_synced(self):
		"""IN_CART and ADDITION are synced; NOT_PROCESSED, NOT_FOUND, REPLACED are skipped."""
		self._make_mapping("SKU-NOTPROCESSED", self.item.name)
		self._make_mapping("SKU-NOTFOUND", self.item.name)
		self._make_mapping("SKU-REPLACED", self.item.name)

		order = upsert_marketplace_order(
			self.account.name,
			{
				"external_order_id": f"sales-order-sync-order-{self.suffix}-{frappe.generate_hash(length=4)}",
				"latest_payload": {"order_id": "external-order"},
				"received_on": nowdate(),
				"promised_for": add_days(nowdate(), 1),
				"customer_first_name": "Talabat Customer",
				"customer_phone_number": "masked",
				"order_total": 2000,
				"items": [
					{
						"external_item_id": "line-1-in-cart",
						"external_sku": "SKU-UNIT",
						"external_status": "IN_CART",
						"pricing_type": "UNIT",
						"fulfilled_quantity": 1,
						"fulfilled_total_price": 100,
					},
					{
						"external_item_id": "line-2-addition",
						"external_sku": "SKU-UNIT",
						"external_status": "ADDITION",
						"pricing_type": "UNIT",
						"fulfilled_quantity": 2,
						"fulfilled_total_price": 200,
					},
					{
						"external_item_id": "line-3-not-processed",
						"external_sku": "SKU-NOTPROCESSED",
						"external_status": "NOT_PROCESSED",
						"pricing_type": "UNIT",
						"fulfilled_quantity": 1,
						"fulfilled_total_price": 50,
					},
					{
						"external_item_id": "line-4-not-found",
						"external_sku": "SKU-NOTFOUND",
						"external_status": "NOT_FOUND",
						"pricing_type": "UNIT",
						"fulfilled_quantity": 1,
						"fulfilled_total_price": 50,
					},
					{
						"external_item_id": "line-5-replaced",
						"external_sku": "SKU-REPLACED",
						"external_status": "REPLACED",
						"pricing_type": "UNIT",
						"fulfilled_quantity": 1,
						"fulfilled_total_price": 50,
					},
				],
			},
		)

		result = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])

		self.assertTrue(result["ok"])
		# Only IN_CART and ADDITION lines should be synced (2 total)
		self.assertEqual(len(sales_order.items), 2)
		self.assertEqual(sales_order.items[0].item_code, self.item.name)
		self.assertEqual(sales_order.items[0].qty, 1)
		self.assertEqual(sales_order.items[0].amount, 100)
		self.assertEqual(sales_order.items[1].item_code, self.item.name)
		self.assertEqual(sales_order.items[1].qty, 2)
		self.assertEqual(sales_order.items[1].amount, 200)
		self.assertEqual(sales_order.grand_total, 300)

	def test_non_evenly_divisible_fulfilled_amounts_sync_successfully(self):
		"""#85 review Fix A: every previously-tested value evenly divided by
		its quantity (999/2, 750/3, 1500/3, 100/1, 200/2, 500/2), leaving the
		non-evenly-divisible path entirely untested even though it is the
		routine case for a discounted multi-quantity line. `effective_rate`
		is rounded to a fixed 6dp (`EFFECTIVE_RATE_PRECISION`), and
		`fixtures/property_setter.json` raises `Sales Order Item.rate`/
		`price_list_rate`'s own field precision so ERPNext's
		`calculate_taxes_and_totals()` does not silently re-round the rate
		back down to 2dp before computing `amount` - without that fixture,
		`9.99 / 2 = 4.995` would round to `5.00` at 2dp and `5.00 * 2 = 10.00
		!= 9.99` would incorrectly block every one of these cases."""
		cases = [
			(9.99, 2),
			(10, 3),
			(19.99, 3),
			(7.50, 4),
			(0.05, 2),
			(100, 7),  # a much higher denominator, stress-testing 6dp precision
			(100, 11),
		]
		for fulfilled_total_price, fulfilled_quantity in cases:
			with self.subTest(
				fulfilled_total_price=fulfilled_total_price, fulfilled_quantity=fulfilled_quantity
			):
				order = self._make_order(
					fulfilled_quantity=fulfilled_quantity, fulfilled_total_price=fulfilled_total_price
				)

				result = sync_sales_order(order.name)

				self.assertTrue(result["ok"], result.get("error"))
				sales_order = frappe.get_doc("Sales Order", result["sales_order"])
				self.assertEqual(sales_order.items[0].amount, frappe.utils.flt(fulfilled_total_price, 2))
				self.assertEqual(sales_order.grand_total, frappe.utils.flt(fulfilled_total_price, 2))
				order.reload()
				self.assertEqual(order.erpnext_sync_status, "Synced")

	def test_zero_total_with_nonzero_unit_price_blocks_sync(self):
		"""#85 review Fix B: a zero fulfilled_total_price paired with a
		nonzero fulfilled_unit_price on the same line looks like missing
		Talabat pricing data, not an honest free item, and must block rather
		than silently post a zero-value line."""
		order = self._make_order(fulfilled_total_price=0)
		order.reload()
		order.items[0].fulfilled_unit_price = 5
		order.save()

		result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertIn("nonzero unit price", result["error"].lower())
		self.assertEqual(order.erpnext_sync_status, "Sync Error")

	def test_unsupported_platform_blocks_sync_without_creating_a_sales_order(self):
		"""Unsupported platforms are rejected without persisting a misleading
		"Sync Error" status for an order that was never eligible for sync in
		the first place - this must be a pure no-op on the stored document:
		no status change, no save (asserted via `modified` being unchanged)."""
		order = self._make_order()
		frappe.db.set_value("Marketplace Order", order.name, "platform", "Amazon", update_modified=False)
		frappe.clear_document_cache("Marketplace Order", order.name)
		order.reload()
		original_status = order.erpnext_sync_status
		original_modified = order.modified

		result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertIn("Talabat and Shopify", result["error"])
		self.assertFalse(order.erpnext_sales_order)
		self.assertEqual(order.erpnext_sync_status, original_status)
		self.assertNotEqual(order.erpnext_sync_status, "Sync Error")
		self.assertEqual(order.modified, original_modified)

	def test_shopify_platform_passes_platform_gate(self):
		order = self._make_order()
		order.platform = "Shopify"
		_validate_platform_supported(order)

	def test_shopify_cancelled_or_unpaid_orders_are_blocked(self):
		account = self._make_shopify_account()
		for options in (
			{"cancelled_at": nowdate()},
			{"financial_status": "PENDING"},
		):
			with self.subTest(options=options):
				order, _mapping = self._make_shopify_order(account, **options)
				result = sync_sales_order(order.name)
				self.assertFalse(result["ok"])
				self.assertEqual(result["status"], "Sync Error")
				self.assertFalse(order.erpnext_sales_order)

	def test_shopify_unmapped_null_variant_and_fully_refunded_lines_block(self):
		account = self._make_shopify_account()
		cases = [
			{"variant_gid": None, "create_mapping": False},
			{"variant_gid": f"gid://shopify/ProductVariant/unmapped-{self.suffix}", "create_mapping": False},
			{"current_quantity": 0},
		]
		for options in cases:
			with self.subTest(options=options):
				order, _mapping = self._make_shopify_order(account, **options)
				if options.get("variant_gid") is None:
					order.items[0].external_variant_id = None
					order.save()
				result = sync_sales_order(order.name)
				self.assertFalse(result["ok"])
				self.assertFalse(order.erpnext_sales_order)

	def test_missing_transaction_amount_blocks_but_real_zero_is_free(self):
		account = self._make_shopify_account()
		missing, _mapping = self._make_shopify_order(
			account, transaction_amount=None, transaction_amount_missing=1
		)
		missing.reload()
		self.assertEqual(missing.items[0].transaction_amount, 0)
		self.assertTrue(missing.items[0].transaction_amount_missing)
		result = sync_sales_order(missing.name)
		self.assertFalse(result["ok"])
		self.assertIn("no transaction amount", result["error"].lower())

		free, _mapping = self._make_shopify_order(account, transaction_amount=0)
		result = sync_sales_order(free.name)
		self.assertTrue(result["ok"], result.get("error"))
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])
		self.assertEqual(sales_order.items[0].rate, 0)
		self.assertTrue(sales_order.items[0].is_free_item)

	def test_shopify_reprocessing_reuses_sales_order_and_preserves_transaction_rate(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, transaction_amount=36, current_quantity=2)
		first = sync_sales_order(order.name)
		self.assertTrue(first["ok"], first.get("error"))
		so_name = first["sales_order"]
		so = frappe.get_doc("Sales Order", so_name)
		self.assertEqual(so.items[0].rate, 18)

		second = sync_sales_order(order.name)
		self.assertTrue(second["ok"], second.get("error"))
		self.assertEqual(second["sales_order"], so_name)
		self.assertFalse(second["updated"])

		item_price = frappe.get_doc(
			"Item Price", {"item_code": self.item.name, "price_list": self.price_list}
		)
		item_price.price_list_rate = 99
		item_price.save()
		third = sync_sales_order(order.name)
		self.assertTrue(third["ok"], third.get("error"))
		self.assertEqual(frappe.db.get_value("Sales Order Item", {"parent": so_name}, "rate"), 18)
		self.assertFalse(third["updated"])

		order.reload()
		order.items[0].transaction_amount = 40
		order.save()
		fourth = sync_sales_order(order.name)
		self.assertTrue(fourth["ok"], fourth.get("error"))
		self.assertEqual(fourth["sales_order"], so_name)
		self.assertTrue(fourth["updated"])

	def test_shopify_sync_creates_no_accounting_or_stock_documents(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)
		doctypes = (
			"Sales Invoice",
			"Payment Entry",
			"GL Entry",
			"Delivery Note",
			"Stock Entry",
		)
		before = {doctype: frappe.db.count(doctype) for doctype in doctypes}
		result = sync_sales_order(order.name)
		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual({doctype: frappe.db.count(doctype) for doctype in doctypes}, before)

	def test_shopify_discounted_transaction_amount_flows_through_without_further_discount_math(self):
		"""Shopify's `transaction_amount` arrives from
		`priceAfterAllDiscountsBeforeTaxesSet` already discount-adjusted
		(see `integrations/shopify/order_mapper.py`), unlike Talabat, which
		computes its own discount logic on this codebase's side. A
		`transaction_amount` lower than `original_unit_price *
		current_quantity` (20 * 2 = 40 here, vs. a 30 transaction_amount - a
		25% discount already baked in by Shopify) must flow straight through
		to `effective_rate`/the Sales Order Item's rate as-is: no further
		discount arithmetic is ever applied on this side."""
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(
			account, original_unit_price=20, current_quantity=2, transaction_amount=30
		)

		result = sync_sales_order(order.name)
		self.assertTrue(result["ok"], result.get("error"))
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])

		# effective_rate = transaction_amount / current_quantity = 30 / 2 = 15,
		# NOT original_unit_price (20) and not a further-discounted value.
		self.assertEqual(sales_order.items[0].rate, 15)
		self.assertEqual(sales_order.items[0].price_list_rate, 15)
		self.assertEqual(sales_order.items[0].discount_percentage, 0)
		self.assertEqual(sales_order.items[0].amount, 30)

	def test_shopify_blank_email_uses_generic_account_customer(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		account.reload()
		self.assertEqual(frappe.db.get_value("Sales Order", result["sales_order"], "customer"), account.erpnext_customer)
		self.assertEqual(frappe.db.get_value("Customer", account.erpnext_customer, "customer_type"), "Company")

	def test_shopify_first_sync_creates_individual_customer_and_repeat_reuses_it(self):
		account = self._make_shopify_account()
		email = f"buyer-{self.suffix}@example.test"
		order, _mapping = self._make_shopify_order(
			account,
			customer_email=email,
			customer_first_name="  Casey ",
			customer_last_name=" Buyer  ",
			customer_phone_number="+20123456789",
		)

		first = sync_sales_order(order.name)
		self.assertTrue(first["ok"], first.get("error"))
		first_customer = frappe.db.get_value("Sales Order", first["sales_order"], "customer")
		customer = frappe.get_doc("Customer", first_customer)
		self.assertEqual(customer.customer_name, "Casey Buyer")
		self.assertEqual(customer.customer_type, "Individual")
		self.assertEqual(customer.email_id, email)
		self.assertEqual(customer.mobile_no, "+20123456789")

		second = sync_sales_order(order.name)
		self.assertTrue(second["ok"], second.get("error"))
		self.assertEqual(frappe.db.get_value("Sales Order", second["sales_order"], "customer"), first_customer)
		self.assertEqual(frappe.db.count("Customer", {"email_id": email}), 1)
		self.assertFalse(account.reload().erpnext_customer)

	def test_shopify_orders_with_same_email_share_customer(self):
		account = self._make_shopify_account()
		email = f"shared-{self.suffix}@example.test"
		orders = [
			self._make_shopify_order(account, customer_email=email, customer_first_name="Buyer")[0]
			for _ in range(2)
		]
		customers = []
		for order in orders:
			result = sync_sales_order(order.name)
			self.assertTrue(result["ok"], result.get("error"))
			customers.append(frappe.db.get_value("Sales Order", result["sales_order"], "customer"))

		self.assertEqual(customers[0], customers[1])
		self.assertEqual(frappe.db.count("Customer", {"email_id": email}), 1)

	def test_shopify_customer_name_falls_back_to_email(self):
		account = self._make_shopify_account()
		email = f"nameless-{self.suffix}@example.test"
		order, _mapping = self._make_shopify_order(account, customer_email=email)

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		customer = frappe.get_doc("Customer", frappe.db.get_value("Sales Order", result["sales_order"], "customer"))
		self.assertEqual(customer.customer_name, email)
		self.assertEqual(customer.customer_type, "Individual")

	def test_shopify_snapshot_default_uses_buyer_customer(self):
		# build_sales_order_snapshot()'s no-customer-passed default is the
		# read-only status-check path: it only looks up a Customer already
		# resolved by a prior real sync, so this syncs once first.
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(
			account, customer_email=f"snapshot-{self.suffix}@example.test"
		)
		result = sync_sales_order(order.name)
		self.assertTrue(result["ok"], result.get("error"))

		snapshot = build_sales_order_snapshot(order, account=account)

		self.assertEqual(
			frappe.db.get_value("Customer", snapshot["customer"], "email_id"), order.customer_email
		)

	def test_shopify_status_check_never_synced_creates_no_customer_or_address(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(
			account,
			customer_email=f"status-no-sync-{self.suffix}@example.test",
			shipping_address={"address1": "1 Untouched Street", "city": "Cairo", "country": "Egypt"},
		)

		customer_count_before = frappe.db.count("Customer")
		address_count_before = frappe.db.count("Address")

		get_current_sync_status(order.name)

		self.assertEqual(frappe.db.count("Customer"), customer_count_before)
		self.assertEqual(frappe.db.count("Address"), address_count_before)

	def test_shopify_status_check_after_sync_with_new_email_creates_no_customer_or_address(self):
		# Once an order has been synced, get_current_sync_status() recomputes
		# a fresh snapshot on every form load to detect drift. If the order's
		# customer_email has since changed to one never resolved before (e.g.
		# a webhook reprocessing the order), the status check must still be
		# read-only: it must not insert a new buyer Customer or Address just
		# because the form was opened.
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(
			account,
			customer_email=f"status-old-{self.suffix}@example.test",
			shipping_address={"address1": "1 Original Street", "city": "Cairo", "country": "Egypt"},
		)
		result = sync_sales_order(order.name)
		self.assertTrue(result["ok"], result.get("error"))

		order_doc = frappe.get_doc("Marketplace Order", order.name)
		order_doc.customer_email = f"status-new-{self.suffix}@example.test"
		order_doc.save(ignore_permissions=True)

		customer_count_before = frappe.db.count("Customer")
		address_count_before = frappe.db.count("Address")

		status = get_current_sync_status(order.name)

		self.assertEqual(frappe.db.count("Customer"), customer_count_before)
		self.assertEqual(frappe.db.count("Address"), address_count_before)
		self.assertEqual(status, "Modified After Sync")

	def test_shopify_shipping_address_is_created_and_linked_to_buyer(self):
		account = self._make_shopify_account()
		email = f"shipping-{self.suffix}@example.test"
		order, _mapping = self._make_shopify_order(
			account,
			customer_email=email,
			customer_phone_number="+20123456789",
			shipping_address={
				"address1": "12 Market Street",
				"address2": "Unit 4",
				"city": "Cairo",
				"country": "Egypt",
				"zip": "11511",
			},
			billing_address={"address1": "Billing must not win", "country": "Egypt"},
		)

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])
		address = frappe.get_doc("Address", sales_order.shipping_address_name)
		self.assertTrue(address.address_title.startswith("Shopify Marketplace Address-"))
		self.assertEqual(address.address_type, "Shipping")
		self.assertEqual(address.address_line1, "12 Market Street")
		self.assertEqual(address.address_line2, "Unit 4")
		self.assertEqual(address.city, "Cairo")
		self.assertEqual(address.country, "Egypt")
		self.assertEqual(address.pincode, "11511")
		self.assertEqual(address.email_id, email)
		self.assertEqual(address.phone, "+20123456789")
		self.assertTrue(
			any(link.link_doctype == "Customer" and link.link_name == sales_order.customer for link in address.links)
		)
		self.assertTrue(address.is_primary_address)

	def test_shopify_billing_fallback_sets_customer_address_and_type(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(
			account,
			customer_email=f"billing-{self.suffix}@example.test",
			billing_address={"address1": "8 Billing Road", "city": "Giza", "country": "Egypt"},
		)

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])
		self.assertTrue(sales_order.shipping_address_name)
		self.assertEqual(sales_order.shipping_address_name, sales_order.customer_address)
		self.assertEqual(frappe.db.get_value("Address", sales_order.customer_address, "address_type"), "Billing")

	def test_shopify_resync_updates_only_dedicated_address_without_duplicate(self):
		account = self._make_shopify_account()
		email = f"address-update-{self.suffix}@example.test"
		order, _mapping = self._make_shopify_order(
			account,
			customer_email=email,
			customer_phone_number="+20111111111",
			shipping_address={"address1": "Original Street", "city": "Cairo", "country": "Egypt"},
		)
		first = sync_sales_order(order.name)
		self.assertTrue(first["ok"], first.get("error"))
		address_name = frappe.db.get_value("Sales Order", first["sales_order"], "shipping_address_name")
		first_address = frappe.get_doc("Address", address_name)
		self.assertEqual(first_address.email_id, email)
		self.assertEqual(first_address.phone, "+20111111111")

		order_doc = frappe.get_doc("Marketplace Order", order.name)
		order_doc.shipping_address = None
		order_doc.billing_address = {
			"address1": "Updated Billing Road",
			"city": "Giza",
			"country": "Egypt",
		}
		order_doc.customer_phone_number = "+20122222222"
		order_doc.save(ignore_permissions=True)
		second = sync_sales_order(order.name)

		self.assertTrue(second["ok"], second.get("error"))
		self.assertEqual(second["sales_order"], first["sales_order"])
		self.assertTrue(second["updated"])
		address = frappe.get_doc("Address", address_name)
		self.assertEqual(address.address_line1, "Updated Billing Road")
		self.assertEqual(address.address_type, "Billing")
		self.assertEqual(address.email_id, email)
		self.assertEqual(address.phone, "+20122222222")
		sales_order = frappe.get_doc("Sales Order", second["sales_order"])
		self.assertEqual(sales_order.shipping_address_name, address_name)
		self.assertEqual(sales_order.customer_address, address_name)
		buyer_address_names = frappe.get_all(
			"Dynamic Link",
			filters={
				"parenttype": "Address",
				"parentfield": "links",
				"link_doctype": "Customer",
				"link_name": sales_order.customer,
			},
			pluck="parent",
		)
		dedicated_addresses = [
			name
			for name in buyer_address_names
			if frappe.db.get_value("Address", name, "address_title").startswith(
				"Shopify Marketplace Address-"
			)
		]
		self.assertEqual(dedicated_addresses, [address_name])
		third = sync_sales_order(order.name)
		self.assertTrue(third["ok"], third.get("error"))
		self.assertFalse(third["updated"])

	def test_shopify_customers_get_distinct_idempotent_dedicated_addresses(self):
		account = self._make_shopify_account()
		orders = [
			self._make_shopify_order(
				account,
				customer_email=f"distinct-address-{index}-{self.suffix}@example.test",
				shipping_address={
					"address1": f"{index} Marketplace Road",
					"city": "Cairo",
					"country": "Egypt",
				},
			)[0]
			for index in (1, 2)
		]
		first_pass = [sync_sales_order(order.name) for order in orders]
		self.assertTrue(all(result["ok"] for result in first_pass), first_pass)
		addresses = [
			frappe.db.get_value("Sales Order", result["sales_order"], "shipping_address_name")
			for result in first_pass
		]
		self.assertTrue(all(addresses))
		self.assertNotEqual(addresses[0], addresses[1])
		titles = [frappe.db.get_value("Address", name, "address_title") for name in addresses]
		self.assertTrue(all(title.startswith("Shopify Marketplace Address-") for title in titles))
		self.assertNotEqual(titles[0], titles[1])

		for order, result, address_name in zip(orders, first_pass, addresses, strict=True):
			customer = frappe.db.get_value("Sales Order", result["sales_order"], "customer")
			self.assertTrue(
				frappe.db.exists(
					"Dynamic Link",
					{
						"parent": address_name,
						"parenttype": "Address",
						"parentfield": "links",
						"link_doctype": "Customer",
						"link_name": customer,
					},
				)
			)
			reprocessed = sync_sales_order(order.name)
			self.assertTrue(reprocessed["ok"], reprocessed.get("error"))
			self.assertEqual(reprocessed["sales_order"], result["sales_order"])
			self.assertFalse(reprocessed["updated"])
			self.assertEqual(
				frappe.db.get_value("Sales Order", reprocessed["sales_order"], "shipping_address_name"),
				address_name,
			)

	def test_shopify_missing_address_data_syncs_with_blank_address_fields(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(
			account, customer_email=f"no-address-{self.suffix}@example.test"
		)

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])
		self.assertFalse(sales_order.shipping_address_name)
		self.assertFalse(sales_order.customer_address)

	def test_shopify_unknown_country_defaults_to_egypt_without_blocking_sync(self):
		account = self._make_shopify_account()
		email = f"unknown-country-{self.suffix}@example.test"
		order, _mapping = self._make_shopify_order(
			account,
			customer_email=email,
			shipping_address={"address1": "12 Quiet Road", "city": "Cairo", "country": "Not a Country"},
		)
		self.assertFalse(order.customer_phone_number)

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])
		self.assertTrue(sales_order.shipping_address_name)
		address = frappe.get_doc("Address", sales_order.shipping_address_name)
		self.assertEqual(address.country, "Egypt")
		self.assertEqual(address.email_id, email)
		self.assertFalse(address.phone)

	def test_shopify_does_not_repurpose_unrelated_customer_address(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(
			account,
			customer_email=f"preserve-address-{self.suffix}@example.test",
			shipping_address={"address1": "Marketplace Road", "city": "Cairo", "country": "Egypt"},
		)
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"Existing Buyer {self.suffix}",
				"customer_type": "Individual",
				"email_id": order.customer_email,
			}
		).insert()
		unrelated = frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": f"User Address {self.suffix}",
				"address_type": "Billing",
				"address_line1": "User-maintained Street",
				"city": "Cairo",
				"links": [{"link_doctype": "Customer", "link_name": customer.name}],
			}
		).insert()
		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(frappe.db.get_value("Address", unrelated.name, "address_line1"), "User-maintained Street")
		self.assertNotEqual(
			frappe.db.get_value("Sales Order", result["sales_order"], "shipping_address_name"), unrelated.name
		)

	def test_blank_platform_is_treated_as_talabat_not_blocked(self):
		"""A pre-#108 order whose platform backfill has not run yet must not
		suddenly lose the ability to sync - every existing Marketplace Order
		today really is a Talabat order."""
		order = self._make_order()
		frappe.db.set_value("Marketplace Order", order.name, "platform", "", update_modified=False)
		frappe.clear_document_cache("Marketplace Order", order.name)

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
