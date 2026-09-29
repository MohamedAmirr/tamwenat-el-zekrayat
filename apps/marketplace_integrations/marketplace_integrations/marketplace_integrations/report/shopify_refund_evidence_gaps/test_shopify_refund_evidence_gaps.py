# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the "Shopify Refund Evidence Gaps" Script Report (#139 section
6, converted from the dashboard's custom Page). Asserts the standard
columns/data shape and that execute() produces the exact same rows as the
underlying get_refund_evidence_gaps() service function it calls - proving
there's one source of truth, not two implementations that could drift."""

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.marketplace_integrations.report.shopify_refund_evidence_gaps.shopify_refund_evidence_gaps import (
	execute,
)
from marketplace_integrations.services.shopify_reconciliation import get_refund_evidence_gaps
from marketplace_integrations.services.test_shopify_reconciliation import make_shopify_order

EXPECTED_FIELDNAMES = {"name", "order_code", "external_financial_status", "last_platform_update_on"}


class IntegrationTestShopifyRefundEvidenceGapsReport(IntegrationTestCase):
	def setUp(self):
		self.account = make_shopify_account(f"Refund Gap Report Account {frappe.generate_hash(length=8)}")

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

		expected_rows = get_refund_evidence_gaps()
		self.assertEqual([row.name for row in data], [row.name for row in expected_rows])
		self.assertIn(order.name, [row.name for row in data])

	def test_execute_requires_system_manager(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				execute()
		finally:
			frappe.set_user("Administrator")
