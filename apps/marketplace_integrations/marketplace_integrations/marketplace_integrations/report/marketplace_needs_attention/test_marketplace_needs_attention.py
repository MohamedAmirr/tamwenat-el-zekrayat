# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the "Marketplace Needs Attention" Script Report (#169).

Exercises execute() end to end against services/marketplace_attention.py -
the per-source cases live in services/test_marketplace_attention.py, so this
file only proves the report itself is wired correctly (columns, filters,
delegation, role gate).
"""

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.test_marketplace_account import (
	STAND_IN_CONNECTION,
	STAND_IN_CONNECTION_DOCTYPE,
)
from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)
from marketplace_integrations.marketplace_integrations.report.marketplace_needs_attention.marketplace_needs_attention import (
	add_reference_labels,
	execute,
)

EXPECTED_FIELDNAMES = {
	"problem_type",
	"platform",
	"account",
	"reference_doctype",
	"reference_name",
	"explanation",
	"detected_at",
	"next_action",
}


class IntegrationTestMarketplaceNeedsAttentionReport(IntegrationTestCase):
	def setUp(self):
		self.account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Needs Attention Report {frappe.generate_hash(length=6)}",
				"platform": "Talabat",
				"external_account_id": f"needs-attention-report-{frappe.generate_hash(length=8)}",
				"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
				"connection": STAND_IN_CONNECTION,
			}
		).insert()
		self.order = frappe.get_doc(
			{
				"doctype": "Marketplace Order",
				"marketplace_account": self.account.name,
				"platform": "Talabat",
				"external_order_id": f"needs-attention-order-{frappe.generate_hash(length=8)}",
				"erpnext_sync_status": "Sync Error",
				"last_sync_error": "boom",
			}
		)
		self.order.flags.skip_sync_status_update = True
		self.order.order_identity_key = compute_order_identity_key(
			self.account.name, self.order.external_order_id
		)
		self.order.insert()

	def tearDown(self):
		frappe.delete_doc("Marketplace Order", self.order.name, force=True)
		frappe.delete_doc("Marketplace Account", self.account.name, force=True)

	def test_execute_returns_expected_columns(self):
		columns, _data = execute()

		self.assertEqual({column["fieldname"] for column in columns}, EXPECTED_FIELDNAMES)
		for column in columns:
			self.assertIn("label", column)
			self.assertIn("fieldtype", column)

	def test_execute_returns_the_attention_rows_and_honors_filters(self):
		_columns, data = execute({"account": self.account.name})
		self.assertEqual([row["reference_name"] for row in data], [self.order.name])
		self.assertEqual(data[0]["problem_type"], "Sync Error")

		_columns, filtered = execute({"account": self.account.name, "problem_type": "Unmapped Items"})
		self.assertEqual(filtered, [])

	def test_report_is_restricted_to_system_manager(self):
		report = frappe.get_doc("Report", "Marketplace Needs Attention")
		self.assertEqual([row.role for row in report.roles], ["System Manager"])

	def test_rows_carry_the_order_title_for_the_dynamic_link_column(self):
		# #324: Frappe sends link titles for Link columns only, so the report adds
		# `reference_label` for the Dynamic Link `reference_name` column.
		_columns, data = execute({"account": self.account.name})

		self.assertEqual(data[0]["reference_name"], self.order.name)
		self.assertEqual(data[0]["reference_label"], self.order.platform_order_reference)
		self.assertNotEqual(data[0]["reference_label"], self.order.name)

	def test_a_user_without_read_permission_gets_rows_without_labels_and_no_error(self):
		# #324: the label lookup is skipped for a doctype the user cannot read.
		_columns, data = execute({"account": self.account.name})
		rows = [{key: row[key] for key in ("reference_doctype", "reference_name")} for row in data]
		self.assertTrue(rows)

		self.addCleanup(frappe.set_user, frappe.session.user)
		frappe.set_user("Guest")
		self.assertFalse(frappe.has_permission("Marketplace Order", "read"))

		add_reference_labels(rows)

		self.assertEqual([row["reference_label"] for row in rows], [None] * len(rows))
		self.assertEqual([row["reference_name"] for row in rows], [self.order.name] * len(rows))
