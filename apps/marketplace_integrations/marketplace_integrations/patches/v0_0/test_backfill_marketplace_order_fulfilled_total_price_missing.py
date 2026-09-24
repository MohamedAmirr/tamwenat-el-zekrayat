# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe

from marketplace_integrations.patches.v0_0.backfill_marketplace_order_fulfilled_total_price_missing import (
	execute,
)
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.testing_utils import CleanupTestCase


class IntegrationTestBackfillMarketplaceOrderFulfilledTotalPriceMissing(CleanupTestCase):
	def make_account(self):
		suffix = frappe.generate_hash(length=8)
		connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Missing Backfill Connection {suffix}",
				"client_id": f"missing-backfill-client-{suffix}",
				"client_secret": "missing-backfill-secret",
				"chain_id": f"missing-backfill-chain-{suffix}",
			}
		).insert()
		return frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Missing Backfill Account {suffix}",
				"platform": "Talabat",
				"external_account_id": f"missing-backfill-vendor-{suffix}",
				"connection_doctype": "Talabat Connection",
				"connection": connection.name,
			}
		).insert()

	def raw_payload(self, order_id, total_price_missing=True):
		"""A Talabat order.Order-shaped raw payload, the same shape stored
		verbatim in Marketplace Order.latest_payload. `pricing.total_price` is
		absent (not merely 0) when `total_price_missing` is True."""
		pricing = {"pricing_type": "UNIT", "quantity": 2}
		if not total_price_missing:
			pricing["total_price"] = 0
		return {
			"order_id": order_id,
			"status": "RECEIVED",
			"customer": {"delivery_address": {}},
			"payment": {"sub_total": 100.0, "order_total": 100.0},
			"sys": {},
			"cancellation": {},
			"items": [
				{
					"_id": "item-001",
					"sku": "SKU-001",
					"name": "Product",
					"status": "IN_CART",
					"original_pricing": {"pricing_type": "UNIT", "quantity": 2},
					"pricing": pricing,
				}
			],
		}

	def make_pre_85_order(self, account, order_id, payload=None):
		"""Simulate an order stored before #85 shipped: latest_payload already
		carries whatever Talabat sent, but fulfilled_total_price_missing was
		never computed/stored at the time - it defaults to 0 (unset), the same
		as a real zero, exactly the ambiguity #85 introduced the flag to
		resolve. Not including the key in the item dict below (rather than
		passing 0 explicitly) models that "never computed" state."""
		payload = payload if payload is not None else self.raw_payload(order_id)
		normalized_order = {
			"external_order_id": order_id,
			"external_status": "RECEIVED",
			"sub_total": 100.0,
			"order_total": 100.0,
			"latest_payload": payload,
			"items": [
				{
					"external_item_id": "item-001",
					"external_sku": "SKU-001",
					"product_name": "Product",
					"fulfilled_quantity": 2,
					"fulfilled_total_price": 0,
				}
			],
		}
		return upsert_marketplace_order(account.name, normalized_order)

	def test_backfills_fulfilled_total_price_missing_from_stored_payload(self):
		account = self.make_account()
		order_id = f"order-{frappe.generate_hash(length=8)}"
		order = self.make_pre_85_order(account, order_id)
		order.reload()
		self.assertEqual(order.items[0].fulfilled_total_price_missing, 0)

		execute()

		order.reload()
		self.assertEqual(order.items[0].fulfilled_total_price_missing, 1)
		# Untouched fields are not disturbed by the backfill.
		self.assertEqual(order.items[0].fulfilled_total_price, 0)
		self.assertEqual(order.sub_total, 100.0)

	def test_a_real_zero_fulfilled_total_price_is_not_flagged_as_missing(self):
		account = self.make_account()
		order_id = f"order-{frappe.generate_hash(length=8)}"
		payload = self.raw_payload(order_id, total_price_missing=False)
		order = self.make_pre_85_order(account, order_id, payload=payload)

		execute()

		order.reload()
		self.assertEqual(order.items[0].fulfilled_total_price_missing, 0)

	def test_backfill_is_idempotent(self):
		account = self.make_account()
		order_id = f"order-{frappe.generate_hash(length=8)}"
		order = self.make_pre_85_order(account, order_id)

		execute()
		first_modified = frappe.db.get_value("Marketplace Order Item", {"parent": order.name}, "modified")
		execute()
		second_modified = frappe.db.get_value("Marketplace Order Item", {"parent": order.name}, "modified")

		order.reload()
		self.assertEqual(order.items[0].fulfilled_total_price_missing, 1)
		self.assertEqual(len(order.items), 1)
		# A second run that finds the value already correct does not write
		# again (no-op, per _backfill_one_order's `if row.field != missing`
		# guard) - the row's own modified timestamp does not move.
		self.assertEqual(first_modified, second_modified)

	def test_malformed_latest_payload_is_skipped_and_logged_not_aborting_the_batch(self):
		"""A single historical row whose latest_payload doesn't match the
		order.Order shape normalize_order()/_normalize_item() expect (here: an
		item entry that isn't an object, which _normalize_item() rejects with
		frappe.throw()) must not abort the whole migration (#85 review finding
		#3) - it is logged and skipped, while every other order still gets
		backfilled. Syntactically invalid JSON can't be used to model this:
		MariaDB itself enforces a JSON_VALID CHECK constraint on this JSON
		fieldtype column, so it would never reach normalize_order() at all -
		the real gap is malformed-but-valid-JSON content."""
		account = self.make_account()
		good_order_id = f"order-{frappe.generate_hash(length=8)}"
		good_order = self.make_pre_85_order(account, good_order_id)

		bad_order_id = f"order-{frappe.generate_hash(length=8)}"
		bad_order = self.make_pre_85_order(account, bad_order_id)
		# A direct frappe.db.set_value() write bypasses this JSON field's own
		# sanitization/validation, the same way a pre-existing corrupted
		# production row would already be sitting in the table before this
		# patch ever runs.
		frappe.db.set_value(
			"Marketplace Order",
			bad_order.name,
			"latest_payload",
			frappe.as_json({"order_id": bad_order_id, "items": ["not-an-object"]}),
			update_modified=False,
		)
		frappe.clear_document_cache("Marketplace Order", bad_order.name)

		error_count_before = frappe.db.count("Error Log")
		execute()  # must not raise

		self.assertGreater(frappe.db.count("Error Log"), error_count_before)

		good_order.reload()
		self.assertEqual(good_order.items[0].fulfilled_total_price_missing, 1)

		bad_order.reload()
		self.assertEqual(bad_order.items[0].fulfilled_total_price_missing, 0)
