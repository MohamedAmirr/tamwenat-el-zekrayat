# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#168 Noon "Needs Mapping" (order-derived from Marketplace Order items) and
mapping upsert. Pure DB fixtures; NoonClient.request is patched to fail if
reached, so nothing here can touch the live Noon store (see
docs/noon-integration/catalog-export-capability.md - Noon catalog export is
not used)."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.noon.catalog import list_unmapped_order_skus, upsert_mapping
from marketplace_integrations.integrations.noon.client import NoonClient
from marketplace_integrations.integrations.noon.test_orders import (
	_make_noon_account as _make_noon_account_raw,
)


def _make_noon_account(account_name):
	# Account validation would otherwise make a real warehouse API call.
	with patch("marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"):
		return _make_noon_account_raw(account_name)


def _make_item(item_code):
	return (
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": item_code,
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		)
		.insert()
		.name
	)


def _make_order(account_name, external_order_id, skus):
	"""Insert one Marketplace Order with one item row per SKU. ``platform`` is
	fetched from the linked account, so it is always the account's platform."""
	return frappe.get_doc(
		{
			"doctype": "Marketplace Order",
			"marketplace_account": account_name,
			"external_order_id": external_order_id,
			"items": [
				{
					"doctype": "Marketplace Order Item",
					"external_item_id": f"{external_order_id}-{index}",
					"external_sku": sku,
				}
				for index, sku in enumerate(skus, start=1)
			],
		}
	).insert(ignore_permissions=True)


def _make_mapping(account_name, item, external_reference, enabled=1):
	return frappe.get_doc(
		{
			"doctype": "Marketplace Product Mapping",
			"marketplace_account": account_name,
			"item": item,
			"external_reference": external_reference,
			"enabled": enabled,
		}
	).insert()


class IntegrationTestNoonNeedsMapping(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.account = _make_noon_account(f"Noon Needs Mapping {self.suffix}")
		self.item = _make_item(f"Noon Needs Mapping Item {self.suffix}")

	def tearDown(self):
		# Orders must be deleted BEFORE the account (naming-series revert gotcha,
		# see integrations/noon/test_orders.py tearDown).
		for order in frappe.get_all(
			"Marketplace Order", {"marketplace_account": self.account.name}, pluck="name"
		):
			frappe.delete_doc("Marketplace Order", order, ignore_permissions=True, force=True)
		frappe.db.delete("Marketplace Product Mapping", filters={"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)

	def _no_noon_http(self):
		return patch.object(NoonClient, "request", side_effect=AssertionError("Noon must not be called"))

	def test_lists_distinct_unmapped_skus_from_this_accounts_noon_orders(self):
		_make_order(self.account.name, f"ORD-1-{self.suffix}", ["SKU-A", "SKU-B"])
		_make_order(self.account.name, f"ORD-2-{self.suffix}", ["SKU-B", "SKU-C", "  "])

		with self._no_noon_http():
			skus = list_unmapped_order_skus(self.account.name)

		self.assertEqual(skus, ["SKU-A", "SKU-B", "SKU-C"])

	def test_mapped_skus_are_excluded(self):
		_make_order(self.account.name, f"ORD-M-{self.suffix}", ["SKU-A", "SKU-MAPPED"])
		_make_mapping(self.account.name, self.item, "SKU-MAPPED")

		with self._no_noon_http():
			skus = list_unmapped_order_skus(self.account.name)

		self.assertEqual(skus, ["SKU-A"])

	def test_disabled_mapping_does_not_hide_sku(self):
		_make_order(self.account.name, f"ORD-D-{self.suffix}", ["SKU-OFF"])
		_make_mapping(self.account.name, self.item, "SKU-OFF", enabled=0)

		with self._no_noon_http():
			skus = list_unmapped_order_skus(self.account.name)

		self.assertEqual(skus, ["SKU-OFF"])

	def test_other_noon_accounts_orders_are_excluded(self):
		other_noon = _make_noon_account(f"Noon Other {self.suffix}")
		self.addCleanup(self._delete_other_noon_account, other_noon)
		_make_order(other_noon.name, f"ORD-OTHER-{self.suffix}", ["SKU-OTHER-ACCOUNT"])
		_make_order(self.account.name, f"ORD-MINE-{self.suffix}", ["SKU-MINE"])

		with self._no_noon_http():
			skus = list_unmapped_order_skus(self.account.name)

		self.assertEqual(skus, ["SKU-MINE"])

	def test_disabled_account_is_rejected(self):
		self.account.db_set("enabled", 0, update_modified=False)
		_make_order(self.account.name, f"ORD-DIS-{self.suffix}", ["SKU-DIS"])

		with self._no_noon_http(), self.assertRaises(frappe.ValidationError):
			list_unmapped_order_skus(self.account.name)

	def test_requires_read_permission_on_the_account(self):
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")

		with self._no_noon_http(), self.assertRaises(frappe.PermissionError):
			list_unmapped_order_skus(self.account.name)

	def test_upsert_creates_then_updates_one_mapping_and_removes_sku_from_list(self):
		_make_order(self.account.name, f"ORD-X-{self.suffix}", ["SKU-X"])
		other_item = _make_item(f"Noon Needs Mapping Other {self.suffix}")

		with self._no_noon_http():
			first = upsert_mapping(self.account.name, "SKU-X", self.item)
			second = upsert_mapping(self.account.name, "SKU-X", other_item)
			skus = list_unmapped_order_skus(self.account.name)

		self.assertEqual(first["name"], second["name"])
		mapping = frappe.get_doc("Marketplace Product Mapping", first["name"])
		self.assertEqual(mapping.item, other_item)
		self.assertEqual(mapping.external_reference, "SKU-X")
		self.assertEqual(mapping.external_sku, "SKU-X")
		self.assertEqual(skus, [])

	def test_upsert_case_variant_creates_a_new_mapping_and_keeps_the_original(self):
		first = upsert_mapping(self.account.name, "abc-1", self.item)
		second = upsert_mapping(self.account.name, "ABC-1", self.item)

		self.assertNotEqual(first["name"], second["name"])
		self.assertEqual(
			frappe.get_doc("Marketplace Product Mapping", first["name"]).external_reference, "abc-1"
		)
		self.assertEqual(
			frappe.get_doc("Marketplace Product Mapping", second["name"]).external_reference, "ABC-1"
		)

	def test_upsert_rejects_blank_partner_sku(self):
		with self._no_noon_http(), self.assertRaises(frappe.ValidationError):
			upsert_mapping(self.account.name, "   ", self.item)

	def _delete_other_noon_account(self, account):
		for order in frappe.get_all("Marketplace Order", {"marketplace_account": account.name}, pluck="name"):
			frappe.delete_doc("Marketplace Order", order, ignore_permissions=True, force=True)
		frappe.delete_doc("Marketplace Account", account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", account.connection, ignore_permissions=True, force=True)
