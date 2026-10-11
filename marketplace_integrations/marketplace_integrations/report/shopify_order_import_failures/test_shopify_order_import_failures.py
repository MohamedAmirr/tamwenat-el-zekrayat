# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the "Shopify Order Import Failures" Script Report (#139 section
4, converted from the dashboard's custom Page). Asserts the standard
columns/data shape and that execute() produces the exact same rows as the
underlying get_order_import_failures() service function it calls - proving
there's one source of truth, not two implementations that could drift."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.marketplace_integrations.report.shopify_order_import_failures.shopify_order_import_failures import (
	execute,
)
from marketplace_integrations.services.shopify_reconciliation import get_order_import_failures

EXPECTED_FIELDNAMES = {"name", "marketplace_account", "trigger_source", "attempted_at", "request_data", "traceback"}


class IntegrationTestShopifyOrderImportFailuresReport(IntegrationTestCase):
	def test_execute_returns_expected_columns_and_matches_service_function(self):
		# The report lists Shopify accounts' failures only, so the row needs one.
		account = make_shopify_account(f"Import Failures Report {frappe.generate_hash(length=8)}")
		self.addCleanup(frappe.delete_doc, "Shopify Connection", account.connection, force=True)
		self.addCleanup(frappe.delete_doc, "Marketplace Account", account.name, force=True)
		log = frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"marketplace_account": account.name,
				"status": "Error",
				"attempted_at": now_datetime(),
				"traceback": "report fixture failure",
			}
		).insert()
		self.addCleanup(frappe.delete_doc, "Marketplace Sync Log", log.name, force=True)

		columns, data = execute()

		fieldnames = {column["fieldname"] for column in columns}
		self.assertEqual(fieldnames, EXPECTED_FIELDNAMES)
		for column in columns:
			self.assertIn("label", column)
			self.assertIn("fieldtype", column)

		expected_rows = get_order_import_failures()
		self.assertEqual([row.name for row in data], [row.name for row in expected_rows])
		self.assertIn(log.name, [row.name for row in data])

	def test_execute_requires_system_manager(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				execute()
		finally:
			frappe.set_user("Administrator")
