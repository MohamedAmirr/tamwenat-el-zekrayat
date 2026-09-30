# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.test_marketplace_account import (
	mock_shopify_verification,
)
from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)
from marketplace_integrations.patches.v0_0.backfill_marketplace_sync_log_marketplace_account import execute


class IntegrationTestBackfillMarketplaceSyncLogMarketplaceAccount(IntegrationTestCase):
	"""Test the patch that backfills marketplace_account on existing
	Order Import Marketplace Sync Log rows created before the field was added.
	"""

	def make_account(self):
		"""Create a Shopify Marketplace Account for testing, cleaned up via addCleanup.

		Verified against its own unique mocked shop GID (rather than the
		fixture's shared default) - validate_shopify_account() always
		overwrites external_account_id to the verified live_shop_id
		(marketplace_account.py), so reusing the same mocked GID across
		tests would collide every account onto the same account_identity_key.
		"""
		suffix = frappe.generate_hash(length=8)
		connection = frappe.get_doc(
			{
				"doctype": "Shopify Connection",
				"connection_name": f"Backfill Connection {suffix}",
				"shop_domain": f"backfill-{suffix}.myshopify.com",
				"client_id": f"backfill-client-{suffix}",
				"client_secret": "backfill-secret",
			}
		).insert()
		self.addCleanup(lambda: frappe.delete_doc("Shopify Connection", connection.name, force=True))

		account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Backfill Account {suffix}",
				"platform": "Shopify",
				"connection_doctype": "Shopify Connection",
				"connection": connection.name,
			}
		)
		with mock_shopify_verification(shop_id=f"gid://shopify/Shop/{suffix}"):
			account.insert()
		self.addCleanup(self._delete_account, account.name)
		return account

	def _delete_account(self, account_name):
		"""Clear any Marketplace Sync Log rows still linking to this account
		before deleting it, so the delete doesn't hit Frappe's link-integrity
		check (LinkExistsError). No-op if already deleted."""
		frappe.db.set_value(
			"Marketplace Sync Log",
			{"marketplace_account": account_name},
			"marketplace_account",
			None,
			update_modified=False,
		)
		if frappe.db.exists("Marketplace Account", account_name):
			frappe.delete_doc("Marketplace Account", account_name, force=True)

	def make_order(self, account):
		"""Create a minimal Marketplace Order for testing, cleaned up via addCleanup."""
		suffix = frappe.generate_hash(length=8)
		order = frappe.get_doc(
			{
				"doctype": "Marketplace Order",
				"marketplace_account": account.name,
				"external_order_id": f"backfill-order-{suffix}",
			}
		)
		order.flags.skip_sync_status_update = True
		order.order_identity_key = compute_order_identity_key(account.name, order.external_order_id)
		order.insert()
		self.addCleanup(lambda: frappe.delete_doc("Marketplace Order", order.name, force=True))
		return order

	def make_sync_log(
		self, account, sync_type="Order Import", order_gid=None, include_account_in_request=True, marketplace_order=None
	):
		"""Create a Marketplace Sync Log row for testing, cleaned up via addCleanup.

		Args:
			account: Marketplace Account document
			sync_type: sync_type value ("Order Import" or "Sales Order Sync")
			order_gid: Shopify order GID (for Order Import rows)
			include_account_in_request: if True, include account name in request_data JSON
			marketplace_order: Marketplace Order name - required (and validated as
				mandatory by the doctype) for "Sales Order Sync" rows

		Returns:
			Name of the inserted Marketplace Sync Log row
		"""
		request_data = {}
		if sync_type == "Order Import" and order_gid:
			request_data["order_gid"] = order_gid
		if include_account_in_request:
			request_data["account"] = account.name

		log = frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": sync_type,
				"marketplace_order": marketplace_order,
				"status": "Error",
				"trigger_source": "Manual",
				"attempted_at": frappe.utils.now_datetime(),
				"completed_at": frappe.utils.now_datetime(),
				"request_data": frappe.as_json(request_data) if request_data else None,
				"traceback": "Test error",
			}
		)
		log.insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("Marketplace Sync Log", log.name, force=True))
		return log.name

	def _clear_marketplace_account(self, log_name):
		"""Simulate the pre-field-addition state: marketplace_account blank."""
		frappe.db.set_value(
			"Marketplace Sync Log", log_name, "marketplace_account", "", update_modified=False
		)
		frappe.clear_document_cache("Marketplace Sync Log", log_name)

	def test_backfills_account_from_request_data_json(self):
		"""A row with valid account in request_data gets backfilled with
		marketplace_account field."""
		account = self.make_account()
		order_gid = f"gid://shopify/Order/{frappe.generate_hash(length=8)}"
		log_name = self.make_sync_log(account, "Order Import", order_gid, include_account_in_request=True)
		self._clear_marketplace_account(log_name)

		execute()

		backfilled_account = frappe.db.get_value("Marketplace Sync Log", log_name, "marketplace_account")
		self.assertEqual(backfilled_account, account.name)

	def test_skips_row_with_missing_account_in_request_data(self):
		"""A row where request_data doesn't contain an account key is skipped
		without failing the patch."""
		account = self.make_account()
		order_gid = f"gid://shopify/Order/{frappe.generate_hash(length=8)}"
		log_name = self.make_sync_log(account, "Order Import", order_gid, include_account_in_request=False)
		self._clear_marketplace_account(log_name)

		execute()

		account_after = frappe.db.get_value("Marketplace Sync Log", log_name, "marketplace_account")
		self.assertEqual(account_after, "")

	def test_skips_row_with_invalid_json_in_request_data(self):
		"""A row where request_data is malformed JSON is skipped without
		failing the patch."""
		log = frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"status": "Error",
				"trigger_source": "Manual",
				"attempted_at": frappe.utils.now_datetime(),
				"completed_at": frappe.utils.now_datetime(),
				"request_data": "not valid json {",
				"traceback": "Test error",
			}
		)
		log.insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("Marketplace Sync Log", log.name, force=True))

		execute()

		# unset Link field reads back as None, not ""
		account_after = frappe.db.get_value("Marketplace Sync Log", log.name, "marketplace_account")
		self.assertEqual(account_after, None)

	def test_skips_row_referencing_deleted_marketplace_account(self):
		"""A row containing a reference to a Marketplace Account that no longer
		exists is skipped without failing the patch - we don't create dangling
		Link references."""
		account = self.make_account()
		order_gid = f"gid://shopify/Order/{frappe.generate_hash(length=8)}"
		log_name = self.make_sync_log(account, "Order Import", order_gid, include_account_in_request=True)
		self._clear_marketplace_account(log_name)

		# Delete the account before the patch ever runs (addCleanup's
		# self._delete_account is a no-op on an already-deleted account).
		self._delete_account(account.name)

		execute()

		account_after = frappe.db.get_value("Marketplace Sync Log", log_name, "marketplace_account")
		self.assertEqual(account_after, "")

	def test_does_not_touch_sales_order_sync_rows(self):
		"""Sales Order Sync rows are deliberately left alone - they have a
		different linking strategy (via marketplace_order) and are not backfilled
		by this patch."""
		account = self.make_account()
		order = self.make_order(account)
		log_name = self.make_sync_log(account, "Sales Order Sync", marketplace_order=order.name)
		self._clear_marketplace_account(log_name)

		execute()

		account_after = frappe.db.get_value("Marketplace Sync Log", log_name, "marketplace_account")
		self.assertEqual(account_after, "")

	def test_backfill_is_idempotent(self):
		"""Re-running the patch after it already backfilled a row must not
		touch that row again. This is proven by: after the first run fills the
		blank marketplace_account, the account is deleted. If the WHERE clause
		guard (looking for blank marketplace_account) were broken, a second run
		would treat the row as fair game and fail trying to backfill a deleted
		account. Because the guard excludes rows whose marketplace_account is
		already set, the row's marketplace_account must stay exactly what the
		first run produced, and a second run must succeed without error."""
		account = self.make_account()
		order_gid = f"gid://shopify/Order/{frappe.generate_hash(length=8)}"
		log_name = self.make_sync_log(account, "Order Import", order_gid, include_account_in_request=True)
		self._clear_marketplace_account(log_name)

		execute()
		self.assertEqual(
			frappe.db.get_value("Marketplace Sync Log", log_name, "marketplace_account"),
			account.name,
		)

		# Delete the account to prove idempotency: if the guard is broken,
		# a second run would fail trying to validate the deleted account.
		self._delete_account(account.name)

		execute()

		self.assertEqual(
			frappe.db.get_value("Marketplace Sync Log", log_name, "marketplace_account"),
			None,
		)
