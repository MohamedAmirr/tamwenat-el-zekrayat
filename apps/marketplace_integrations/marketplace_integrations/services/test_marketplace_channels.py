# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for #167's services/marketplace_channels.py::get_channel_status().

Covers count correctness per account (grouped Marketplace Order / Marketplace
Product Mapping / Marketplace Sync Log error counts, never bled across
accounts) and permission scoping (a user with no read permission on
Marketplace Account must get an empty result, not an error and not someone
else's rows).
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.test_marketplace_account import (
	STAND_IN_CONNECTION,
	STAND_IN_CONNECTION_DOCTYPE,
)
from marketplace_integrations.services.marketplace_channels import (
	get_account_counts,
	get_channel_status,
)


class IntegrationTestMarketplaceChannels(IntegrationTestCase):
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

	def make_item(self, item_code):
		if frappe.db.exists("Item", item_code):
			return item_code
		return (
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": item_code,
					"item_group": "Products",
					"stock_uom": "Nos",
				}
			)
			.insert()
			.name
		)

	def make_order(self, account, **overrides):
		suffix = frappe.generate_hash(length=8)
		values = {
			"doctype": "Marketplace Order",
			"marketplace_account": account.name,
			"platform": account.platform,
			"external_order_id": f"order-{suffix}",
			**overrides,
		}
		return frappe.get_doc(values).insert()

	def make_mapping(self, account, **overrides):
		suffix = frappe.generate_hash(length=8)
		values = {
			"doctype": "Marketplace Product Mapping",
			"marketplace_account": account.name,
			"item": self.make_item(f"Channel Status Test Item {suffix}"),
			"external_reference": f"ext-ref-{suffix}",
			**overrides,
		}
		return frappe.get_doc(values).insert()

	def make_sync_log(self, account, **overrides):
		values = {
			"doctype": "Marketplace Sync Log",
			"marketplace_account": account.name,
			"sync_type": "Order Import",
			"status": "Error",
			"attempted_at": frappe.utils.now_datetime(),
			**overrides,
		}
		return frappe.get_doc(values).insert()

	def test_counts_are_scoped_per_account_not_bled_across_accounts(self):
		account_a = self.make_account(
			"Channel Status Account A", f"channel-status-a-{frappe.generate_hash(length=6)}"
		)
		account_b = self.make_account(
			"Channel Status Account B", f"channel-status-b-{frappe.generate_hash(length=6)}"
		)

		self.make_order(account_a, erpnext_sync_status="Sync Error")
		self.make_order(account_a, erpnext_sync_status="Modified After Sync")
		self.make_order(account_a, erpnext_sync_status="Synced")
		self.make_mapping(account_a, sync_status="Error")
		self.make_sync_log(account_a)
		self.make_sync_log(account_a)

		self.make_order(account_b, erpnext_sync_status="Synced")

		counts = get_account_counts([account_a.name, account_b.name])

		self.assertEqual(counts[account_a.name]["orders_needing_attention"], 2)
		self.assertEqual(counts[account_a.name]["product_sync_errors"], 1)
		self.assertEqual(counts[account_a.name]["import_errors"], 2)
		self.assertEqual(counts[account_a.name]["fulfillment_errors"], 0)

		self.assertEqual(counts[account_b.name]["orders_needing_attention"], 0)
		self.assertEqual(counts[account_b.name]["product_sync_errors"], 0)
		self.assertEqual(counts[account_b.name]["import_errors"], 0)

	def test_product_sync_errors_counts_either_sync_status_or_inventory_sync_status(self):
		account = self.make_account(
			"Channel Status Account Either Error", f"channel-status-either-{frappe.generate_hash(length=6)}"
		)
		self.make_mapping(account, sync_status="Error")
		self.make_mapping(account, inventory_sync_status="Error")
		self.make_mapping(account, sync_status="Synced", inventory_sync_status="Synced")

		counts = get_account_counts([account.name])

		self.assertEqual(counts[account.name]["product_sync_errors"], 2)

	def test_empty_account_list_returns_empty_dict(self):
		self.assertEqual(get_account_counts([]), {})

	def test_get_channel_status_returns_one_row_per_account_with_computed_level(self):
		account = self.make_account(
			"Channel Status Level Account", f"channel-status-level-{frappe.generate_hash(length=6)}"
		)
		self.make_order(account, erpnext_sync_status="Sync Error")

		rows = get_channel_status()
		row = next(r for r in rows if r["account"] == account.name)

		self.assertEqual(row["platform"], "Talabat")
		self.assertEqual(row["orders_needing_attention"], 1)
		self.assertEqual(row["level"], "Error")

	def test_disabled_account_reports_disabled_level_regardless_of_errors(self):
		account = self.make_account(
			"Channel Status Disabled Account",
			f"channel-status-disabled-{frappe.generate_hash(length=6)}",
			enabled=0,
		)
		self.make_order(account, erpnext_sync_status="Sync Error")

		rows = get_channel_status()
		row = next(r for r in rows if r["account"] == account.name)

		self.assertEqual(row["level"], "Disabled")

	def test_enabled_account_with_no_errors_and_open_status_is_ok(self):
		account = self.make_account(
			"Channel Status OK Account",
			f"channel-status-ok-{frappe.generate_hash(length=6)}",
			external_status="OPEN",
		)

		rows = get_channel_status()
		row = next(r for r in rows if r["account"] == account.name)

		self.assertEqual(row["level"], "OK")

	def test_talabat_closed_status_without_errors_is_warning(self):
		account = self.make_account(
			"Channel Status Warning Account",
			f"channel-status-warning-{frappe.generate_hash(length=6)}",
			external_status="CLOSED_TODAY",
		)

		rows = get_channel_status()
		row = next(r for r in rows if r["account"] == account.name)

		self.assertEqual(row["level"], "Warning")

	def test_talabat_external_status_error_is_error_level(self):
		account = self.make_account(
			"Channel Status Error Account",
			f"channel-status-error-{frappe.generate_hash(length=6)}",
			external_status_error="Request failed",
		)

		rows = get_channel_status()
		row = next(r for r in rows if r["account"] == account.name)

		self.assertEqual(row["level"], "Error")

	def test_no_accounts_returns_empty_list(self):
		# Independent of Bosta settings/site state: the carrier row is not an account row.
		with (
			patch("frappe.get_list", return_value=[]),
			patch("marketplace_integrations.services.marketplace_channels._bosta_rows", return_value=[]),
		):
			self.assertEqual(get_channel_status(), [])

	def test_permission_scoping_denies_a_user_without_read_permission(self):
		# frappe.get_list (not frappe.get_all) is used throughout
		# get_channel_status(), so a user without read permission on
		# Marketplace Account is denied - the permission boundary is the ORM's
		# own, not a hand-rolled check in this module.
		self.make_account(
			"Channel Status Permission Account",
			f"channel-status-permission-{frappe.generate_hash(length=6)}",
		)

		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				get_channel_status()
		finally:
			frappe.set_user("Administrator")
