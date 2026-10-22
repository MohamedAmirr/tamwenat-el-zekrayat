# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.backfill_marketplace_sync_log_platform import execute

# Same structural stand-in connection used by the other backfill tests.
STAND_IN_CONNECTION_DOCTYPE = "UOM"
STAND_IN_CONNECTION = "Nos"


class IntegrationTestBackfillMarketplaceSyncLogPlatform(IntegrationTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Sync Log Platform Backfill {suffix}",
				"platform": "Talabat",
				"external_account_id": f"sync-log-platform-backfill-{suffix}",
				"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
				"connection": STAND_IN_CONNECTION,
			}
		).insert()
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		# Logs before the account (naming-series revert gotcha).
		frappe.db.delete("Marketplace Sync Log", {"marketplace_account": self.account.name})
		frappe.db.delete("Marketplace Sync Log", {"traceback": "sync-log-platform-backfill"})
		frappe.delete_doc("Marketplace Account", self.account.name, force=True)

	def make_log(self, account=None):
		log = frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"marketplace_account": account,
				"status": "Error",
				"trigger_source": "Manual",
				"attempted_at": frappe.utils.now_datetime(),
				"traceback": "sync-log-platform-backfill",
			}
		)
		log.insert(ignore_permissions=True)
		return log

	def blank_platform(self, log):
		frappe.db.set_value("Marketplace Sync Log", log.name, "platform", "", update_modified=False)
		frappe.clear_document_cache("Marketplace Sync Log", log.name)

	def platform(self, log):
		return frappe.db.get_value("Marketplace Sync Log", log.name, "platform")

	def test_new_log_fetches_platform_from_its_account(self):
		self.assertEqual(self.platform(self.make_log(self.account.name)), "Talabat")

	def test_fills_blank_platform_for_rows_with_an_account(self):
		log = self.make_log(self.account.name)
		self.blank_platform(log)

		execute()

		self.assertEqual(self.platform(log), "Talabat")

	def test_leaves_rows_without_an_account_empty(self):
		log = self.make_log()

		execute()

		self.assertFalse(self.platform(log))

	def test_second_run_changes_nothing(self):
		log = self.make_log(self.account.name)
		self.blank_platform(log)
		execute()
		modified = frappe.db.get_value("Marketplace Sync Log", log.name, "modified")
		# A correctly guarded second run must not propagate a changed account platform.
		frappe.db.set_value(
			"Marketplace Account", self.account.name, "platform", "Shopify", update_modified=False
		)

		execute()

		self.assertEqual(self.platform(log), "Talabat")
		self.assertEqual(frappe.db.get_value("Marketplace Sync Log", log.name, "modified"), modified)
