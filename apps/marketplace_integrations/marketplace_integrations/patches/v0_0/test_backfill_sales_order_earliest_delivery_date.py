# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Mocked test of the patch filling ``custom_earliest_delivery_date``. No database is changed."""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.patches.v0_0 import backfill_sales_order_earliest_delivery_date as backfill

_MODULE = "marketplace_integrations.patches.v0_0.backfill_sales_order_earliest_delivery_date"


class UnitTestBackfillSalesOrderEarliestDeliveryDate(UnitTestCase):
	def _run(self, batches, item_dates=None):
		"""batches: lists of (name, header date); item_dates: {name: earliest item date}."""
		order_batches = [
			[frappe._dict(name=name, delivery_date=date) for name, date in batch] for batch in batches
		] + [[]]
		item_rows = [frappe._dict(parent=name, earliest=date) for name, date in (item_dates or {}).items()]

		def get_all(doctype, **kwargs):
			if doctype == "Sales Order Item":
				return item_rows
			return order_batches.pop(0)

		with (
			patch(f"{_MODULE}.frappe") as frappe_mock,
			patch(f"{_MODULE}.create_custom_fields") as create_fields,
		):
			frappe_mock.get_all.side_effect = get_all
			backfill.execute()
		return frappe_mock, create_fields

	def test_it_creates_the_field_from_the_fixture_before_filling(self):
		frappe_mock, create_fields = self._run([[("SO-1", "2026-11-09")]])

		create_fields.assert_called_once()
		fields = create_fields.call_args.args[0]["Sales Order"]
		self.assertEqual([field["fieldname"] for field in fields], ["custom_earliest_delivery_date"])
		self.assertTrue(create_fields.call_args.kwargs["ignore_validate"])
		frappe_mock.db.set_value.assert_called_once()

	def test_it_uses_the_earliest_item_date_without_touching_modified(self):
		frappe_mock, _ = self._run(
			[[("SO-1", "2026-11-12"), ("SO-2", "2026-11-10")]], item_dates={"SO-1": "2026-11-09"}
		)

		frappe_mock.db.set_value.assert_any_call(
			"Sales Order", "SO-1", "custom_earliest_delivery_date", "2026-11-09", update_modified=False
		)
		# No item date on SO-2: the header date is the fallback.
		frappe_mock.db.set_value.assert_any_call(
			"Sales Order", "SO-2", "custom_earliest_delivery_date", "2026-11-10", update_modified=False
		)
		self.assertEqual(frappe_mock.db.set_value.call_count, 2)

	def test_an_order_without_any_date_is_left_empty(self):
		frappe_mock, _ = self._run([[("SO-1", None)]])

		frappe_mock.db.set_value.assert_not_called()

	def test_it_selects_only_marketplace_orders_with_an_empty_field(self):
		frappe_mock, _ = self._run([])

		filters = frappe_mock.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["custom_marketplace_order"], ["is", "set"])
		self.assertEqual(filters["custom_earliest_delivery_date"], ["is", "not set"])

	def test_it_works_through_several_batches(self):
		frappe_mock, _ = self._run([[("SO-1", "2026-11-09")], [("SO-2", "2026-11-10")]])

		self.assertEqual(frappe_mock.db.set_value.call_count, 2)

	def test_a_second_run_with_nothing_left_changes_nothing(self):
		frappe_mock, _ = self._run([])

		frappe_mock.db.set_value.assert_not_called()
