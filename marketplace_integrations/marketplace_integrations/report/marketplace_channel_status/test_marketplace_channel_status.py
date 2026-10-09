# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the "Marketplace Channel Status" Script Report (#167).

Exercises execute() end to end against services/marketplace_channels.py -
the finer-grained count/permission cases live in
services/test_marketplace_channels.py, so this file only proves the report
itself is wired correctly (columns, delegation, role gate).
"""

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.test_marketplace_account import (
	STAND_IN_CONNECTION,
	STAND_IN_CONNECTION_DOCTYPE,
)
from marketplace_integrations.marketplace_integrations.report.marketplace_channel_status.marketplace_channel_status import (
	execute,
)
from marketplace_integrations.services.marketplace_channels import get_channel_status

EXPECTED_FIELDNAMES = {
	"account",
	"platform",
	"enabled",
	"level",
	"orders_needing_attention",
	"product_sync_errors",
	"import_errors",
	"fulfillment_errors",
	"last_checked",
}


class IntegrationTestMarketplaceChannelStatusReport(IntegrationTestCase):
	def make_account(self, account_name, external_account_id, **kwargs):
		doc = {
			"doctype": "Marketplace Account",
			"account_name": account_name,
			"platform": "Talabat",
			"external_account_id": external_account_id,
			"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
			"connection": STAND_IN_CONNECTION,
		}
		doc.update(kwargs)
		return frappe.get_doc(doc).insert()

	def test_execute_returns_expected_columns(self):
		columns, _data = execute()

		fieldnames = {column["fieldname"] for column in columns}
		self.assertEqual(fieldnames, EXPECTED_FIELDNAMES)
		for column in columns:
			self.assertIn("label", column)
			self.assertIn("fieldtype", column)

	def test_execute_matches_service_function(self):
		self.make_account(
			"Channel Status Report Account", f"channel-status-report-{frappe.generate_hash(length=6)}"
		)

		_columns, data = execute()
		expected = get_channel_status()

		self.assertEqual(data, expected)

	def test_execute_denies_a_user_without_read_permission(self):
		# Unlike Shopify Reconciliation Summary's reports (which hard-gate on
		# frappe.only_for("System Manager") and then use frappe.get_all()
		# freely), this report's data comes entirely from
		# get_channel_status(), which is permission-scoped via frappe.get_list().
		# A user with no read permission on Marketplace Account is denied. The
		# Report doctype's own `roles: [System Manager]` is still what gates
		# a real Desk user from ever reaching this report at all; that gate
		# runs in frappe.desk.query_report's dispatcher, outside execute()
		# itself, so it isn't exercised by calling execute() directly.
		self.make_account(
			"Channel Status Report Permission Account",
			f"channel-status-report-permission-{frappe.generate_hash(length=6)}",
		)

		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				execute()
		finally:
			frappe.set_user("Administrator")
