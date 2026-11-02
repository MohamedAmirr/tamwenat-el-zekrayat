# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.noon.catalog import (
	fbpi_ready,
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


class UnitTestNoonFbpiReady(UnitTestCase):
	"""``fbpi_ready`` reads stored values only: no Noon call and no database."""

	def _account(self, **values):
		stored = {
			"name": "MKTACC-1",
			"noon_fulfillment_system_code": "FBPI",
			"noon_warehouse_active": 1,
			"noon_warehouse_checked_on": "2026-11-01 10:00:00",
		}
		stored.update(values)
		return frappe._dict(stored)

	def test_ready_only_when_the_code_is_fbpi_and_the_warehouse_is_active(self):
		self.assertEqual(fbpi_ready(self._account()), (True, ""))

	def test_a_never_checked_warehouse_is_refused(self):
		ok, reason = fbpi_ready(self._account(noon_fulfillment_system_code="", noon_warehouse_checked_on=None))
		self.assertFalse(ok)
		self.assertIn("never checked", reason)

	def test_a_different_code_is_refused_and_shows_the_returned_value_escaped(self):
		ok, reason = fbpi_ready(self._account(noon_fulfillment_system_code="<b>OTHER</b>"))
		self.assertFalse(ok)
		self.assertIn("&lt;b&gt;OTHER&lt;/b&gt;", reason)
		self.assertIn("FBPI", reason)
		self.assertNotIn("<b>", reason)

	def test_an_inactive_warehouse_is_refused(self):
		ok, reason = fbpi_ready(self._account(noon_warehouse_active=0))
		self.assertFalse(ok)
		self.assertIn("not active", reason)

	def test_surrounding_spaces_and_letter_case_do_not_make_the_code_differ(self):
		for code in (" fbpi ", "FBPI", "Fbpi"):
			self.assertTrue(fbpi_ready(self._account(noon_fulfillment_system_code=code))[0])
