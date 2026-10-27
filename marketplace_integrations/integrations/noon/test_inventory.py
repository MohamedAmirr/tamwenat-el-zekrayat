# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.noon.inventory import push_stock


class Response:
	status_code = 200

	def __init__(self, items):
		self.items = items

	def json(self):
		return {"items": self.items}


def result(sku, code="OK", status_id=0):
	return {
		"warehouse_code": "NOON-WH",
		"partner_sku": sku,
		"status": {
			"status_id": status_id,
			"status_code": code,
			"message": "" if code == "OK" else "Rejected",
		},
	}


class UnitTestNoonInventory(UnitTestCase):
	def setUp(self):
		connection_check = patch(
			"marketplace_integrations.integrations.noon.inventory.validate_connection_enabled"
		)
		connection_check.start()
		self.addCleanup(connection_check.stop)
		paused_check = patch(
			"marketplace_integrations.integrations.noon.inventory.is_push_paused", return_value=False
		)
		self.paused = paused_check.start()
		self.addCleanup(paused_check.stop)

	def _two_rows(self):
		return [
			(
				SimpleNamespace(name=f"M{n}", external_reference=f"SKU-{n}"),
				{"item": f"ITEM-{n}", "sales_uom": None},
				SimpleNamespace(name=f"ITEM-{n}", stock_uom="Nos"),
			)
			for n in (1, 2)
		]

	def test_switch_off_before_start_makes_no_noon_call_or_status_write(self):
		self.paused.return_value = True
		with patch("marketplace_integrations.integrations.noon.inventory.get_account") as get_account:
			with patch("marketplace_integrations.integrations.noon.inventory.NoonClient") as client:
				with patch(
					"marketplace_integrations.integrations.noon.inventory.persist_outcomes"
				) as persist:
					result_ = push_stock("ACC")
		self.assertEqual(result_, {"synced": 0, "failed": 0, "paused": True})
		self.paused.assert_called_with("ACC", "stock")
		get_account.assert_not_called()
		client.assert_not_called()
		persist.assert_not_called()

	def test_switch_turned_off_mid_job_stops_before_next_chunk(self):
		account = SimpleNamespace(
			name="ACC", connection="CONN", warehouse="ERP-WH", noon_warehouse_code="NOON-WH"
		)
		# entry check, chunk 1 check, chunk 2 check (now paused)
		self.paused.side_effect = [False, False, True]
		patches = "marketplace_integrations.integrations.noon.inventory"
		with patch(f"{patches}.get_account", return_value=account):
			with patch(f"{patches}.selected_mappings", return_value=self._two_rows()):
				with patch(f"{patches}.validate_account_warehouse"):
					with patch(f"{patches}.validate_warehouse_code"):
						with patch(f"{patches}.get_sellable_qty", return_value=5):
							with patch(f"{patches}.STOCK_CHUNK_SIZE", 1):
								with patch(f"{patches}.NoonClient") as client_class:
									with patch(f"{patches}.persist_outcomes") as persist:
										client = client_class.return_value
										client.request.return_value = Response([result("SKU-1")])
										outcome = push_stock("ACC")
		self.assertEqual(outcome, {"synced": 1, "failed": 0, "paused": True})
		self.assertEqual(client.request.call_count, 1)
		persist.assert_called_once()
		self.assertIn("M1", persist.call_args.args[0])
		self.assertNotIn("M2", persist.call_args.args[0])

	def test_manual_run_pushes_with_the_switch_off_and_never_rechecks_it(self):
		account = SimpleNamespace(
			name="ACC", connection="CONN", warehouse="ERP-WH", noon_warehouse_code="NOON-WH"
		)
		self.paused.return_value = True
		patches = "marketplace_integrations.integrations.noon.inventory"
		with patch(f"{patches}.get_account", return_value=account):
			with patch(f"{patches}.selected_mappings", return_value=self._two_rows()):
				with patch(f"{patches}.validate_account_warehouse"):
					with patch(f"{patches}.validate_warehouse_code"):
						with patch(f"{patches}.get_sellable_qty", return_value=5):
							with patch(f"{patches}.STOCK_CHUNK_SIZE", 1):
								with patch(f"{patches}.NoonClient") as client_class:
									with patch(f"{patches}.persist_outcomes"):
										client = client_class.return_value
										client.request.side_effect = [
											Response([result("SKU-1")]),
											Response([result("SKU-2")]),
										]
										outcome = push_stock("ACC", manual=True)
		# Both chunks pushed although the switch reads off at entry and before each chunk.
		self.assertEqual(outcome, {"synced": 2, "failed": 0})
		self.assertEqual(client.request.call_count, 2)
		self.paused.assert_not_called()

	def test_manual_run_for_a_disabled_connection_still_errors(self):
		account = SimpleNamespace(
			name="ACC", connection="CONN", warehouse="ERP-WH", noon_warehouse_code="NOON-WH"
		)
		self.paused.return_value = True
		patches = "marketplace_integrations.integrations.noon.inventory"
		with patch(f"{patches}.get_account", return_value=account):
			with patch(f"{patches}.selected_mappings", return_value=self._two_rows()):
				with patch(f"{patches}.validate_account_warehouse"):
					with patch(
						f"{patches}.validate_connection_enabled",
						side_effect=frappe.ValidationError("Noon Connection CONN is disabled."),
					):
						with patch(f"{patches}.NoonClient") as client_class:
							with patch(f"{patches}.persist_outcomes") as persist:
								outcome = push_stock("ACC", manual=True)
		self.assertEqual(outcome, {"synced": 0, "failed": 2})
		client_class.assert_not_called()
		self.assertEqual(set(persist.call_args.args[0]), {"M1", "M2"})
		self.assertIn("disabled", persist.call_args.args[0]["M1"])

	def test_warehouse_partition_payload_failure_and_retry(self):
		account = SimpleNamespace(
			name="ACC", connection="CONN", warehouse="ERP-WH", noon_warehouse_code="NOON-WH"
		)
		rows = [
			(
				SimpleNamespace(name="M1", external_reference="SKU-1"),
				{"item": "ITEM-1", "sales_uom": "Box"},
				SimpleNamespace(name="ITEM-1", stock_uom="Nos"),
			),
			(
				SimpleNamespace(name="M2", external_reference="SKU-2"),
				{"item": "ITEM-2", "sales_uom": None},
				SimpleNamespace(name="ITEM-2", stock_uom="Nos"),
			),
		]
		validated = []

		def validate_warehouse(*args):
			validated.append(args)

		with patch("marketplace_integrations.integrations.noon.inventory.get_account", return_value=account):
			with patch(
				"marketplace_integrations.integrations.noon.inventory.selected_mappings", return_value=rows
			):
				with patch(
					"marketplace_integrations.integrations.noon.inventory.validate_account_warehouse",
					side_effect=validate_warehouse,
				):
					with patch(
						"marketplace_integrations.integrations.noon.inventory.validate_warehouse_code"
					) as noon_warehouse:
						with patch(
							"marketplace_integrations.integrations.noon.inventory.get_sellable_qty",
							side_effect=[7, 0, 7, 0],
						) as quantities:
							with patch(
								"marketplace_integrations.integrations.noon.inventory.NoonClient"
							) as client_class:
								with patch(
									"marketplace_integrations.integrations.noon.inventory.persist_outcomes"
								) as persist:
									client = client_class.return_value
									client.request.side_effect = [
										Response([result("SKU-1"), result("SKU-2", "INVALID_ARGUMENT", 3)]),
										Response([result("SKU-1"), result("SKU-2")]),
									]
									first = push_stock("ACC")
									second = push_stock("ACC")
		self.assertEqual(first, {"synced": 1, "failed": 1})
		self.assertEqual(second, {"synced": 2, "failed": 0})
		self.assertEqual(len(validated), 2)
		noon_warehouse.assert_called_with("CONN", "NOON-WH")
		# The same listing UOM pricing.py resolves for each mapping (mapping's
		# sales_uom, falling back to the Item's stock UOM) must be the unit the
		# stock quantity is converted into - never the Item's raw stock-UOM
		# count - or Noon can oversell (#150 review finding).
		quantities.assert_any_call("ITEM-1", "ERP-WH", "Box")
		quantities.assert_any_call("ITEM-2", "ERP-WH", "Nos")
		payload = client.request.call_args_list[0].kwargs["json"]
		self.assertEqual(payload, client.request.call_args_list[1].kwargs["json"])
		self.assertEqual(
			payload,
			{
				"items": [
					{"warehouse_code": "NOON-WH", "partner_sku": "SKU-1", "qty": 7},
					{"warehouse_code": "NOON-WH", "partner_sku": "SKU-2", "qty": 0},
				]
			},
		)
		self.assertIsNone(persist.call_args_list[0].args[0]["M1"])
		self.assertIn("INVALID_ARGUMENT", persist.call_args_list[0].args[0]["M2"])
		self.assertIsNone(persist.call_args_list[1].args[0]["M2"])

	def test_stock_converted_to_listing_uom_not_raw_stock_uom(self):
		"""A mapping listing in a coarser sales_uom (e.g. Box = 12 Nos) must send
		the box count Noon can actually sell, not the raw stock-UOM count -
		otherwise Noon lets customers order more than physically exist (#150
		review finding: price and stock computed in different units)."""
		account = SimpleNamespace(
			name="ACC", connection="CONN", warehouse="ERP-WH", noon_warehouse_code="NOON-WH"
		)
		rows = [
			(
				SimpleNamespace(name="M1", external_reference="SKU-1"),
				{"item": "ITEM-1", "sales_uom": "Box"},
				SimpleNamespace(name="ITEM-1", stock_uom="Nos"),
			)
		]
		with patch("marketplace_integrations.integrations.noon.inventory.get_account", return_value=account):
			with patch(
				"marketplace_integrations.integrations.noon.inventory.selected_mappings", return_value=rows
			):
				with patch(
					"marketplace_integrations.integrations.noon.inventory.validate_account_warehouse"
				):
					with patch(
						"marketplace_integrations.integrations.noon.inventory.validate_warehouse_code"
					):
						with patch(
							"marketplace_integrations.integrations.noon.inventory.get_sellable_qty",
							return_value=10,
						) as quantities:
							with patch(
								"marketplace_integrations.integrations.noon.inventory.NoonClient"
							) as client_class:
								with patch(
									"marketplace_integrations.integrations.noon.inventory.persist_outcomes"
								):
									client = client_class.return_value
									client.request.return_value = Response([result("SKU-1")])
									push_stock("ACC")
		# 120 Nos in stock, sales_uom is Box (1 Box = 12 Nos): get_sellable_qty()
		# must be asked to convert into "Box" - the same listing UOM pricing.py
		# would use for this mapping - and the resulting converted quantity (10
		# boxes, not a raw 120) is what gets sent to Noon.
		quantities.assert_called_once_with("ITEM-1", "ERP-WH", "Box")
		payload = client.request.call_args.kwargs["json"]
		self.assertEqual(
			payload, {"items": [{"warehouse_code": "NOON-WH", "partner_sku": "SKU-1", "qty": 10}]}
		)

	def test_invalid_erpnext_warehouse_prevents_noon_http(self):
		account = SimpleNamespace(
			name="ACC", connection="CONN", warehouse="BAD", noon_warehouse_code="NOON-WH"
		)
		rows = [(SimpleNamespace(name="M1"), {}, SimpleNamespace(name="ITEM-1"))]
		with patch("marketplace_integrations.integrations.noon.inventory.get_account", return_value=account):
			with patch(
				"marketplace_integrations.integrations.noon.inventory.selected_mappings", return_value=rows
			):
				with patch(
					"marketplace_integrations.integrations.noon.inventory.validate_account_warehouse",
					side_effect=ValueError("bad warehouse"),
				):
					with patch("marketplace_integrations.integrations.noon.inventory.NoonClient") as client:
						with patch(
							"marketplace_integrations.integrations.noon.inventory.persist_outcomes"
						) as persist:
							with patch(
								"marketplace_integrations.integrations.noon.inventory.frappe.log_error"
							):
								self.assertEqual(push_stock("ACC"), {"synced": 0, "failed": 1})
		client.assert_not_called()
		self.assertEqual(persist.call_args.kwargs, {"inventory": True})
