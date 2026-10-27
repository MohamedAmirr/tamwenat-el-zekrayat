# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Safety tests: Shopify order webhooks and Sales Order sync never move ERPNext stock.

A stock Item with real stock (a submitted Stock Entry) goes through the real webhook,
import and debounced sync path with only ``ShopifyClient.execute()`` mocked. Stock
only moves when a Delivery Note is submitted. Also covers webhook idempotency and
out-of-order delivery, and the inventory push reading its own warehouse Bin.

Cleanup: nothing here commits (``frappe.db.commit`` is patched where the code under
test would commit: the inventory worker and the webhook retry sweep), so the
class-level rollback of ``IntegrationTestCase`` removes everything, including the real
stock entries. Ids are unique per test (``frappe.generate_hash``).
"""

from unittest.mock import patch

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

import marketplace_integrations.integrations.shopify.orders as shopify_orders
import marketplace_integrations.integrations.shopify.webhooks as webhooks_module
from marketplace_integrations.integrations.shopify import inventory_sync
from marketplace_integrations.integrations.shopify.test_inventory_sync import (
	FETCH_LOCATIONS_TARGET,
	LOCATION,
	FakeShopify,
	_variant_gid,
)
from marketplace_integrations.integrations.shopify.test_orders import make_order_payload
from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.integrations.shopify.test_webhooks import make_connection, make_delivery
from marketplace_integrations.integrations.shopify.webhooks import (
	MAX_WEBHOOK_DELIVERY_ATTEMPTS,
	STUCK_RECEIVED_THRESHOLD_MINUTES,
	process_shopify_webhook,
	retry_errored_webhook_deliveries,
)
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.services.sales_order_sync import sync_sales_order
from marketplace_integrations.services.sales_order_sync_debounce import (
	_run_debounced_sync,
	_stuck_reconciliation_candidates,
	sweep_stuck_sales_order_reconciliations,
)

EXECUTE_TARGET = "marketplace_integrations.integrations.shopify.client.ShopifyClient.execute"
DEBOUNCE_SYNC_TARGET = "marketplace_integrations.services.sales_order_sync_debounce.sync_sales_order"

STOCK_QTY = 10
ORDER_QTY = 2
UNIT_PRICE = 20


class ShopifyStockOrderFixture:
	"""One Shopify account (Connection, Company, price list, warehouse, location), one
	stock Item with ``STOCK_QTY`` units in that warehouse, and a variant mapping."""

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
		self.item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"TEST-SAFETY-{self.suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}
		).insert()
		self.connection = make_connection(f"Safety {self.suffix}")
		self.account = self._make_account(self.connection, self.warehouse, LOCATION)
		self.variant_gid = _variant_gid()  # numeric: the inventory worker validates the GID shape
		self.mapping = self._make_mapping(self.account, self.variant_gid)
		self._created_accounts = [self.account.name]
		make_stock_entry(
			item_code=self.item.name,
			qty=STOCK_QTY,
			to_warehouse=self.warehouse,
			company=self.company,
			rate=5,
		)

	def tearDown(self):
		# Only cache entries live outside the transaction; the DB is rolled back per class.
		for account_name in self._created_accounts:
			frappe.cache().delete_value(inventory_sync._watermark_cache_key(account_name))

	def _make_account(self, connection, warehouse, location):
		account = make_shopify_account(f"Safety Account {frappe.generate_hash(length=8)}", connection.name)
		account.db_set("erpnext_company", self.company, update_modified=False)
		account.db_set("erpnext_selling_price_list", self.price_list, update_modified=False)
		account.db_set("warehouse", warehouse, update_modified=False)
		account.db_set("shopify_location_gid", location, update_modified=False)
		account.reload()
		return account

	def _make_mapping(self, account, variant_gid):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": account.name,
				"item": self.item.name,
				"external_reference": variant_gid,
				"external_variant_id": variant_gid,
				"external_product_id": "gid://shopify/Product/1",
				"sales_uom": "Nos",
			}
		).insert()

	def order_gid(self):
		return f"gid://shopify/Order/{self.suffix}-{frappe.generate_hash(length=6)}"

	def webhook_id(self, label="wh"):
		return f"{label}-{self.suffix}-{frappe.generate_hash(length=6)}"

	def payload(self, order_gid, updated_at="2026-09-27T10:05:00Z", quantity=ORDER_QTY, **overrides):
		"""A plain, syncable PAID order of ``quantity`` units: no tax, no shipping, no refunds."""
		total = f"{UNIT_PRICE * quantity:.2f}"
		overrides.setdefault("displayFinancialStatus", "PAID")
		overrides.setdefault("refunds", [])
		overrides.setdefault("processedAt", "2026-09-27T10:00:05Z")
		payload = make_order_payload(order_gid, updated_at, **overrides)
		line = payload["lineItems"]["nodes"][0]
		line.update(
			{
				"quantity": ORDER_QTY,
				"currentQuantity": quantity,
				"unfulfilledQuantity": quantity,
				"variant": {"id": self.variant_gid},
				"priceAfterAllDiscountsBeforeTaxesSet": {
					"shopMoney": {"amount": total, "currencyCode": "EGP"}
				},
			}
		)
		for field, amount in (
			("currentTotalTaxSet", "0.00"),
			("currentShippingPriceSet", "0.00"),
			("currentTotalPriceSet", total),
		):
			payload[field] = {"shopMoney": {"amount": amount, "currencyCode": "EGP"}}
		return payload

	def deliver(self, topic, webhook_id, order_gid, payload):
		with patch(EXECUTE_TARGET, return_value={"order": payload}):
			process_shopify_webhook(
				self.connection.name, topic, webhook_id, self.connection.shop_domain, order_gid=order_gid
			)

	def run_debounced_sync(self, order_gid):
		"""Run the real debounced-sync job body for the order; return sync_sales_order's results."""
		order = self.marketplace_order(order_gid)
		until = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")
		self.assertIsNotNone(until, "the webhook should have scheduled a debounced sync")
		results = []

		def capture(*args, **kwargs):
			results.append(sync_sales_order(*args, **kwargs))
			return results[-1]

		with patch(DEBOUNCE_SYNC_TARGET, side_effect=capture):
			_run_debounced_sync(order.name, until)
		return results

	def marketplace_order(self, order_gid):
		return frappe.get_doc(
			"Marketplace Order",
			{"marketplace_account": self.account.name, "external_order_id": order_gid},
		)

	def order_names(self, order_gid):
		return frappe.get_all(
			"Marketplace Order",
			{"marketplace_account": self.account.name, "external_order_id": order_gid},
			pluck="name",
		)

	def sales_orders(self, order_gid):
		return frappe.get_all(
			"Sales Order",
			{"custom_marketplace_order": ["in", self.order_names(order_gid) or [""]]},
			pluck="name",
		)

	def snapshot(self, warehouse=None):
		warehouse = warehouse or self.warehouse
		bin_values = (
			frappe.db.get_value(
				"Bin",
				{"item_code": self.item.name, "warehouse": warehouse},
				["actual_qty", "reserved_qty"],
				as_dict=True,
			)
			or {}
		)
		return {
			"sle": frappe.db.count(
				"Stock Ledger Entry", {"item_code": self.item.name, "warehouse": warehouse}
			),
			"stock_entry": frappe.db.count("Stock Entry"),
			"delivery_note": frappe.db.count("Delivery Note"),
			"gl_entry": frappe.db.count("GL Entry"),
			"reservation": frappe.db.count("Stock Reservation Entry", {"item_code": self.item.name}),
			"actual_qty": bin_values.get("actual_qty", 0),
			"reserved_qty": bin_values.get("reserved_qty", 0),
			"sales_order": frappe.db.count("Sales Order"),
			"marketplace_order": frappe.db.count(
				"Marketplace Order", {"marketplace_account": self.account.name}
			),
		}

	def assertNoStockMovement(self, before, after):
		for key in ("sle", "stock_entry", "delivery_note", "gl_entry", "reservation", "actual_qty"):
			self.assertEqual(after[key], before[key], f"{key} changed")

	def import_and_sync(self, order_gid=None):
		"""ORDERS_CREATE webhook then the debounced sync; returns (order_gid, sales_order_name)."""
		order_gid = order_gid or self.order_gid()
		self.deliver("ORDERS_CREATE", self.webhook_id(), order_gid, self.payload(order_gid))
		results = self.run_debounced_sync(order_gid)
		self.assertTrue(results[0]["ok"], results[0])
		return order_gid, self.marketplace_order(order_gid).erpnext_sales_order


class IntegrationTestShopifyOrdersMoveNoStock(ShopifyStockOrderFixture, IntegrationTestCase):
	def test_webhook_import_and_debounced_sync_of_a_stock_item_move_no_stock(self):
		before = self.snapshot()
		self.assertEqual(before["actual_qty"], STOCK_QTY)
		order_gid = self.order_gid()

		self.deliver("ORDERS_CREATE", self.webhook_id(), order_gid, self.payload(order_gid))
		results = self.run_debounced_sync(order_gid)

		after = self.snapshot()
		self.assertTrue(results[0]["ok"], results[0])
		self.assertNoStockMovement(before, after)
		self.assertEqual(after["marketplace_order"], before["marketplace_order"] + 1)
		sales_orders = self.sales_orders(order_gid)
		self.assertEqual(len(sales_orders), 1)
		self.assertEqual(frappe.db.get_value("Sales Order", sales_orders[0], "docstatus"), 1)
		# Documented ERPNext behavior: a submitted Sales Order reserves (not removes) stock.
		self.assertEqual(after["reserved_qty"], before["reserved_qty"] + ORDER_QTY)

	def test_delivery_note_from_the_synced_sales_order_deducts_stock_exactly_once(self):
		order_gid, sales_order = self.import_and_sync()
		before = self.snapshot()

		delivery_note = make_delivery_note(sales_order)
		delivery_note.insert()
		delivery_note.submit()

		after = self.snapshot()
		self.assertEqual(after["actual_qty"], STOCK_QTY - ORDER_QTY)
		self.assertEqual(after["delivery_note"], before["delivery_note"] + 1)
		entries = frappe.get_all(
			"Stock Ledger Entry",
			{"voucher_type": "Delivery Note", "voucher_no": delivery_note.name, "is_cancelled": 0},
			pluck="actual_qty",
		)
		self.assertEqual(entries, [-ORDER_QTY])
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "per_delivered"), 100)

		# Another webhook and sync for the same order adds no stock movement.
		self.deliver("ORDERS_UPDATED", self.webhook_id(), order_gid, self.payload(order_gid))
		self.run_debounced_sync(order_gid)

		self.assertEqual(self.snapshot(), after)

	def test_same_webhook_id_delivered_twice_imports_and_syncs_once(self):
		before = self.snapshot()
		order_gid = self.order_gid()
		webhook_id = self.webhook_id()
		payload = self.payload(order_gid)

		self.deliver("ORDERS_CREATE", webhook_id, order_gid, payload)
		self.deliver("ORDERS_CREATE", webhook_id, order_gid, payload)
		self.run_debounced_sync(order_gid)

		rows = frappe.get_all(
			"Shopify Webhook Delivery", {"webhook_id": webhook_id}, ["status", "attempt_count"]
		)
		self.assertEqual([(row.status, row.attempt_count) for row in rows], [("Processed", 1)])
		self.assertEqual(len(self.order_names(order_gid)), 1)
		self.assertEqual(len(self.sales_orders(order_gid)), 1)
		self.assertNoStockMovement(before, self.snapshot())
		logs = frappe.get_all(
			"Marketplace Sync Log",
			{"marketplace_order": self.order_names(order_gid)[0], "status": "Success"},
		)
		self.assertEqual(len(logs), 1)

	def test_same_order_via_two_different_webhook_ids_yields_one_order_and_one_sales_order(self):
		before = self.snapshot()
		order_gid = self.order_gid()
		payload = self.payload(order_gid)

		self.deliver("ORDERS_CREATE", self.webhook_id("create"), order_gid, payload)
		self.deliver("ORDERS_UPDATED", self.webhook_id("update"), order_gid, payload)
		self.run_debounced_sync(order_gid)

		self.assertEqual(len(self.order_names(order_gid)), 1)
		self.assertEqual(len(self.sales_orders(order_gid)), 1)
		self.assertNoStockMovement(before, self.snapshot())
		self.assertEqual(frappe.db.count("Shopify Webhook Delivery", {"connection": self.connection.name}), 2)

	def test_refund_webhook_after_delivery_creates_no_return_and_moves_no_stock(self):
		order_gid, sales_order = self.import_and_sync()
		delivery_note = make_delivery_note(sales_order)
		delivery_note.insert()
		delivery_note.submit()
		before = self.snapshot()
		self.assertEqual(before["actual_qty"], STOCK_QTY - ORDER_QTY)

		# One of the two delivered units is refunded; make_order_payload's default refund
		# carries restockType RESTOCK.
		refunded = self.payload(
			order_gid,
			updated_at="2026-09-27T11:00:00Z",
			quantity=1,
			displayFinancialStatus="PARTIALLY_REFUNDED",
		)
		refunded["refunds"] = make_order_payload(order_gid)["refunds"]
		refunded["lineItems"]["nodes"][0]["unfulfilledQuantity"] = 0
		self.deliver("REFUNDS_CREATE", self.webhook_id(), order_gid, refunded)
		results = self.run_debounced_sync(order_gid)

		self.assertEqual(results[0]["reconciliation"]["outcome"], "UNSUPPORTED_COMPENSATING_TRANSACTION")
		self.assertNoStockMovement(before, self.snapshot())
		self.assertEqual(len(self.sales_orders(order_gid)), 1)


class IntegrationTestShopifyInventoryReadsOwnWarehouse(ShopifyStockOrderFixture, IntegrationTestCase):
	def _sync(self, account, shopify, locations, mapping_names=None):
		with (
			patch(EXECUTE_TARGET, side_effect=shopify),
			patch(
				FETCH_LOCATIONS_TARGET,
				return_value={
					"shop_id": "s",
					"active_locations": [{"id": location, "name": location} for location in locations],
				},
			),
			patch.object(frappe.db, "commit"),
		):
			return inventory_sync.sync_account_inventory(account.name, mapping_names)

	def test_each_account_sends_its_own_warehouse_quantity_to_its_own_location(self):
		second_location = "gid://shopify/Location/2"
		second_warehouse = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": f"Safety WH {self.suffix}",
				"company": self.company,
				"parent_warehouse": frappe.db.get_value("Warehouse", self.warehouse, "parent_warehouse"),
			}
		).insert()
		make_stock_entry(
			item_code=self.item.name,
			qty=4,
			to_warehouse=second_warehouse.name,
			company=self.company,
			rate=5,
		)
		second_account = self._make_account(
			make_connection(f"Safety Two {self.suffix}"), second_warehouse.name, second_location
		)
		self._created_accounts.append(second_account.name)
		second_variant = _variant_gid()
		second_mapping = self._make_mapping(second_account, second_variant)
		locations = [LOCATION, second_location]

		first_shopify = FakeShopify()
		first_shopify.add(self.variant_gid, on_hand=0)
		self._sync(self.account, first_shopify, locations)
		second_shopify = FakeShopify()
		second_shopify.add(second_variant, on_hand=0)
		self._sync(second_account, second_shopify, locations)

		first_sets = first_shopify.calls_of("set")
		second_sets = second_shopify.calls_of("set")
		self.assertEqual(len(first_sets), 1)
		self.assertEqual(len(second_sets), 1)
		first_rows = first_sets[0]["input"]["quantities"]
		second_rows = second_sets[0]["input"]["quantities"]
		self.assertEqual(
			[(row["locationId"], row["quantity"]) for row in first_rows], [(LOCATION, STOCK_QTY)]
		)
		self.assertEqual(
			[(row["locationId"], row["quantity"]) for row in second_rows], [(second_location, 4)]
		)
		self.assertEqual(first_shopify.variants[self.variant_gid]["on_hand"], STOCK_QTY)
		self.assertEqual(second_shopify.variants[second_variant]["on_hand"], 4)
		self.assertEqual(
			frappe.db.get_value("Marketplace Product Mapping", second_mapping.name, "inventory_sync_status"),
			"Synced",
		)

	def test_sales_order_submit_then_bin_sweep_sends_no_shopify_set(self):
		self.import_and_sync()
		self.assertEqual(self.snapshot()["actual_qty"], STOCK_QTY)
		shopify = FakeShopify()
		shopify.add(self.variant_gid, on_hand=STOCK_QTY)

		with (
			patch("frappe.enqueue") as mocked_enqueue,
			patch.object(inventory_sync, "_enabled_shopify_accounts", return_value=[self.account]),
		):
			inventory_sync.sweep_changed_bins()
		calls = [
			call
			for call in mocked_enqueue.call_args_list
			if call.kwargs.get("account_name") == self.account.name
		]
		# The submit touched the Bin (reserved_qty), so the sweep selects the mapping...
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0].kwargs["mapping_names"], [self.mapping.name])
		self._sync(self.account, shopify, [LOCATION], calls[0].kwargs["mapping_names"])

		# ...but the target (actual_qty) is unchanged, so Shopify is not written.
		self.assertEqual(shopify.calls_of("set"), [])
		self.assertEqual(self.snapshot()["actual_qty"], STOCK_QTY)


class IntegrationTestShopifyWebhookIdempotency(ShopifyStockOrderFixture, IntegrationTestCase):
	def _failing_delivery(self, webhook_id, order_gid, times=1):
		for _attempt in range(times):
			with patch(EXECUTE_TARGET, side_effect=Exception("shopify unavailable")):
				process_shopify_webhook(
					self.connection.name,
					"ORDERS_CREATE",
					webhook_id,
					self.connection.shop_domain,
					order_gid=order_gid,
				)

	def _delivery(self, webhook_id):
		return frappe.get_doc("Shopify Webhook Delivery", {"webhook_id": webhook_id})

	def _sweep_only(self, delivery_name, payload):
		"""Run the site-wide retry sweep, processing only ``delivery_name`` (leftover rows from
		other tests are skipped) and never committing."""
		real = webhooks_module._process_delivery

		def only_ours(delivery, connection):
			if delivery.name == delivery_name:
				return real(delivery, connection)

		with (
			patch(EXECUTE_TARGET, return_value={"order": payload}),
			patch.object(webhooks_module, "_process_delivery", side_effect=only_ours),
			patch.object(frappe.db, "commit"),
		):
			retry_errored_webhook_deliveries()

	def test_errored_webhook_redelivered_with_a_good_payload_is_processed_once(self):
		order_gid = self.order_gid()
		webhook_id = self.webhook_id()
		self._failing_delivery(webhook_id, order_gid)
		delivery = self._delivery(webhook_id)
		self.assertEqual((delivery.status, delivery.attempt_count), ("Error", 1))
		self.assertEqual(self.order_names(order_gid), [])

		self.deliver("ORDERS_CREATE", webhook_id, order_gid, self.payload(order_gid))
		self.run_debounced_sync(order_gid)

		delivery = self._delivery(webhook_id)
		self.assertEqual((delivery.status, delivery.attempt_count), ("Processed", 2))
		self.assertEqual(frappe.db.count("Shopify Webhook Delivery", {"webhook_id": webhook_id}), 1)
		self.assertEqual(len(self.order_names(order_gid)), 1)
		self.assertEqual(len(self.sales_orders(order_gid)), 1)

	def test_errored_webhook_recovered_by_the_retry_sweep_is_processed_once(self):
		order_gid = self.order_gid()
		webhook_id = self.webhook_id()
		self._failing_delivery(webhook_id, order_gid)
		delivery = self._delivery(webhook_id)

		self._sweep_only(delivery.name, self.payload(order_gid))
		self.run_debounced_sync(order_gid)
		self._sweep_only(delivery.name, self.payload(order_gid))

		delivery = self._delivery(webhook_id)
		self.assertEqual((delivery.status, delivery.attempt_count), ("Processed", 2))
		self.assertEqual(len(self.order_names(order_gid)), 1)
		self.assertEqual(len(self.sales_orders(order_gid)), 1)

	def test_delivery_that_keeps_failing_stops_at_the_attempt_cap(self):
		order_gid = self.order_gid()
		webhook_id = self.webhook_id()

		self._failing_delivery(webhook_id, order_gid, times=MAX_WEBHOOK_DELIVERY_ATTEMPTS)
		delivery = self._delivery(webhook_id)
		self.assertEqual((delivery.status, delivery.attempt_count), ("Error", MAX_WEBHOOK_DELIVERY_ATTEMPTS))

		with patch(EXECUTE_TARGET, return_value={"order": self.payload(order_gid)}) as mocked_execute:
			process_shopify_webhook(
				self.connection.name,
				"ORDERS_CREATE",
				webhook_id,
				self.connection.shop_domain,
				order_gid=order_gid,
			)

		mocked_execute.assert_not_called()
		delivery = self._delivery(webhook_id)
		self.assertEqual((delivery.status, delivery.attempt_count), ("Error", MAX_WEBHOOK_DELIVERY_ATTEMPTS))
		self.assertEqual(self.order_names(order_gid), [])

	def test_stuck_received_delivery_is_recovered_by_the_sweep_and_imports_once(self):
		order_gid = self.order_gid()
		webhook_id = self.webhook_id()
		delivery = make_delivery(self.connection, webhook_id, order_gid=order_gid)
		frappe.db.set_value(
			"Shopify Webhook Delivery",
			delivery.name,
			"creation",
			add_to_date(now_datetime(), minutes=-(STUCK_RECEIVED_THRESHOLD_MINUTES + 1)),
			update_modified=False,
		)

		self._sweep_only(delivery.name, self.payload(order_gid))
		self.run_debounced_sync(order_gid)
		self._sweep_only(delivery.name, self.payload(order_gid))

		delivery.reload()
		self.assertEqual((delivery.status, delivery.attempt_count), ("Processed", 1))
		self.assertEqual(len(self.order_names(order_gid)), 1)
		self.assertEqual(len(self.sales_orders(order_gid)), 1)


class IntegrationTestShopifyOutOfOrderWebhooks(ShopifyStockOrderFixture, IntegrationTestCase):
	def _refund_payload(self, order_gid, **overrides):
		"""A syncable (PAID) order that already carries one refund, for ordering mechanics."""
		payload = self.payload(order_gid, updated_at="2026-09-27T11:00:00Z", quantity=1, **overrides)
		payload["refunds"] = make_order_payload(order_gid)["refunds"]
		return payload

	def _refund_ids(self, order):
		return [refund["id"] for refund in frappe.parse_json(order.refunds)]

	def _baseline_refund_ids(self, order):
		return frappe.parse_json(order.last_synced_snapshot).get("refund_ids")

	def _log_count(self, order_gid):
		return frappe.db.count("Marketplace Sync Log", {"marketplace_order": self.order_names(order_gid)[0]})

	def test_refund_webhook_delivered_twice_with_different_ids_does_not_duplicate_the_refund(self):
		before = self.snapshot()
		order_gid, _sales_order = self.import_and_sync()
		refunded = self._refund_payload(order_gid, displayFinancialStatus="PARTIALLY_REFUNDED")
		refunded["lineItems"]["nodes"][0]["unfulfilledQuantity"] = 1

		self.deliver("REFUNDS_CREATE", self.webhook_id("refund-a"), order_gid, refunded)
		self.run_debounced_sync(order_gid)
		logs_after_first = self._log_count(order_gid)
		self.deliver("REFUNDS_CREATE", self.webhook_id("refund-b"), order_gid, refunded)
		self.run_debounced_sync(order_gid)

		order = self.marketplace_order(order_gid)
		self.assertEqual(self._refund_ids(order), ["gid://shopify/Refund/1"])
		self.assertEqual(self._baseline_refund_ids(order), ["gid://shopify/Refund/1"])
		self.assertEqual(len(self.order_names(order_gid)), 1)
		self.assertEqual(len(self.sales_orders(order_gid)), 1)
		self.assertNoStockMovement(before, self.snapshot())
		# Current behavior: the unchanged second delivery still runs a sync and appends one more
		# (no-change) Success row.
		self.assertEqual(self._log_count(order_gid), logs_after_first + 1)

	def test_refund_webhook_before_order_webhook_for_a_never_imported_order(self):
		order_gid = self.order_gid()
		payload = self._refund_payload(order_gid)

		self.deliver("REFUNDS_CREATE", self.webhook_id("refund"), order_gid, payload)
		self.deliver("ORDERS_CREATE", self.webhook_id("create"), order_gid, payload)
		results = self.run_debounced_sync(order_gid)

		self.assertTrue(results[0]["ok"], results[0])
		order = self.marketplace_order(order_gid)
		self.assertEqual(len(self.order_names(order_gid)), 1)
		self.assertEqual(len(self.sales_orders(order_gid)), 1)
		self.assertEqual(self._refund_ids(order), ["gid://shopify/Refund/1"])
		self.assertEqual(self._baseline_refund_ids(order), ["gid://shopify/Refund/1"])

	def test_partially_refunded_never_synced_order_is_blocked_not_synced(self):
		# Current behavior: the refund-planning tolerance for PARTIALLY_REFUNDED applies only to an
		# order that already has a submitted Sales Order, so a partial refund that exists before
		# the first sync blocks the order (docs/shopify/README.md, full refund policy).
		order_gid = self.order_gid()
		payload = self._refund_payload(order_gid, displayFinancialStatus="PARTIALLY_REFUNDED")

		self.deliver("REFUNDS_CREATE", self.webhook_id("refund"), order_gid, payload)
		self.deliver("ORDERS_CREATE", self.webhook_id("create"), order_gid, payload)
		results = self.run_debounced_sync(order_gid)

		self.assertFalse(results[0]["ok"])
		self.assertIn("not financially eligible", results[0]["error"])
		self.assertEqual(len(self.order_names(order_gid)), 1)
		self.assertEqual(self.sales_orders(order_gid), [])

	def test_stale_update_after_cancellation_does_not_clear_cancelled_state(self):
		order_gid = self.order_gid()
		cancelled = self.payload(
			order_gid,
			updated_at="2026-09-27T12:00:00Z",
			cancelledAt="2026-09-27T11:59:00Z",
			cancelReason="CUSTOMER",
			displayFinancialStatus="VOIDED",
		)
		cancelled["refunds"] = make_order_payload(order_gid)["refunds"]
		stale = self.payload(order_gid, updated_at="2026-09-27T10:05:00Z")
		sales_orders_before = self.snapshot()["sales_order"]

		self.deliver("ORDERS_CANCELLED", self.webhook_id("cancel"), order_gid, cancelled)
		self.deliver("ORDERS_UPDATED", self.webhook_id("stale"), order_gid, stale)

		order = self.marketplace_order(order_gid)
		self.assertTrue(order.cancelled_at)
		self.assertEqual(order.external_financial_status, "VOIDED")
		self.assertEqual(self._refund_ids(order), ["gid://shopify/Refund/1"])

		results = self.run_debounced_sync(order_gid)

		order.reload()
		self.assertTrue(results[0]["ok"])
		self.assertTrue(results[0]["skipped"])
		self.assertEqual(order.erpnext_sync_status, "Cancelled")
		self.assertEqual(self.sales_orders(order_gid), [])
		self.assertEqual(self.snapshot()["sales_order"], sales_orders_before)

	def test_cancelled_never_linked_order_is_never_requeued_by_the_sweeps(self):
		order_gid = self.order_gid()
		cancelled = self.payload(
			order_gid, updated_at="2026-09-27T12:00:00Z", cancelledAt="2026-09-27T11:59:00Z"
		)
		for index in range(3):
			self.deliver("ORDERS_CANCELLED", self.webhook_id(f"cancel{index}"), order_gid, cancelled)
			self.run_debounced_sync(order_gid)

		order = self.marketplace_order(order_gid)
		self.assertEqual(self.sales_orders(order_gid), [])
		# Every processed webhook adds one log row; the default payload is a PAID order, so the
		# paid cancellation is a Needs Action (Invalid) row, never a Sales Order.
		statuses = frappe.get_all("Marketplace Sync Log", {"marketplace_order": order.name}, pluck="status")
		self.assertEqual(statuses, ["Invalid"] * 3)

		self.assertNotIn(order.name, _stuck_reconciliation_candidates(500))
		sweep_stuck_sales_order_reconciliations()
		self.assertIsNone(frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until"))

		self._reconcile_all_accounts_orders(order_gid, cancelled)
		self.assertIsNone(frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until"))
		self.assertEqual(self.sales_orders(order_gid), [])
		self.assertEqual(self.order_names(order_gid), [order.name])

	def _reconcile_all_accounts_orders(self, order_gid, payload):
		real_get_all = frappe.get_all

		def only_our_account(doctype, *args, **kwargs):
			rows = real_get_all(doctype, *args, **kwargs)
			if doctype == "Marketplace Account":
				return [row for row in rows if row.name == self.account.name]
			return rows

		def execute(query, variables=None):
			if "query" in (variables or {}):
				return {
					"orders": {
						"nodes": [{"id": order_gid, "updatedAt": payload["updatedAt"]}],
						"pageInfo": {"hasNextPage": False},
					}
				}
			return {"order": payload}

		with (
			patch(EXECUTE_TARGET, side_effect=execute),
			patch("frappe.get_all", side_effect=only_our_account),
			patch.object(frappe.db, "commit"),
		):
			shopify_orders.reconcile_all_accounts_orders()


class IntegrationTestShopifyOrderLocking(ShopifyStockOrderFixture, IntegrationTestCase):
	def _recorded_get_values(self):
		calls = []
		real = frappe.db.get_value

		def record(*args, **kwargs):
			calls.append((args, kwargs))
			return real(*args, **kwargs)

		return calls, patch.object(frappe.db, "get_value", side_effect=record)

	def test_sync_sales_order_locks_the_marketplace_order_row_first(self):
		order_gid = self.order_gid()
		self.deliver("ORDERS_CREATE", self.webhook_id(), order_gid, self.payload(order_gid))
		order = self.marketplace_order(order_gid)

		calls, recorder = self._recorded_get_values()
		with recorder:
			sync_sales_order(order.name)

		args, kwargs = calls[0]
		self.assertEqual(args[0], "Marketplace Order")
		self.assertEqual(args[1], order.name)
		self.assertTrue(kwargs.get("for_update"))

	def test_upsert_locks_the_existing_row_only_when_a_staleness_guard_is_supplied(self):
		order_gid = self.order_gid()
		self.deliver("ORDERS_CREATE", self.webhook_id(), order_gid, self.payload(order_gid))
		order = self.marketplace_order(order_gid)
		normalized = {
			"external_order_id": order_gid,
			"latest_payload": {"updatedAt": "2026-09-27T10:05:00Z"},
			"items": [],
		}

		def identity_lookups(calls):
			return [
				kwargs
				for args, kwargs in calls
				if args and args[0] == "Marketplace Order" and isinstance(args[1], dict)
			]

		calls, recorder = self._recorded_get_values()
		with recorder:
			upsert_marketplace_order(self.account.name, normalized, is_stale=lambda existing: True)
		self.assertTrue(identity_lookups(calls)[0].get("for_update"))

		calls, recorder = self._recorded_get_values()
		with recorder:
			upsert_marketplace_order(self.account.name, normalized)
		self.assertFalse(identity_lookups(calls)[0].get("for_update"))
		self.assertEqual(self.order_names(order_gid), [order.name])
