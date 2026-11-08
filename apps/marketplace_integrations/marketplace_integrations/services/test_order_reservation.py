# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests of ``services/order_reservation.py`` (#499). Run on test.localhost only; nothing commits, so the
class-level rollback removes everything."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.services import order_reservation as svc


def _item():
	code = f"RES-{frappe.generate_hash(length=8)}"
	frappe.get_doc(
		{"doctype": "Item", "item_code": code, "item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 1}
	).insert(ignore_permissions=True)
	return code


class IntegrationTestOrderReservation(IntegrationTestCase):
	def setUp(self):
		self.settings = frappe.get_single("Stock Settings")
		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", 1)
		frappe.db.set_single_value("Stock Settings", "auto_reserve_stock", 0)

	def test_disabled_setting_does_nothing(self):
		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", 0)
		self.assertEqual(svc.reserve_or_wait("SO-DOES-NOT-EXIST"), svc.DISABLED)

	def test_non_shopify_or_missing_order_is_skipped(self):
		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", 1)
		with patch.object(svc.frappe.db, "get_value", return_value="x"), patch.object(
			svc.frappe, "get_doc", return_value=frappe._dict(docstatus=1, status="To Deliver", get=lambda k: "Noon", name="x")
		):
			self.assertEqual(svc.reserve_or_wait("x"), svc.SKIPPED)

	def test_reserved_by_others_excludes_own_orders_and_is_zero_for_unknown_items(self):
		item = _item()
		self.assertEqual(svc.reserved_by_others("Stores - _TC", [item], ()), {item: 0})

	def test_stage_field_ships(self):
		self.assertTrue(svc.stage_installed())

	def test_fmt(self):
		self.assertEqual(svc._fmt(3), "3")
		self.assertEqual(svc._fmt(2.5), "2.5")
