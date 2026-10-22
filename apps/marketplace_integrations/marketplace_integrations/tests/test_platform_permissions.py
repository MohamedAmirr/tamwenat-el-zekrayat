# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Platform roles, doctype permissions and platform row filtering (#322).

Users are ordinary (non-Administrator) users built with ``make_role_user`` and then given the
real platform roles created by the Role fixture, so the shipped DocPerm rows and the
``permissions.py`` hooks are what is exercised. The test class deletes what it creates in
``tearDown`` because ``make_role_user`` commits.

There is no read-only platform user by design: every platform role is granted full rights on the
doctypes it sees, so tests that need a user lacking one ptype cannot use a platform role.
"""

import json
from contextlib import contextmanager

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations import permissions
from marketplace_integrations.integrations.bosta.deliveries import CREATOR_ROLES
from marketplace_integrations.tests.utils import make_role_user

# Same structural stand-in connection used by the other tests (no platform Connection is needed
# just to exercise permissions).
STAND_IN_CONNECTION_DOCTYPE = "UOM"
STAND_IN_CONNECTION = "Nos"

PLATFORMS = ("Talabat", "Noon", "Shopify")
FILTERED = ("Marketplace Account", "Marketplace Order", "Marketplace Product Mapping", "Marketplace Sync Log")


@contextmanager
def as_user(user):
	previous = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(previous)


class IntegrationTestPlatformPermissions(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.item_code = f"PLATFORM-PERM-{self.suffix}"
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": self.item_code,
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		self.accounts = {}
		self.orders = {}
		self.mappings = {}
		self.logs = {}
		for platform in PLATFORMS:
			account = self.make_account(platform)
			self.accounts[platform] = account.name
			self.orders[platform] = self.make_order(account.name, platform).name
			self.mappings[platform] = self.make_mapping(account.name, platform).name
			self.logs[platform] = self.make_log(account.name).name
		self.no_account_log = self.make_log(None).name

	def tearDown(self):
		frappe.set_user("Administrator")
		# Children before accounts (naming-series revert gotcha).
		frappe.db.delete("Marketplace Sync Log", {"name": ["in", [*self.logs.values(), self.no_account_log]]})
		frappe.db.delete(
			"Marketplace Sync Log", {"marketplace_account": ["in", list(self.accounts.values())]}
		)
		frappe.db.delete("Marketplace Product Mapping", {"item": self.item_code})
		frappe.db.delete("Marketplace Order", {"marketplace_account": ["in", list(self.accounts.values())]})
		frappe.db.delete("Marketplace Account", {"name": ["in", list(self.accounts.values())]})
		frappe.db.delete("Item", {"item_code": self.item_code})
		frappe.db.commit()

	# -- fixtures ---------------------------------------------------------------------------------

	def make_account(self, platform):
		account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Platform Perm {platform} {self.suffix}",
				"platform": "Talabat",
				"external_account_id": f"platform-perm-{platform.lower()}-{self.suffix}",
				"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
				"connection": STAND_IN_CONNECTION,
			}
		).insert()
		if platform != "Talabat":
			# A real Noon/Shopify account needs its own Connection; permissions only read the
			# platform value, so switch it on the stand-in account.
			frappe.db.set_value(
				"Marketplace Account", account.name, "platform", platform, update_modified=False
			)
		return account

	def make_order(self, account, platform):
		order = frappe.get_doc(
			{
				"doctype": "Marketplace Order",
				"marketplace_account": account,
				"external_order_id": f"platform-perm-{platform}-{self.suffix}",
			}
		)
		order.flags.skip_sync_status_update = True
		return order.insert()

	def make_mapping(self, account, platform):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": account,
				"item": self.item_code,
				"external_reference": f"ref-{platform}-{self.suffix}",
			}
		).insert()

	def make_log(self, account):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"status": "Error",
				"trigger_source": "Manual",
				"attempted_at": frappe.utils.now_datetime(),
				"marketplace_account": account,
			}
		).insert()

	def make_user(self, label, *roles):
		email, _role = make_role_user(self, label, {})
		if roles:
			frappe.get_doc("User", email).add_roles(*roles)
		frappe.clear_cache(user=email)
		return email

	def names_for(self, doctype):
		return {
			"Marketplace Account": self.accounts,
			"Marketplace Order": self.orders,
			"Marketplace Product Mapping": self.mappings,
			"Marketplace Sync Log": self.logs,
		}[doctype]

	def visible(self, user, doctype):
		"""Names among this test's rows that ``user`` can list."""
		mine = list(self.names_for(doctype).values())
		if doctype == "Marketplace Sync Log":
			mine.append(self.no_account_log)
		with as_user(user):
			return set(frappe.get_list(doctype, filters={"name": ["in", mine]}, pluck="name"))

	# -- tests ------------------------------------------------------------------------------------

	def test_roles_exist_with_desk_access(self):
		for role in ("Talabat User", "Noon User", "Shopify User", "Bosta User"):
			with self.subTest(role=role):
				self.assertEqual(frappe.db.get_value("Role", role, "desk_access"), 1)

	def test_explicit_docperm_without_platform_role_gets_no_rows(self):
		# #322 strict decision: a DocPerm alone never grants marketplace rows; the user needs a
		# platform role (or System Manager). make_role_user grants real Custom DocPerm rows.
		email, _role = make_role_user(self, "No Platform Role", {doctype: ["read"] for doctype in FILTERED})
		frappe.clear_cache(user=email)
		for doctype in FILTERED:
			with self.subTest(doctype=doctype):
				self.assertEqual(self.visible(email, doctype), set())
				for name in self.names_for(doctype).values():
					doc = frappe.get_doc(doctype, name)
					with as_user(email), self.assertRaises(frappe.PermissionError):
						doc.check_permission("read")

	def test_talabat_user_lists_only_talabat_rows(self):
		user = self.make_user("Talabat Staff", "Talabat User")
		for doctype in FILTERED:
			with self.subTest(doctype=doctype):
				self.assertEqual(self.visible(user, doctype), {self.names_for(doctype)["Talabat"]})

	def test_talabat_user_cannot_open_other_platform_rows_by_name(self):
		user = self.make_user("Talabat Staff", "Talabat User")
		for doctype in FILTERED:
			for platform in ("Noon", "Shopify"):
				with self.subTest(doctype=doctype, platform=platform):
					doc = frappe.get_doc(doctype, self.names_for(doctype)[platform])
					with as_user(user), self.assertRaises(frappe.PermissionError):
						doc.check_permission("read")
			with self.subTest(doctype=doctype, platform="Talabat"):
				doc = frappe.get_doc(doctype, self.names_for(doctype)["Talabat"])
				with as_user(user):
					doc.check_permission("read")

	def test_talabat_user_edits_account_but_cannot_create_or_delete(self):
		user = self.make_user("Talabat Staff", "Talabat User")
		own = frappe.get_doc("Marketplace Account", self.accounts["Talabat"])
		other = frappe.get_doc("Marketplace Account", self.accounts["Noon"])
		with as_user(user):
			own.check_permission("write")
			with self.assertRaises(frappe.PermissionError):
				other.check_permission("write")
			new_account = frappe.get_doc(
				{
					"doctype": "Marketplace Account",
					"account_name": f"Platform Perm New {self.suffix}",
					"platform": "Talabat",
					"external_account_id": f"platform-perm-new-{self.suffix}",
					"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
					"connection": STAND_IN_CONNECTION,
				}
			)
			with self.assertRaises(frappe.PermissionError):
				new_account.insert()
			with self.assertRaises(frappe.PermissionError):
				frappe.delete_doc("Marketplace Account", own.name)
		self.assertTrue(frappe.db.exists("Marketplace Account", own.name))

	def test_system_manager_can_create_and_delete_accounts(self):
		user = self.make_user("Manager Only", "System Manager")
		own = frappe.get_doc("Marketplace Account", self.accounts["Talabat"])
		with as_user(user):
			own.check_permission("delete")
			self.assertTrue(frappe.has_permission("Marketplace Account", "create"))

	def test_talabat_user_creates_and_deletes_own_platform_rows(self):
		user = self.make_user("Talabat Staff", "Talabat User")
		account = self.accounts["Talabat"]
		with as_user(user):
			order = frappe.get_doc(
				{
					"doctype": "Marketplace Order",
					"marketplace_account": account,
					"external_order_id": f"platform-perm-new-{self.suffix}",
				}
			)
			order.flags.skip_sync_status_update = True
			order.insert()
			frappe.delete_doc("Marketplace Order", order.name)

			log = frappe.get_doc(
				{
					"doctype": "Marketplace Sync Log",
					"sync_type": "Order Import",
					"status": "Error",
					"trigger_source": "Manual",
					"attempted_at": frappe.utils.now_datetime(),
					"marketplace_account": account,
				}
			).insert()
			frappe.delete_doc("Marketplace Sync Log", log.name)

			mapping = frappe.get_doc(
				{
					"doctype": "Marketplace Product Mapping",
					"marketplace_account": account,
					"item": self.item_code,
					"external_reference": f"ref-new-{self.suffix}",
				}
			)
			mapping.check_permission("create")
			frappe.get_doc("Marketplace Product Mapping", self.mappings["Talabat"]).check_permission("delete")

			# A new row for another platform's account is refused.
			foreign = frappe.get_doc(
				{
					"doctype": "Marketplace Product Mapping",
					"marketplace_account": self.accounts["Noon"],
					"item": self.item_code,
					"external_reference": f"ref-foreign-{self.suffix}",
				}
			)
			with self.assertRaises(frappe.PermissionError):
				foreign.check_permission("create")

	def test_user_with_two_platform_roles_sees_both_and_not_the_third(self):
		user = self.make_user("Two Platforms", "Talabat User", "Noon User")
		for doctype in FILTERED:
			with self.subTest(doctype=doctype):
				names = self.names_for(doctype)
				self.assertEqual(self.visible(user, doctype), {names["Talabat"], names["Noon"]})

	def test_system_manager_sees_everything_and_platform_user_sees_no_orphan_log(self):
		manager = self.make_user("Manager Only", "System Manager")
		platform_user = self.make_user("All Platforms", "Talabat User", "Noon User", "Shopify User")
		for doctype in FILTERED:
			with self.subTest(doctype=doctype):
				expected = set(self.names_for(doctype).values())
				if doctype == "Marketplace Sync Log":
					self.assertEqual(self.visible(manager, doctype), expected | {self.no_account_log})
					self.assertEqual(self.visible(platform_user, doctype), expected)
				else:
					self.assertEqual(self.visible(manager, doctype), expected)
		orphan = frappe.get_doc("Marketplace Sync Log", self.no_account_log)
		with as_user(platform_user), self.assertRaises(frappe.PermissionError):
			orphan.check_permission("read")
		with as_user(manager):
			orphan.check_permission("read")

	def test_administrator_is_not_filtered(self):
		self.assertEqual(permissions.marketplace_order_query_conditions("Administrator"), "")
		self.assertEqual(permissions.marketplace_account_query_conditions("Administrator"), "")

	def test_noon_user_reaches_noon_doctypes_and_talabat_user_does_not(self):
		noon = self.make_user("Noon Staff", "Noon User")
		talabat = self.make_user("Talabat Staff", "Talabat User")
		for doctype in ("Noon AWB", "Noon Return"):
			for ptype in ("read", "create"):
				with self.subTest(doctype=doctype, ptype=ptype):
					self.assertTrue(frappe.has_permission(doctype, ptype, user=noon))
					self.assertFalse(frappe.has_permission(doctype, ptype, user=talabat))

	def test_shopify_webhook_delivery_is_shopify_user_only(self):
		shopify = self.make_user("Shopify Staff", "Shopify User")
		noon = self.make_user("Noon Staff", "Noon User")
		self.assertTrue(frappe.has_permission("Shopify Webhook Delivery", "create", user=shopify))
		self.assertFalse(frappe.has_permission("Shopify Webhook Delivery", "read", user=noon))

	def test_platform_users_cannot_touch_connection_and_settings_doctypes(self):
		user = self.make_user("All Platforms", "Talabat User", "Noon User", "Shopify User", "Bosta User")
		for doctype in ("Talabat Connection", "Noon Connection", "Shopify Connection", "Bosta Settings"):
			for ptype in ("read", "write", "create"):
				with self.subTest(doctype=doctype, ptype=ptype):
					self.assertFalse(frappe.has_permission(doctype, ptype, user=user))

	def test_bosta_user_can_create_shipments_and_existing_roles_are_unchanged(self):
		user = self.make_user("Bosta Staff", "Bosta User")
		self.assertTrue(frappe.has_permission("Bosta Shipment", "create", user=user))
		self.assertTrue(frappe.has_permission("Bosta Shipment", "delete", user=user))
		self.assertIn("Bosta User", CREATOR_ROLES)
		for role in ("Sales Manager", "Stock User", "System Manager"):
			self.assertIn(role, CREATOR_ROLES)

		path = frappe.get_app_path(
			"marketplace_integrations",
			"marketplace_integrations",
			"doctype",
			"bosta_shipment",
			"bosta_shipment.json",
		)
		with open(path) as handle:
			rows = {row["role"]: row for row in json.load(handle)["permissions"]}
		self.assertEqual(
			rows["Sales Manager"],
			{
				"create": 1,
				"email": 1,
				"export": 1,
				"print": 1,
				"read": 1,
				"report": 1,
				"role": "Sales Manager",
				"write": 1,
			},
		)
		self.assertEqual(
			rows["Stock User"],
			{
				"create": 1,
				"email": 1,
				"export": 1,
				"print": 1,
				"read": 1,
				"report": 1,
				"role": "Stock User",
				"write": 1,
			},
		)
		self.assertEqual(rows["Sales User"], {"print": 1, "read": 1, "report": 1, "role": "Sales User"})

	def test_ordinary_staff_without_a_platform_role_gets_no_access(self):
		user = self.make_user("Ordinary Staff", "Sales User")
		for doctype in FILTERED:
			for ptype in ("read", "write", "create"):
				with self.subTest(doctype=doctype, ptype=ptype):
					self.assertFalse(frappe.has_permission(doctype, ptype, user=user))
		self.assertEqual(permissions.get_user_platforms(user), [])

	def test_moving_a_row_to_another_platforms_account_is_denied_on_write(self):
		# The hook checks the document as it will be saved, so pointing an own row at another
		# platform's account is refused instead of handing the row over.
		user = self.make_user("Talabat Staff", "Talabat User")
		for doctype in ("Marketplace Order", "Marketplace Product Mapping"):
			with self.subTest(doctype=doctype):
				doc = frappe.get_doc(doctype, self.names_for(doctype)["Talabat"])
				doc.marketplace_account = self.accounts["Noon"]
				with as_user(user), self.assertRaises(frappe.PermissionError):
					doc.check_permission("write")
				self.assertEqual(
					frappe.db.get_value(doctype, doc.name, "marketplace_account"), self.accounts["Talabat"]
				)

	def test_row_whose_account_was_deleted_is_invisible_to_platform_users(self):
		account = self.make_account("Talabat")
		order = self.make_order(account.name, "Orphan")
		self.addCleanup(frappe.db.delete, "Marketplace Order", {"name": order.name})
		# Bypass link checks to leave an order pointing at a missing account.
		frappe.db.delete("Marketplace Account", {"name": account.name})
		platform_user = self.make_user("All Platforms", "Talabat User", "Noon User", "Shopify User")
		manager = self.make_user("Manager Only", "System Manager")
		with as_user(platform_user):
			self.assertEqual(
				frappe.get_list("Marketplace Order", filters={"name": order.name}, pluck="name"), []
			)
		with as_user(platform_user), self.assertRaises(frappe.PermissionError):
			frappe.get_doc("Marketplace Order", order.name).check_permission("read")
		with as_user(manager):
			self.assertEqual(
				frappe.get_list("Marketplace Order", filters={"name": order.name}, pluck="name"), [order.name]
			)

	def test_guest_sees_no_rows_and_is_denied(self):
		for doctype in FILTERED:
			with self.subTest(doctype=doctype):
				self.assertFalse(frappe.has_permission(doctype, "read", user="Guest"))
				with as_user("Guest"), self.assertRaises(frappe.PermissionError):
					frappe.get_list(doctype, limit_page_length=1)
				doc = frappe.get_doc(doctype, next(iter(self.names_for(doctype).values())))
				with as_user("Guest"), self.assertRaises(frappe.PermissionError):
					doc.check_permission("read")
		self.assertEqual(permissions.marketplace_order_query_conditions("Guest"), "1=0")
		self.assertEqual(permissions.marketplace_account_query_conditions("Guest"), "1=0")
		self.assertEqual(permissions.get_user_platforms("Guest"), [])
