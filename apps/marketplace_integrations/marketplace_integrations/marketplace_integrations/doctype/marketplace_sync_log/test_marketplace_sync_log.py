# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime

from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)
from marketplace_integrations.marketplace_integrations.doctype.marketplace_sync_log.marketplace_sync_log import (
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
		self.assertEqual(fields["sync_type"].fieldtype, "Select")
		self.assertEqual(
			fields["sync_type"].options.splitlines(),
			["Sales Order Sync", "Order Import", "Fulfillment", "Return Lookup", "Order Action"],
		)
		self.assertEqual(fields["sync_type"].default, "Sales Order Sync")
		self.assertTrue(fields["sync_type"].reqd)
		self.assertEqual(fields["marketplace_order"].fieldtype, "Link")
		self.assertEqual(fields["marketplace_order"].options, "Marketplace Order")
		# #139: no longer unconditionally required - an Order Import failure
		# has no Marketplace Order yet to link to. Still mandatory for the
		# original Sales Order Sync rows.
		self.assertFalse(fields["marketplace_order"].reqd)
		self.assertEqual(fields["marketplace_order"].mandatory_depends_on, 'eval:doc.sync_type=="Sales Order Sync"')
		self.assertEqual(fields["status"].options.splitlines(), ["Queued", "Success", "Error", "Invalid"])
		self.assertEqual(fields["status"].default, "Queued")
		self.assertTrue(fields["status"].read_only)
		self.assertEqual(
			fields["trigger_source"].options.splitlines(),
			[
				"Manual",
				"Manual Resync",
				"Shopify Webhook",
				"Talabat Webhook",
				"Scheduled Reconciliation",
				"Noon Order Import",
				"Noon Webhook",
				"Scheduled Sweep",
			],
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

	def test_order_import_row_does_not_require_marketplace_order(self):
		log = frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"status": "Error",
				"attempted_at": now_datetime(),
			}
		).insert()
		self.assertFalse(log.marketplace_order)

	def test_sales_order_sync_row_still_requires_marketplace_order(self):
		with self.assertRaises(frappe.MandatoryError):
			frappe.get_doc(
				{
					"doctype": "Marketplace Sync Log",
					"sync_type": "Sales Order Sync",
					"status": "Error",
					"attempted_at": now_datetime(),
				}
			).insert()

	def test_resync_delegates_to_sync_sales_order_as_manual_resync_without_its_own_log_row(self):
		"""`resync()` must not write/update its own `Marketplace Sync Log` row -
		`sync_sales_order()`'s internal `_create_sync_log()` (see
		`services/sales_order_sync.py`) is the single source of truth for what
		happened. Before this fix, `resync()` ALSO inserted its own attempt row
		around the call, producing two log rows per resync attempt. Here
		`sync_sales_order()` itself is mocked out entirely (its own logging
		behavior is covered by `services/test_sales_order_sync.py`), so this
		test only proves `resync()`'s own code path creates nothing beyond the
		pre-existing `original` row."""
		original = self.make_log(status="Error")
		before_count = frappe.db.count("Marketplace Sync Log", {"marketplace_order": self.order.name})

		with patch(
			"marketplace_integrations.services.sales_order_sync.sync_sales_order",
			return_value={"ok": True, "sales_order": "SO-TEST"},
		) as sync_sales_order:
			result = original.resync()

		sync_sales_order.assert_called_once_with(self.order.name, trigger_source="Manual Resync")
		self.assertEqual(result, {"ok": True, "sales_order": "SO-TEST"})
		self.assertEqual(
			frappe.db.count("Marketplace Sync Log", {"marketplace_order": self.order.name}), before_count
		)

	def test_resync_on_order_import_failure_raises_clear_error(self):
		"""An Order Import failure row has no `marketplace_order` (there's no
		Marketplace Order to link yet - the import itself is what failed).
		Calling `resync()` on it used to fall through to
		`frappe.get_doc("Marketplace Order", None)`, raising a confusing
		`DoesNotExistError`. It must now raise a clear, actionable
		`frappe.throw()` instead. The list view JS excludes these rows from the
		bulk-retry selection (see `marketplace_sync_log_list.test.js`); this
		covers any other caller that bypasses that filter."""
		log = frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"status": "Error",
				"attempted_at": now_datetime(),
			}
		).insert()

		with self.assertRaises(frappe.ValidationError) as ctx:
			log.resync()
		self.assertNotIsInstance(ctx.exception, frappe.DoesNotExistError)
		self.assertIn("no Marketplace Order to resync", str(ctx.exception))

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
