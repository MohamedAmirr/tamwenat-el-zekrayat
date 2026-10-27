# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.services.listing_quantity import (
	BundleStockError,
	get_sellable_qty,
	get_stock_quantities,
	resolve_stock_quantities,
)


class _QuantityHelpers:
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



class IntegrationTestListingQuantity(_QuantityHelpers, IntegrationTestCase):
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


class IntegrationTestBundleQuantity(_QuantityHelpers, IntegrationTestCase):
	"""Product Bundle sellable quantity (#368). Reuses the helpers above."""

	def _make_parent(self, marker, is_stock_item=0):
		return frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"TEST-BUNDLE-PARENT-{marker}-{self.suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": is_stock_item,
			}
		).insert()

	def _make_bundle(self, parent, components):
		"""``components`` is ``[(item_code, qty)]``.

		ERPNext rejects a bundle with no rows or with a component Item that does not exist at
		insert, so an invalid one is built legitimately and then broken with direct DB writes:
		a missing component, or one that is itself an enabled bundle, is first a placeholder Item
		that is then repointed in the row to that code, and an empty bundle has its rows deleted.
		"""
		rows = []
		for index, (code, qty) in enumerate(components):
			is_bundle = frappe.db.exists("Product Bundle", {"new_item_code": code, "disabled": 0})
			if frappe.db.exists("Item", code) and not is_bundle:
				rows.append((code, qty, None))
			else:
				rows.append((self._make_item(f"PH{index}").name, qty, code))
		if not rows:
			rows = [(self._make_item("PHEMPTY").name, 1, None)]
		bundle = frappe.get_doc(
			{
				"doctype": "Product Bundle",
				"new_item_code": parent.name,
				"items": [{"item_code": code, "qty": qty} for code, qty, _missing in rows],
			}
		).insert(ignore_permissions=True)
		if not components:
			frappe.db.delete("Product Bundle Item", {"parent": bundle.name})
		for row, (_code, _qty, missing_code) in zip(bundle.items, rows, strict=True):
			if missing_code:
				frappe.db.set_value("Product Bundle Item", row.name, "item_code", missing_code)
		return bundle

	def _quantities(self, parent):
		return resolve_stock_quantities(self.warehouse, [parent.name])

	def test_single_component(self):
		part = self._make_item("SINGLE")
		self._set_actual_qty(part, 10)
		parent = self._make_parent("SINGLE")
		self._make_bundle(parent, [(part.name, 2)])

		self.assertEqual(self._quantities(parent), ({parent.name: 5}, {}))

	def test_several_components_take_the_minimum_floored(self):
		first, second, third = (self._make_item(marker) for marker in ("A", "B", "C"))
		self._set_actual_qty(first, 100)  # 100 / 1 = 100
		self._set_actual_qty(second, 25)  # 25 / 4 = 6.25 -> 6
		self._set_actual_qty(third, 9)  # 9 / 3 = 3 -> the minimum
		parent = self._make_parent("MULTI")
		self._make_bundle(parent, [(first.name, 1), (second.name, 4), (third.name, 3)])

		self.assertEqual(self._quantities(parent), ({parent.name: 3}, {}))

	def test_zero_and_negative_component_stock_reads_zero_not_a_problem(self):
		stocked = self._make_item("STOCKED")
		empty = self._make_item("EMPTY")
		self._set_actual_qty(stocked, 8)
		self._set_actual_qty(empty, -4)
		parent = self._make_parent("ZERO")
		self._make_bundle(parent, [(stocked.name, 1), (empty.name, 1)])

		self.assertEqual(self._quantities(parent), ({parent.name: 0}, {}))

	def test_component_without_bin_reads_zero(self):
		no_bin = self._make_item("NOBIN")
		parent = self._make_parent("NOBINP")
		self._make_bundle(parent, [(no_bin.name, 1)])

		self.assertEqual(self._quantities(parent), ({parent.name: 0}, {}))

	def test_missing_component_item_is_reported_not_zero(self):
		parent = self._make_parent("MISSING")
		self._make_bundle(parent, [("TEST-NO-SUCH-ITEM", 1)])

		quantities, problems = self._quantities(parent)

		self.assertEqual(quantities, {})
		self.assertIn("TEST-NO-SUCH-ITEM", problems[parent.name])

	def test_non_stock_component_is_reported_not_zero(self):
		service = self._make_parent("SERVICE")
		parent = self._make_parent("NONSTOCKP")
		self._make_bundle(parent, [(service.name, 1)])

		quantities, problems = self._quantities(parent)

		self.assertEqual(quantities, {})
		self.assertIn(service.name, problems[parent.name])

	def test_bundle_without_components_is_reported_not_zero(self):
		parent = self._make_parent("EMPTYB")
		self._make_bundle(parent, [])

		quantities, problems = self._quantities(parent)

		self.assertEqual(quantities, {})
		self.assertIn(parent.name, problems[parent.name])

	def test_component_that_is_itself_a_bundle_is_reported(self):
		inner_part = self._make_item("INNERPART")
		self._set_actual_qty(inner_part, 5)
		inner = self._make_parent("INNER")
		self._make_bundle(inner, [(inner_part.name, 1)])
		outer = self._make_parent("OUTER")
		self._make_bundle(outer, [(inner.name, 1)])

		quantities, problems = resolve_stock_quantities(self.warehouse, [outer.name, inner.name])

		self.assertEqual(quantities, {inner.name: 5})
		self.assertIn(inner.name, problems[outer.name])

	def test_disabled_bundle_is_ignored_and_item_keeps_plain_path(self):
		part = self._make_item("DISABLEDPART")
		self._set_actual_qty(part, 10)
		parent = self._make_parent("DISABLEDB")
		bundle = self._make_bundle(parent, [(part.name, 1)])
		frappe.db.set_value("Product Bundle", bundle.name, "disabled", 1)

		self.assertEqual(self._quantities(parent), ({parent.name: 0}, {}))

	def test_item_without_bundle_keeps_plain_result(self):
		item = self._make_item("PLAIN")
		self._set_actual_qty(item, 7.9)

		self.assertEqual(resolve_stock_quantities(self.warehouse, [item.name]), ({item.name: 7}, {}))

	def test_mixed_batch_splits_plain_bundle_and_problem(self):
		plain = self._make_item("MIXPLAIN")
		part = self._make_item("MIXPART")
		self._set_actual_qty(plain, 3)
		self._set_actual_qty(part, 9)
		good = self._make_parent("MIXGOOD")
		self._make_bundle(good, [(part.name, 3)])
		bad = self._make_parent("MIXBAD")
		self._make_bundle(bad, [])

		quantities, problems = resolve_stock_quantities(self.warehouse, [plain.name, good.name, bad.name])

		self.assertEqual(quantities, {plain.name: 3, good.name: 3})
		self.assertEqual(list(problems), [bad.name])

	def test_sales_uom_conversion_applies_on_top_of_bundle_quantity(self):
		part = self._make_item("UOMPART")
		self._set_actual_qty(part, 60)
		parent = self._make_parent("UOMP")
		parent.append("uoms", {"uom": "Box", "conversion_factor": 4})
		parent.save()
		self._make_bundle(parent, [(part.name, 1)])

		# 60 Nos bundle quantity, 4 Nos per Box -> 15 Box.
		self.assertEqual(get_sellable_qty(parent.name, self.warehouse, "Box"), 15)
		self.assertEqual(get_sellable_qty(parent.name, self.warehouse, "Nos"), 60)

	def test_get_sellable_qty_raises_for_an_invalid_bundle(self):
		parent = self._make_parent("RAISES")
		self._make_bundle(parent, [])

		with self.assertRaises(BundleStockError):
			get_sellable_qty(parent.name, self.warehouse, "Nos")
