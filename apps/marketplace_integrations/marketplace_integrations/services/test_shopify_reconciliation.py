# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the Shopify Reconciliation dashboard's Section 4 (Order Import
failures) and Section 6 (refund evidence gaps) live queries (#139)."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)
from marketplace_integrations.services.shopify_reconciliation import (
	get_order_import_failures,
	get_refund_evidence_gaps,
)

def make_shopify_order(account_name, **overrides):
	suffix = frappe.generate_hash(length=8)
	values = {
		"doctype": "Marketplace Order",
		"marketplace_account": account_name,
		"external_order_id": f"reconciliation-order-{suffix}",
		"platform": "Shopify",
		"order_code": f"#{suffix}",
		**overrides,
	}
	order = frappe.get_doc(values)
	order.flags.skip_sync_status_update = True
	order.order_identity_key = compute_order_identity_key(account_name, order.external_order_id)
	order.insert()
	return order


class IntegrationTestGetOrderImportFailures(IntegrationTestCase):
	def test_returns_only_order_import_error_rows(self):
		frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"status": "Error",
				"attempted_at": now_datetime(),
				"traceback": "boom",
			}
		).insert()
		# A Sales Order Sync error row must not show up here.
		account = make_shopify_account(f"Order Import Failures {frappe.generate_hash(length=8)}")
		try:
			order = make_shopify_order(account.name)
			frappe.get_doc(
				{
					"doctype": "Marketplace Sync Log",
					"sync_type": "Sales Order Sync",
					"marketplace_order": order.name,
					"status": "Error",
					"attempted_at": now_datetime(),
				}
			).insert()

			rows = get_order_import_failures()

			self.assertTrue(any(row.traceback == "boom" for row in rows))
			self.assertFalse(any(row.name == order.name for row in rows))
		finally:
			frappe.delete_doc("Marketplace Account", account.name, ignore_permissions=True, force=True)
			frappe.delete_doc(
				"Shopify Connection", account.connection, ignore_permissions=True, force=True
			)

	def test_requires_system_manager(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				get_order_import_failures()
		finally:
			frappe.set_user("Administrator")


class IntegrationTestGetRefundEvidenceGaps(IntegrationTestCase):
	def setUp(self):
		self.account = make_shopify_account(f"Refund Gap Account {frappe.generate_hash(length=8)}")

	def tearDown(self):
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc(
			"Shopify Connection", self.account.connection, ignore_permissions=True, force=True
		)

	def test_refunded_order_with_empty_refunds_json_string_is_a_gap(self):
		order = make_shopify_order(
			self.account.name, external_financial_status="REFUNDED", refunds="[]"
		)
		rows = get_refund_evidence_gaps()
		self.assertIn(order.name, [row.name for row in rows])

	def test_refunded_order_with_null_refunds_is_a_gap(self):
		order = make_shopify_order(self.account.name, external_financial_status="PARTIALLY_REFUNDED")
		self.assertFalse(order.refunds)
		rows = get_refund_evidence_gaps()
		self.assertIn(order.name, [row.name for row in rows])

	def test_refunded_order_with_populated_refunds_is_not_a_gap(self):
		order = make_shopify_order(
			self.account.name,
			external_financial_status="REFUNDED",
			refunds='[{"id": "gid://shopify/Refund/1"}]',
		)
		rows = get_refund_evidence_gaps()
		self.assertNotIn(order.name, [row.name for row in rows])

	def test_non_refund_financial_status_is_not_a_gap(self):
		order = make_shopify_order(self.account.name, external_financial_status="PAID", refunds="[]")
		rows = get_refund_evidence_gaps()
		self.assertNotIn(order.name, [row.name for row in rows])

	def test_requires_system_manager(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				get_refund_evidence_gaps()
		finally:
			frappe.set_user("Administrator")
