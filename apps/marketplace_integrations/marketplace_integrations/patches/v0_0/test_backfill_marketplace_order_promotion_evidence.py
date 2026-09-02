# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.backfill_marketplace_order_promotion_evidence import execute
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order


class IntegrationTestBackfillMarketplaceOrderPromotionEvidence(IntegrationTestCase):
	def make_account(self):
		suffix = frappe.generate_hash(length=8)
		connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Promo Backfill Connection {suffix}",
				"client_id": f"promo-backfill-client-{suffix}",
				"client_secret": "promo-backfill-secret",
				"chain_id": f"promo-backfill-chain-{suffix}",
			}
		).insert()
		return frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Promo Backfill Account {suffix}",
				"platform": "Talabat",
				"external_account_id": f"promo-backfill-vendor-{suffix}",
				"connection_doctype": "Talabat Connection",
				"connection": connection.name,
			}
		).insert()

	def raw_payload(self, order_id, promotion_status="AVAILABLE"):
		"""A Talabat order.Order-shaped raw payload, the same shape stored
		verbatim in Marketplace Order.latest_payload."""
		return {
			"order_id": order_id,
			"status": "RECEIVED",
			"customer": {"delivery_address": {}},
			"payment": {"sub_total": 100.0, "order_total": 100.0},
			"sys": {},
			"cancellation": {},
			"promotion_status": promotion_status,
			"items": [
				{
					"_id": "item-001",
					"sku": "SKU-001",
					"name": "Product",
					"container_deposit": 0.25,
					"promotion": [
						{
							"name": "Sale",
							"type": "STRIKETHROUGH",
							"discount_amount": 1.0,
							"sponsorships": [{"sponsor": "VENDOR", "amount": 1.0}],
						}
					],
				}
			],
		}

	def make_pre_83_order(self, account, order_id, payload=None):
		"""Simulate an order stored before #83 shipped: latest_payload
		already carries whatever promotion evidence Talabat sent, but the
		normalized fields applied at the time never included it."""
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
				}
			],
		}
		return upsert_marketplace_order(account.name, normalized_order)

	def test_backfills_promotion_fields_from_stored_payload_without_disturbing_other_fields(self):
		account = self.make_account()
		order_id = f"order-{frappe.generate_hash(length=8)}"
		order = self.make_pre_83_order(account, order_id)
		order.reload()
		self.assertIsNone(order.promotion_status)
		self.assertEqual(order.items[0].container_deposit, 0.0)
		self.assertIsNone(order.items[0].promotions)

		execute()

		order.reload()
		self.assertEqual(order.promotion_status, "AVAILABLE")
		self.assertEqual(order.external_status, "RECEIVED")
		self.assertEqual(order.sub_total, 100.0)
		item = order.items[0]
		self.assertEqual(item.container_deposit, 0.25)
		self.assertEqual(frappe.parse_json(item.promotions)[0]["name"], "Sale")

	def test_backfill_is_idempotent(self):
		account = self.make_account()
		order_id = f"order-{frappe.generate_hash(length=8)}"
		order = self.make_pre_83_order(account, order_id)

		execute()
		execute()

		order.reload()
		self.assertEqual(order.promotion_status, "AVAILABLE")
		self.assertEqual(len(order.items), 1)
		self.assertEqual(order.items[0].container_deposit, 0.25)
		self.assertEqual(frappe.parse_json(order.items[0].promotions)[0]["name"], "Sale")

	def test_backfill_leaves_pre_promotion_payload_blank_without_raising(self):
		account = self.make_account()
		order_id = f"order-{frappe.generate_hash(length=8)}"
		payload = self.raw_payload(order_id)
		del payload["promotion_status"]
		del payload["items"][0]["container_deposit"]
		del payload["items"][0]["promotion"]
		order = self.make_pre_83_order(account, order_id, payload=payload)

		execute()

		order.reload()
		self.assertIsNone(order.promotion_status)
		self.assertEqual(order.items[0].container_deposit, 0.0)
		self.assertIsNone(order.items[0].promotions)
