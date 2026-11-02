# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#374: ``get_item_listings`` feeds the Item form's Marketplace Listings tab.

Covers the returned shape, the Item read check, the mapping read check and the platform row filter
(#322) on an Item that has Talabat and Noon listings. The class deletes what it creates in ``tearDown``
because ``make_role_user`` commits.

Also covers the Item dashboard "Marketplace" connection count: Frappe counts with ``frappe.get_all``
(``get_doc_count``), which skips the row filter, so ``services.item_dashboard.get_open_count`` recounts
that entry with ``frappe.get_list`` and the badge matches the tab rows.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.item_listings import get_item_listings
from marketplace_integrations.integrations.shopify.test_products import make_item
from marketplace_integrations.services.item_dashboard import _visible_mapping_count, get_open_count
from marketplace_integrations.tests.utils import make_role_user

NOON_PEM = "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----"


class IntegrationTestItemListings(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self._created = []
		self.item = make_item(f"Item Listings Item {self.suffix}")
		self._created.append(("Item", self.item))

		connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Item Listings Talabat Connection {self.suffix}",
				"client_id": "test-client-id",
				"client_secret": "test-client-secret",
				"chain_id": "test-chain-id",
				"environment": "Sandbox",
			}
		).insert()
		self._created.append(("Talabat Connection", connection.name))
		self.talabat = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Item Listings Talabat {self.suffix}",
				"platform": "Talabat",
				"external_account_id": f"item-listings-talabat-{self.suffix}",
				"connection_doctype": "Talabat Connection",
				"connection": connection.name,
			}
		).insert()
		self._created.append(("Marketplace Account", self.talabat.name))
		self.noon = self.make_noon_account()

		self.talabat_mapping = self.make_mapping(self.talabat, f"talabat-ref-{self.suffix}")
		self.noon_mapping = self.make_mapping(self.noon, f"noon-ref-{self.suffix}")

	def tearDown(self):
		frappe.set_user("Administrator")
		accounts = [self.talabat.name, self.noon.name]
		frappe.db.delete("Marketplace Sync Log", {"marketplace_account": ["in", accounts]})
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": ["in", accounts]})
		for doctype, name in reversed(self._created):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def make_noon_account(self):
		code = f"item-listings-noon-{self.suffix}"
		connection = frappe.get_doc(
			{
				"doctype": "Noon Connection",
				"connection_name": code,
				"project_code": code,
				"key_id": "test-key-id",
				"private_key": NOON_PEM,
				"environment": "Static Sandbox",
				"enabled": 1,
			}
		).insert()
		self._created.append(("Noon Connection", connection.name))
		with patch("marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"):
			account = frappe.get_doc(
				{
					"doctype": "Marketplace Account",
					"account_name": code,
					"platform": "Noon",
					"external_account_id": code,
					"connection_doctype": "Noon Connection",
					"connection": connection.name,
					"noon_warehouse_code": "NOON-WH-ITEM",
					"noon_country_code": "ae",
				}
			).insert()
		self._created.append(("Marketplace Account", account.name))
		return account

	def make_mapping(self, account, external_reference):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": account.name,
				"item": self.item,
				"external_reference": external_reference,
			}
		).insert()

	def make_user(self, label, *roles, perms=None):
		email, _role = make_role_user(self, label, perms or {})
		if roles:
			frappe.get_doc("User", email).add_roles(*roles)
		frappe.clear_cache(user=email)
		return email

	def as_user(self, user):
		frappe.set_user(user)
		self.addCleanup(frappe.set_user, "Administrator")

	def test_returns_the_table_fields_account_name_and_can_write_ordered(self):
		rows = get_item_listings(self.item)

		self.assertEqual([row["name"] for row in rows], [self.noon_mapping.name, self.talabat_mapping.name])
		row = rows[1]
		for key in (
			"marketplace_account",
			"external_reference",
			"platform",
			"enabled",
			"sales_uom",
			"sync_status",
			"last_synced_on",
			"last_error",
			"inventory_sync_status",
			"inventory_last_synced_on",
			"inventory_last_error",
			"account_name",
			"price_push_off",
			"stock_push_off",
			"can_write",
		):
			self.assertIn(key, row)
		self.assertEqual(row["platform"], "Talabat")
		self.assertEqual(row["account_name"], self.talabat.account_name)
		self.assertTrue(row["can_write"])
		self.assertFalse(row["price_push_off"])
		self.assertFalse(row["stock_push_off"])

	def test_noon_row_reports_each_automatic_push_switch_separately(self):
		frappe.db.set_value(
			"Marketplace Account",
			self.noon.name,
			{"noon_inventory_push_enabled": 0, "noon_price_push_enabled": 1},
			update_modified=False,
		)

		rows = {row["name"]: row for row in get_item_listings(self.item)}

		self.assertTrue(rows[self.noon_mapping.name]["stock_push_off"])
		self.assertFalse(rows[self.noon_mapping.name]["price_push_off"])
		self.assertFalse(rows[self.talabat_mapping.name]["stock_push_off"])
		self.assertFalse(rows[self.talabat_mapping.name]["price_push_off"])

	def mapping_count(self):
		result = get_open_count("Item", self.item, ["Marketplace Product Mapping"])
		(link,) = result["count"]["external_links_found"]
		return link["count"]

	def test_dashboard_count_matches_the_rows_each_platform_user_sees(self):
		self.assertEqual(self.mapping_count(), 2)

		# Create both users while still Administrator: a non-admin cannot insert Role/User.
		talabat_user = self.make_user("Count Talabat", "Talabat User", "Stock User")
		noon_user = self.make_user("Count Noon", "Noon User", "Stock User")

		self.as_user(talabat_user)
		self.assertEqual(self.mapping_count(), len(get_item_listings(self.item)))
		self.assertEqual(self.mapping_count(), 1)

		self.as_user(noon_user)
		self.assertEqual(self.mapping_count(), 1)

	def test_dashboard_count_is_zero_without_mapping_read(self):
		self.as_user(self.make_user("Count Item Only", "Stock User"))

		self.assertEqual(self.mapping_count(), 0)

	def test_dashboard_count_is_refused_without_item_read(self):
		self.as_user(self.make_user("Count Mapping Only", "Talabat User"))
		self.assertFalse(frappe.has_permission("Item", "read", doc=self.item))

		with self.assertRaises(frappe.PermissionError):
			get_open_count("Item", self.item, ["Marketplace Product Mapping"])

	def test_dashboard_count_shows_a_question_mark_when_the_recount_times_out(self):
		# Called directly: patching frappe.get_list would also break Frappe's own get_all counting.
		with patch("frappe.get_list", side_effect=frappe.QueryTimeoutError()), patch.object(
			frappe.db, "is_statement_timeout", return_value=True
		):
			self.assertEqual(_visible_mapping_count(self.item), "?")

	def test_dashboard_count_reraises_errors_that_are_not_timeouts(self):
		with patch("frappe.get_list", side_effect=ValueError("boom")), patch.object(
			frappe.db, "is_statement_timeout", return_value=False
		):
			with self.assertRaises(ValueError):
				_visible_mapping_count(self.item)

	def test_a_talabat_user_sees_only_talabat_rows(self):
		user = self.make_user("Listings Talabat", "Talabat User", "Stock User")
		self.as_user(user)

		rows = get_item_listings(self.item)

		self.assertEqual([row["name"] for row in rows], [self.talabat_mapping.name])
		self.assertTrue(rows[0]["can_write"])

	def test_a_noon_user_sees_only_noon_rows(self):
		user = self.make_user("Listings Noon", "Noon User", "Stock User")
		self.as_user(user)

		self.assertEqual([row["name"] for row in get_item_listings(self.item)], [self.noon_mapping.name])

	def test_without_mapping_read_there_are_no_rows(self):
		user = self.make_user("Listings Item Only", "Stock User")
		self.as_user(user)
		self.assertFalse(frappe.has_permission("Marketplace Product Mapping", "read"))

		self.assertEqual(get_item_listings(self.item), [])

	def test_without_item_read_the_call_is_refused(self):
		user = self.make_user("Listings Mapping Only", "Talabat User")
		self.as_user(user)
		self.assertFalse(frappe.has_permission("Item", "read", doc=self.item))

		with self.assertRaises(frappe.PermissionError):
			get_item_listings(self.item)

	def test_an_item_with_no_listings_returns_an_empty_list(self):
		other = make_item(f"Item Listings Empty {self.suffix}")
		self._created.append(("Item", other))

		self.assertEqual(get_item_listings(other), [])

	def test_the_endpoint_is_get_only_and_reads_through_get_list(self):
		method = frappe.get_attr("marketplace_integrations.api.item_listings.get_item_listings")
		self.assertIn(method, frappe.whitelisted)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["GET"])

		source = frappe.get_app_path("marketplace_integrations", "api", "item_listings.py")
		with open(source) as handle:
			text = handle.read()
		self.assertIn("frappe.get_list(", text)
		self.assertNotIn("get_all(", text)
		self.assertNotIn("ignore_permissions", text)
