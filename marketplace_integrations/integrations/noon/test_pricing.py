# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.noon.pricing import push_pricing, validate_price_setup


class Response:
	status_code = 200

	def __init__(self, items):
		self.items = items

	def json(self):
		return {"items": self.items}


def result(sku, code="OK", status_id=0):
	return {
		"partner_sku": sku,
		"country_code": "ae",
		"status": {
			"status_id": status_id,
			"status_code": code,
			"message": "" if code == "OK" else "Rejected",
		},
	}


class UnitTestNoonPricing(UnitTestCase):
	def setUp(self):
		connection_check = patch(
			"marketplace_integrations.integrations.noon.pricing.validate_connection_enabled"
		)
		connection_check.start()
		self.addCleanup(connection_check.stop)

	def test_price_payload_partial_failure_and_retry(self):
		account = SimpleNamespace(name="ACC", connection="CONN", noon_country_code="ae")
		rows = [
			(
				SimpleNamespace(name="M1", external_reference="SKU-1"),
				{"sales_uom": "Box"},
				SimpleNamespace(name="ITEM-1", stock_uom="Nos"),
			),
			(
				SimpleNamespace(name="M2", external_reference="SKU-2"),
				{"sales_uom": None},
				SimpleNamespace(name="ITEM-2", stock_uom="Nos"),
			),
		]
		with patch("marketplace_integrations.integrations.noon.pricing.get_account", return_value=account):
			with patch(
				"marketplace_integrations.integrations.noon.pricing.selected_mappings", return_value=rows
			):
				with patch(
					"marketplace_integrations.integrations.noon.pricing.validate_price_setup",
					return_value=SimpleNamespace(name="PL"),
				):
					with patch(
						"marketplace_integrations.integrations.noon.pricing.resolve_listing_price",
						return_value=Decimal("12.50"),
					) as price:
						with patch(
							"marketplace_integrations.integrations.noon.pricing.NoonClient"
						) as client_class:
							with patch(
								"marketplace_integrations.integrations.noon.pricing.persist_outcomes"
							) as persist:
								client = client_class.return_value
								client.request.side_effect = [
									Response([result("SKU-1"), result("SKU-2", "INVALID_ARGUMENT", 3)]),
									Response([result("SKU-1"), result("SKU-2")]),
								]
								first = push_pricing("ACC")
								second = push_pricing("ACC")
		self.assertEqual(first, {"synced": 1, "failed": 1})
		self.assertEqual(second, {"synced": 2, "failed": 0})
		self.assertEqual(client.request.call_count, 2)
		payload = client.request.call_args_list[0].kwargs["json"]
		self.assertEqual(payload, client.request.call_args_list[1].kwargs["json"])
		self.assertEqual(set(payload), {"items"})
		self.assertEqual(
			payload["items"],
			[
				{"partner_sku": "SKU-1", "country_code": "ae", "price": 12.5},
				{"partner_sku": "SKU-2", "country_code": "ae", "price": 12.5},
			],
		)
		self.assertEqual(price.call_args_list[0].args[2], "Box")
		self.assertEqual(price.call_args_list[1].args[2], "Nos")
		self.assertIsNone(persist.call_args_list[0].args[0]["M1"])
		self.assertIn("INVALID_ARGUMENT", persist.call_args_list[0].args[0]["M2"])
		self.assertIsNone(persist.call_args_list[1].args[0]["M2"])

	def test_currency_mismatch_blocks_price_setup(self):
		account = SimpleNamespace(name="ACC", noon_country_code="ae", erpnext_selling_price_list="PL")
		price_list = SimpleNamespace(name="PL", enabled=1, selling=1, currency="SAR")
		with patch(
			"marketplace_integrations.integrations.noon.pricing.frappe.get_doc", return_value=price_list
		):
			with self.assertRaisesRegex(frappe.ValidationError, "does not match"):
				validate_price_setup(account)

	def test_currency_mismatch_marks_mapping_error_without_noon_request(self):
		account = SimpleNamespace(name="ACC", connection="CONN", noon_country_code="ae")
		rows = [(SimpleNamespace(name="M1"), {"sales_uom": None}, SimpleNamespace(name="ITEM-1"))]
		with patch("marketplace_integrations.integrations.noon.pricing.get_account", return_value=account):
			with patch(
				"marketplace_integrations.integrations.noon.pricing.selected_mappings", return_value=rows
			):
				with patch(
					"marketplace_integrations.integrations.noon.pricing.validate_price_setup",
					side_effect=frappe.ValidationError(
						"Price List currency does not match Noon country currency."
					),
				):
					with patch("marketplace_integrations.integrations.noon.pricing.NoonClient") as client:
						with patch(
							"marketplace_integrations.integrations.noon.pricing.persist_outcomes"
						) as persist:
							self.assertEqual(push_pricing("ACC"), {"synced": 0, "failed": 1})
		client.assert_not_called()
		self.assertIn("currency", persist.call_args.args[0]["M1"])

	def test_manual_entry_authorizes_before_queueing(self):
		from marketplace_integrations.integrations.noon.pricing import queue_pricing_push

		with patch(
			"marketplace_integrations.integrations.noon.pricing.authorize_selection", return_value=["M1"]
		) as authorize:
			with patch("marketplace_integrations.integrations.noon.pricing.get_account"):
				with patch(
					"marketplace_integrations.integrations.noon.pricing.enqueue_push",
					return_value={"queued": True},
				) as enqueue:
					self.assertEqual(queue_pricing_push("ACC", ["M1"]), {"queued": True})
		authorize.assert_called_once_with("ACC", ["M1"])
		enqueue.assert_called_once()
