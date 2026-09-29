# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the "Shopify Refunded Orders" Script Report (#139) - the
general-purpose view of ALL Shopify orders with a refund financial status,
unlike "Shopify Refund Evidence Gaps" which only shows the subset missing
``refunds`` evidence. Asserts the standard columns/data shape, that the
``from_date``/``to_date`` filters actually filter on
``last_platform_update_on``, and that ``has_refund_evidence`` is correct for
both a gap case (empty ``refunds``) and a has-evidence case (populated
``refunds``)."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime

from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.marketplace_integrations.report.shopify_refunded_orders.shopify_refunded_orders import (
	execute,
)
from marketplace_integrations.services.shopify_reconciliation import get_refunded_orders
from marketplace_integrations.services.test_shopify_reconciliation import make_shopify_order

EXPECTED_FIELDNAMES = {
	"name",
	"order_code",
	"external_financial_status",
	"last_platform_update_on",
	"has_refund_evidence",
}


class IntegrationTestShopifyRefundedOrdersReport(IntegrationTestCase):
	def setUp(self):
		self.account = make_shopify_account(
			f"Refunded Orders Report Account {frappe.generate_hash(length=8)}"
		)

	def tearDown(self):
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Shopify Connection", self.account.connection, ignore_permissions=True, force=True)

	def test_execute_returns_expected_columns_and_matches_service_function(self):
		order = make_shopify_order(self.account.name, external_financial_status="REFUNDED", refunds="[]")

		columns, data = execute()

		fieldnames = {column["fieldname"] for column in columns}
		self.assertEqual(fieldnames, EXPECTED_FIELDNAMES)
		for column in columns:
			self.assertIn("label", column)
			self.assertIn("fieldtype", column)

		expected_rows = get_refunded_orders()
		self.assertEqual([row.name for row in data], [row.name for row in expected_rows])
		self.assertIn(order.name, [row.name for row in data])

	def test_includes_orders_regardless_of_refund_evidence(self):
		gap_order = make_shopify_order(self.account.name, external_financial_status="REFUNDED", refunds="[]")
		evidenced_order = make_shopify_order(
			self.account.name,
			external_financial_status="PARTIALLY_REFUNDED",
			refunds='[{"id": "gid://shopify/Refund/1"}]',
		)

		rows = get_refunded_orders()
		row_names = [row.name for row in rows]

		self.assertIn(gap_order.name, row_names)
		self.assertIn(evidenced_order.name, row_names)

		by_name = {row.name: row for row in rows}
		self.assertFalse(by_name[gap_order.name].has_refund_evidence)
		self.assertTrue(by_name[evidenced_order.name].has_refund_evidence)

	def test_non_refund_financial_status_is_excluded(self):
		order = make_shopify_order(self.account.name, external_financial_status="PAID", refunds="[]")
		rows = get_refunded_orders()
		self.assertNotIn(order.name, [row.name for row in rows])

	def test_date_filter_excludes_orders_outside_range(self):
		in_range = make_shopify_order(
			self.account.name,
			external_financial_status="REFUNDED",
			refunds="[]",
			last_platform_update_on=now_datetime(),
		)
		out_of_range = make_shopify_order(
			self.account.name,
			external_financial_status="REFUNDED",
			refunds="[]",
			last_platform_update_on=add_days(now_datetime(), -30),
		)

		from_date = add_days(now_datetime(), -1).date()
		to_date = add_days(now_datetime(), 1).date()

		_columns, data = execute({"from_date": str(from_date), "to_date": str(to_date)})
		row_names = [row.name for row in data]

		self.assertIn(in_range.name, row_names)
		self.assertNotIn(out_of_range.name, row_names)

	def test_execute_requires_system_manager(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				execute()
		finally:
			frappe.set_user("Administrator")
