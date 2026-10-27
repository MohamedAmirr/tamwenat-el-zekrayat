# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#323: Channel Status and Needs Attention for platform users.

Ordinary users built with ``make_role_user`` plus the real platform roles. A platform user sees
only their platform's rows, the Bosta carrier row needs Bosta Shipment read, and a Bosta-only
user (no Marketplace Account read) gets no access. The class deletes what it creates in
``tearDown`` because ``make_role_user`` commits.
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.services.marketplace_attention import BOSTA_PLATFORM, get_attention_items
from marketplace_integrations.services.marketplace_channels import get_channel_status
from marketplace_integrations.services.sync_status import NEEDS_ACTION
from marketplace_integrations.tests.utils import make_role_user

# Same structural stand-in connection the other tests use: permissions and these services only
# read Marketplace Account.platform, so no real platform Connection is needed.
STAND_IN_CONNECTION_DOCTYPE = "UOM"
STAND_IN_CONNECTION = "Nos"


@contextmanager
def as_user(user):
	previous = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(previous)


class IntegrationTestPlatformAccess(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.accounts = {}
		self.orders = {}
		for platform in ("Talabat", "Shopify"):
			account = frappe.get_doc(
				{
					"doctype": "Marketplace Account",
					"account_name": f"Platform Access {platform} {self.suffix}",
					"platform": "Talabat",
					"external_account_id": f"platform-access-{platform.lower()}-{self.suffix}",
					"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
					"connection": STAND_IN_CONNECTION,
				}
			).insert()
			if platform != "Talabat":
				frappe.db.set_value(
					"Marketplace Account", account.name, "platform", platform, update_modified=False
				)
			self.accounts[platform] = account.name
			order = frappe.get_doc(
				{
					"doctype": "Marketplace Order",
					"marketplace_account": account.name,
					"platform": platform,
					"external_order_id": f"platform-access-{platform}-{self.suffix}",
				}
			)
			order.flags.skip_sync_status_update = True
			order.insert()
			order.db_set("erpnext_sync_status", "Sync Error", update_modified=False)
			self.orders[platform] = order.name

	def tearDown(self):
		frappe.set_user("Administrator")
		names = list(self.accounts.values())
		frappe.db.delete("Marketplace Sync Log", {"marketplace_account": ["in", names]})
		frappe.db.delete("Marketplace Order", {"marketplace_account": ["in", names]})
		frappe.db.delete("Marketplace Account", {"name": ["in", names]})
		frappe.db.commit()

	def make_user(self, label, *roles):
		email, _role = make_role_user(self, label, {})
		frappe.get_doc("User", email).add_roles(*roles)
		frappe.clear_cache(user=email)
		return email

	def test_talabat_user_sees_only_talabat_attention_items(self):
		user = self.make_user("Attention Talabat", "Talabat User")
		with as_user(user):
			references = {item["reference_name"] for item in get_attention_items()}
		self.assertIn(self.orders["Talabat"], references)
		self.assertNotIn(self.orders["Shopify"], references)

	def test_needs_action_rows_are_scoped_to_the_platform_user_but_not_to_administrator(self):
		for name in self.orders.values():
			frappe.db.set_value(
				"Marketplace Order", name, "erpnext_sync_status", NEEDS_ACTION, update_modified=False
			)
		user = self.make_user("Needs Action Talabat", "Talabat User")

		def needs_action_references():
			return {
				item["reference_name"]
				for item in get_attention_items()
				if item["problem_type"] == NEEDS_ACTION
			}

		with as_user(user):
			platform_user_rows = needs_action_references()
		with as_user("Administrator"):
			administrator_rows = needs_action_references()

		self.assertIn(self.orders["Talabat"], platform_user_rows)
		self.assertNotIn(self.orders["Shopify"], platform_user_rows)
		self.assertTrue({self.orders["Talabat"], self.orders["Shopify"]} <= administrator_rows)

	def test_talabat_user_sees_only_talabat_channel_rows(self):
		user = self.make_user("Channel Talabat", "Talabat User")
		with as_user(user):
			rows = get_channel_status()
		accounts = {row["account"] for row in rows}
		self.assertIn(self.accounts["Talabat"], accounts)
		self.assertNotIn(self.accounts["Shopify"], accounts)
		# No Bosta Shipment read: no carrier row either.
		self.assertNotIn(BOSTA_PLATFORM, {row["platform"] for row in rows})

	def test_bosta_row_needs_bosta_shipment_read(self):
		plain = self.make_user("Carrier Plain", "Talabat User")
		with_bosta = self.make_user("Carrier Bosta", "Talabat User", "Bosta User")
		# Patch only the Bosta Settings read so the test does not have to flip the shared single
		# doctype's `enabled` flag on the site.
		real_get_single_value = frappe.db.get_single_value

		def get_single_value(doctype, *args, **kwargs):
			if doctype == "Bosta Settings":
				return 1
			return real_get_single_value(doctype, *args, **kwargs)

		with patch("frappe.db.get_single_value", side_effect=get_single_value):
			with as_user(plain):
				self.assertNotIn(BOSTA_PLATFORM, {row["platform"] for row in get_channel_status()})
			with as_user(with_bosta):
				self.assertIn(BOSTA_PLATFORM, {row["platform"] for row in get_channel_status()})
			manager = self.make_user("Carrier Manager", "System Manager")
			with as_user(manager):
				self.assertIn(BOSTA_PLATFORM, {row["platform"] for row in get_channel_status()})

	def test_bosta_only_user_gets_no_channel_status_or_attention_report(self):
		user = self.make_user("Bosta Only", "Bosta User")
		with as_user(user):
			with self.assertRaises(frappe.PermissionError):
				get_channel_status()
			with self.assertRaises(frappe.PermissionError):
				get_attention_items()
