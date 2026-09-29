# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from types import SimpleNamespace
from unittest.mock import patch

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

	def test_warehouse_partition_payload_failure_and_retry(self):
		account = SimpleNamespace(
			name="ACC", connection="CONN", warehouse="ERP-WH", noon_warehouse_code="NOON-WH"
		)
		rows = [
			(
				SimpleNamespace(name="M1", external_reference="SKU-1"),
				{"item": "ITEM-1"},
				SimpleNamespace(name="ITEM-1"),
			),
			(
				SimpleNamespace(name="M2", external_reference="SKU-2"),
				{"item": "ITEM-2"},
				SimpleNamespace(name="ITEM-2"),
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
							"marketplace_integrations.integrations.noon.inventory.get_stock_quantities",
							return_value={"ITEM-1": 7, "ITEM-2": 0},
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
		quantities.assert_called_with("ERP-WH", ["ITEM-1", "ITEM-2"])
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
