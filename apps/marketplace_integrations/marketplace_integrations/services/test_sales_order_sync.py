# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, cstr, flt, getdate, now_datetime, nowdate

from marketplace_integrations.patches.v0_0 import backfill_last_synced_snapshot as backfill
from marketplace_integrations.services.marketplace_attention import (
	NEXT_RETRY,
	SYNC_ERROR,
	UNMAPPED_ITEMS,
	get_attention_items,
)
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.services.sales_order_reconciliation import (
	SalesOrderLinesMismatch,
	read_erp_state,
)
from marketplace_integrations.services.sales_order_sync import (
	SyncBlocker,
	_build_sales_order_lines,
	_fill_buyer_phone,
	_validate_platform_supported,
	build_sales_order_snapshot,
	get_current_sync_status,
	snapshot_hash,
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

	def _make_shopify_account(self, shipping_account=None):
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
					"erpnext_shipping_account": shipping_account,
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
		payment_transactions=None,
		shipping_total=0,
		order_total=None,
		total_taxes=0,
		duties_total=0,
	):
		if order_total is None:
			order_total = flt(transaction_amount) + flt(shipping_total)
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
				"payment_transactions": payment_transactions,
				"shipping_total": shipping_total,
				"order_total": order_total,
				"total_taxes": total_taxes,
				"duties_total": duties_total,
				"latest_payload": {"updatedAt": "2026-09-28T10:00:00Z"},
				"items": [
					{
						"external_item_id": f"gid://shopify/LineItem/{self.suffix}",
						"external_variant_id": variant_gid,
						"current_quantity": current_quantity,
						# #178: both fulfillment quantities read from Shopify, nothing fulfilled
						# yet; without them the lifecycle stage is unknown and blocks changes.
						"unfulfilled_quantity": current_quantity,
						"non_fulfillable_quantity": 0,
						"fulfillment_quantities_known": 1,
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

	def _make_noon_account(self):
		"""#146: a Noon Marketplace Account for Sales Order sync tests.
		Mirrors integrations/noon/test_orders.py's own ``_make_noon_account()``
		shape (dummy PEM, matching ``external_account_id``/project_code),
		plus the erpnext_company/erpnext_selling_price_list/warehouse fields
		every other platform's account fixture here already sets."""
		unique_hash = frappe.generate_hash(length=8)
		connection = frappe.get_doc(
			{
				"doctype": "Noon Connection",
				"connection_name": f"Noon SO Sync {self.suffix}-{unique_hash}",
				"project_code": f"noon-so-sync-{self.suffix}-{unique_hash}",
				"key_id": "test-key-id",
				"private_key": "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
				"environment": "Static Sandbox",
				"enabled": 1,
			}
		).insert()
		with patch("marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"):
			account = frappe.get_doc(
				{
					"doctype": "Marketplace Account",
					"account_name": f"Noon SO Sync Account {self.suffix}-{unique_hash}",
					"platform": "Noon",
					"external_account_id": f"noon-so-sync-{self.suffix}-{unique_hash}",
					"connection_doctype": "Noon Connection",
					"connection": connection.name,
					"noon_warehouse_code": "NOON-WH-SO-SYNC",
					"noon_country_code": "ae",
					"erpnext_company": self.company,
					"erpnext_selling_price_list": self.price_list,
					"warehouse": self.warehouse,
				}
			).insert()
		return account

	def _make_noon_order(
		self, account, *, items=None, presentment_currency=None, create_mapping=True, sku="SKU-NOON"
	):
		"""#146 decisions 1-6: build a Marketplace Order directly via
		``upsert_marketplace_order()`` with raw Noon-shaped item rows (not
		through ``order_mapper``/``import_order()`` - those are #145's own
		concern, already covered by integrations/noon/test_orders.py). Each
		row here is written exactly as `_build_noon_sales_order_lines()`
		reads it: ``external_status`` (raw ``CONFIRMED``/``CANCELLED``),
		``integration_status`` (raw ``ACKNOWLEDGED``/``SHIPPED``/
		``OUT_OF_STOCK``/``UNSPECIFIED``), ``current_quantity``,
		``transaction_amount``, ``external_sku``.
		"""
		mapping = None
		if create_mapping:
			mapping = frappe.get_doc(
				{
					"doctype": "Marketplace Product Mapping",
					"marketplace_account": account.name,
					"item": self.item.name,
					"external_reference": sku,
					"sales_uom": "Nos",
				}
			).insert()
		if items is None:
			items = [
				{
					"external_item_id": "sku:SKU-NOON:MP_ITEM_STATUS_CONFIRMED:INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					"external_sku": sku,
					"external_status": "CONFIRMED",
					"integration_status": "ACKNOWLEDGED",
					"current_quantity": 2,
					"transaction_amount": 200.0,
				}
			]
		order = upsert_marketplace_order(
			account.name,
			{
				"external_order_id": f"noon-so-sync-order-{self.suffix}-{frappe.generate_hash(length=4)}",
				"received_on": nowdate(),
				"presentment_currency": presentment_currency,
				"latest_payload": {"fbpi_order_nr": "ORD-NOON"},
				"items": items,
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
				# #178: Talabat lifecycle evidence is read from the status; a blank status
				# is "unknown" and would block any change after sync.
				"external_status": "RECEIVED",
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

	def test_creates_one_submitted_sales_order_with_talabat_transaction_pricing(self):
		order = self._make_order()

		result = sync_sales_order(order.name)
		order.reload()
		self.account.reload()
		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)

		self.assertTrue(result["ok"])
		self.assertEqual(order.erpnext_sync_status, "Synced")
		# #131: a freshly-inserted Sales Order is immediately submitted, never
		# left in Draft.
		self.assertEqual(sales_order.docstatus, 1)
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
		persisted_snapshot = frappe.parse_json(order.last_synced_snapshot)
		self.assertEqual(persisted_snapshot["items"][0]["sales_order_item"], sales_order.items[0].name)

	def test_sets_custom_marketplace_order_and_platform_fields(self):
		"""#135: the resulting Sales Order must carry back-links to its
		Marketplace Order and platform - opening a Sales Order created from a
		Talabat sync should not give zero indication of its origin."""
		order = self._make_order()

		result = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])

		self.assertTrue(result["ok"])
		self.assertEqual(sales_order.custom_marketplace_order, order.name)
		self.assertEqual(sales_order.custom_marketplace_platform, "Talabat")

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

	def test_fulfilled_total_price_change_after_auto_submit_updates_the_submitted_order(self):
		"""#179: a later Talabat-side price change on the auto-submitted Sales
		Order is applied through ERPNext's native submitted-order update."""
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

		self.assertTrue(second["ok"], second.get("error"))
		self.assertEqual(sales_order.docstatus, 1)
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

	def test_changed_fulfilled_quantity_after_auto_submit_updates_the_submitted_order(self):
		"""#179: a later qty/total change is applied to the submitted Sales Order."""
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

		self.assertTrue(second["ok"], second.get("error"))
		self.assertEqual(sales_order.items[0].qty, 3)
		self.assertEqual(sales_order.items[0].rate, 500)
		self.assertEqual(order.erpnext_sync_status, "Synced")

	def test_update_branch_still_reapplies_snapshot_for_a_legacy_draft_sales_order(self):
		"""#131's auto-submit means the existing-Sales-Order UPDATE branch
		(`_apply_snapshot()` + `.save()`, as opposed to `.insert()` +
		`.submit()`) is only reachable today for a Sales Order that predates
		this feature and was never submitted. Simulated here by forcing the
		freshly-auto-submitted Sales Order's docstatus back to Draft via a
		direct db write - the update branch itself is unchanged and still
		needs coverage."""
		order = self._make_order()
		first = sync_sales_order(order.name)
		frappe.db.set_value("Sales Order", first["sales_order"], "docstatus", 0, update_modified=False)
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

	def test_submitted_sales_order_is_updated_by_sync(self):
		# #131: the first sync already auto-submits the Sales Order - no
		# manual .submit() call needed (and calling it again here would raise).
		order = self._make_order()
		first = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", first["sales_order"])
		self.assertEqual(sales_order.docstatus, 1)
		order.reload()
		order.items[0].fulfilled_quantity = 3
		order.items[0].fulfilled_total_price = 1500
		order.save()

		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["reconciliation"]["outcome"], "UPDATE_SUBMITTED_ORDER")
		self.assertEqual(result["executed_action"], "UPDATE_SUBMITTED_ORDER")
		self.assertIn("was updated", result["executed_message"])
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(frappe.db.get_value("Sales Order Item", {"parent": sales_order.name}, "qty"), 3)

	def _set_first_item_fields(self, order, **fields):
		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
		for field, value in fields.items():
			frappe.db.set_value("Sales Order Item", sales_order.items[0].name, field, value)

	def _sync_and_set_item_fields(self, order, **fields):
		first = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", first["sales_order"])
		for field, value in fields.items():
			frappe.db.set_value("Sales Order Item", sales_order.items[0].name, field, value)
		order.reload()
		return sales_order

	def test_billed_and_delivered_unchanged_submitted_order_needs_no_action(self):
		order = self._make_order()
		self._sync_and_set_item_fields(order, billed_amt=999, delivered_qty=2)

		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["ok"])
		self.assertEqual(result["reconciliation"]["outcome"], "NO_ACTION")
		self.assertNotIn("executed_action", result)
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertFalse(order.last_sync_error)

	def test_talabat_cancelled_before_execution_cancels_the_sales_order(self):
		order = self._make_order()
		sales_order = self._sync_and_set_item_fields(order)
		order.external_status = "CANCELLED"
		order.save()
		baseline = (order.last_synced_hash, order.last_synced_snapshot)

		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["reconciliation"]["outcome"], "CANCEL_ORDER")
		self.assertEqual(result["executed_action"], "CANCEL_ORDER")
		self.assertIn(sales_order.name, result["executed_message"])
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order.name, "docstatus"), 2)
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertFalse(order.last_sync_error)
		self.assertEqual((order.last_synced_hash, order.last_synced_snapshot), baseline)
		log = frappe.get_doc("Marketplace Sync Log", {"marketplace_order": order.name, "status": "Success"})
		self.assertEqual(frappe.parse_json(log.request_data)["action"], "CANCEL_ORDER")
		self.assertEqual(sync_sales_order(order.name)["reconciliation"]["outcome"], "NO_ACTION")

	def test_cancelled_and_closed_order_converges_and_stays_synced(self):
		order = self._make_order()
		sales_order = self._sync_and_set_item_fields(order, delivered_qty=2)
		frappe.db.set_value("Sales Order", sales_order.name, "status", "Closed", update_modified=False)
		order.external_status = "CANCELLED"
		order.save()
		order.reload()

		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertFalse(order.last_sync_error)
		self.assertEqual(get_current_sync_status(order.name), "Synced")

		order.customer_phone_number = "another save"
		order.save()
		order.reload()
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(sync_sales_order(order.name)["reconciliation"]["outcome"], "NO_ACTION")

	# --- #180 slice 2: reopen executor ------------------------------------------

	def _closed_cancelled_order(self):
		order = self._make_order()
		first = sync_sales_order(order.name)
		frappe.db.set_value("Sales Order", first["sales_order"], "status", "Closed", update_modified=False)
		order.reload()
		order.external_status = "CANCELLED"
		order.save()
		order.reload()
		return order, first["sales_order"]

	def _reopen_log_actions(self, order):
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name},
			fields=["request_data", "creation", "name"],
			order_by="creation asc, name asc",
		)
		return [frappe.parse_json(log.request_data).get("action") for log in logs]

	def _reopen_comments(self, sales_order_name):
		return frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": "Sales Order",
				"reference_name": sales_order_name,
				"content": ["like", "%Reopened by marketplace reconciliation%"],
			},
			pluck="content",
		)

	def test_closed_undelivered_cancelled_order_is_reopened_and_cancelled(self):
		order, sales_order = self._closed_cancelled_order()
		baseline = (order.last_synced_hash, order.last_synced_snapshot)

		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["executed_action"], "CANCEL_ORDER")
		self.assertTrue(result["executed"]["reopened"])
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "docstatus"), 2)
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual((order.last_synced_hash, order.last_synced_snapshot), baseline)
		comments = self._reopen_comments(sales_order)
		self.assertEqual(len(comments), 1)
		self.assertIn("unknown", comments[0])
		self.assertEqual(self._reopen_log_actions(order), [None, "Reopen", "CANCEL_ORDER"])
		self.assertEqual(sync_sales_order(order.name)["reconciliation"]["outcome"], "NO_ACTION")

	def test_closed_undelivered_changed_order_is_reopened_and_updated(self):
		order = self._synced_talabat_order()
		frappe.db.set_value(
			"Sales Order", order.erpnext_sales_order, "status", "Closed", update_modified=False
		)
		self._change_talabat_line(order, 3, 1500)
		baseline = self._baseline(order)

		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["executed_action"], "UPDATE_SUBMITTED_ORDER")
		self.assertEqual(
			frappe.db.get_value("Sales Order Item", {"parent": order.erpnext_sales_order}, "qty"), 3
		)
		self.assertNotEqual(frappe.db.get_value("Sales Order", order.erpnext_sales_order, "status"), "Closed")
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertNotEqual((order.last_synced_hash, order.last_synced_snapshot), baseline)
		self.assertEqual(self._reopen_log_actions(order), [None, "Reopen", "UPDATE_SUBMITTED_ORDER"])
		self.assertEqual(len(self._reopen_comments(order.erpnext_sales_order)), 1)
		self.assertEqual(sync_sales_order(order.name)["reconciliation"]["outcome"], "NO_ACTION")

	def test_failed_reopen_rolls_back_and_leaves_the_order_closed(self):
		order, sales_order = self._closed_cancelled_order()
		baseline = (order.last_synced_hash, order.last_synced_snapshot)
		real_update_status = SalesOrder.update_status

		def failing(doc, status):
			if status == "Draft":
				# Writes before failing, to prove the savepoint restores them.
				doc.add_comment("Comment", "Reopened by marketplace reconciliation (probe)")
				frappe.throw("Credit limit crossed for customer")
			return real_update_status(doc, status)

		with patch.object(SalesOrder, "update_status", failing):
			result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(order.erpnext_sync_status, "Sync Error")
		self.assertIn("Credit limit", order.last_sync_error)
		self.assertIn("Reopen and cancel failed; the Sales Order was left Closed.", order.last_sync_error)
		self.assertEqual((order.last_synced_hash, order.last_synced_snapshot), baseline)
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "status"), "Closed")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "docstatus"), 1)
		self.assertEqual(self._reopen_comments(sales_order), [])
		self.assertNotIn("Reopen", self._reopen_log_actions(order))
		self.assertTrue(
			frappe.db.exists("Marketplace Sync Log", {"marketplace_order": order.name, "status": "Invalid"})
		)

	def test_reopen_without_submit_permission_is_a_sync_error_and_stays_closed(self):
		order, sales_order = self._closed_cancelled_order()
		baseline = (order.last_synced_hash, order.last_synced_snapshot)
		self.addCleanup(frappe.set_user, frappe.session.user)
		frappe.set_user(self._low_privilege_user())

		result = sync_sales_order(order.name)
		frappe.set_user("Administrator")
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(order.erpnext_sync_status, "Sync Error")
		self.assertEqual((order.last_synced_hash, order.last_synced_snapshot), baseline)
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "status"), "Closed")
		self.assertEqual(self._reopen_comments(sales_order), [])

	def test_reopen_rollback_failure_needs_attention_and_asks_to_verify_the_state(self):
		order, _sales_order = self._closed_cancelled_order()

		with (
			patch.object(SalesOrder, "update_status", side_effect=frappe.ValidationError("rejected")),
			patch(
				"marketplace_integrations.services.sales_order_sync._rollback_reconcile_savepoint",
				return_value=False,
			),
		):
			result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertIn("already rolled back; verify the Sales Order state", order.last_sync_error)

	def test_failure_after_a_successful_reopen_rolls_the_reopen_back(self):
		order, sales_order = self._closed_cancelled_order()
		baseline = (order.last_synced_hash, order.last_synced_snapshot)

		with patch.object(SalesOrder, "cancel", side_effect=frappe.ValidationError("cancel rejected")):
			result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(order.erpnext_sync_status, "Sync Error")
		self.assertIn("Reopen and cancel failed; the Sales Order was left Closed.", order.last_sync_error)
		self.assertIn("cancel rejected", order.last_sync_error)
		self.assertEqual((order.last_synced_hash, order.last_synced_snapshot), baseline)
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "status"), "Closed")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "docstatus"), 1)
		self.assertEqual(self._reopen_comments(sales_order), [])
		self.assertNotIn("Reopen", self._reopen_log_actions(order))
		self.assertTrue(
			frappe.db.exists("Marketplace Sync Log", {"marketplace_order": order.name, "status": "Invalid"})
		)

	def test_manual_close_with_an_unchanged_snapshot_needs_attention(self):
		order = self._synced_talabat_order()
		sales_order = order.erpnext_sales_order
		baseline = self._baseline(order)
		frappe.db.set_value("Sales Order", sales_order, "status", "Closed", update_modified=False)

		status = get_current_sync_status(order.name)
		result = sync_sales_order(order.name)
		order.reload()

		self.assertEqual(status, "Modified After Sync")
		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertNotIn("reopen", result["reconciliation"])
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertIn("still expects fulfillment", order.last_sync_error)
		self.assertEqual(self._baseline(order), baseline)
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "status"), "Closed")

	def test_closed_order_with_delivery_is_never_reopened(self):
		order = self._make_order()
		sales_order = self._sync_and_set_item_fields(order, delivered_qty=1)
		frappe.db.set_value("Sales Order", sales_order.name, "status", "Closed", update_modified=False)
		order.external_status = "CANCELLED"
		order.save()

		with patch.object(SalesOrder, "update_status") as update_status:
			result = sync_sales_order(order.name)

		update_status.assert_not_called()
		self.assertNotIn("executed_action", result)
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order.name, "status"), "Closed")

	def test_closed_order_with_advance_payment_is_not_reopened(self):
		order, sales_order = self._closed_cancelled_order()
		frappe.db.set_value("Sales Order", sales_order, "advance_paid", 50)

		with patch.object(SalesOrder, "update_status") as update_status:
			result = sync_sales_order(order.name)

		update_status.assert_not_called()
		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "status"), "Closed")

	def test_status_checks_for_a_reopen_plan_are_plan_only(self):
		order, sales_order = self._closed_cancelled_order()

		with (
			patch("marketplace_integrations.services.sales_order_sync.execute") as execute,
			patch.object(SalesOrder, "update_status") as update_status,
		):
			status = get_current_sync_status(order.name)
			order.customer_phone_number = "another save"
			order.save()
		order.reload()

		execute.assert_not_called()
		update_status.assert_not_called()
		self.assertEqual(status, "Modified After Sync")
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertIn("reopened", order.last_sync_error)
		self.assertNotIn("not yet enabled", order.last_sync_error)
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "status"), "Closed")

	# --- #181: echo of an outbound platform cancel ------------------------------

	def _closed_order_with_outbound_cancel(self):
		"""A Closed, undelivered Sales Order whose Talabat order ERPNext cancelled (marker set)
		and that Talabat now reports CANCELLED - the echo."""
		order = self._make_order()
		first = sync_sales_order(order.name)
		frappe.db.set_value("Sales Order", first["sales_order"], "status", "Closed", update_modified=False)
		frappe.db.set_value(
			"Marketplace Order", order.name, "outbound_cancelled_on", now_datetime(), update_modified=False
		)
		order.reload()
		order.external_status = "CANCELLED"
		order.save()
		order.reload()
		return order, first["sales_order"]

	def test_outbound_cancel_echo_keeps_the_closed_order_closed_in_the_status_path(self):
		order, sales_order = self._closed_order_with_outbound_cancel()

		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertFalse(order.last_sync_error)
		self.assertEqual(get_current_sync_status(order.name), "Synced")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "status"), "Closed")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "docstatus"), 1)

	def test_outbound_cancel_echo_is_no_action_in_the_sync_path_and_nothing_is_reopened(self):
		order, sales_order = self._closed_order_with_outbound_cancel()

		with patch.object(SalesOrder, "update_status") as update_status:
			result = sync_sales_order(order.name)

		update_status.assert_not_called()
		self.assertEqual(result["reconciliation"]["outcome"], "NO_ACTION")
		self.assertNotIn("executed_action", result)
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "status"), "Closed")
		self.assertEqual(self._reopen_comments(sales_order), [])

	def test_outbound_cancel_echo_is_still_no_action_after_the_sync_log_rows_are_deleted(self):
		order, sales_order = self._closed_order_with_outbound_cancel()
		frappe.db.delete("Marketplace Sync Log", {"marketplace_order": order.name})

		with patch.object(SalesOrder, "update_status") as update_status:
			result = sync_sales_order(order.name)
		order.reload()
		order.customer_phone_number = "another save"
		order.save()

		update_status.assert_not_called()
		self.assertEqual(result["reconciliation"]["outcome"], "NO_ACTION")
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "status"), "Closed")

	def test_marker_set_before_the_returned_order_is_saved_prevents_the_reopen(self):
		"""The order of operations #181 relies on: marker first, then the platform's CANCELLED
		order is saved (the save re-plans it)."""
		order = self._make_order()
		first = sync_sales_order(order.name)
		frappe.db.set_value("Sales Order", first["sales_order"], "status", "Closed", update_modified=False)
		order.reload()
		self.assertEqual(order.erpnext_sync_status, "Synced")
		frappe.db.set_value(
			"Marketplace Order", order.name, "outbound_cancelled_on", now_datetime(), update_modified=False
		)
		cancelled = frappe._dict(
			external_order_id=order.external_order_id,
			latest_payload={"order_id": "external-order"},
			external_status="CANCELLED",
			items=[],
		)

		upsert_marketplace_order(self.account.name, cancelled)
		order.reload()

		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(frappe.db.get_value("Sales Order", first["sales_order"], "status"), "Closed")

	def test_cancelled_sales_order_with_an_active_talabat_order_gets_the_platform_instruction(self):
		order = self._make_order()  # external_status RECEIVED
		first = sync_sales_order(order.name)
		frappe.db.set_value("Sales Order", first["sales_order"], "status", "Cancelled")

		result = sync_sales_order(order.name)
		order.reload()

		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertIn("no longer an active submitted document", order.last_sync_error)
		self.assertIn("Cancel the order on Talabat", order.last_sync_error)

	def test_closed_delivered_sales_order_with_a_delivered_talabat_order_says_it_cannot_be_cancelled(self):
		order = self._make_order()
		sales_order = self._sync_and_set_item_fields(order, delivered_qty=1)
		frappe.db.set_value("Sales Order", sales_order.name, "status", "Closed", update_modified=False)
		order.external_status = "DELIVERED"
		order.save()

		result = sync_sales_order(order.name)
		order.reload()

		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertIn("no longer an active submitted document", order.last_sync_error)
		self.assertIn("can no longer be cancelled from ERPNext", order.last_sync_error)
		self.assertNotIn("executed_action", result)

	def test_shopify_closed_order_is_reopened_and_cancelled(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)
		first = sync_sales_order(order.name)
		frappe.db.set_value("Sales Order", first["sales_order"], "status", "Closed", update_modified=False)
		order.reload()
		order.cancelled_at = nowdate()
		order.save()

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["executed_action"], "CANCEL_ORDER")
		self.assertEqual(frappe.db.get_value("Sales Order", first["sales_order"], "docstatus"), 2)
		self.assertEqual(self._reopen_log_actions(order), [None, "Reopen", "CANCEL_ORDER"])

	def test_noon_closed_order_is_reopened_and_cancelled(self):
		account = self._make_noon_account()
		order, _mapping = self._make_noon_order(account)
		first = sync_sales_order(order.name)
		frappe.db.set_value("Sales Order", first["sales_order"], "status", "Closed", update_modified=False)
		order.reload()
		order.items[0].external_status = "CANCELLED"
		order.items[0].integration_status = "OUT_OF_STOCK"
		order.latest_payload = frappe.as_json(
			{
				"items": [
					self._noon_payload_unit(
						"1", "MP_ITEM_STATUS_CANCELLED", "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"
					),
					self._noon_payload_unit(
						"2", "MP_ITEM_STATUS_CANCELLED", "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"
					),
				]
			}
		)
		order.save()

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["executed_action"], "CANCEL_ORDER")
		self.assertEqual(frappe.db.get_value("Sales Order", first["sales_order"], "docstatus"), 2)
		self.assertEqual(self._reopen_log_actions(order), [None, "Reopen", "CANCEL_ORDER"])

	def test_closed_partly_delivered_cancelled_order_converges_to_synced(self):
		# #179: Closed after delivery is what CLOSE_REMAINDER leaves, so it is converged.
		order = self._make_order()
		sales_order = self._sync_and_set_item_fields(order, delivered_qty=1)
		frappe.db.set_value("Sales Order", sales_order.name, "status", "Closed", update_modified=False)
		order.external_status = "CANCELLED"
		order.save()
		order.reload()

		self.assertEqual(order.erpnext_sync_status, "Synced")

	def test_lines_mismatch_on_fresh_insert_blocks_with_specific_message_and_leaves_no_half_link(self):
		order = self._make_order()
		before = frappe.db.count("Sales Order", {"custom_marketplace_order": order.name})

		with patch(
			"marketplace_integrations.services.sales_order_sync.snapshot_with_sales_order_item_names",
			side_effect=SalesOrderLinesMismatch("specific mismatch message"),
		):
			result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertIn("specific mismatch message", result["error"])
		self.assertEqual(order.erpnext_sync_status, "Sync Error")
		self.assertFalse(order.erpnext_sales_order)
		self.assertFalse(order.last_synced_hash)
		self.assertFalse(order.last_synced_snapshot)
		self.assertEqual(frappe.db.count("Sales Order", {"custom_marketplace_order": order.name}), before)

	def test_lines_mismatch_on_draft_update_blocks_with_specific_message_and_keeps_baseline(self):
		order = self._make_order()
		first = sync_sales_order(order.name)
		frappe.db.set_value("Sales Order", first["sales_order"], "docstatus", 0, update_modified=False)
		order.reload()
		baseline = (order.last_synced_hash, order.last_synced_snapshot)
		order.items[0].fulfilled_quantity = 3
		order.items[0].fulfilled_total_price = 1500
		order.save()

		with patch(
			"marketplace_integrations.services.sales_order_sync.snapshot_with_sales_order_item_names",
			side_effect=SalesOrderLinesMismatch("specific mismatch message"),
		):
			result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertIn("specific mismatch message", result["error"])
		self.assertEqual((order.last_synced_hash, order.last_synced_snapshot), baseline)

	def test_talabat_cancelled_with_outstanding_remainder_closes_the_remainder(self):
		order = self._make_order()
		sales_order = self._sync_and_set_item_fields(order, delivered_qty=1)
		order.external_status = "CANCELLED"
		order.save()
		baseline = (order.last_synced_hash, order.last_synced_snapshot)
		per_delivered = frappe.db.get_value("Sales Order", sales_order.name, "per_delivered")

		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["reconciliation"]["outcome"], "CLOSE_REMAINDER")
		self.assertEqual(result["executed_action"], "CLOSE_REMAINDER")
		self.assertIn(sales_order.name, result["executed_message"])
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order.name, "status"), "Closed")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order.name, "per_delivered"), per_delivered)
		self.assertEqual((order.last_synced_hash, order.last_synced_snapshot), baseline)
		self.assertEqual(sync_sales_order(order.name)["reconciliation"]["outcome"], "NO_ACTION")

	def test_talabat_cancelled_after_full_delivery_needs_compensating_transaction(self):
		order = self._make_order()
		self._sync_and_set_item_fields(order, delivered_qty=2)
		order.external_status = "CANCELLED"
		order.save()

		result = sync_sales_order(order.name)

		self.assertEqual(result["reconciliation"]["outcome"], "UNSUPPORTED_COMPENSATING_TRANSACTION")
		self.assertIn("compensating transaction", result["error"])

	def _assert_cancelled_talabat_order_was_blocked(self, order, result):
		order.reload()
		self.assertFalse(result["ok"])
		self.assertEqual(order.erpnext_sync_status, "Sync Error")
		self.assertEqual(order.last_sync_error, "Cancelled Talabat orders cannot be synchronized.")
		self.assertFalse(order.erpnext_sales_order)
		self.assertFalse(frappe.db.exists("Sales Order", {"custom_marketplace_order": order.name}))
		logs = frappe.get_all(
			"Marketplace Sync Log", filters={"marketplace_order": order.name}, fields=["status"]
		)
		self.assertEqual([log.status for log in logs], ["Invalid"])

	def test_never_synced_cancelled_talabat_order_does_not_create_a_sales_order(self):
		order = self._make_order()
		order.external_status = "CANCELLED"
		order.save()

		result = sync_sales_order(order.name)

		self._assert_cancelled_talabat_order_was_blocked(order, result)

	def test_never_synced_cancelled_talabat_order_is_blocked_through_the_debounced_path(self):
		from marketplace_integrations.services import sales_order_sync_debounce as debounce

		order = self._make_order()
		order.external_status = "CANCELLED"
		order.save()
		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook", delay_seconds=45)
		expected = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")

		debounce._run_debounced_sync(order.name, expected)

		order.reload()
		self.assertEqual(order.erpnext_sync_status, "Sync Error")
		self.assertEqual(order.last_sync_error, "Cancelled Talabat orders cannot be synchronized.")
		self.assertFalse(order.erpnext_sales_order)
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name},
			fields=["status", "trigger_source"],
		)
		self.assertEqual([(log.status, log.trigger_source) for log in logs], [("Invalid", "Talabat Webhook")])

	def _converge_erp_with_changed_marketplace_order(self, order):
		"""Marketplace qty 2 -> 3 (same rate), then ERPNext is edited to match and is billed/delivered."""
		sales_order = self._sync_and_set_item_fields(order)
		order.items[0].fulfilled_quantity = 3
		order.items[0].fulfilled_total_price = 1498.5
		order.items[0].fulfilled_unit_price = 499.5
		order.save()
		frappe.db.set_value(
			"Sales Order Item",
			sales_order.items[0].name,
			{"qty": 3, "amount": 1498.5, "delivered_qty": 3, "billed_amt": 1000},
		)
		return sales_order

	def test_save_converges_when_erp_already_matches_changed_marketplace_state(self):
		order = self._make_order()
		self._converge_erp_with_changed_marketplace_order(order)
		order.reload()
		order.customer_phone_number = "trigger a before_save status check"
		order.save()
		order.reload()

		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertFalse(order.last_sync_error)
		self.assertEqual(order.last_synced_hash, snapshot_hash(build_sales_order_snapshot(order)))
		self.assertEqual(frappe.parse_json(order.last_synced_snapshot)["items"][0]["qty"], 3)
		self.assertEqual(get_current_sync_status(order.name), "Synced")

	def test_explicit_sync_and_status_check_converge_and_advance_the_baseline(self):
		order = self._make_order()
		sales_order = self._sync_and_set_item_fields(order)
		order.reload()
		first_hash = order.last_synced_hash
		order.items[0].fulfilled_quantity = 3
		order.items[0].fulfilled_total_price = 1498.5
		order.items[0].fulfilled_unit_price = 499.5
		order.save()
		order.reload()
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		frappe.db.set_value(
			"Sales Order Item",
			sales_order.items[0].name,
			{"qty": 3, "amount": 1498.5, "delivered_qty": 3, "billed_amt": 1000},
		)

		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["ok"])
		self.assertEqual(result["reconciliation"]["outcome"], "NO_ACTION")
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertNotEqual(order.last_synced_hash, first_hash)
		self.assertEqual(get_current_sync_status(order.name), "Synced")

	def test_refund_present_at_first_sync_is_not_new_evidence_but_a_later_refund_is(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)
		order.refunds = frappe.as_json([{"id": f"refund-a-{self.suffix}"}])
		order.save()
		sync_sales_order(order.name)
		order.reload()
		self.assertIn(f"refund-a-{self.suffix}", frappe.parse_json(order.last_synced_snapshot)["refund_ids"])
		self.assertEqual(order.last_synced_hash, snapshot_hash(build_sales_order_snapshot(order)))

		result = sync_sales_order(order.name)
		self.assertTrue(result["ok"])
		self.assertEqual(result["reconciliation"]["outcome"], "NO_ACTION")

		order.reload()
		self._set_first_item_fields(order, delivered_qty=1)
		order.refunds = frappe.as_json([{"id": f"refund-a-{self.suffix}"}, {"id": f"refund-b-{self.suffix}"}])
		order.save()
		result = sync_sales_order(order.name)
		self.assertEqual(result["reconciliation"]["outcome"], "UNSUPPORTED_COMPENSATING_TRANSACTION")

	def test_legacy_snapshot_without_refund_ids_treats_every_refund_as_new(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)
		sync_sales_order(order.name)
		order.reload()
		legacy = frappe.parse_json(order.last_synced_snapshot)
		legacy.pop("refund_ids")
		frappe.db.set_value("Marketplace Order", order.name, "last_synced_snapshot", frappe.as_json(legacy))
		order.reload()
		self._set_first_item_fields(order, delivered_qty=1)
		order.refunds = frappe.as_json([{"id": f"refund-{self.suffix}"}])
		order.save()

		result = sync_sales_order(order.name)

		self.assertEqual(result["reconciliation"]["outcome"], "UNSUPPORTED_COMPENSATING_TRANSACTION")

	def test_submitted_order_without_stored_snapshot_needs_attention(self):
		order = self._make_order()
		self._sync_and_set_item_fields(order)
		frappe.db.set_value("Marketplace Order", order.name, "last_synced_snapshot", None)

		result = sync_sales_order(order.name)
		order.reload()

		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertIn("No reconciled snapshot baseline", order.last_sync_error)
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")

	def test_backfill_patch_writes_matching_baselines_but_only_relabels_submitted_orders(self):
		submitted = self._make_order()
		draft = self._make_order()
		errored = self._make_order()
		diverged = self._make_order()
		for order in (submitted, draft, errored, diverged):
			sync_sales_order(order.name)
		draft.reload()
		frappe.db.set_value("Sales Order", draft.erpnext_sales_order, "docstatus", 0)
		frappe.db.set_value(
			"Marketplace Order",
			errored.name,
			{"erpnext_sync_status": "Sync Error", "last_sync_error": "kept message"},
		)
		frappe.db.set_value("Marketplace Order", diverged.name, "last_synced_hash", "0" * 64)
		for order in (submitted, draft, errored, diverged):
			frappe.db.set_value("Marketplace Order", order.name, "last_synced_snapshot", None)

		names = [order.name for order in (submitted, draft, errored, diverged)]
		# Scoped to this test's orders: execute() would walk every order on the site.
		backfill.backfill_orders(backfill._candidate_orders(names))
		backfill.backfill_orders(backfill._candidate_orders(names))  # idempotent

		def values(order):
			return frappe.db.get_value(
				"Marketplace Order",
				order.name,
				["last_synced_snapshot", "erpnext_sync_status", "last_sync_error"],
				as_dict=True,
			)

		submitted_row = values(submitted)
		self.assertTrue(submitted_row.last_synced_snapshot)
		self.assertEqual(submitted_row.erpnext_sync_status, "Synced")
		# A matching hash persists the baseline whatever the docstatus/status, but
		# draft and Sync Error rows are never relabelled.
		draft_row = values(draft)
		self.assertTrue(draft_row.last_synced_snapshot)
		self.assertEqual(draft_row.erpnext_sync_status, "Synced")
		errored_row = values(errored)
		self.assertTrue(errored_row.last_synced_snapshot)
		self.assertEqual(errored_row.erpnext_sync_status, "Sync Error")
		self.assertEqual(errored_row.last_sync_error, "kept message")
		diverged_row = values(diverged)
		self.assertFalse(diverged_row.last_synced_snapshot)
		self.assertEqual(diverged_row.erpnext_sync_status, "Modified After Sync")
		self.assertEqual(diverged_row.last_sync_error, backfill.NO_BASELINE_MESSAGE)
		frappe.db.set_value("Sales Order", draft.erpnext_sales_order, "docstatus", 1)

	def test_stale_sync_error_is_replaced_when_order_becomes_modified_after_sync(self):
		order = self._make_order()
		sales_order = self._sync_and_set_item_fields(order)
		# Simulate a still-draft Sales Order so the hash-mismatch path is used.
		frappe.db.set_value("Sales Order", sales_order.name, "docstatus", 0)
		order.last_sync_error = "stale error from an earlier attempt"
		order.items[0].fulfilled_quantity = 3
		order.items[0].fulfilled_total_price = 1500
		order.save()
		order.reload()

		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertNotIn("stale error", cstr(order.last_sync_error))
		self.assertTrue(order.last_sync_error)
		frappe.db.set_value("Sales Order", sales_order.name, "docstatus", 1)

	def test_current_status_does_not_hide_submitted_sales_order_erp_drift(self):
		order = self._make_order()
		first = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", first["sales_order"])

		for field, value in (("amount", sales_order.items[0].amount + 1), ("status", "Cancelled")):
			with self.subTest(field=field):
				if field == "amount":
					frappe.db.set_value("Sales Order Item", sales_order.items[0].name, field, value)
				else:
					frappe.db.set_value("Sales Order", sales_order.name, field, value)
				order.reload()

				self.assertEqual(get_current_sync_status(order.name), "Modified After Sync")
				order.reload()
				self.assertIn("ERPNext Sales Order", order.last_sync_error)
				frappe.db.set_value("Sales Order", sales_order.name, "status", "To Deliver and Bill")
				frappe.db.set_value(
					"Sales Order Item", sales_order.items[0].name, "amount", sales_order.items[0].amount
				)

	# -------------------------------------------------------------------
	# #179 slice A: executing CANCEL_ORDER / CLOSE_REMAINDER.
	# -------------------------------------------------------------------

	def _cancelled_talabat_order(self, **item_fields):
		order = self._make_order()
		sales_order = self._sync_and_set_item_fields(order, **item_fields)
		order.external_status = "CANCELLED"
		order.save()
		return order, sales_order

	def _assert_rejected_and_untouched(self, order, sales_order, baseline, result):
		order.reload()
		self.assertFalse(result["ok"])
		self.assertEqual(order.erpnext_sync_status, "Sync Error")
		self.assertTrue(order.last_sync_error)
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order.name, "docstatus"), 1)
		self.assertEqual((order.last_synced_hash, order.last_synced_snapshot), baseline)
		self.assertTrue(
			frappe.db.exists(
				"Marketplace Sync Log", {"marketplace_order": order.name, "status": ["!=", "Success"]}
			)
		)

	def test_cancel_with_a_linked_delivery_note_is_rejected_and_rolled_back(self):
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		order, sales_order = self._cancelled_talabat_order()
		delivery_note = make_delivery_note(sales_order.name)
		delivery_note.insert()
		delivery_note.submit()
		# Hide the delivery from the planner so it still plans CANCEL_ORDER, while the
		# Delivery Note link makes ERPNext refuse the cancel.
		frappe.db.set_value("Sales Order Item", sales_order.items[0].name, "delivered_qty", 0)
		baseline = (order.last_synced_hash, order.last_synced_snapshot)

		result = sync_sales_order(order.name)

		self._assert_rejected_and_untouched(order, sales_order, baseline, result)

	def test_cancel_is_blocked_by_an_advance_payment_without_calling_erpnext(self):
		order, sales_order = self._cancelled_talabat_order()
		frappe.db.set_value("Sales Order", sales_order.name, "advance_paid", 50)

		with patch("marketplace_integrations.services.sales_order_sync.execute") as execute:
			result = sync_sales_order(order.name)
		order.reload()

		execute.assert_not_called()
		self.assertNotIn("executed_action", result)
		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order.name, "docstatus"), 1)

	def test_permission_error_during_execution_becomes_sync_error(self):
		order, sales_order = self._cancelled_talabat_order()
		baseline = (order.last_synced_hash, order.last_synced_snapshot)

		with patch(
			"marketplace_integrations.services.sales_order_sync.execute",
			side_effect=frappe.PermissionError,
		):
			result = sync_sales_order(order.name)

		self._assert_rejected_and_untouched(order, sales_order, baseline, result)
		self.assertIn("permission", result["error"])

	def test_unexpected_error_during_execution_becomes_sync_error_and_is_logged(self):
		order, sales_order = self._cancelled_talabat_order()
		baseline = (order.last_synced_hash, order.last_synced_snapshot)

		with (
			patch(
				"marketplace_integrations.services.sales_order_sync.execute", side_effect=RuntimeError("boom")
			),
			patch("marketplace_integrations.services.sales_order_sync.frappe.log_error") as log_error,
		):
			result = sync_sales_order(order.name)

		log_error.assert_called_once()
		self._assert_rejected_and_untouched(order, sales_order, baseline, result)
		self.assertEqual(
			frappe.db.get_value(
				"Marketplace Sync Log", {"marketplace_order": order.name, "status": "Error"}, "name"
			)
			is not None,
			True,
		)

	def test_baseline_is_written_only_after_a_successful_execution(self):
		order, _sales_order = self._cancelled_talabat_order()
		seen = {}

		def fake_execute(*args, **kwargs):
			seen["status"] = frappe.db.get_value("Marketplace Order", order.name, "erpnext_sync_status")
			raise frappe.ValidationError("nope")

		with patch("marketplace_integrations.services.sales_order_sync.execute", side_effect=fake_execute):
			result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertNotEqual(seen["status"], "Synced")

	def test_no_action_never_calls_execute(self):
		order = self._make_order()
		sync_sales_order(order.name)

		with patch("marketplace_integrations.services.sales_order_sync.execute") as execute:
			result = sync_sales_order(order.name)

		execute.assert_not_called()
		self.assertEqual(result["reconciliation"]["outcome"], "NO_ACTION")

	# --- #179 slice B: UPDATE_SUBMITTED_ORDER execution ---------------------------

	def _synced_talabat_order(self):
		order = self._make_order()
		self.assertTrue(sync_sales_order(order.name)["ok"])
		order.reload()
		return order

	def _change_talabat_line(self, order, qty, total):
		order.reload()
		order.items[0].fulfilled_quantity = qty
		order.items[0].fulfilled_total_price = total
		order.items[0].fulfilled_unit_price = total / qty
		order.save()
		order.reload()

	def _baseline(self, order):
		order.reload()
		return (order.last_synced_hash, order.last_synced_snapshot)

	def _assert_update_executed_and_idempotent(self, order, qty, amount):
		result = sync_sales_order(order.name)
		order.reload()
		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["reconciliation"]["outcome"], "UPDATE_SUBMITTED_ORDER")
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertFalse(order.last_sync_error)
		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
		self.assertEqual(sales_order.docstatus, 1)
		self.assertEqual(sales_order.items[0].qty, qty)
		self.assertEqual(sales_order.items[0].amount, amount)
		baseline_line = frappe.parse_json(order.last_synced_snapshot)["items"][0]
		self.assertEqual(baseline_line["qty"], qty)
		self.assertEqual(baseline_line["sales_order_item"], sales_order.items[0].name)
		self.assertEqual(order.last_synced_hash, snapshot_hash(build_sales_order_snapshot(order)))
		log = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name, "status": "Success"},
			fields=["request_data"],
			order_by="creation desc",
			limit=1,
		)[0]
		self.assertEqual(frappe.parse_json(log.request_data)["action"], "UPDATE_SUBMITTED_ORDER")
		with patch("erpnext.controllers.accounts_controller.update_child_qty_rate") as update:
			replay = sync_sales_order(order.name)
		update.assert_not_called()
		self.assertEqual(replay["reconciliation"]["outcome"], "NO_ACTION")
		return sales_order

	def test_quantity_increase_before_delivery_updates_the_sales_order(self):
		order = self._synced_talabat_order()
		self._change_talabat_line(order, 3, 1500)
		self._assert_update_executed_and_idempotent(order, 3, 1500)

	def test_quantity_decrease_before_delivery_updates_the_sales_order(self):
		order = self._synced_talabat_order()
		self._change_talabat_line(order, 1, 500)
		self._assert_update_executed_and_idempotent(order, 1, 500)

	def test_rate_only_correction_updates_the_sales_order(self):
		order = self._synced_talabat_order()
		self._change_talabat_line(order, 2, 1000)
		sales_order = self._assert_update_executed_and_idempotent(order, 2, 1000)
		self.assertEqual(sales_order.items[0].rate, 500)

	def test_line_refunded_to_zero_is_deleted_and_kept_in_the_baseline_without_a_row(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, transaction_amount=36, current_quantity=2)
		order.append(
			"items",
			{
				"external_item_id": f"gid://shopify/LineItem/second-{self.suffix}",
				"external_variant_id": order.items[0].external_variant_id,
				"current_quantity": 1,
				"unfulfilled_quantity": 1,
				"non_fulfillable_quantity": 0,
				"fulfillment_quantities_known": 1,
				"transaction_amount": 10,
			},
		)
		order.order_total = 46
		order.save()
		self.assertTrue(sync_sales_order(order.name)["ok"])
		order.reload()
		self.assertEqual(len(frappe.get_doc("Sales Order", order.erpnext_sales_order).items), 2)
		first_row = frappe.parse_json(order.last_synced_snapshot)["items"][0]["sales_order_item"]
		order.items[1].current_quantity = 0
		order.items[1].unfulfilled_quantity = 0
		order.items[1].transaction_amount = 0
		order.save()

		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["reconciliation"]["outcome"], "UPDATE_SUBMITTED_ORDER")
		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
		self.assertEqual([row.name for row in sales_order.items], [first_row])
		self.assertEqual(sales_order.items[0].amount, 36)
		lines = frappe.parse_json(order.last_synced_snapshot)["items"]
		self.assertEqual(lines[0]["sales_order_item"], first_row)
		self.assertEqual(lines[1]["qty"], 0)
		self.assertIsNone(lines[1]["sales_order_item"])
		with patch("erpnext.controllers.accounts_controller.update_child_qty_rate") as update:
			replay = sync_sales_order(order.name)
		update.assert_not_called()
		self.assertEqual(replay["reconciliation"]["outcome"], "NO_ACTION")

	def test_change_after_delivery_or_billing_needs_attention_without_calling_erpnext(self):
		for field, value in (("delivered_qty", 1), ("billed_amt", 100)):
			with self.subTest(field=field):
				order = self._synced_talabat_order()
				self._set_first_item_fields(order, **{field: value})
				self._change_talabat_line(order, 3, 1500)
				baseline = self._baseline(order)

				with patch("erpnext.controllers.accounts_controller.update_child_qty_rate") as update:
					result = sync_sales_order(order.name)

				update.assert_not_called()
				self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
				self.assertEqual(self._baseline(order), baseline)
				self.assertEqual(order.erpnext_sync_status, "Modified After Sync")

	def test_reduction_below_delivered_quantity_needs_attention_without_calling_erpnext(self):
		order = self._synced_talabat_order()
		self._set_first_item_fields(order, delivered_qty=2)
		self._change_talabat_line(order, 1, 500)

		with patch("erpnext.controllers.accounts_controller.update_child_qty_rate") as update:
			result = sync_sales_order(order.name)

		update.assert_not_called()
		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertIn("delivered", result["error"])

	def test_erpnext_rejection_of_the_update_is_a_sync_error_and_rolls_back(self):
		order = self._synced_talabat_order()
		self._change_talabat_line(order, 3, 1500)
		baseline = self._baseline(order)

		def reject(*args, **kwargs):
			frappe.db.set_value("Sales Order Item", {"parent": order.erpnext_sales_order}, "qty", 9)
			frappe.throw("Cannot set quantity less than delivered quantity", frappe.ValidationError)

		with patch("erpnext.controllers.accounts_controller.update_child_qty_rate", side_effect=reject):
			result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(order.erpnext_sync_status, "Sync Error")
		self.assertIn("less than delivered", order.last_sync_error)
		self.assertEqual(self._baseline(order), baseline)
		self.assertEqual(
			frappe.db.get_value("Sales Order Item", {"parent": order.erpnext_sales_order}, "qty"), 2
		)
		self.assertTrue(
			frappe.db.exists("Marketplace Sync Log", {"marketplace_order": order.name, "status": "Invalid"})
		)

	def test_amount_drift_after_the_update_rolls_back_and_needs_attention(self):
		from erpnext.controllers.accounts_controller import update_child_qty_rate as real_update

		order = self._synced_talabat_order()
		self._change_talabat_line(order, 3, 1500)
		baseline = self._baseline(order)

		def drifting_update(*args, **kwargs):
			real_update(*args, **kwargs)
			frappe.db.set_value("Sales Order Item", {"parent": order.erpnext_sales_order}, "amount", 1499.99)

		with patch(
			"erpnext.controllers.accounts_controller.update_child_qty_rate", side_effect=drifting_update
		):
			result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertIn("cannot reproduce the marketplace amounts", result["error"])
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertEqual(self._baseline(order), baseline)
		row = frappe.db.get_value(
			"Sales Order Item", {"parent": order.erpnext_sales_order}, ["qty", "amount"], as_dict=True
		)
		self.assertEqual((row.qty, row.amount), (2, 999))

	def test_shopify_refund_reflected_by_the_update_stops_counting_as_evidence(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, transaction_amount=36, current_quantity=2)
		self.assertTrue(sync_sales_order(order.name)["ok"])
		order.reload()
		order.items[0].current_quantity = 1
		order.items[0].unfulfilled_quantity = 1
		order.items[0].transaction_amount = 18
		order.refunds = frappe.as_json([{"id": f"refund-{self.suffix}"}])
		order.save()

		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["reconciliation"]["outcome"], "UPDATE_SUBMITTED_ORDER")
		self.assertEqual(
			frappe.db.get_value("Sales Order Item", {"parent": order.erpnext_sales_order}, "qty"), 1
		)
		self.assertIn(f"refund-{self.suffix}", frappe.parse_json(order.last_synced_snapshot)["refund_ids"])
		# The refund is now reflected: delivering the remaining unit is not compensating.
		self._set_first_item_fields(order, delivered_qty=1)
		self.assertEqual(sync_sales_order(order.name)["reconciliation"]["outcome"], "NO_ACTION")

	def test_noon_quantity_change_updates_the_sales_order(self):
		account = self._make_noon_account()
		order, _mapping = self._make_noon_order(account)
		self.assertTrue(sync_sales_order(order.name)["ok"])
		order.reload()
		order.items[0].current_quantity = 3
		order.items[0].transaction_amount = 300
		order.latest_payload = frappe.as_json(
			{
				"items": [
					self._noon_payload_unit(
						str(number), "MP_ITEM_STATUS_CONFIRMED", "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED"
					)
					for number in (1, 2, 3)
				]
			}
		)
		order.save()

		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["reconciliation"]["outcome"], "UPDATE_SUBMITTED_ORDER")
		row = frappe.db.get_value(
			"Sales Order Item", {"parent": order.erpnext_sales_order}, ["qty", "amount"], as_dict=True
		)
		self.assertEqual((row.qty, row.amount), (3, 300))
		self.assertEqual(sync_sales_order(order.name)["reconciliation"]["outcome"], "NO_ACTION")

	# --- #179 review-fix pass 1 -------------------------------------------------

	def _low_privilege_user(self):
		"""A real user who may sync (System Manager) and write, but not submit, Sales Orders."""
		role = f"SO Sync Writer {self.suffix}"
		frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)
		perm = frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": "Sales Order",
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": role,
				"permlevel": 0,
				"read": 1,
				"write": 1,
			}
		).insert(ignore_permissions=True)
		email = f"so-sync-lowpriv-{self.suffix}@example.com"
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Lowpriv",
				"send_welcome_email": 0,
				"roles": [{"role": "System Manager"}, {"role": role}],
			}
		).insert(ignore_permissions=True)

		def cleanup():
			frappe.set_user("Administrator")
			frappe.delete_doc("User", user.name, force=True, ignore_permissions=True)
			frappe.delete_doc("Custom DocPerm", perm.name, force=True, ignore_permissions=True)
			frappe.delete_doc("Role", role, force=True, ignore_permissions=True)
			frappe.clear_cache(doctype="Sales Order")

		self.addCleanup(cleanup)
		frappe.clear_cache(doctype="Sales Order")
		self.assertTrue(frappe.has_permission("Sales Order", "write", user=email))
		self.assertFalse(frappe.has_permission("Sales Order", "submit", user=email))
		return email

	def test_update_keeps_the_sales_order_item_description(self):
		order = self._synced_talabat_order()
		row = frappe.get_doc("Sales Order", order.erpnext_sales_order).items[0].name
		frappe.db.set_value("Sales Order Item", row, "description", "Real item description")
		self._change_talabat_line(order, 3, 1500)

		self.assertTrue(sync_sales_order(order.name)["ok"])

		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
		self.assertEqual(sales_order.items[0].qty, 3)
		self.assertEqual(sales_order.items[0].description, "Real item description")

	def test_close_remainder_without_submit_permission_is_a_sync_error(self):
		order = self._make_order()
		sales_order = self._sync_and_set_item_fields(order, delivered_qty=1)
		order.external_status = "CANCELLED"
		order.save()
		baseline = (order.last_synced_hash, order.last_synced_snapshot)
		self.addCleanup(frappe.set_user, frappe.session.user)
		frappe.set_user(self._low_privilege_user())

		result = sync_sales_order(order.name)
		frappe.set_user("Administrator")

		self.assertEqual(result["reconciliation"]["outcome"], "CLOSE_REMAINDER")
		self._assert_rejected_and_untouched(order, sales_order, baseline, result)
		self.assertNotEqual(frappe.db.get_value("Sales Order", sales_order.name, "status"), "Closed")
		self.assertIn("permission", result["error"])

	def test_update_without_permission_shows_a_clear_sync_error(self):
		# ERPNext's check_doc_permissions re-throws as a ValidationError; its text is kept
		# ("You do not have permissions to ...") instead of our PermissionError wording.
		order = self._synced_talabat_order()
		self._change_talabat_line(order, 3, 1500)
		baseline = self._baseline(order)
		self.addCleanup(frappe.set_user, frappe.session.user)
		frappe.set_user(self._low_privilege_user())

		result = sync_sales_order(order.name)
		frappe.set_user("Administrator")
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(order.erpnext_sync_status, "Sync Error")
		self.assertIn("permission", order.last_sync_error.lower())
		self.assertEqual(self._baseline(order), baseline)
		self.assertEqual(
			frappe.db.get_value("Sales Order Item", {"parent": order.erpnext_sales_order}, "qty"), 2
		)

	def test_missing_savepoint_on_release_ends_as_sync_error(self):
		order, sales_order = self._cancelled_talabat_order()
		baseline = (order.last_synced_hash, order.last_synced_snapshot)

		with patch(
			"marketplace_integrations.services.sales_order_sync.frappe.db.release_savepoint",
			side_effect=Exception("SAVEPOINT marketplace_reconcile_execute does not exist"),
		):
			result = sync_sales_order(order.name)

		self._assert_rejected_and_untouched(order, sales_order, baseline, result)
		self.assertNotEqual(order.erpnext_sync_status, "Synced")

	def test_failure_persisting_after_execution_rolls_back_the_erp_change_and_baseline(self):
		order = self._synced_talabat_order()
		self._change_talabat_line(order, 3, 1500)
		baseline = self._baseline(order)
		from marketplace_integrations.services import sales_order_sync as module

		real_log = module._create_sync_log
		calls = []

		def failing_first_success_log(*args, **kwargs):
			if args[2] == "Success" and not calls:
				calls.append(1)
				raise RuntimeError("log failed")
			return real_log(*args, **kwargs)

		with patch.object(module, "_create_sync_log", side_effect=failing_first_success_log):
			result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(order.erpnext_sync_status, "Sync Error")
		self.assertEqual(self._baseline(order), baseline)
		self.assertEqual(
			frappe.db.get_value("Sales Order Item", {"parent": order.erpnext_sales_order}, "qty"), 2
		)

	def test_update_carries_the_exact_row_names_for_lines_sharing_an_item(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, transaction_amount=10, current_quantity=1)
		order.append(
			"items",
			{
				"external_item_id": f"gid://shopify/LineItem/second-{self.suffix}",
				"external_variant_id": order.items[0].external_variant_id,
				"current_quantity": 1,
				"unfulfilled_quantity": 1,
				"non_fulfillable_quantity": 0,
				"fulfillment_quantities_known": 1,
				"transaction_amount": 10,
			},
		)
		order.order_total = 20
		order.save()
		self.assertTrue(sync_sales_order(order.name)["ok"])
		order.reload()
		snapshot = frappe.parse_json(order.last_synced_snapshot)
		row_one, row_two = (line["sales_order_item"] for line in snapshot["items"])
		# Pair the marketplace lines with the ERP rows in the OPPOSITE order to row order.
		snapshot["items"][0]["sales_order_item"] = row_two
		snapshot["items"][1]["sales_order_item"] = row_one
		frappe.db.set_value("Marketplace Order", order.name, "last_synced_snapshot", frappe.as_json(snapshot))
		order.reload()
		order.items[1].current_quantity = 2
		order.items[1].unfulfilled_quantity = 2
		order.items[1].transaction_amount = 20
		order.save()

		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["reconciliation"]["outcome"], "UPDATE_SUBMITTED_ORDER")
		lines = frappe.parse_json(order.last_synced_snapshot)["items"]
		self.assertEqual([line["sales_order_item"] for line in lines], [row_two, row_one])
		rows = {row.name: row for row in frappe.get_doc("Sales Order", order.erpnext_sales_order).items}
		self.assertEqual(rows[row_one].qty, 2)
		self.assertEqual(rows[row_two].qty, 1)

	def test_status_checks_never_execute_an_update(self):
		order = self._synced_talabat_order()
		with patch("erpnext.controllers.accounts_controller.update_child_qty_rate") as update:
			self._change_talabat_line(order, 3, 1500)
			order.save()
			get_current_sync_status(order.name)
		update.assert_not_called()
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertEqual(
			frappe.db.get_value("Sales Order Item", {"parent": order.erpnext_sales_order}, "qty"), 2
		)

	def test_form_load_and_save_status_checks_never_execute(self):
		order, sales_order = self._cancelled_talabat_order()

		with patch("marketplace_integrations.services.sales_order_sync.execute") as execute:
			get_current_sync_status(order.name)
			order.reload()
			order.customer_phone_number = "another save"
			order.save()

		execute.assert_not_called()
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order.name, "docstatus"), 1)

	def test_current_snapshot_sync_status_stays_plan_only_for_close_remainder(self):
		order, sales_order = self._cancelled_talabat_order(delivered_qty=1)

		self.assertEqual(get_current_sync_status(order.name), "Modified After Sync")
		self.assertEqual(
			frappe.db.get_value("Sales Order", sales_order.name, "status"), "To Deliver and Bill"
		)

	def test_current_status_refreshes_reason_when_submitted_erp_drift_changes(self):
		order = self._make_order()
		first = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", first["sales_order"])

		frappe.db.set_value(
			"Sales Order Item", sales_order.items[0].name, "amount", sales_order.items[0].amount + 1
		)
		self.assertEqual(get_current_sync_status(order.name), "Modified After Sync")
		order.reload()
		rate_drift_reason = order.last_sync_error
		self.assertIn("no longer represents", rate_drift_reason)

		# Cancelled (not Closed: a Closed order is judged as the open order it would be, #180).
		frappe.db.set_value("Sales Order", sales_order.name, "status", "Cancelled")
		self.assertEqual(get_current_sync_status(order.name), "Modified After Sync")
		order.reload()

		self.assertNotEqual(order.last_sync_error, rate_drift_reason)
		self.assertIn("no longer an active submitted document", order.last_sync_error)

	def test_submitted_shopify_cancellation_cancels_the_sales_order(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)
		first = sync_sales_order(order.name)
		order.reload()
		sales_order = frappe.get_doc("Sales Order", first["sales_order"])
		order.cancelled_at = nowdate()
		order.save()

		result = sync_sales_order(order.name)
		order.reload()
		sales_order.reload()
		log = frappe.get_doc("Marketplace Sync Log", {"marketplace_order": order.name, "status": "Success"})
		request_data = frappe.parse_json(log.request_data)

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["reconciliation"]["outcome"], "CANCEL_ORDER")
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(sales_order.docstatus, 2)
		self.assertEqual(request_data["action"], "CANCEL_ORDER")
		self.assertEqual(request_data["reconciliation"]["action"], "CANCEL_ORDER")
		self.assertIn("erp_state", request_data["reconciliation"])

	def test_submitted_shopify_refund_with_delivery_is_planned_from_a_rebuilt_snapshot(self):
		# #178: only cancellation uses the stored baseline now; refund evidence is judged
		# against a rebuilt snapshot (here unchanged, so the refund evidence decides).
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)
		sync_sales_order(order.name)
		order.reload()
		self._set_first_item_fields(order, delivered_qty=1)
		order.refunds = frappe.as_json([{"id": f"refund-{self.suffix}"}])
		order.save()

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertEqual(result["reconciliation"]["outcome"], "UNSUPPORTED_COMPENSATING_TRANSACTION")

	def test_submitted_shopify_refund_without_delivery_or_billing_is_not_a_compensating_transaction(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)
		sync_sales_order(order.name)
		order.reload()
		order.refunds = frappe.as_json([{"id": f"refund-{self.suffix}"}])
		order.cancelled_at = nowdate()
		order.save()

		result = sync_sales_order(order.name)

		self.assertEqual(result["reconciliation"]["outcome"], "CANCEL_ORDER")
		self.assertEqual(frappe.db.get_value("Sales Order", order.erpnext_sales_order, "docstatus"), 2)

	def test_submitted_shopify_refund_that_zeroes_a_line_is_judged_not_blocked(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)
		sync_sales_order(order.name)
		order.reload()
		self._set_first_item_fields(order, delivered_qty=1)
		order.items[0].current_quantity = 0
		order.refunds = frappe.as_json([{"id": f"refund-{self.suffix}"}])
		order.save()

		result = sync_sales_order(order.name)
		order.reload()

		self.assertEqual(result["reconciliation"]["outcome"], "UNSUPPORTED_COMPENSATING_TRANSACTION")
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")

	def test_submitted_shopify_refund_that_zeroes_every_undelivered_line_cancels_the_order(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)
		sync_sales_order(order.name)
		order.reload()
		order.items[0].current_quantity = 0
		order.refunds = frappe.as_json([{"id": f"refund-{self.suffix}"}])
		order.save()

		result = sync_sales_order(order.name)

		self.assertEqual(result["reconciliation"]["outcome"], "CANCEL_ORDER")
		self.assertEqual(frappe.db.get_value("Sales Order", order.erpnext_sales_order, "docstatus"), 2)

	def test_linked_submitted_order_with_refunded_status_flows_to_the_refund_planner(self):
		account = self._make_shopify_account()
		for status in ("REFUNDED", "PARTIALLY_REFUNDED"):
			with self.subTest(status=status):
				order, _mapping = self._make_shopify_order(account)
				sync_sales_order(order.name)
				order.reload()
				self._set_first_item_fields(order, delivered_qty=1)
				order.external_financial_status = status
				order.refunds = frappe.as_json([{"id": f"refund-{status}-{self.suffix}"}])
				order.save()

				result = sync_sales_order(order.name)

				self.assertEqual(result["reconciliation"]["outcome"], "UNSUPPORTED_COMPENSATING_TRANSACTION")

	def test_refund_reflected_before_delivery_stops_counting_as_evidence(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)
		sync_sales_order(order.name)
		order.reload()
		order.refunds = frappe.as_json([{"id": f"refund-{self.suffix}"}])
		order.save()
		order.reload()

		# Nothing was delivered when the refund arrived and ERPNext already matches:
		# NO_ACTION stores the CURRENT refund ids, so the refund is now "reflected".
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertIn(f"refund-{self.suffix}", frappe.parse_json(order.last_synced_snapshot)["refund_ids"])

		self._set_first_item_fields(order, delivered_qty=1)
		order.customer_city = "Later Save City"
		order.save()
		order.reload()

		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(sync_sales_order(order.name)["reconciliation"]["outcome"], "NO_ACTION")
		order.reload()
		self.assertEqual(order.erpnext_sync_status, "Synced")

	def test_refund_evidence_outranks_a_rebuild_blocker_on_a_delivered_order(self):
		account = self._make_shopify_account()
		order, mapping = self._make_shopify_order(account)
		sync_sales_order(order.name)
		order.reload()
		self._set_first_item_fields(order, delivered_qty=1)
		order.refunds = frappe.as_json([{"id": f"refund-{self.suffix}"}])
		order.save()
		frappe.db.set_value("Marketplace Product Mapping", mapping.name, "enabled", 0)

		result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(result["reconciliation"]["outcome"], "UNSUPPORTED_COMPENSATING_TRANSACTION")
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertIn("compensating", order.last_sync_error)
		# The status check on every save/form load agrees.
		order.customer_email = f"disabled-mapping-{self.suffix}@example.com"
		order.save()
		order.reload()
		self.assertIn("compensating", order.last_sync_error)

	def test_rebuild_blocker_without_refund_evidence_still_surfaces(self):
		account = self._make_shopify_account()
		order, mapping = self._make_shopify_order(account)
		sync_sales_order(order.name)
		order.reload()
		self._set_first_item_fields(order, delivered_qty=1)
		frappe.db.set_value("Marketplace Product Mapping", mapping.name, "enabled", 0)

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertNotIn("compensating", result["error"])

	def _make_unsynced_refunded_order(
		self, account, financial_status="REFUNDED", restock_types=("CANCEL",), quantity=0, **changes
	):
		"""A never-synced Shopify order in the state of a full refund before any shipment."""
		order, _mapping = self._make_shopify_order(account, financial_status=financial_status)
		order.external_fulfillment_status = "UNFULFILLED"
		order.items[0].current_quantity = quantity
		order.items[0].unfulfilled_quantity = quantity
		order.order_total = 0
		order.refunds = frappe.as_json(
			[
				{
					"id": f"refund-{self.suffix}",
					"refundLineItems": {
						"nodes": [{"id": "rli", "restockType": kind} for kind in restock_types]
					},
				}
			]
		)
		for fieldname, value in changes.items():
			setattr(order, fieldname, value)
		order.save()
		return order

	def test_never_synced_fully_refunded_shopify_order_is_a_terminal_skip(self):
		account = self._make_shopify_account()
		order = self._make_unsynced_refunded_order(account, last_sync_error="old error")
		orders_before = frappe.db.count("Sales Order")

		result = sync_sales_order(order.name)
		order.reload()
		logs = frappe.get_all(
			"Marketplace Sync Log", filters={"marketplace_order": order.name}, fields=["status", "sync_type"]
		)

		self.assertTrue(result["ok"], result.get("error"))
		self.assertTrue(result["skipped"])
		self.assertFalse(order.erpnext_sales_order)
		self.assertEqual(order.erpnext_sync_status, "Not Synced")
		self.assertFalse(order.last_sync_error)
		self.assertEqual([(row.sync_type, row.status) for row in logs], [("Sales Order Sync", "Success")])
		self.assertEqual(frappe.db.count("Sales Order"), orders_before)

	def test_never_synced_refunded_order_that_is_not_fully_refunded_still_blocks(self):
		account = self._make_shopify_account()
		cases = {
			"partially refunded": {"financial_status": "PARTIALLY_REFUNDED"},
			"a line still has quantity": {"quantity": 1},
			"order total left": {"order_total": 10},
			"shipped": {"external_fulfillment_status": "FULFILLED"},
			"restocked as a return": {"restock_types": ("CANCEL", "RETURN")},
		}
		for label, changes in cases.items():
			with self.subTest(case=label):
				order = self._make_unsynced_refunded_order(account, **changes)

				result = sync_sales_order(order.name)
				order.reload()

				self.assertFalse(result["ok"])
				self.assertNotIn("skipped", result)
				self.assertIn("financially eligible", result["error"])
				self.assertFalse(order.erpnext_sales_order)

	@staticmethod
	def _transaction(*, manual, status="PENDING", gateway="Cash on Delivery (COD)", kind="SALE"):
		return {"kind": kind, "status": status, "gateway": gateway, "manualPaymentGateway": manual}

	def test_pending_shopify_order_with_a_manual_payment_syncs(self):
		"""#216: COD and any other manual method arrive PENDING with a manual-gateway transaction."""
		account = self._make_shopify_account()
		for gateway in ("Cash on Delivery (COD)", "Bank Deposit", "Any Renamed Method"):
			with self.subTest(gateway=gateway):
				order, _mapping = self._make_shopify_order(
					account,
					financial_status="PENDING",
					payment_transactions=[self._transaction(manual=True, gateway=gateway)],
				)

				result = sync_sales_order(order.name)

				self.assertTrue(result["ok"], result.get("error"))

	def test_pending_shopify_order_without_a_manual_payment_stays_blocked(self):
		account = self._make_shopify_account()
		cases = {
			"online": [self._transaction(manual=False, gateway="shopify_payments")],
			"no transactions": None,
			"empty transactions": [],
			"manual but failed": [self._transaction(manual=True, status="FAILURE")],
		}
		for label, transactions in cases.items():
			with self.subTest(case=label):
				order, _mapping = self._make_shopify_order(
					account, financial_status="PENDING", payment_transactions=transactions
				)

				result = sync_sales_order(order.name)
				order.reload()

				self.assertFalse(result["ok"])
				self.assertIn("financially eligible", result["error"])
				self.assertFalse(order.erpnext_sales_order)

	def test_blocked_pending_order_message_names_what_shopify_sent(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(
			account,
			financial_status="PENDING",
			payment_transactions=[self._transaction(manual=False, gateway="paymob")],
		)

		result = sync_sales_order(order.name)

		self.assertIn("SALE/PENDING/paymob/manual=no", result["error"])

	def test_authorized_and_voided_orders_stay_blocked_even_with_a_manual_transaction(self):
		account = self._make_shopify_account()
		for status in ("AUTHORIZED", "VOIDED", "EXPIRED"):
			with self.subTest(status=status):
				order, _mapping = self._make_shopify_order(
					account,
					financial_status=status,
					payment_transactions=[self._transaction(manual=True)],
				)

				result = sync_sales_order(order.name)

				self.assertFalse(result["ok"])
				self.assertIn("financially eligible", result["error"])

	def test_cancelled_pending_manual_shopify_order_creates_no_sales_order(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(
			account,
			financial_status="PENDING",
			cancelled_at=now_datetime(),
			payment_transactions=[self._transaction(manual=True)],
		)

		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["skipped"])
		self.assertFalse(order.erpnext_sales_order)

	def test_cod_order_marked_paid_keeps_its_sales_order_synced(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(
			account,
			financial_status="PENDING",
			payment_transactions=[self._transaction(manual=True)],
		)
		first = sync_sales_order(order.name)
		self.assertTrue(first["ok"], first.get("error"))
		order.reload()
		first_hash = order.last_synced_hash

		order.external_financial_status = "PAID"
		order.payment_transactions = frappe.as_json([self._transaction(manual=True, status="SUCCESS")])
		order.save()
		second = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(second["ok"], second.get("error"))
		self.assertEqual(second["sales_order"], first["sales_order"])
		self.assertEqual(order.last_synced_hash, first_hash)
		self.assertEqual(order.erpnext_sync_status, "Synced")

	def test_shopify_order_cancelled_before_a_sales_order_is_a_terminal_skip(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, cancelled_at=now_datetime())

		result = sync_sales_order(order.name)
		order.reload()
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name},
			fields=["status", "sync_type"],
		)

		self.assertTrue(result["ok"], result.get("error"))
		self.assertTrue(result["skipped"])
		self.assertFalse(order.erpnext_sales_order)
		self.assertEqual(order.erpnext_sync_status, "Not Synced")
		self.assertFalse(order.last_sync_error)
		self.assertEqual([(row.sync_type, row.status) for row in logs], [("Sales Order Sync", "Success")])
		self.assertFalse(frappe.db.exists("Sales Order", {"custom_marketplace_order": order.name}))

	def test_cancelling_after_an_earlier_sync_error_clears_the_obsolete_error(self):
		"""A COD order first blocked as unpaid, then cancelled, must not stay in Sync Error."""
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, financial_status="PENDING")
		first = sync_sales_order(order.name)
		order.reload()
		self.assertFalse(first["ok"])
		self.assertEqual(order.erpnext_sync_status, "Sync Error")

		order.cancelled_at = now_datetime()
		order.save()
		second = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(second["ok"], second.get("error"))
		self.assertEqual(order.erpnext_sync_status, "Not Synced")
		self.assertFalse(order.last_sync_error)
		statuses = [
			row.status
			for row in frappe.get_all(
				"Marketplace Sync Log",
				filters={"marketplace_order": order.name},
				fields=["status"],
				order_by="creation asc",
			)
		]
		self.assertEqual(statuses, ["Invalid", "Success"])  # history preserved, nothing deleted

	def test_repeated_syncs_of_a_cancelled_unsynced_order_never_create_a_sales_order(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, cancelled_at=now_datetime())

		for _attempt in range(3):
			result = sync_sales_order(order.name)
			self.assertTrue(result["ok"])
		order.reload()

		self.assertFalse(order.erpnext_sales_order)
		self.assertEqual(order.erpnext_sync_status, "Not Synced")

	def test_cancelled_shopify_order_with_a_draft_sales_order_gets_an_actionable_message(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)
		first = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", first["sales_order"])
		frappe.db.set_value("Sales Order", sales_order.name, "docstatus", 0, update_modified=False)
		order.reload()
		order.cancelled_at = now_datetime()
		order.save()

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertIn(sales_order.name, result["error"])
		self.assertIn("Draft", result["error"])

	def test_pending_order_message_shows_unknown_for_a_missing_manual_flag(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(
			account, financial_status="PENDING", payment_transactions=[{"kind": "SALE", "status": "PENDING"}]
		)

		result = sync_sales_order(order.name)

		self.assertIn("SALE/PENDING/?/manual=unknown", result["error"])

	def test_never_synced_zero_quantity_shopify_line_still_blocks(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, current_quantity=0)

		result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertFalse(order.erpnext_sales_order)

	def test_talabat_change_after_pickup_status_needs_attention_with_the_lifecycle_reason(self):
		for status in ("READY_FOR_PICKUP", "DISPATCHED", "DELIVERED"):
			with self.subTest(status=status):
				order = self._make_order()
				sync_sales_order(order.name)
				order.reload()
				order.external_status = status
				order.items[0].fulfilled_quantity = 3
				order.items[0].fulfilled_total_price = 1500
				order.save()

				result = sync_sales_order(order.name)
				order.reload()

				self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
				self.assertIn(status, result["error"])
				self.assertEqual(order.erpnext_sync_status, "Modified After Sync")

	def test_talabat_unchanged_order_after_pickup_status_stays_synced(self):
		order = self._make_order()
		sync_sales_order(order.name)
		order.reload()
		order.external_status = "DISPATCHED"
		order.save()

		result = sync_sales_order(order.name)

		self.assertEqual(result["reconciliation"]["outcome"], "NO_ACTION")

	def test_talabat_change_with_unrecognized_status_needs_attention(self):
		order = self._make_order()
		sync_sales_order(order.name)
		order.reload()
		order.external_status = "CANCELED"  # only the documented CANCELLED spelling is accepted
		order.items[0].fulfilled_quantity = 3
		order.items[0].fulfilled_total_price = 1500
		order.save()

		result = sync_sales_order(order.name)

		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertIn("not recognized", result["error"])

	def test_talabat_cancelled_after_pickup_needs_a_compensating_transaction_only_with_erp_delivery(self):
		for delivered_qty, expected in ((1, "UNSUPPORTED_COMPENSATING_TRANSACTION"), (0, "CANCEL_ORDER")):
			with self.subTest(delivered_qty=delivered_qty):
				order = self._make_order()
				self._sync_and_set_item_fields(order, delivered_qty=delivered_qty)
				order.external_status = "CANCELLED"
				order.post_picked_up = 1
				order.save()

				result = sync_sales_order(order.name)

				self.assertEqual(result["reconciliation"]["outcome"], expected)

	def test_talabat_cancelled_after_pickup_converges_when_erp_order_is_closed_without_remainder(self):
		order = self._make_order()
		sales_order = self._sync_and_set_item_fields(order, delivered_qty=2)
		frappe.db.set_value("Sales Order", sales_order.name, "status", "Closed", update_modified=False)
		order.external_status = "CANCELLED"
		order.post_picked_up = 1
		order.save()

		result = sync_sales_order(order.name)

		self.assertEqual(result["reconciliation"]["outcome"], "NO_ACTION")

	def test_promotion_only_refresh_is_not_an_edit(self):
		order = self._make_order()
		sync_sales_order(order.name)
		order.reload()
		first_hash = order.last_synced_hash
		order.promotion_status = "AVAILABLE"
		order.items[0].container_deposit = 1
		order.items[0].promotions = frappe.as_json(
			[{"name": "Promo", "type": "PERCENT", "discount_amount": 5, "sponsorships": []}]
		)
		order.save()
		order.reload()

		self.assertEqual(snapshot_hash(build_sales_order_snapshot(order)), first_hash)
		self.assertEqual(order.last_synced_hash, first_hash)
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(sync_sales_order(order.name)["reconciliation"]["outcome"], "NO_ACTION")

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
		cut the merchandise total below the Talabat transaction total.

		#131: the first sync now auto-submits the Sales Order, so this
		scenario (a hand-edited still-Draft Sales Order between syncs) is only
		reachable for a pre-existing legacy Draft - forced back to Draft here
		via a direct db write, matching the update-branch coverage pattern in
		test_update_branch_still_reapplies_snapshot_for_a_legacy_draft_sales_order."""
		order = self._make_order()
		first = sync_sales_order(order.name)
		frappe.db.set_value("Sales Order", first["sales_order"], "docstatus", 0, update_modified=False)
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
			external_status="IN_CART",
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
			],
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
		self.assertIn("Talabat, Shopify and Noon", result["error"])
		self.assertFalse(order.erpnext_sales_order)
		self.assertEqual(order.erpnext_sync_status, original_status)
		self.assertNotEqual(order.erpnext_sync_status, "Sync Error")
		self.assertEqual(order.modified, original_modified)

	def test_shopify_platform_passes_platform_gate(self):
		order = self._make_order()
		order.platform = "Shopify"
		_validate_platform_supported(order)

	def test_shopify_unpaid_orders_are_blocked(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, financial_status="PENDING")
		result = sync_sales_order(order.name)
		self.assertFalse(result["ok"])
		self.assertEqual(result["status"], "Sync Error")
		self.assertFalse(order.erpnext_sales_order)

	def test_shopify_cancelled_unsynced_order_is_skipped_not_blocked(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, cancelled_at=nowdate())
		result = sync_sales_order(order.name)
		self.assertTrue(result["ok"])
		self.assertEqual(result["status"], "Not Synced")
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

		# #179: a later Shopify-side transaction_amount change is applied to the
		# submitted Sales Order through ERPNext's native update.
		order.reload()
		order.items[0].transaction_amount = 40
		order.save()
		fourth = sync_sales_order(order.name)
		self.assertTrue(fourth["ok"], fourth.get("error"))
		order.reload()
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(frappe.db.get_value("Sales Order Item", {"parent": so_name}, "rate"), 20)

	def test_shopify_sync_sets_custom_marketplace_order_and_platform_fields(self):
		"""#135: platform-agnostic wiring in `_apply_snapshot()` - the same
		back-link fields set for Talabat above must also be set for Shopify."""
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)

		result = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(sales_order.custom_marketplace_order, order.name)
		self.assertEqual(sales_order.custom_marketplace_platform, "Shopify")

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
		self.assertEqual(
			frappe.db.get_value("Sales Order", result["sales_order"], "customer"), account.erpnext_customer
		)
		self.assertEqual(
			frappe.db.get_value("Customer", account.erpnext_customer, "customer_type"), "Company"
		)

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
		self.assertEqual(
			frappe.db.get_value("Sales Order", second["sales_order"], "customer"), first_customer
		)
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
		customer = frappe.get_doc(
			"Customer", frappe.db.get_value("Sales Order", result["sales_order"], "customer")
		)
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
				"province": "Cairo Governorate",
				"provinceCode": "C",
				"phone": "+20111111111",
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
		self.assertEqual(address.state, "Cairo Governorate")
		self.assertEqual(address.email_id, email)
		# The address phone stays the buyer phone, not shippingAddress.phone (#156).
		self.assertEqual(address.phone, "+20123456789")
		self.assertTrue(
			any(
				link.link_doctype == "Customer" and link.link_name == sales_order.customer
				for link in address.links
			)
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
		self.assertEqual(
			frappe.db.get_value("Address", sales_order.customer_address, "address_type"), "Billing"
		)

	def test_shopify_resync_updates_only_dedicated_address_without_duplicate(self):
		"""#131: the first sync auto-submits the Sales Order, so a later
		address/phone change on the Marketplace Order can no longer flow
		through to the SALES ORDER - the resync attempt hits
		`_reconcile_submitted_order()` and the submitted Sales Order keeps
		pointing at the same Address it always did. The buyer's dedicated
		Address record itself is still resolved/updated in-place as part of
		building the snapshot (unchanged pre-#131 behavior, upstream of the
		Sales Order docstatus check) - so it picks up the new billing details,
		but no SECOND/duplicate Address is ever created for the same buyer."""
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
		order_doc.reload()

		self.assertFalse(second["ok"])
		self.assertEqual(order_doc.erpnext_sync_status, "Modified After Sync")
		address = frappe.get_doc("Address", address_name)
		self.assertEqual(address.address_line1, "Updated Billing Road")
		self.assertEqual(address.address_type, "Billing")
		self.assertEqual(address.phone, "+20122222222")
		sales_order = frappe.get_doc("Sales Order", first["sales_order"])
		self.assertEqual(sales_order.shipping_address_name, address_name)
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
		self.assertEqual(
			frappe.db.get_value("Address", unrelated.name, "address_line1"), "User-maintained Street"
		)
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

	# --- #131: Marketplace Sync Log row creation ---------------------------

	def test_successful_sync_creates_marketplace_sync_log_success_row(self):
		order = self._make_order()

		result = sync_sales_order(order.name, trigger_source="Manual")

		self.assertTrue(result["ok"], result.get("error"))
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name},
			fields=["status", "trigger_source", "traceback"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].status, "Success")
		self.assertEqual(logs[0].trigger_source, "Manual")
		self.assertFalse(logs[0].traceback)

	def test_sync_log_records_the_caller_supplied_trigger_source(self):
		"""`trigger_source` is passed through as-is - #133/#134's future
		webhook callers aren't built yet, but the parameter is already wired
		end-to-end into the log row."""
		order = self._make_order()

		result = sync_sales_order(order.name, trigger_source="Shopify Webhook")

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(
			frappe.db.get_value("Marketplace Sync Log", {"marketplace_order": order.name}, "trigger_source"),
			"Shopify Webhook",
		)

	def test_blocked_sync_creates_marketplace_sync_log_invalid_row_without_traceback(self):
		order = self._make_order(sku="UNMAPPED-SKU")

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name},
			fields=["status", "traceback"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].status, "Invalid")
		self.assertFalse(logs[0].traceback)

	def test_unsupported_platform_creates_marketplace_sync_log_invalid_row(self):
		order = self._make_order()
		frappe.db.set_value("Marketplace Order", order.name, "platform", "Amazon", update_modified=False)
		frappe.clear_document_cache("Marketplace Order", order.name)

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertEqual(
			frappe.db.get_value("Marketplace Sync Log", {"marketplace_order": order.name}, "status"),
			"Invalid",
		)

	def test_submitted_order_update_creates_marketplace_sync_log_success_row(self):
		order = self._make_order()
		sync_sales_order(order.name)
		order.reload()
		order.items[0].fulfilled_quantity = 3
		order.items[0].fulfilled_total_price = 1500
		order.save()

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name},
			fields=["status", "request_data"],
			order_by="creation asc",
		)
		self.assertEqual(len(logs), 2)
		self.assertEqual(logs[0].status, "Success")
		self.assertEqual(logs[1].status, "Success")
		self.assertEqual(frappe.parse_json(logs[1].request_data)["action"], "UPDATE_SUBMITTED_ORDER")

	def test_unexpected_exception_creates_marketplace_sync_log_error_row_with_traceback(self):
		order = self._make_order()

		with patch(
			"marketplace_integrations.services.sales_order_sync._verify_line_amounts",
			side_effect=RuntimeError("boom"),
		):
			result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name},
			fields=["status", "traceback"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].status, "Error")
		self.assertIn("RuntimeError", logs[0].traceback)
		order.reload()
		self.assertFalse(order.erpnext_sales_order)

	# -------------------------------------------------------------------
	# #146 decisions 1-6: Noon Sales Order eligibility and sync policy.
	# -------------------------------------------------------------------

	def test_noon_platform_passes_platform_gate(self):
		order = self._make_order()
		order.platform = "Noon"
		_validate_platform_supported(order)

	def test_noon_eligible_statuses_count_toward_the_sales_order(self):
		"""Decision 2: CONFIRMED + {ACKNOWLEDGED, SHIPPED, raw UNSPECIFIED}
		all count. Each case uses its own order/account so one subTest's
		result can't leak into another's eligibility total."""
		cases = ["ACKNOWLEDGED", "SHIPPED", "INTEGRATION_ITEM_STATUS_UNSPECIFIED"]
		for integration_status in cases:
			with self.subTest(integration_status=integration_status):
				account = self._make_noon_account()
				order, _mapping = self._make_noon_order(
					account,
					items=[
						{
							"external_item_id": f"sku:SKU-NOON:MP_ITEM_STATUS_CONFIRMED:{integration_status}",
							"external_sku": "SKU-NOON",
							"external_status": "CONFIRMED",
							"integration_status": integration_status,
							"current_quantity": 2,
							"transaction_amount": 200.0,
						}
					],
				)
				result = sync_sales_order(order.name)
				self.assertTrue(result["ok"], result.get("error"))
				sales_order = frappe.get_doc("Sales Order", result["sales_order"])
				self.assertEqual(sales_order.items[0].qty, 2)
				self.assertEqual(sales_order.items[0].amount, 200.0)

	def test_noon_ineligible_statuses_never_count(self):
		"""Decision 2: CANCELLED, raw MP_ITEM_STATUS_UNSPECIFIED and
		OUT_OF_STOCK units never count - with no other eligible row, the
		whole order has nothing to sync (SyncBlocker, decision 3's "no
		eligible lines" case)."""
		cases = [
			{"external_status": "CANCELLED", "integration_status": "ACKNOWLEDGED"},
			{"external_status": "MP_ITEM_STATUS_UNSPECIFIED", "integration_status": "ACKNOWLEDGED"},
			{"external_status": "CONFIRMED", "integration_status": "OUT_OF_STOCK"},
		]
		for options in cases:
			with self.subTest(options=options):
				account = self._make_noon_account()
				order, _mapping = self._make_noon_order(
					account,
					items=[
						{
							"external_item_id": "sku:SKU-NOON:x:y",
							"external_sku": "SKU-NOON",
							"current_quantity": 2,
							"transaction_amount": 200.0,
							**options,
						}
					],
				)
				result = sync_sales_order(order.name)
				self.assertFalse(result["ok"])
				self.assertFalse(order.erpnext_sales_order)

	def test_noon_zeroed_stale_row_never_counts(self):
		"""Decision 2: #145 zeroes (never deletes) a stale status-group row -
		current_quantity=0 must exclude it even though external_status/
		integration_status still read CONFIRMED/ACKNOWLEDGED."""
		account = self._make_noon_account()
		order, _mapping = self._make_noon_order(
			account,
			items=[
				{
					"external_item_id": "sku:SKU-NOON:MP_ITEM_STATUS_CONFIRMED:INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					"external_sku": "SKU-NOON",
					"external_status": "CONFIRMED",
					"integration_status": "ACKNOWLEDGED",
					"current_quantity": 0,
					"transaction_amount": 0.0,
				}
			],
		)
		result = sync_sales_order(order.name)
		self.assertFalse(result["ok"])
		self.assertFalse(order.erpnext_sales_order)

	def test_noon_status_transition_keeps_same_line_and_sync_status(self):
		"""Decision 3: grouping by external_sku (not the status-bearing
		external_item_id) means an ACKNOWLEDGED -> SHIPPED transition - the
		old ACKNOWLEDGED row zeroed, a new SHIPPED row added, same SKU -
		must NOT look like a modification once already synced."""
		account = self._make_noon_account()
		order, _mapping = self._make_noon_order(
			account,
			items=[
				{
					"external_item_id": "sku:SKU-NOON:MP_ITEM_STATUS_CONFIRMED:INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					"external_sku": "SKU-NOON",
					"external_status": "CONFIRMED",
					"integration_status": "ACKNOWLEDGED",
					"current_quantity": 2,
					"transaction_amount": 200.0,
				}
			],
		)
		first = sync_sales_order(order.name)
		self.assertTrue(first["ok"], first.get("error"))
		so_name = first["sales_order"]

		# Simulate the next poll's #145 zero-out + new SHIPPED row, same shape
		# `integrations/noon/orders.py::_zero_out_stale_status_groups()` produces.
		order.reload()
		order.items[0].current_quantity = 0
		order.items[0].transaction_amount = 0.0
		order.append(
			"items",
			{
				"external_item_id": "sku:SKU-NOON:MP_ITEM_STATUS_CONFIRMED:INTEGRATION_ITEM_STATUS_SHIPPED",
				"external_sku": "SKU-NOON",
				"external_status": "CONFIRMED",
				"integration_status": "SHIPPED",
				"current_quantity": 2,
				"transaction_amount": 200.0,
			},
		)
		order.save()

		self.assertEqual(get_current_sync_status(order.name), "Synced")
		order.reload()
		self.assertEqual(order.erpnext_sales_order, so_name)

	def test_noon_unmapped_sku_blocks_sync(self):
		account = self._make_noon_account()
		order, _mapping = self._make_noon_order(account, create_mapping=False)

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertIn("No enabled Marketplace Product Mapping", result["error"])
		self.assertFalse(order.erpnext_sales_order)

	def test_noon_currency_mismatch_blocks_sync(self):
		"""Decision 6: presentment_currency must equal the resolved ERPNext
		currency, on top of the existing single-currency account checks."""
		account = self._make_noon_account()
		order, _mapping = self._make_noon_order(account, presentment_currency="XAU")

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertIn("presentment currency", result["error"])
		self.assertFalse(order.erpnext_sales_order)

	def test_noon_currency_match_ignores_case(self):
		"""Noon sends lowercase ISO codes (e.g. "egp"); that must match ERPNext's "EGP"."""
		account = self._make_noon_account()
		currency = frappe.get_cached_value("Company", self.company, "default_currency")
		order, _mapping = self._make_noon_order(account, presentment_currency=currency.lower())

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))

	def test_noon_dates_use_received_on_for_both_transaction_and_delivery(self):
		"""Decision 5: Noon documents no promised/delivery date - both
		Sales Order dates are set to received_on, never left to default to
		Talabat's promised_for (which Noon orders never populate)."""
		account = self._make_noon_account()
		order, _mapping = self._make_noon_order(account)

		result = sync_sales_order(order.name)
		self.assertTrue(result["ok"], result.get("error"))
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])

		self.assertEqual(sales_order.transaction_date, getdate(order.received_on))
		self.assertEqual(sales_order.delivery_date, getdate(order.received_on))

	def test_noon_sync_sets_custom_marketplace_order_and_platform_fields(self):
		account = self._make_noon_account()
		order, _mapping = self._make_noon_order(account)

		result = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(sales_order.custom_marketplace_order, order.name)
		self.assertEqual(sales_order.custom_marketplace_platform, "Noon")

	def test_noon_uses_generic_account_customer(self):
		"""Decision 4: Noon has no unique buyer key, so every order uses the
		account's generic Customer - same non-Shopify path Talabat uses."""
		account = self._make_noon_account()
		order, _mapping = self._make_noon_order(account)

		result = sync_sales_order(order.name)
		self.assertTrue(result["ok"], result.get("error"))
		account.reload()
		self.assertEqual(
			frappe.db.get_value("Sales Order", result["sales_order"], "customer"), account.erpnext_customer
		)

	def test_noon_submitted_order_unit_going_out_of_stock_sets_modified_after_sync(self):
		"""Decision 8: after the Sales Order is auto-submitted (#131), a unit
		later going OUT_OF_STOCK must surface as "Modified After Sync" - and
		must never amend/cancel the already-submitted Sales Order itself."""
		account = self._make_noon_account()
		order, _mapping = self._make_noon_order(account)

		first = sync_sales_order(order.name)
		self.assertTrue(first["ok"], first.get("error"))
		so_name = first["sales_order"]
		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "docstatus"), 1)

		order.reload()
		order.items[0].integration_status = "OUT_OF_STOCK"
		order.save()

		order.reload()
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertEqual(order.erpnext_sales_order, so_name)
		# The submitted Sales Order itself is never touched by this.
		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Sales Order Item", {"parent": so_name}, "qty"), 2)

	COMPENSATING_MESSAGE = "Marketplace return or refund evidence needs a compensating ERP transaction."

	def _link_noon_return(self, account, order):
		noon_return = frappe.get_doc(
			{
				"doctype": "Noon Return",
				"marketplace_account": account.name,
				"purchase_item_nr": f"PI-SO-SYNC-{self.suffix}",
				"order_nr": "ORD-NOON",
				"found_via": "Manual Lookup",
				"first_seen_on": frappe.utils.now_datetime(),
				"last_seen_on": frappe.utils.now_datetime(),
				"marketplace_order": order.name,
			}
		).insert()
		self.addCleanup(
			frappe.delete_doc, "Noon Return", noon_return.name, force=True, ignore_permissions=True
		)
		return noon_return

	def test_noon_return_on_a_synced_order_sets_modified_after_sync_without_changing_the_sales_order(self):
		account = self._make_noon_account()
		order, _mapping = self._make_noon_order(account)
		first = sync_sales_order(order.name)
		self.assertTrue(first["ok"], first.get("error"))
		so_before = frappe.db.get_value(
			"Sales Order", first["sales_order"], ["docstatus", "modified"], as_dict=True
		)

		self._link_noon_return(account, order)
		sync_sales_order(order.name)

		order.reload()
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertEqual(order.last_sync_error, self.COMPENSATING_MESSAGE)
		self.assertEqual(
			frappe.db.get_value("Sales Order", first["sales_order"], ["docstatus", "modified"], as_dict=True),
			so_before,
		)
		self.assertEqual(frappe.db.get_value("Sales Order Item", {"parent": first["sales_order"]}, "qty"), 2)

	def test_noon_return_linked_before_the_sales_order_exists_is_not_reported_synced(self):
		"""#164: a return linked while the order has no Sales Order bumps no
		re-plan, so the first (fresh-insert) sync must itself flag it."""
		account = self._make_noon_account()
		order, _mapping = self._make_noon_order(account)
		self._link_noon_return(account, order)

		result = sync_sales_order(order.name)

		order.reload()
		self.assertTrue(order.erpnext_sales_order)
		self.assertEqual(result["sales_order"], order.erpnext_sales_order)
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertEqual(order.last_sync_error, self.COMPENSATING_MESSAGE)
		self.assertEqual(frappe.db.get_value("Sales Order", order.erpnext_sales_order, "docstatus"), 1)

	# -------------------------------------------------------------------
	# #178 part 3: Noon evidence + snapshot builder for a linked order.
	# -------------------------------------------------------------------

	def _noon_payload_unit(self, mp_item_nr, mp_status, integration_status, sku="SKU-NOON"):
		return {
			"mp_item_nr": mp_item_nr,
			"partner_sku": sku,
			"mp_status": mp_status,
			"integration_status": integration_status,
		}

	def test_noon_linked_order_with_every_unit_cancelled_cancels_the_sales_order(self):
		account = self._make_noon_account()
		order, _mapping = self._make_noon_order(account)
		first = sync_sales_order(order.name)
		self.assertTrue(first["ok"], first.get("error"))
		order.reload()
		order.items[0].external_status = "CANCELLED"
		order.items[0].integration_status = "OUT_OF_STOCK"
		order.latest_payload = frappe.as_json(
			{
				"items": [
					self._noon_payload_unit(
						"1", "MP_ITEM_STATUS_CANCELLED", "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"
					),
					self._noon_payload_unit(
						"2", "MP_ITEM_STATUS_CANCELLED", "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"
					),
				]
			}
		)
		order.save()

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["reconciliation"]["outcome"], "CANCEL_ORDER")
		self.assertEqual(frappe.db.get_value("Sales Order", first["sales_order"], "docstatus"), 2)

	def test_noon_never_synced_order_with_no_eligible_lines_still_blocks(self):
		account = self._make_noon_account()
		order, _mapping = self._make_noon_order(
			account,
			items=[
				{
					"external_item_id": "sku:SKU-NOON:x:y",
					"external_sku": "SKU-NOON",
					"external_status": "CANCELLED",
					"integration_status": "OUT_OF_STOCK",
					"current_quantity": 2,
					"transaction_amount": 200.0,
				}
			],
		)

		with self.assertRaises(SyncBlocker):
			build_sales_order_snapshot(order, for_submitted_order=False)
		result = sync_sales_order(order.name)
		self.assertFalse(result["ok"])
		self.assertIn("No eligible Noon order lines", result["error"])

	# --- Shopify buyer phone on the Sales Order (#277) -----------------------------------

	def test_shopify_sync_stores_the_buyer_phone_on_the_submitted_sales_order(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, customer_phone_number="+201001234567")

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])
		self.assertEqual(sales_order.docstatus, 1)
		self.assertEqual(sales_order.contact_mobile, "+201001234567")
		self.assertEqual(get_current_sync_status(order.name), "Synced")

	def test_contact_mobile_is_editable_after_submit_and_survives_a_later_sync(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, customer_phone_number="+201001234567")
		result = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", result["sales_order"])

		sales_order.contact_mobile = "01112223334"
		sales_order.save()  # update-after-submit: needs the Property Setters
		sales_order.reload()
		self.assertEqual(sales_order.contact_mobile, "01112223334")

		self.assertTrue(sync_sales_order(order.name)["ok"])
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order.name, "contact_mobile"), "01112223334")
		self.assertEqual(get_current_sync_status(order.name), "Synced")

	def test_a_staff_typed_phone_on_a_draft_is_not_overwritten_and_an_empty_one_is_filled(self):
		order = SimpleNamespace(platform="Shopify", customer_phone_number=" +201001234567 ")
		draft = SimpleNamespace(contact_mobile="01112223334")
		_fill_buyer_phone(draft, order)
		self.assertEqual(draft.contact_mobile, "01112223334")

		empty = SimpleNamespace(contact_mobile=None)
		_fill_buyer_phone(empty, order)
		self.assertEqual(empty.contact_mobile, "+201001234567")

		other = SimpleNamespace(contact_mobile=None)
		_fill_buyer_phone(other, SimpleNamespace(platform="Talabat", customer_phone_number="masked"))
		self.assertIsNone(other.contact_mobile)

	def test_the_buyer_phone_is_not_part_of_the_snapshot_hash(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, customer_phone_number="+201001234567")
		sync_sales_order(order.name)
		order.reload()
		self.assertNotIn("+201001234567", order.last_synced_snapshot)

	# --- Shopify shipping charge -------------------------------------------------------

	def _income_account(self):
		return frappe.get_all(
			"Account",
			filters={"company": self.company, "is_group": 0, "disabled": 0, "root_type": "Income"},
			pluck="name",
			limit=1,
		)[0]

	def _sync_shopify(self, *, with_account=True, account=None, **order_kwargs):
		"""Sync a one-line Shopify order (line total ``transaction_amount``, qty ``current_quantity``)."""
		account = account or self._make_shopify_account(self._income_account() if with_account else None)
		order, _mapping = self._make_shopify_order(account, **order_kwargs)
		result = sync_sales_order(order.name)
		order.reload()
		return account, order, result

	def _shipping_rows(self, sales_order_name):
		return frappe.get_doc("Sales Order", sales_order_name).taxes

	def test_shopify_free_shipping_order_has_no_charge_row_and_no_snapshot_key(self):
		_account, order, result = self._sync_shopify(transaction_amount=74.99, current_quantity=1)

		self.assertTrue(result["ok"], result.get("error"))
		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
		self.assertEqual(len(sales_order.taxes), 0)
		self.assertEqual(sales_order.grand_total, 74.99)
		self.assertNotIn("shipping", frappe.parse_json(order.last_synced_snapshot))

	def test_shopify_free_shipping_order_needs_no_shipping_account(self):
		_account, order, result = self._sync_shopify(with_account=False)

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(len(self._shipping_rows(order.erpnext_sales_order)), 0)

	def test_shopify_paid_shipping_adds_one_actual_charge_row_to_the_total(self):
		account, order, result = self._sync_shopify(
			transaction_amount=74.99, current_quantity=1, shipping_total=419.22
		)

		self.assertTrue(result["ok"], result.get("error"))
		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
		self.assertEqual(sales_order.docstatus, 1)
		self.assertEqual(len(sales_order.taxes), 1)
		row = sales_order.taxes[0]
		self.assertEqual(row.charge_type, "Actual")
		self.assertEqual(row.account_head, account.erpnext_shipping_account)
		self.assertEqual(row.description, "Shipping")
		self.assertEqual(row.tax_amount, 419.22)
		self.assertEqual(sales_order.net_total, 74.99)
		self.assertEqual(sales_order.grand_total, 494.21)
		self.assertEqual(frappe.parse_json(order.last_synced_snapshot)["shipping"], 419.22)
		self.assertNotIn("account", frappe.as_json(frappe.parse_json(order.last_synced_snapshot)["shipping"]))

	def test_shopify_cod_order_with_shipping_matches_shopify_and_creates_no_payment_documents(self):
		"""The #1033 shape: COD (PENDING + manual transaction), items 74.99 + shipping 419.22 = 494.21."""
		doctypes = ("Payment Entry", "Sales Invoice")
		before = {doctype: frappe.db.count(doctype) for doctype in doctypes}

		_account, order, result = self._sync_shopify(
			transaction_amount=74.99,
			current_quantity=1,
			shipping_total=419.22,
			order_total=494.21,
			financial_status="PENDING",
			payment_transactions=[self._transaction(manual=True)],
		)

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(frappe.db.get_value("Sales Order", order.erpnext_sales_order, "grand_total"), 494.21)
		self.assertEqual({doctype: frappe.db.count(doctype) for doctype in doctypes}, before)

	def test_shopify_prepaid_order_with_shipping_matches_shopify(self):
		_account, order, result = self._sync_shopify(
			transaction_amount=36, financial_status="PAID", shipping_total=50, order_total=86
		)

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(frappe.db.get_value("Sales Order", order.erpnext_sales_order, "grand_total"), 86)

	def test_shopify_shipping_discount_is_already_net_in_shipping_total(self):
		"""currentShippingPriceSet is after shipping discounts: the row carries exactly that amount."""
		_account, order, result = self._sync_shopify(transaction_amount=36, shipping_total=12.5, order_total=48.5)

		self.assertTrue(result["ok"], result.get("error"))
		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
		self.assertEqual(sales_order.taxes[0].tax_amount, 12.5)
		self.assertEqual(sales_order.grand_total, 48.5)

	def test_shopify_product_and_order_level_discounts_are_already_net_in_the_lines(self):
		cases = {
			"product discount": {"transaction_amount": 30, "original_unit_price": 25, "order_total": 30},
			"order discount": {"transaction_amount": 27.5, "original_unit_price": 25, "order_total": 77.5},
		}
		account = self._make_shopify_account(self._income_account())
		for label, kwargs in cases.items():
			with self.subTest(case=label):
				_account, order, result = self._sync_shopify(
					account=account, shipping_total=50 if label == "order discount" else 0, **kwargs
				)

				self.assertTrue(result["ok"], result.get("error"))
				self.assertEqual(
					frappe.db.get_value("Sales Order", order.erpnext_sales_order, "grand_total"),
					kwargs["order_total"],
				)

	def test_shopify_orders_with_taxes_or_duties_are_blocked_and_create_nothing(self):
		cases = {
			"tax exclusive": {"transaction_amount": 36, "total_taxes": 5.04, "order_total": 41.04},
			"tax inclusive": {"transaction_amount": 36, "total_taxes": 4.4, "order_total": 36},
			"shipping tax": {"transaction_amount": 36, "shipping_total": 10, "total_taxes": 2, "order_total": 48},
			"duties": {"transaction_amount": 36, "duties_total": 3, "order_total": 39},
		}
		account = self._make_shopify_account(self._income_account())
		for label, kwargs in cases.items():
			with self.subTest(case=label):
				before = frappe.db.count("Sales Order")

				_account, order, result = self._sync_shopify(account=account, **kwargs)

				self.assertFalse(result["ok"])
				self.assertIn("Taxes and duties are not synced to ERPNext yet", result["error"])
				self.assertFalse(order.erpnext_sales_order)
				self.assertEqual(frappe.db.count("Sales Order"), before)

	def test_legacy_synced_order_with_taxes_stays_synced_on_form_load_and_resync(self):
		_account, order, result = self._sync_shopify(transaction_amount=36)
		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(frappe.db.get_value("Sales Order", order.erpnext_sales_order, "docstatus"), 1)
		# Synced before the tax block existed: the order now carries tax.
		order.db_set({"total_taxes": 5.04, "order_total": 41.04})

		self.assertEqual(get_current_sync_status(order.name), "Synced")
		again = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(again["ok"], again.get("error"))
		self.assertEqual(again["reconciliation"]["outcome"], "NO_ACTION")
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertNotIn("adjust", cstr(order.last_sync_error))

	def test_taxes_appearing_on_a_submitted_order_do_not_produce_the_adjust_manually_blocker(self):
		_account, order, result = self._sync_shopify(transaction_amount=36, shipping_total=0)
		self.assertTrue(result["ok"], result.get("error"))
		order.db_set({"total_taxes": 5, "duties_total": 2})

		again = sync_sales_order(order.name)

		self.assertNotIn("Taxes and duties are not synced", cstr(again.get("error")))
		self.assertNotIn("manually", cstr(again.get("error")))
		self.assertEqual(
			build_sales_order_snapshot(order, for_submitted_order=True)["items"][0]["qty"], 2
		)
		with self.assertRaises(SyncBlocker):
			build_sales_order_snapshot(order)

	def test_draft_sales_order_with_taxes_is_still_blocked(self):
		_account, order, result = self._sync_shopify(transaction_amount=36)
		self.assertTrue(result["ok"], result.get("error"))
		frappe.db.set_value("Sales Order", order.erpnext_sales_order, "docstatus", 0, update_modified=False)
		order.db_set({"total_taxes": 5.04, "order_total": 41.04})

		self.assertEqual(get_current_sync_status(order.name), "Modified After Sync")
		again = sync_sales_order(order.name)

		self.assertFalse(again["ok"])
		self.assertIn("Taxes and duties are not synced to ERPNext yet", again["error"])

	def test_shopify_shipping_sync_is_idempotent_for_submitted_and_draft_orders(self):
		_account, order, first = self._sync_shopify(transaction_amount=36, shipping_total=50, order_total=86)
		first_hash = order.last_synced_hash

		second = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(second["ok"], second.get("error"))
		self.assertEqual(order.last_synced_hash, first_hash)
		self.assertEqual(len(self._shipping_rows(first["sales_order"])), 1)

		# A legacy Draft is re-applied in place: the same input keeps one row, a changed charge replaces it.
		frappe.db.set_value("Sales Order", first["sales_order"], "docstatus", 0, update_modified=False)
		third = sync_sales_order(order.name)
		order.reload()
		self.assertTrue(third["ok"], third.get("error"))
		self.assertEqual(order.last_synced_hash, first_hash)
		self.assertEqual(len(self._shipping_rows(first["sales_order"])), 1)

		order.shipping_total = 60
		order.order_total = 96
		order.save()
		fourth = sync_sales_order(order.name)
		order.reload()
		self.assertTrue(fourth["ok"], fourth.get("error"))
		self.assertNotEqual(order.last_synced_hash, first_hash)
		rows = self._shipping_rows(first["sales_order"])
		self.assertEqual([row.tax_amount for row in rows], [60])
		self.assertEqual(frappe.db.get_value("Sales Order", first["sales_order"], "grand_total"), 96)

	def test_changed_shipping_on_a_submitted_order_needs_attention_and_changes_nothing(self):
		_account, order, first = self._sync_shopify(
			transaction_amount=74.99, current_quantity=1, shipping_total=419.22, order_total=494.21
		)
		baseline = (order.last_synced_hash, order.last_synced_snapshot)
		before = frappe.get_doc("Sales Order", first["sales_order"]).as_dict()

		# e.g. a shipping refund: Shopify's current* fields drop the charge
		order.shipping_total = 0
		order.order_total = 74.99
		order.save()
		result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertIn("shipping", result["error"])
		self.assertIn("shipping", [change["field"] for change in result["reconciliation"]["changes"]])
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertEqual((order.last_synced_hash, order.last_synced_snapshot), baseline)
		after = frappe.get_doc("Sales Order", first["sales_order"])
		self.assertEqual((after.grand_total, len(after.taxes), after.modified), (494.21, 1, before.modified))

	# --- Shopify full refund before fulfillment (shipping charge on the Sales Order) -----

	def _synced_shipping_order(self):
		account, order, first = self._sync_shopify(
			transaction_amount=74.99, current_quantity=1, shipping_total=419.22, order_total=494.21
		)
		self.assertTrue(first["ok"], first.get("error"))
		return account, order, first["sales_order"]

	def _fully_refund(self, order, fulfillment_status="FULFILLMENT_NOT_REQUIRED"):
		"""Shopify's state after a full refund: not cancelled, current quantities and totals all zero."""
		order.external_financial_status = "REFUNDED"
		order.external_fulfillment_status = fulfillment_status
		order.items[0].current_quantity = 0
		order.items[0].unfulfilled_quantity = 0
		order.shipping_total = 0
		order.order_total = 0
		order.refunds = frappe.as_json([{"id": f"refund-{self.suffix}"}])
		order.save()

	def test_fully_refunded_order_with_a_shipping_charge_cancels_the_sales_order(self):
		_account, order, sales_order = self._synced_shipping_order()
		self._fully_refund(order)
		refunds = order.refunds
		orders_before = frappe.db.count("Sales Order")

		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["reconciliation"]["outcome"], "CANCEL_ORDER")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "docstatus"), 2)
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(order.refunds, refunds)
		self.assertEqual(order.erpnext_sales_order, sales_order)
		self.assertEqual(frappe.db.count("Sales Order"), orders_before)
		self.assertEqual(get_current_sync_status(order.name), "Synced")

		again = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(again["ok"], again.get("error"))
		self.assertEqual(again["reconciliation"]["outcome"], "NO_ACTION")
		self.assertNotIn("executed_action", again)
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(frappe.db.count("Sales Order"), orders_before)

	def test_fully_refunded_unfulfilled_status_also_cancels(self):
		_account, order, sales_order = self._synced_shipping_order()
		self._fully_refund(order, "UNFULFILLED")

		result = sync_sales_order(order.name)

		self.assertEqual(result["reconciliation"]["outcome"], "CANCEL_ORDER")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "docstatus"), 2)

	def test_fully_refunded_closed_order_is_reopened_and_cancelled(self):
		_account, order, sales_order = self._synced_shipping_order()
		self._fully_refund(order)
		frappe.db.set_value("Sales Order", sales_order, "status", "Closed", update_modified=False)

		result = sync_sales_order(order.name)

		self.assertEqual(result["reconciliation"]["outcome"], "CANCEL_ORDER")
		self.assertTrue(result["reconciliation"]["reopen"])
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "docstatus"), 2)

	def test_fully_refunded_order_that_was_delivered_is_a_compensating_transaction(self):
		_account, order, sales_order = self._synced_shipping_order()
		self._set_first_item_fields(order, delivered_qty=1)
		self._fully_refund(order)

		result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(result["reconciliation"]["outcome"], "UNSUPPORTED_COMPENSATING_TRANSACTION")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "docstatus"), 1)
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")

	def test_fully_refunded_order_that_was_billed_or_has_an_advance_is_not_cancelled(self):
		# Billing is execution, so the refund rule applies first; an advance is not.
		cases = (
			(
				"billed",
				"UNSUPPORTED_COMPENSATING_TRANSACTION",
				lambda so, item: frappe.db.set_value("Sales Order Item", item, "billed_amt", 10),
			),
			(
				"advance",
				"NEEDS_ATTENTION",
				lambda so, item: frappe.db.set_value("Sales Order", so, "advance_paid", 50),
			),
		)
		for label, outcome, change in cases:
			with self.subTest(label):
				self.setUp()
				_account, order, sales_order = self._synced_shipping_order()
				change(sales_order, frappe.get_doc("Sales Order", sales_order).items[0].name)
				self._fully_refund(order)

				result = sync_sales_order(order.name)

				self.assertFalse(result["ok"])
				self.assertEqual(result["reconciliation"]["outcome"], outcome)
				self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "docstatus"), 1)

	def test_fully_refunded_order_shopify_shows_fulfilled_keeps_the_shipping_attention(self):
		_account, order, sales_order = self._synced_shipping_order()
		self._fully_refund(order, "FULFILLED")
		modified = frappe.db.get_value("Sales Order", sales_order, "modified")

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertIn("shipping", result["error"])
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "modified"), modified)

	def test_partial_refund_of_shipping_only_stays_attention(self):
		_account, order, sales_order = self._synced_shipping_order()
		order.external_financial_status = "PARTIALLY_REFUNDED"
		order.shipping_total = 0
		order.order_total = 74.99
		order.refunds = frappe.as_json([{"id": f"refund-{self.suffix}"}])
		order.save()

		result = sync_sales_order(order.name)

		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "docstatus"), 1)

	# --- Merchant-readable blocker messages with real linked documents -------------------

	def _deliver(self, sales_order_name, *, submit=True):
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		delivery_note = make_delivery_note(sales_order_name)
		delivery_note.insert()
		if submit:
			delivery_note.submit()
		return delivery_note

	# --- Auto-cancel is held while the Sales Order has unfinished (draft) documents -----

	def _draft_sales_invoice(self, sales_order_name):
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		invoice = make_sales_invoice(sales_order_name)
		invoice.insert()
		return invoice

	def _draft_payment_entry(self, sales_order_name):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		payment_entry = get_payment_entry("Sales Order", sales_order_name)
		payment_entry.reference_no = f"draft-{self.suffix}"
		payment_entry.reference_date = nowdate()
		payment_entry.insert()
		return payment_entry

	def _assert_cancel_held(self, order, sales_order, result, *named):
		order.reload()
		self.assertFalse(result["ok"])
		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertNotIn("executed_action", result)
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "docstatus"), 1)
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertIn("The Sales Order was not cancelled", order.last_sync_error)
		self.assertLess(len(order.last_sync_error), 500)
		for name in named:
			self.assertIn(name, order.last_sync_error)
		self.assertEqual(
			frappe.db.count("Marketplace Sync Log", {"marketplace_order": order.name}),
			self._sync_log_count + 1,
		)
		# Opening the form re-plans from the same evidence and must keep the draft message.
		self.assertEqual(get_current_sync_status(order.name), "Modified After Sync")
		order.reload()
		self.assertIn("The Sales Order was not cancelled", order.last_sync_error)

	def _held_refund(self, create_draft):
		_account, order, sales_order = self._synced_shipping_order()
		draft = create_draft(sales_order)
		self._fully_refund(order)
		self._sync_log_count = frappe.db.count("Marketplace Sync Log", {"marketplace_order": order.name})
		result = sync_sales_order(order.name)
		return order, sales_order, draft, result

	def test_full_refund_is_held_by_a_draft_delivery_note(self):
		order, sales_order, draft, result = self._held_refund(lambda so: self._deliver(so, submit=False))
		self._assert_cancel_held(order, sales_order, result, f"Delivery Note {draft.name}")

	def test_full_refund_is_held_by_a_draft_sales_invoice(self):
		order, sales_order, draft, result = self._held_refund(self._draft_sales_invoice)
		self._assert_cancel_held(order, sales_order, result, f"Sales Invoice {draft.name}")

	def test_full_refund_is_held_by_a_draft_payment_entry(self):
		order, sales_order, draft, result = self._held_refund(self._draft_payment_entry)
		self._assert_cancel_held(order, sales_order, result, f"Payment Entry {draft.name}")

	def test_held_cancel_runs_once_the_draft_is_deleted_and_stays_idempotent(self):
		order, sales_order, draft, result = self._held_refund(lambda so: self._deliver(so, submit=False))
		self._assert_cancel_held(order, sales_order, result, draft.name)

		frappe.delete_doc("Delivery Note", draft.name)
		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["reconciliation"]["outcome"], "CANCEL_ORDER")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "docstatus"), 2)
		self.assertEqual(order.erpnext_sync_status, "Synced")
		again = sync_sales_order(order.name)
		self.assertEqual(again["reconciliation"]["outcome"], "NO_ACTION")

	def test_a_cancelled_draft_document_no_longer_holds_the_cancel(self):
		_account, order, sales_order = self._synced_shipping_order()
		invoice = self._draft_sales_invoice(sales_order)
		frappe.delete_doc("Sales Invoice", invoice.name)
		self._fully_refund(order)

		result = sync_sales_order(order.name)

		self.assertEqual(result["reconciliation"]["outcome"], "CANCEL_ORDER")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "docstatus"), 2)

	def test_several_drafts_are_named_per_type_and_capped(self):
		_account, order, sales_order = self._synced_shipping_order()
		notes = [self._deliver(sales_order, submit=False) for _ in range(5)]
		invoice = self._draft_sales_invoice(sales_order)
		self._fully_refund(order)

		result = sync_sales_order(order.name)
		order.reload()

		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertIn(", and more", order.last_sync_error)
		self.assertIn(f"Sales Invoice {invoice.name}", order.last_sync_error)
		self.assertIn(notes[0].name, order.last_sync_error)
		self.assertNotIn(notes[4].name, order.last_sync_error)
		self.assertLess(len(order.last_sync_error), 500)

	def test_plain_marketplace_cancellation_is_held_by_a_draft_delivery_note(self):
		_account, order, first = self._sync_shopify(transaction_amount=36, current_quantity=2)
		draft = self._deliver(first["sales_order"], submit=False)
		order.cancelled_at = nowdate()
		order.save()
		self._sync_log_count = frappe.db.count("Marketplace Sync Log", {"marketplace_order": order.name})

		result = sync_sales_order(order.name)

		self._assert_cancel_held(order, first["sales_order"], result, f"Delivery Note {draft.name}")

	def test_talabat_cancelled_after_pickup_is_held_by_a_draft_delivery_note(self):
		order = self._make_order()
		sales_order = self._sync_and_set_item_fields(order)
		draft = self._deliver(sales_order.name, submit=False)
		order.external_status = "CANCELLED"
		order.post_picked_up = 1
		order.save()
		self._sync_log_count = frappe.db.count("Marketplace Sync Log", {"marketplace_order": order.name})

		result = sync_sales_order(order.name)

		self._assert_cancel_held(order, sales_order.name, result, f"Delivery Note {draft.name}")

	def test_draft_documents_do_not_hold_close_remainder_or_update(self):
		order = self._make_order()
		sales_order = self._sync_and_set_item_fields(order, delivered_qty=1)
		self._deliver(sales_order.name, submit=False)
		order.external_status = "CANCELLED"
		order.save()

		result = sync_sales_order(order.name)

		self.assertEqual(result["reconciliation"]["outcome"], "CLOSE_REMAINDER")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order.name, "status"), "Closed")

		updated = self._synced_talabat_order()
		self._deliver(updated.erpnext_sales_order, submit=False)
		self._change_talabat_line(updated, 3, 1500)

		result = sync_sales_order(updated.name)

		self.assertEqual(result["reconciliation"]["outcome"], "UPDATE_SUBMITTED_ORDER")
		self.assertTrue(result["ok"], result.get("error"))

	def test_blocked_refund_names_the_real_delivery_note_and_sales_invoice(self):
		from erpnext.controllers.sales_and_purchase_return import make_return_doc
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

		account, order, first = self._sync_shopify(transaction_amount=36, current_quantity=2)
		sales_order = first["sales_order"]
		self.assertEqual(
			read_erp_state(frappe.get_doc("Sales Order", sales_order)).get("linked_documents"), None
		)

		# A cancelled Delivery Note and another order's Delivery Note must not be named.
		self._deliver(sales_order).cancel()
		_other_account, _other_order, other = self._sync_shopify(account=account, transaction_amount=36)
		self._deliver(other["sales_order"])
		delivery_note = self._deliver(sales_order)
		# A return against it is a compensating document, not the delivery itself.
		return_note = make_return_doc("Delivery Note", delivery_note.name)
		return_note.items[0].qty = -1
		return_note.insert()
		return_note.submit()
		self.assertEqual(return_note.items[0].against_sales_order, sales_order)
		# An invoice made from the Delivery Note still points at the Sales Order.
		invoice = make_sales_invoice(delivery_note.name)
		invoice.insert()
		invoice.submit()

		linked = read_erp_state(frappe.get_doc("Sales Order", sales_order))["linked_documents"]

		self.assertEqual(linked, {"Delivery Note": [delivery_note.name], "Sales Invoice": [invoice.name]})

		order.external_financial_status = "PARTIALLY_REFUNDED"
		order.refunds = frappe.as_json([{"id": f"refund-{self.suffix}"}])
		order.save()
		result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(result["reconciliation"]["outcome"], "UNSUPPORTED_COMPENSATING_TRANSACTION")
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertIn(delivery_note.name, order.last_sync_error)
		self.assertIn(invoice.name, order.last_sync_error)
		self.assertIn("compensating", order.last_sync_error)
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "docstatus"), 1)
		rows = {row["reference_name"]: row for row in get_attention_items({"account": account.name})}
		self.assertEqual(rows[order.name]["problem_type"], "Modified After Sync")
		self.assertIn(delivery_note.name, rows[order.name]["explanation"])
		self.assertIn("review the order manually", rows[order.name]["explanation"])

	def test_cancelled_order_with_a_real_sales_invoice_names_it(self):
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		_account, order, first = self._sync_shopify(transaction_amount=36, current_quantity=2)
		invoice = make_sales_invoice(first["sales_order"])
		invoice.insert()
		invoice.submit()
		order.cancelled_at = nowdate()
		order.save()

		result = sync_sales_order(order.name)
		order.reload()

		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertIn(f"Sales Invoice {invoice.name} already bills it", order.last_sync_error)
		self.assertEqual(frappe.db.get_value("Sales Order", first["sales_order"], "docstatus"), 1)

	def test_cancelled_order_fully_delivered_by_a_real_delivery_note_names_it(self):
		_account, order, first = self._sync_shopify(transaction_amount=36, current_quantity=2)
		delivery_note = self._deliver(first["sales_order"])
		order.cancelled_at = nowdate()
		order.save()

		result = sync_sales_order(order.name)
		order.reload()

		self.assertEqual(result["reconciliation"]["outcome"], "UNSUPPORTED_COMPENSATING_TRANSACTION")
		self.assertIn(
			f"Delivery Note {delivery_note.name} already delivered all of it", order.last_sync_error
		)
		self.assertEqual(frappe.db.get_value("Sales Order", first["sales_order"], "docstatus"), 1)

	def test_cancelled_order_with_a_shipping_baseline_cancels_through_the_stored_snapshot(self):
		_account, order, first = self._sync_shopify(
			transaction_amount=74.99, current_quantity=1, shipping_total=419.22, order_total=494.21
		)
		# Shopify's current* fields no longer carry the charge, but the stored baseline does.
		order.cancelled_at = nowdate()
		order.shipping_total = 0
		order.order_total = 0
		order.save()

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["reconciliation"]["outcome"], "CANCEL_ORDER")
		self.assertEqual(frappe.db.get_value("Sales Order", first["sales_order"], "docstatus"), 2)

	def test_shipping_removed_from_a_draft_sales_order_removes_the_row(self):
		_account, order, first = self._sync_shopify(transaction_amount=36, shipping_total=50, order_total=86)
		frappe.db.set_value("Sales Order", first["sales_order"], "docstatus", 0, update_modified=False)
		order.shipping_total = 0
		order.order_total = 36
		order.save()

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(len(self._shipping_rows(first["sales_order"])), 0)
		self.assertEqual(frappe.db.get_value("Sales Order", first["sales_order"], "grand_total"), 36)

	def test_refund_after_delivery_with_a_shipping_baseline_is_a_compensating_transaction(self):
		_account, order, first = self._sync_shopify(
			transaction_amount=74.99, current_quantity=1, shipping_total=419.22, order_total=494.21
		)
		self._set_first_item_fields(order, delivered_qty=1)
		order.external_financial_status = "PARTIALLY_REFUNDED"
		order.shipping_total = 0
		order.order_total = 74.99
		order.refunds = frappe.as_json([{"id": f"refund-{self.suffix}"}])
		order.save()

		result = sync_sales_order(order.name)

		self.assertEqual(result["reconciliation"]["outcome"], "UNSUPPORTED_COMPENSATING_TRANSACTION")
		self.assertEqual(frappe.db.get_value("Sales Order", first["sales_order"], "docstatus"), 1)

	def test_existing_submitted_order_synced_without_shipping_needs_attention_and_is_not_mutated(self):
		"""An order synced before shipping was carried has a baseline without the key."""
		account = self._make_shopify_account(self._income_account())
		order, _mapping = self._make_shopify_order(account, transaction_amount=74.99, current_quantity=1)
		first = sync_sales_order(order.name)
		order.reload()
		self.assertTrue(first["ok"], first.get("error"))
		# Shopify's shipping was there all along; the Sales Order and its baseline never carried it.
		order.shipping_total = 419.22
		order.order_total = 494.21
		order.save()
		baseline = (order.last_synced_hash, order.last_synced_snapshot)
		modified = frappe.db.get_value("Sales Order", first["sales_order"], "modified")

		result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(result["reconciliation"]["outcome"], "NEEDS_ATTENTION")
		self.assertIn("cancel and amend", result["error"])
		self.assertEqual((order.last_synced_hash, order.last_synced_snapshot), baseline)
		after = frappe.get_doc("Sales Order", first["sales_order"])
		self.assertEqual((after.grand_total, len(after.taxes), after.modified), (74.99, 0, modified))
		self.assertEqual(get_current_sync_status(order.name), "Modified After Sync")

	def test_shopify_shipping_without_a_shipping_account_blocks_with_an_actionable_message(self):
		before = frappe.db.count("Sales Order")

		_account, order, result = self._sync_shopify(with_account=False, shipping_total=50, order_total=86)

		self.assertFalse(result["ok"])
		self.assertIn("Set the ERPNext Shipping Income Account", result["error"])
		self.assertFalse(order.erpnext_sales_order)
		self.assertEqual(frappe.db.count("Sales Order"), before)

	def test_shopify_shipping_account_that_became_unusable_blocks_the_sync(self):
		account = self._make_shopify_account(self._income_account())
		order, _mapping = self._make_shopify_order(account, shipping_total=50)
		frappe.db.set_value("Account", account.erpnext_shipping_account, "disabled", 1)

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertIn("is disabled", result["error"])

	def test_snapshot_preview_with_shipping_but_no_account_raises_only_the_sync_blocker(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, shipping_total=50)

		with self.assertRaisesRegex(SyncBlocker, "Shipping Income Account"):
			build_sales_order_snapshot(order)

	def test_linked_order_whose_shipping_account_was_removed_reads_as_modified_after_sync(self):
		account, order, result = self._sync_shopify(shipping_total=50, order_total=86)
		self.assertTrue(result["ok"], result.get("error"))
		account.reload()
		account.erpnext_shipping_account = None
		account.save()

		status = get_current_sync_status(order.name)
		order.reload()

		self.assertEqual(status, "Modified After Sync")
		self.assertIn("Shipping Income Account", order.last_sync_error)

	def test_shopify_total_that_disagrees_with_items_and_shipping_blocks_with_the_numbers(self):
		before = frappe.db.count("Sales Order")

		_account, order, result = self._sync_shopify(transaction_amount=36, shipping_total=50, order_total=90)

		self.assertFalse(result["ok"])
		self.assertIn("Shopify total 90.0 does not match the Sales Order total 86.0", result["error"])
		self.assertIn("items 36.0 + shipping 50.0", result["error"])
		self.assertFalse(order.erpnext_sales_order)
		self.assertEqual(frappe.db.count("Sales Order"), before)

	def test_shopify_total_within_one_currency_unit_passes(self):
		account = self._make_shopify_account(self._income_account())
		for order_total in (86.01, 85.99):
			with self.subTest(order_total=order_total):
				_account, _order, result = self._sync_shopify(
					account=account, transaction_amount=36, shipping_total=50, order_total=order_total
				)
				self.assertTrue(result["ok"], result.get("error"))
		_account, _order, result = self._sync_shopify(
			account=account, transaction_amount=36, shipping_total=50, order_total=86.02
		)
		self.assertFalse(result["ok"])

	def test_shopify_order_without_a_stored_total_blocks_unless_it_is_a_free_order(self):
		account = self._make_shopify_account(self._income_account())
		# Items worth money but no recorded Shopify total (a row imported before totals were stored).
		for shipping_total in (0, 50):
			with self.subTest(shipping_total=shipping_total):
				before = frappe.db.count("Sales Order")
				_account, order, result = self._sync_shopify(
					account=account, transaction_amount=36, shipping_total=shipping_total, order_total=0
				)
				self.assertFalse(result["ok"])
				self.assertIn("Shopify's order total was not recorded", result["error"])
				self.assertIn("Refresh from Shopify", result["error"])
				self.assertNotIn("does not match", result["error"])
				self.assertFalse(order.erpnext_sales_order)
				self.assertEqual(frappe.db.count("Sales Order"), before)

		# A genuinely free order (no shipping, every item free) has nothing to verify.
		_account, _order, result = self._sync_shopify(account=account, transaction_amount=0, order_total=0)
		self.assertTrue(result["ok"], result.get("error"))

	def test_shopify_shipping_on_an_all_free_order_blocks_instead_of_losing_the_charge(self):
		_account, order, result = self._sync_shopify(transaction_amount=0, shipping_total=50, order_total=50)

		self.assertFalse(result["ok"])
		self.assertIn("all of its items are free", result["error"])
		self.assertFalse(order.erpnext_sales_order)

	def test_talabat_and_noon_snapshots_and_hashes_ignore_shipping_fields(self):
		talabat_order = self._make_order()
		first = sync_sales_order(talabat_order.name)
		talabat_order.reload()
		self.assertTrue(first["ok"], first.get("error"))
		talabat_order.db_set({"shipping_total": 50, "total_taxes": 5, "duties_total": 1})
		snapshot = build_sales_order_snapshot(talabat_order)
		self.assertNotIn("shipping", snapshot)
		self.assertEqual(snapshot_hash(snapshot), talabat_order.last_synced_hash)
		self.assertEqual(len(self._shipping_rows(first["sales_order"])), 0)

		noon_account = self._make_noon_account()
		noon_order, _mapping = self._make_noon_order(noon_account)
		noon_result = sync_sales_order(noon_order.name)
		noon_order.reload()
		self.assertTrue(noon_result["ok"], noon_result.get("error"))
		noon_order.db_set({"shipping_total": 50, "total_taxes": 5})
		noon_snapshot = build_sales_order_snapshot(noon_order)
		self.assertNotIn("shipping", noon_snapshot)
		self.assertEqual(snapshot_hash(noon_snapshot), noon_order.last_synced_hash)


	# -- retry after a blocker or error (Finding 7) ----------------------

	def _sync_logs(self, order):
		return frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name},
			fields=["name", "status", "trigger_source", "traceback"],
			order_by="creation asc, name asc",
		)

	def _resync_latest(self, order):
		"""What the list/form retry does: resync the order's latest sync log."""
		latest = self._sync_logs(order)[-1]
		return frappe.get_doc("Marketplace Sync Log", latest.name).resync()

	def _sales_orders_of(self, order):
		return frappe.get_all("Sales Order", filters={"custom_marketplace_order": order.name}, pluck="name")

	def _attention_types(self, account):
		return {row["problem_type"] for row in get_attention_items({"account": account.name})}

	def _make_variant_mapping(self, account, variant_gid, **overrides):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": account.name,
				"item": self.item.name,
				"external_reference": variant_gid,
				"external_variant_id": variant_gid,
				"sales_uom": "Nos",
				**overrides,
			}
		).insert()

	def _unmapped_shopify_order(self):
		variant_gid = f"gid://shopify/ProductVariant/{frappe.generate_hash(length=12)}"
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, variant_gid=variant_gid, create_mapping=False)
		return account, order, variant_gid

	def test_missing_mapping_blocker_is_retried_until_the_mapping_exists_then_recovers_once(self):
		account, order, variant_gid = self._unmapped_shopify_order()

		first = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(first["ok"])
		self.assertEqual(order.erpnext_sync_status, "Sync Error")
		self.assertEqual([log.status for log in self._sync_logs(order)], ["Invalid"])
		rows = get_attention_items({"account": account.name})
		by_type = {row["problem_type"]: row for row in rows}
		self.assertEqual(set(by_type), {SYNC_ERROR, UNMAPPED_ITEMS})
		self.assertEqual(by_type[SYNC_ERROR]["next_action"], NEXT_RETRY)
		self.assertEqual(by_type[UNMAPPED_ITEMS]["next_action_reference"], variant_gid)

		# Retrying without fixing anything stays blocked: one new log per attempt.
		self.assertFalse(self._resync_latest(order)["ok"])
		self.assertFalse(self._resync_latest(order)["ok"])
		order.reload()
		self.assertEqual(order.erpnext_sync_status, "Sync Error")
		self.assertEqual(len(self._sync_logs(order)), 3)
		self.assertFalse(self._sales_orders_of(order))

		self._make_variant_mapping(account, variant_gid)
		recovered = self._resync_latest(order)
		order.reload()

		self.assertTrue(recovered["ok"], recovered.get("error"))
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertFalse(order.last_sync_error)
		self.assertTrue(order.erpnext_sales_order)
		self.assertEqual(self._attention_types(account), set())
		self.assertEqual(self._sales_orders_of(order), [order.erpnext_sales_order])
		self.assertEqual(self._sync_logs(order)[-1].status, "Success")

		again = self._resync_latest(order)
		order.reload()

		self.assertTrue(again["ok"], again.get("error"))
		self.assertEqual(self._sales_orders_of(order), [order.erpnext_sales_order])

	def test_disabled_mapping_blocker_recovers_when_re_enabled(self):
		account, order, variant_gid = self._unmapped_shopify_order()
		mapping = self._make_variant_mapping(account, variant_gid, enabled=0)

		self.assertFalse(sync_sales_order(order.name)["ok"])
		self.assertIn(UNMAPPED_ITEMS, self._attention_types(account))

		mapping.enabled = 1
		mapping.save()
		result = self._resync_latest(order)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(self._attention_types(account), set())
		self.assertEqual(len(self._sales_orders_of(order)), 1)

	def test_unusable_selling_price_list_blocker_recovers_when_fixed(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)
		frappe.db.set_value("Price List", self.price_list, "enabled", 0)
		try:
			blocked = sync_sales_order(order.name)
			order.reload()
			self.assertFalse(blocked["ok"])
			self.assertEqual(order.erpnext_sync_status, "Sync Error")
			self.assertFalse(self._sales_orders_of(order))
		finally:
			frappe.db.set_value("Price List", self.price_list, "enabled", 1)

		result = self._resync_latest(order)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(len(self._sales_orders_of(order)), 1)

	def test_unexpected_exception_is_logged_and_the_retry_leaves_exactly_one_sales_order(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)

		with patch.object(SalesOrder, "submit", side_effect=RuntimeError("submit exploded")):
			failed = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(failed["ok"])
		self.assertEqual(order.erpnext_sync_status, "Sync Error")
		self.assertFalse(order.erpnext_sales_order)
		log = self._sync_logs(order)[-1]
		self.assertEqual(log.status, "Error")
		self.assertIn("submit exploded", log.traceback)
		self.assertIn(SYNC_ERROR, self._attention_types(account))

		result = self._resync_latest(order)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(self._sales_orders_of(order), [order.erpnext_sales_order])
		self.assertEqual(frappe.db.get_value("Sales Order", order.erpnext_sales_order, "docstatus"), 1)

	def test_permanent_accounting_blocker_stays_actionable_across_retries(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, total_taxes=5)
		doctypes = ("Sales Invoice", "Payment Entry", "Journal Entry", "GL Entry")
		before = {doctype: frappe.db.count(doctype) for doctype in doctypes}

		results = [sync_sales_order(order.name)]
		results += [self._resync_latest(order), self._resync_latest(order)]
		order.reload()

		self.assertTrue(all(not result["ok"] for result in results))
		self.assertTrue(all("taxes" in result["error"] for result in results))
		self.assertEqual(order.erpnext_sync_status, "Sync Error")
		self.assertEqual(len(self._sync_logs(order)), 3)
		self.assertEqual([log.status for log in self._sync_logs(order)], ["Invalid"] * 3)
		self.assertFalse(self._sales_orders_of(order))
		self.assertFalse(order.erpnext_sales_order)
		self.assertEqual({doctype: frappe.db.count(doctype) for doctype in doctypes}, before)
		rows = [r for r in get_attention_items({"account": account.name}) if r["problem_type"] == SYNC_ERROR]
		self.assertEqual([row["next_action"] for row in rows], [NEXT_RETRY])

	def test_manual_resync_appends_exactly_one_log_row_through_the_real_sync(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account)
		self.assertTrue(sync_sales_order(order.name)["ok"])
		before = self._sync_logs(order)

		result = self._resync_latest(order)
		after = self._sync_logs(order)

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(len(after), len(before) + 1)
		self.assertEqual(before[-1].trigger_source, "Manual")
		self.assertEqual(after[-1].trigger_source, "Manual Resync")

	# -- Shopify price stability (Finding 8.4) -----------------------------

	def _change_item_price_and_add_pricing_rule(self):
		"""Make ERPNext's pricing differ from what the order was synced at."""
		item_price = frappe.get_doc(
			"Item Price", {"item_code": self.item.name, "price_list": self.price_list}
		)
		item_price.price_list_rate = 99
		item_price.save()
		rule = self._make_pricing_rule()
		self.addCleanup(frappe.delete_doc, "Pricing Rule", rule.name, force=True)

	def _assert_shopify_line(self, sales_order_name, rate):
		line = frappe.get_doc("Sales Order", sales_order_name).items[0]
		self.assertEqual(line.rate, rate)
		self.assertEqual(line.price_list_rate, rate)
		self.assertEqual(line.discount_percentage, 0)
		self.assertFalse(line.pricing_rules)

	def test_shopify_pricing_rule_existing_before_first_sync_does_not_discount_the_order(self):
		rule = self._make_pricing_rule()
		self.addCleanup(frappe.delete_doc, "Pricing Rule", rule.name, force=True)
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, transaction_amount=36.0, current_quantity=2)

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		self._assert_shopify_line(result["sales_order"], 18)
		self.assertEqual(frappe.db.get_value("Sales Order", result["sales_order"], "grand_total"), 36)

	def test_shopify_submitted_order_ignores_a_later_item_price_and_pricing_rule_change(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, transaction_amount=36.0, current_quantity=2)
		first = sync_sales_order(order.name)
		order.reload()
		synced_hash = order.last_synced_hash

		self._change_item_price_and_add_pricing_rule()
		second = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(second["ok"], second.get("error"))
		self.assertEqual(second["sales_order"], first["sales_order"])
		self.assertFalse(second["updated"])
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(order.last_synced_hash, synced_hash)
		self._assert_shopify_line(first["sales_order"], 18)

	def test_shopify_draft_order_follows_shopify_only_after_item_price_and_pricing_rule_change(self):
		account = self._make_shopify_account()
		order, _mapping = self._make_shopify_order(account, transaction_amount=36.0, current_quantity=2)
		first = sync_sales_order(order.name)
		frappe.db.set_value("Sales Order", first["sales_order"], "docstatus", 0, update_modified=False)

		self._change_item_price_and_add_pricing_rule()
		unchanged = sync_sales_order(order.name)

		self.assertTrue(unchanged["ok"], unchanged.get("error"))
		self.assertFalse(unchanged["updated"])
		self._assert_shopify_line(first["sales_order"], 18)

		order.reload()
		order.items[0].transaction_amount = 40
		order.order_total = 40
		order.save()
		changed = sync_sales_order(order.name)

		self.assertTrue(changed["ok"], changed.get("error"))
		self.assertTrue(changed["updated"])
		self._assert_shopify_line(first["sales_order"], 20)
		self.assertEqual(frappe.db.get_value("Sales Order", first["sales_order"], "grand_total"), 40)
		frappe.db.set_value("Sales Order", first["sales_order"], "docstatus", 1, update_modified=False)
