# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.services.listing_quantity import get_sellable_qty, get_stock_quantities


class IntegrationTestListingQuantity(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.warehouse = frappe.get_all(
			"Warehouse", filters={"disabled": 0, "is_group": 0}, pluck="name", limit=1
		)[0]

	def _make_item(self, marker):
		return frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"TEST-LISTING-QTY-{marker}-{self.suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}
		).insert()

	def _set_actual_qty(self, item, qty, warehouse=None):
		warehouse = warehouse or self.warehouse
		bin_name = frappe.db.get_value("Bin", {"item_code": item.name, "warehouse": warehouse}, "name")
		if bin_name:
			frappe.db.set_value("Bin", bin_name, "actual_qty", qty)
		else:
			frappe.get_doc(
				{"doctype": "Bin", "item_code": item.name, "warehouse": warehouse, "actual_qty": qty}
			).insert()

	def test_bulk_read_floors_clamps_negatives_and_defaults_missing_bins_to_zero(self):
		fractional = self._make_item("FRAC")
		negative = self._make_item("NEG")
		no_bin = self._make_item("NOBIN")
		whole = self._make_item("WHOLE")
		self._set_actual_qty(fractional, 7.9)
		self._set_actual_qty(negative, -3)
		self._set_actual_qty(whole, 12)

		result = get_stock_quantities(
			self.warehouse, [fractional.name, negative.name, no_bin.name, whole.name, whole.name]
		)

		self.assertEqual(result, {fractional.name: 7, negative.name: 0, no_bin.name: 0, whole.name: 12})
		self.assertTrue(all(isinstance(value, int) for value in result.values()))

	def test_bulk_read_uses_one_bin_query(self):
		items = [self._make_item(f"Q{index}") for index in range(3)]
		for item in items:
			self._set_actual_qty(item, 5)

		with patch.object(frappe, "get_all", wraps=frappe.get_all) as mocked_get_all:
			get_stock_quantities(self.warehouse, [item.name for item in items])

		mocked_get_all.assert_called_once()
		self.assertEqual(mocked_get_all.call_args.args[0], "Bin")

	def test_bulk_read_only_counts_the_given_warehouse(self):
		other_warehouse = frappe.get_all(
			"Warehouse",
			filters={"disabled": 0, "is_group": 0, "name": ["!=", self.warehouse]},
			pluck="name",
			limit=1,
		)
		if not other_warehouse:
			self.skipTest("needs a second leaf Warehouse")
		item = self._make_item("OTHERWH")
		self._set_actual_qty(item, 9, other_warehouse[0])

		self.assertEqual(get_stock_quantities(self.warehouse, [item.name]), {item.name: 0})

	def test_empty_input_returns_empty_dict(self):
		self.assertEqual(get_stock_quantities(self.warehouse, []), {})

	def test_talabat_single_item_read_is_unchanged_by_the_move(self):
		item = self._make_item("TALABAT")
		self._set_actual_qty(item, 7.9)

		self.assertEqual(get_sellable_qty(item.name, self.warehouse, "Nos"), 7)
