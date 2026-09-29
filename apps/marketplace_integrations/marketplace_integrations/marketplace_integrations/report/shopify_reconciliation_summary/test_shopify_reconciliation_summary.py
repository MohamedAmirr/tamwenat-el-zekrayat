# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the "Shopify Reconciliation Summary" Script Report (#139),
which replaces the dashboard's 5 Number Cards with one date-filterable
report covering both those error/failure counts (metrics 1-5, current-state,
date-filter-independent) and 5 new business metrics (metrics 6-10, scoped to
the from_date/to_date filter). Exercises execute() end to end and the
underlying services/shopify_reconciliation.py functions directly for the
finer-grained cases.

test.localhost accumulates real Marketplace Order / Shopify Webhook Delivery
rows across test runs (dozens of orders, hundreds of webhook deliveries with
today's timestamp), so a live date-range query for "today" is not a reliable
way to test the currency-grouping and success-rate arithmetic in isolation -
a same-day assertion would be polluted by unrelated rows already in the
database. Those specific cases (mixed-currency flagging, exact rate math)
mock ``frappe.get_all`` to control exactly what rows the function under test
sees, same as this project's other pure-computation unit tests; the
DB-backed tests (execute() end to end, date filter presence/absence, refund
JSON extraction) still hit the real database."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime

from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.marketplace_integrations.report.shopify_reconciliation_summary.shopify_reconciliation_summary import (
	execute,
)
from marketplace_integrations.services.shopify_reconciliation import (
	get_reconciliation_summary,
	get_sales_order_sync_success_rate_in_range,
	get_total_order_value_in_range,
	get_total_refunded_amount_in_range,
	get_webhook_processing_success_rate_in_range,
)
from marketplace_integrations.services.test_shopify_reconciliation import make_shopify_order

EXPECTED_FIELDNAMES = {"metric", "value", "detail"}
CURRENT_STATE_METRICS = {
	"Shopify Catalog Push Errors (current)",
	"Shopify Inventory Sync Errors (current)",
	"Marketplace Orders: Sync Error (current)",
	"Shopify Webhook Deliveries: Error (current)",
	"Shopify Webhook Deliveries: Exhausted Retries (current)",
}
RANGED_METRICS = {
	"Total Shopify Orders Received (in selected range)",
	"Total Order Value (in selected range)",
	"Sales Order Sync Success Rate (in selected range)",
	"Total Refunded Amount (in selected range)",
	"Webhook Processing Success Rate (in selected range)",
}


def make_webhook_delivery(connection, **overrides):
	suffix = frappe.generate_hash(length=8)
	values = {
		"doctype": "Shopify Webhook Delivery",
		"connection": connection,
		"shop_domain": "reconciliation-summary-test.myshopify.com",
		"topic": "ORDERS_UPDATED",
		"webhook_id": suffix,
		"status": "Received",
		"attempt_count": 0,
		"received_at": now_datetime(),
		**overrides,
	}
	return frappe.get_doc(values).insert()


class IntegrationTestShopifyReconciliationSummaryReport(IntegrationTestCase):
	def setUp(self):
		self.account = make_shopify_account(
			f"Reconciliation Summary Report Account {frappe.generate_hash(length=8)}"
		)

	def tearDown(self):
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Shopify Connection", self.account.connection, ignore_permissions=True, force=True)

	def test_execute_returns_expected_columns_and_all_10_metrics(self):
		columns, data = execute()

		fieldnames = {column["fieldname"] for column in columns}
		self.assertEqual(fieldnames, EXPECTED_FIELDNAMES)
		for column in columns:
			self.assertIn("label", column)
			self.assertIn("fieldtype", column)

		metrics = {row["metric"] for row in data}
		self.assertEqual(metrics, CURRENT_STATE_METRICS | RANGED_METRICS)

	def test_execute_matches_service_function(self):
		_columns, data = execute({"from_date": "2020-01-01", "to_date": "2020-01-31"})
		expected = get_reconciliation_summary(from_date="2020-01-01", to_date="2020-01-31")
		self.assertEqual(data, expected)

	def test_execute_requires_system_manager(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				execute()
		finally:
			frappe.set_user("Administrator")

	def test_current_state_metrics_ignore_date_filter(self):
		mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": _make_test_item(),
				"external_reference": f"gid://shopify/ProductVariant/{frappe.generate_hash(length=8)}",
				"sync_status": "Error",
			}
		).insert()
		try:
			far_past = get_reconciliation_summary(from_date="2000-01-01", to_date="2000-01-02")
			far_future = get_reconciliation_summary(from_date="2999-01-01", to_date="2999-01-02")
			no_filter = get_reconciliation_summary()

			for rows in (far_past, far_future, no_filter):
				by_metric = {row["metric"]: row["value"] for row in rows}
				self.assertGreaterEqual(by_metric["Shopify Catalog Push Errors (current)"], 1)

			self.assertEqual(
				{row["value"] for row in far_past if row["metric"] == "Shopify Catalog Push Errors (current)"},
				{row["value"] for row in far_future if row["metric"] == "Shopify Catalog Push Errors (current)"},
			)
		finally:
			frappe.delete_doc(
				"Marketplace Product Mapping", mapping.name, ignore_permissions=True, force=True
			)

	def test_orders_received_in_range_metric_respects_date_filter(self):
		in_range = make_shopify_order(self.account.name, received_on=now_datetime())
		out_of_range = make_shopify_order(self.account.name, received_on=add_days(now_datetime(), -30))

		from_date = str(add_days(now_datetime(), -1).date())
		to_date = str(add_days(now_datetime(), 1).date())

		rows = get_reconciliation_summary(from_date=from_date, to_date=to_date)
		count_row = next(
			row for row in rows if row["metric"] == "Total Shopify Orders Received (in selected range)"
		)
		self.assertGreaterEqual(count_row["value"], 1)

		rows_out = get_reconciliation_summary(from_date="2000-01-01", to_date="2000-01-02")
		count_row_out = next(
			row for row in rows_out if row["metric"] == "Total Shopify Orders Received (in selected range)"
		)
		self.assertEqual(count_row_out["value"], 0)

		self.assertTrue(in_range.name)
		self.assertTrue(out_of_range.name)

	def test_total_order_value_sums_single_currency_orders(self):
		rows = [
			frappe._dict({"order_total": 100.0, "shop_currency": "EGP"}),
			frappe._dict({"order_total": 50.0, "shop_currency": "EGP"}),
		]
		with patch("frappe.get_all", return_value=rows) as mocked_get_all:
			result = get_total_order_value_in_range(from_date="2020-01-01", to_date="2020-01-31")

		mocked_get_all.assert_called_once()
		self.assertFalse(result["is_mixed"])
		self.assertEqual(result["currency"], "EGP")
		self.assertEqual(result["total"], 150.0)

	def test_total_order_value_flags_mixed_currencies_instead_of_summing(self):
		rows = [
			frappe._dict({"order_total": 100.0, "shop_currency": "EGP"}),
			frappe._dict({"order_total": 10.0, "shop_currency": "USD"}),
		]
		with patch("frappe.get_all", return_value=rows):
			result = get_total_order_value_in_range(from_date="2020-01-01", to_date="2020-01-31")

		self.assertTrue(result["is_mixed"])
		self.assertIsNone(result["total"])
		self.assertEqual(result["breakdown"], {"EGP": 100.0, "USD": 10.0})

	def test_sales_order_sync_success_rate_computes_correctly(self):
		rows = [
			frappe._dict({"erpnext_sync_status": "Synced"}),
			frappe._dict({"erpnext_sync_status": "Sync Error"}),
		]
		with patch("frappe.get_all", return_value=rows):
			result = get_sales_order_sync_success_rate_in_range(
				from_date="2020-01-01", to_date="2020-01-31"
			)

		self.assertEqual(result["denominator"], 2)
		self.assertEqual(result["numerator"], 1)
		self.assertEqual(result["rate"], 50.0)

	def test_sales_order_sync_success_rate_uses_real_orders_in_range(self):
		"""One DB-backed sanity check (unlike the mocked test above) that the
		function actually queries live Marketplace Order rows scoped to this
		test's own account-created orders, not just that its arithmetic is
		correct in isolation."""
		order = make_shopify_order(
			self.account.name, received_on=now_datetime(), erpnext_sync_status="Synced"
		)
		from_date = str(add_days(now_datetime(), -1).date())
		to_date = str(add_days(now_datetime(), 1).date())

		result = get_sales_order_sync_success_rate_in_range(from_date=from_date, to_date=to_date)

		self.assertGreaterEqual(result["denominator"], 1)
		self.assertGreaterEqual(result["numerator"], 1)
		self.assertTrue(order.name)

	def test_sales_order_sync_success_rate_zero_denominator_does_not_crash(self):
		result = get_sales_order_sync_success_rate_in_range(
			from_date="2000-01-01", to_date="2000-01-02"
		)
		self.assertEqual(result["denominator"], 0)
		self.assertEqual(result["numerator"], 0)
		self.assertIsNone(result["rate"])

	def test_total_refunded_amount_sums_refund_line_amounts(self):
		make_shopify_order(
			self.account.name,
			last_platform_update_on=now_datetime(),
			external_financial_status="PARTIALLY_REFUNDED",
			refunds=frappe.as_json(
				[
					{
						"id": "gid://shopify/Refund/1",
						"totalRefundedSet": {"shopMoney": {"amount": "18.00", "currencyCode": "EGP"}},
					}
				]
			),
		)
		make_shopify_order(
			self.account.name,
			last_platform_update_on=now_datetime(),
			external_financial_status="REFUNDED",
			refunds=frappe.as_json(
				[
					{
						"id": "gid://shopify/Refund/2",
						"totalRefundedSet": {"shopMoney": {"amount": "5.50", "currencyCode": "EGP"}},
					}
				]
			),
		)
		# A gap order (no refund evidence) must not contribute.
		make_shopify_order(
			self.account.name,
			last_platform_update_on=now_datetime(),
			external_financial_status="REFUNDED",
			refunds="[]",
		)

		from_date = str(add_days(now_datetime(), -1).date())
		to_date = str(add_days(now_datetime(), 1).date())
		result = get_total_refunded_amount_in_range(from_date=from_date, to_date=to_date)

		self.assertFalse(result["is_mixed"])
		self.assertEqual(result["currency"], "EGP")
		self.assertEqual(result["total"], 23.5)

	def test_total_refunded_amount_out_of_range_order_excluded(self):
		make_shopify_order(
			self.account.name,
			last_platform_update_on=add_days(now_datetime(), -30),
			external_financial_status="REFUNDED",
			refunds=frappe.as_json(
				[{"id": "gid://shopify/Refund/3", "totalRefundedSet": {"shopMoney": {"amount": "9.00", "currencyCode": "EGP"}}}]
			),
		)

		from_date = str(add_days(now_datetime(), -1).date())
		to_date = str(add_days(now_datetime(), 1).date())
		result = get_total_refunded_amount_in_range(from_date=from_date, to_date=to_date)

		self.assertEqual(result["total"], 0.0)

	def test_webhook_processing_success_rate_computes_correctly(self):
		rows = [
			frappe._dict({"status": "Processed"}),
			frappe._dict({"status": "Error"}),
			frappe._dict({"status": "Received"}),
		]
		with patch("frappe.get_all", return_value=rows):
			result = get_webhook_processing_success_rate_in_range(
				from_date="2020-01-01", to_date="2020-01-31"
			)

		self.assertEqual(result["denominator"], 3)
		self.assertEqual(result["numerator"], 1)
		self.assertAlmostEqual(result["rate"], 33.33, places=1)

	def test_webhook_processing_success_rate_uses_real_deliveries_in_range(self):
		"""One DB-backed sanity check (unlike the mocked test above) that the
		function actually queries live Shopify Webhook Delivery rows, not
		just that its arithmetic is correct in isolation."""
		delivery = make_webhook_delivery(
			self.account.connection, status="Processed", received_at=now_datetime()
		)
		from_date = str(add_days(now_datetime(), -1).date())
		to_date = str(add_days(now_datetime(), 1).date())

		result = get_webhook_processing_success_rate_in_range(from_date=from_date, to_date=to_date)

		self.assertGreaterEqual(result["denominator"], 1)
		self.assertGreaterEqual(result["numerator"], 1)
		self.assertTrue(delivery.name)

	def test_webhook_processing_success_rate_zero_denominator_does_not_crash(self):
		result = get_webhook_processing_success_rate_in_range(
			from_date="2000-01-01", to_date="2000-01-02"
		)
		self.assertEqual(result["denominator"], 0)
		self.assertEqual(result["numerator"], 0)
		self.assertIsNone(result["rate"])


def _make_test_item():
	from marketplace_integrations.integrations.shopify.test_products import make_item

	return make_item(f"Reconciliation Summary Test Item {frappe.generate_hash(length=8)}")
