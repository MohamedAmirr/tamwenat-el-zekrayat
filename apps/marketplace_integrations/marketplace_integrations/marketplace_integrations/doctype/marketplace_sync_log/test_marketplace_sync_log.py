# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime

from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)
from marketplace_integrations.marketplace_integrations.doctype.marketplace_sync_log.marketplace_sync_log import (
	UNEXPECTED_SYNC_ERROR,
	clear_old_logs,
)

IGNORE_TEST_RECORD_DEPENDENCIES = ["Marketplace Order"]


class TestMarketplaceSyncLog(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.previous_user = frappe.session.user
		frappe.set_user("Administrator")
		self.order = self.make_order()

	def tearDown(self):
		frappe.set_user(self.previous_user)
		super().tearDown()

	def make_order(self):
		suffix = frappe.generate_hash(length=8)
		connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Sync Log Test Connection {suffix}",
				"client_id": f"sync-log-client-{suffix}",
				"client_secret": "sync-log-secret",
				"chain_id": f"sync-log-chain-{suffix}",
			}
		).insert()
		account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Sync Log Test Account {suffix}",
				"platform": "Talabat",
				"external_account_id": f"sync-log-vendor-{suffix}",
				"connection_doctype": "Talabat Connection",
				"connection": connection.name,
			}
		).insert()
		order = frappe.get_doc(
			{
				"doctype": "Marketplace Order",
				"marketplace_account": account.name,
				"external_order_id": f"sync-log-order-{suffix}",
			}
		)
		order.flags.skip_sync_status_update = True
		order.order_identity_key = compute_order_identity_key(account.name, order.external_order_id)
		order.insert()
		return order

	def make_log(self, **values):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"marketplace_order": self.order.name,
				"attempted_at": now_datetime(),
				**values,
			}
		).insert()

	def test_doctype_fields_defaults_and_permissions_match_contract(self):
		meta = frappe.get_meta("Marketplace Sync Log")
		fields = {field.fieldname: field for field in meta.fields if field.fieldname}
		self.assertEqual(fields["marketplace_order"].fieldtype, "Link")
		self.assertEqual(fields["marketplace_order"].options, "Marketplace Order")
		self.assertTrue(fields["marketplace_order"].reqd)
		self.assertEqual(fields["status"].options.splitlines(), ["Queued", "Success", "Error", "Invalid"])
		self.assertEqual(fields["status"].default, "Queued")
		self.assertTrue(fields["status"].read_only)
		self.assertEqual(
			fields["trigger_source"].options.splitlines(),
			["Manual", "Shopify Webhook", "Talabat Webhook"],
		)
		self.assertEqual(fields["trigger_source"].default, "Manual")
		self.assertTrue(fields["attempted_at"].reqd)
		for fieldname in ("attempted_at", "completed_at", "request_data", "response_data", "traceback"):
			self.assertTrue(fields[fieldname].read_only, fieldname)
		permissions = {perm.role: perm for perm in meta.permissions}
		self.assertEqual(set(permissions), {"System Manager"})
		self.assertTrue(permissions["System Manager"].read)
		self.assertTrue(permissions["System Manager"].create)
		self.assertTrue(permissions["System Manager"].write)

	def test_resync_appends_manual_success_attempt_linked_to_same_order(self):
		original = self.make_log(status="Error")
		with patch(
			"marketplace_integrations.services.sales_order_sync.sync_sales_order",
			return_value={"ok": True, "sales_order": "SO-TEST"},
		):
			result = original.resync()

		attempt = frappe.get_doc("Marketplace Sync Log", result["name"])
		self.assertNotEqual(attempt.name, original.name)
		self.assertEqual(attempt.marketplace_order, self.order.name)
		self.assertEqual(attempt.trigger_source, "Manual")
		self.assertEqual(attempt.status, "Success")
		self.assertTrue(attempt.completed_at)
		self.assertEqual(json.loads(attempt.response_data)["sales_order"], "SO-TEST")
		self.assertEqual(frappe.db.count("Marketplace Sync Log", {"marketplace_order": self.order.name}), 2)

	def test_resync_classifies_expected_blocker_result_as_invalid_without_traceback(self):
		original = self.make_log(status="Error")
		with (
			patch(
				"marketplace_integrations.services.sales_order_sync.sync_sales_order",
				return_value={"ok": False, "error": "Missing mapping"},
			),
			patch("marketplace_integrations.marketplace_integrations.doctype.marketplace_sync_log.marketplace_sync_log.frappe.get_traceback") as traceback,
		):
			result = original.resync()

		attempt = frappe.get_doc("Marketplace Sync Log", result["name"])
		self.assertEqual(attempt.status, "Invalid")
		self.assertFalse(attempt.traceback)
		traceback.assert_not_called()

	def test_resync_classifies_unexpected_result_as_error_without_invented_traceback(self):
		original = self.make_log(status="Error")
		with (
			patch(
				"marketplace_integrations.services.sales_order_sync.sync_sales_order",
				return_value={"ok": False, "error": UNEXPECTED_SYNC_ERROR},
			),
			patch("marketplace_integrations.marketplace_integrations.doctype.marketplace_sync_log.marketplace_sync_log.frappe.get_traceback", return_value=None),
		):
			result = original.resync()

		attempt = frappe.get_doc("Marketplace Sync Log", result["name"])
		self.assertEqual(attempt.status, "Error")
		self.assertFalse(attempt.traceback)

	def test_resync_records_traceback_only_when_unexpected_exception_has_one(self):
		original = self.make_log(status="Error")
		with (
			patch(
				"marketplace_integrations.services.sales_order_sync.sync_sales_order",
				side_effect=RuntimeError("unexpected sync failure"),
			),
			patch(
				"marketplace_integrations.marketplace_integrations.doctype.marketplace_sync_log.marketplace_sync_log.frappe.get_traceback",
				return_value="Traceback (most recent call last): test",
			),
		):
			result = original.resync()

		attempt = frappe.get_doc("Marketplace Sync Log", result["name"])
		self.assertEqual(attempt.status, "Error")
		self.assertEqual(attempt.traceback, "Traceback (most recent call last): test")

	def test_resync_requires_system_manager(self):
		original = self.make_log(status="Error")
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			original.resync()

	def test_clear_old_logs_deletes_only_rows_older_than_cutoff(self):
		old = self.make_log(status="Error")
		new = self.make_log(status="Error")
		cutoff = add_days(now_datetime(), -90)
		frappe.db.set_value("Marketplace Sync Log", old.name, "creation", add_days(cutoff, -1))
		frappe.db.set_value("Marketplace Sync Log", new.name, "creation", add_days(cutoff, 1))

		clear_old_logs(days=90)

		self.assertFalse(frappe.db.exists("Marketplace Sync Log", old.name))
		self.assertTrue(frappe.db.exists("Marketplace Sync Log", new.name))
