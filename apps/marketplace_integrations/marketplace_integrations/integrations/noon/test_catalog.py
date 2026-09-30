# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.noon.catalog import (
	list_warehouses,
	validate_warehouse_code,
)


class Response:
	status_code = 200

	def __init__(self, body):
		self.body = body

	def json(self):
		return self.body


class UnitTestNoonCatalog(UnitTestCase):
	def test_list_warehouses_follows_documented_cursor(self):
		first = {
			"next_token": "page-two",
			"warehouses": [
				{
					"warehouse_code": "WH-1",
					"display_name": "One",
					"fulfillment_system_code": "X",
					"is_active": True,
				}
			],
		}
		second = {
			"next_token": "",
			"warehouses": [
				{
					"warehouse_code": "WH-2",
					"display_name": "Two",
					"fulfillment_system_code": "X",
					"is_active": False,
				}
			],
		}
		with patch(
			"marketplace_integrations.integrations.noon.catalog.frappe.get_doc",
			return_value=SimpleNamespace(name="CONN", enabled=1),
		):
			with patch("marketplace_integrations.integrations.noon.catalog.NoonClient") as client_class:
				client = client_class.return_value
				client.request.side_effect = [Response(first), Response(second)]
				rows = list_warehouses("CONN")
		self.assertEqual([row["warehouse_code"] for row in rows], ["WH-1", "WH-2"])
		self.assertEqual(
			client.request.call_args_list[0].args, ("POST", "/warehouse-platform/v1/warehouses/list")
		)
		self.assertEqual(client.request.call_args_list[0].kwargs["json"], {})
		self.assertEqual(client.request.call_args_list[1].kwargs["json"], {"next_token": "page-two"})

	def test_inactive_warehouse_is_rejected(self):
		with patch(
			"marketplace_integrations.integrations.noon.catalog.list_warehouses",
			return_value=[
				{
					"warehouse_code": "WH-1",
					"display_name": "One",
					"fulfillment_system_code": "X",
					"is_active": False,
				}
			],
		):
			with self.assertRaises(frappe.ValidationError):
				validate_warehouse_code("CONN", "WH-1")
