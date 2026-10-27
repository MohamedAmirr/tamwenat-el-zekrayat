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
		paused_check = patch(
			"marketplace_integrations.integrations.noon.pricing.is_push_paused", return_value=False
		)
		self.paused = paused_check.start()
		self.addCleanup(paused_check.stop)

	def test_price_switch_off_before_start_makes_no_noon_call(self):
		self.paused.return_value = True
		with patch("marketplace_integrations.integrations.noon.pricing.get_account") as get_account:
			with patch("marketplace_integrations.integrations.noon.pricing.NoonClient") as client:
				with patch("marketplace_integrations.integrations.noon.pricing.persist_outcomes") as persist:
					outcome = push_pricing("ACC")
		self.assertEqual(outcome, {"synced": 0, "failed": 0, "paused": True})
		self.paused.assert_called_with("ACC", "pricing")
		get_account.assert_not_called()
		client.assert_not_called()
		persist.assert_not_called()

	def test_price_switch_turned_off_mid_job_stops_before_next_chunk(self):
		account = SimpleNamespace(name="ACC", connection="CONN", noon_country_code="ae")
		rows = [
			(
				SimpleNamespace(name=f"M{n}", external_reference=f"SKU-{n}"),
				{"sales_uom": None},
				SimpleNamespace(name=f"ITEM-{n}", stock_uom="Nos"),
			)
			for n in (1, 2)
		]
		self.paused.side_effect = [False, False, True]
		module = "marketplace_integrations.integrations.noon.pricing"
		with patch(f"{module}.get_account", return_value=account):
			with patch(f"{module}.selected_mappings", return_value=rows):
				with patch(f"{module}.validate_price_setup", return_value=SimpleNamespace(name="PL")):
					with patch(f"{module}.resolve_listing_price", return_value=Decimal("10")):
						with patch(f"{module}.PRICING_CHUNK_SIZE", 1):
							with patch(f"{module}.NoonClient") as client_class:
								with patch(f"{module}.persist_outcomes") as persist:
									client = client_class.return_value
									client.request.return_value = Response([result("SKU-1")])
									outcome = push_pricing("ACC")
		self.assertEqual(outcome, {"synced": 1, "failed": 0, "paused": True})
		self.assertEqual(client.request.call_count, 1)
		persist.assert_called_once()

	def test_manual_price_run_pushes_with_the_switch_off_and_never_rechecks_it(self):
		account = SimpleNamespace(name="ACC", connection="CONN", noon_country_code="ae")
		rows = [
			(
				SimpleNamespace(name=f"M{n}", external_reference=f"SKU-{n}"),
				{"sales_uom": None},
				SimpleNamespace(name=f"ITEM-{n}", stock_uom="Nos"),
			)
			for n in (1, 2)
		]
		self.paused.return_value = True
		module = "marketplace_integrations.integrations.noon.pricing"
		with patch(f"{module}.get_account", return_value=account):
			with patch(f"{module}.selected_mappings", return_value=rows):
				with patch(f"{module}.validate_price_setup", return_value=SimpleNamespace(name="PL")):
					with patch(f"{module}.resolve_listing_price", return_value=Decimal("10")):
						with patch(f"{module}.PRICING_CHUNK_SIZE", 1):
							with patch(f"{module}.NoonClient") as client_class:
								with patch(f"{module}.persist_outcomes"):
									client = client_class.return_value
									client.request.side_effect = [
										Response([result("SKU-1")]),
										Response([result("SKU-2")]),
									]
									outcome = push_pricing("ACC", manual=True)
		self.assertEqual(outcome, {"synced": 2, "failed": 0})
		self.assertEqual(client.request.call_count, 2)
		self.paused.assert_not_called()

	def test_manual_price_run_with_a_bad_price_list_still_errors(self):
		account = SimpleNamespace(name="ACC", connection="CONN", noon_country_code="ae")
		rows = [
			(
				SimpleNamespace(name="M1", external_reference="SKU-1"),
				{"sales_uom": None},
				SimpleNamespace(name="ITEM-1", stock_uom="Nos"),
			)
		]
		self.paused.return_value = True
		module = "marketplace_integrations.integrations.noon.pricing"
		with patch(f"{module}.get_account", return_value=account):
			with patch(f"{module}.selected_mappings", return_value=rows):
				with patch(
					f"{module}.validate_price_setup",
					side_effect=frappe.ValidationError("Price List PL must be an enabled selling price list."),
				):
					with patch(f"{module}.NoonClient") as client_class:
						with patch(f"{module}.persist_outcomes") as persist:
							outcome = push_pricing("ACC", manual=True)
		self.assertEqual(outcome, {"synced": 0, "failed": 1})
		client_class.assert_not_called()
		self.assertIn("enabled selling price list", persist.call_args.args[0]["M1"])

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
