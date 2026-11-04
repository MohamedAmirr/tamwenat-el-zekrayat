# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import datetime

from frappe.tests import IntegrationTestCase

from marketplace_integrations.services.sales_order_delivery import (
	earliest_delivery_date,
	needs_update,
)


class TestSalesOrderDeliveryPure(IntegrationTestCase):
	def test_earliest_is_the_minimum_of_the_item_dates(self):
		self.assertEqual(
			earliest_delivery_date(["2026-11-08", "2026-11-05", datetime.date(2026, 11, 9)]),
			datetime.date(2026, 11, 5),
		)

	def test_items_without_a_date_are_ignored(self):
		self.assertEqual(
			earliest_delivery_date([None, "", "2026-11-08"], "2026-11-20"), datetime.date(2026, 11, 8)
		)

	def test_header_date_is_used_when_no_item_has_a_date(self):
		self.assertEqual(earliest_delivery_date([None], "2026-11-20"), datetime.date(2026, 11, 20))

	def test_nothing_when_there_is_no_date_at_all(self):
		self.assertIsNone(earliest_delivery_date([None, ""], None))
		self.assertFalse(needs_update("2026-11-04", None))

	def test_update_needed_only_when_the_stored_value_differs(self):
		earliest = datetime.date(2026, 11, 8)
		self.assertFalse(needs_update("2026-11-08", earliest))
		self.assertFalse(needs_update(datetime.date(2026, 11, 8), earliest))
		self.assertTrue(needs_update("2026-11-04", earliest))
		self.assertTrue(needs_update(None, earliest))
