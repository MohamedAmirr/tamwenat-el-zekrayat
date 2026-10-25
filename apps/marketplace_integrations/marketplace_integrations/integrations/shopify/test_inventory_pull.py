# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Shopify is always the in-memory FakeShopify from test_inventory_sync (never a
real call). NOT RUN in the authoring worktree: bench could not reach it."""

import random
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify import inventory_pull
from marketplace_integrations.integrations.shopify.test_inventory_sync import (
	EXECUTE_TARGET,
	FETCH_LOCATIONS_TARGET,
	LOCATION,
	InventoryFixtureMixin,
)
from marketplace_integrations.tests.utils import make_role_user


class IntegrationTestInventoryPull(InventoryFixtureMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self._drafts = []
		self.company = frappe.db.get_value("Warehouse", self.warehouse, "company")
		self.has_adjustment_account = bool(
			frappe.db.get_value("Company", self.company, "stock_adjustment_account")
		)

	def tearDown(self):
		for name in self._drafts:
			frappe.db.delete("Comment", {"reference_doctype": "Stock Reconciliation", "reference_name": name})
			frappe.delete_doc("Stock Reconciliation", name, ignore_permissions=True, force=True)
		super().tearDown()

	def _pull(self):
		locations = {"shop_id": "s", "active_locations": [{"id": LOCATION, "name": "Main"}]}
		with (
			patch(EXECUTE_TARGET, side_effect=self.shopify),
			patch(FETCH_LOCATIONS_TARGET, return_value=locations),
		):
			result = inventory_pull._pull(self.account.name)
		if result["draft"]:
			self._drafts.append(result["draft"])
		return result

	def _skipped(self, result):
		return {row["item"]: row["reason"] for row in result["skipped"]}

	def _make_variant_item(self, qty, on_hand, **shopify_state):
		item = self._make_item(f"Pull{random.randint(0, 10**6)}", qty=qty)
		mapping = self._make_mapping(item)
		self.shopify.add(mapping.external_reference, on_hand=on_hand, **shopify_state)
		return item, mapping

	def test_includes_only_differing_items_with_zero_valuation_and_never_mutates(self):
		if not self.has_adjustment_account:
			self.skipTest("Company has no stock adjustment account")
		diff, _m = self._make_variant_item(qty=2, on_hand=9)
		same, _m = self._make_variant_item(qty=4, on_hand=4)

		result = self._pull()

		self.assertEqual(result["included"], 1)
		self.assertEqual(result["unchanged"], 1)
		doc = frappe.get_doc("Stock Reconciliation", result["draft"])
		self.assertEqual(doc.docstatus, 0)
		self.assertEqual([row.item_code for row in doc.items], [diff])
		row = doc.items[0]
		self.assertEqual(row.qty, 9)
		self.assertEqual(row.valuation_rate, 0)
		self.assertEqual(row.allow_zero_valuation_rate, 1)
		self.assertEqual(row.warehouse, self.warehouse)
		self.assertNotIn(same, [r.item_code for r in doc.items])
		self.assertEqual(self.shopify.kinds(), ["read"])

	def test_no_difference_creates_no_document(self):
		self._make_variant_item(qty=3, on_hand=3)
		before = frappe.db.count("Stock Reconciliation")

		result = self._pull()

		self.assertIsNone(result["draft"])
		self.assertEqual(result["included"], 0)
		self.assertEqual(frappe.db.count("Stock Reconciliation"), before)

	def test_skip_reasons(self):
		untracked, _m = self._make_variant_item(qty=1, on_hand=5, tracked=False)
		nolevel, _m = self._make_variant_item(qty=1, on_hand=5, has_level=False)
		nonstock = self._make_item(f"NonStock{random.randint(0, 10**6)}", is_stock_item=0)
		self._make_mapping(nonstock)
		batch, _m = self._make_variant_item(qty=1, on_hand=5)
		frappe.db.set_value("Item", batch, "has_batch_no", 1)
		disabled, _m = self._make_variant_item(qty=1, on_hand=5)
		frappe.db.set_value("Item", disabled, "disabled", 1)

		result = self._pull()
		skipped = self._skipped(result)

		self.assertEqual(set(skipped), {untracked, nolevel, nonstock, batch, disabled})
		self.assertIsNone(result["draft"])
		self.assertEqual(self.shopify.kinds(), ["read"])

	def test_unmapped_and_disabled_mapping_items_are_ignored(self):
		item, mapping = self._make_variant_item(qty=1, on_hand=5)
		frappe.db.set_value("Marketplace Product Mapping", mapping.name, "enabled", 0)

		result = self._pull()

		self.assertNotIn(item, self._skipped(result))
		self.assertEqual(result["included"], 0)

	def test_item_with_two_variants_is_skipped_not_guessed(self):
		item, _m = self._make_variant_item(qty=1, on_hand=5)
		other = self._make_mapping(item)
		self.shopify.add(other.external_reference, on_hand=7)

		self.assertIn(item, self._skipped(self._pull()))

	def test_missing_stock_adjustment_account_stops_with_error(self):
		self._make_variant_item(qty=1, on_hand=5)
		with patch(
			"marketplace_integrations.integrations.shopify.inventory_pull.frappe.db.get_value",
			side_effect=lambda doctype, *a, **k: (
				frappe._dict(stock_adjustment_account=None, cost_center=None)
				if doctype == "Company"
				else self.warehouse_company(doctype)
			),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "Stock Adjustment Account"):
				self._pull()
		self.assertEqual(self.shopify.calls, [])

	def warehouse_company(self, doctype):
		return self.company

	def test_works_with_push_switch_off(self):
		self.account.db_set("shopify_inventory_push_enabled", 0, update_modified=False)
		self._make_variant_item(qty=1, on_hand=1)

		self.assertEqual(self._pull()["included"], 0)

	def test_finds_existing_draft_for_same_warehouse_only(self):
		if not self.has_adjustment_account:
			self.skipTest("Company has no stock adjustment account")
		self._make_variant_item(qty=1, on_hand=8)
		first = self._pull()["draft"]
		self.assertEqual(inventory_pull.find_existing_drafts(self.account.name), [first])

		frappe.db.set_value("Comment", {"reference_name": first}, "content", "Shopify inventory pull other")
		self.assertEqual(inventory_pull.find_existing_drafts(self.account.name), [])

	def test_queue_requires_permissions_and_enqueues_on_long_queue(self):
		email, _role = make_role_user(self, "Pull Writer", {"Marketplace Account": ["write"]})
		frappe.set_user(email)
		self.addCleanup(frappe.set_user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			inventory_pull.queue_inventory_pull(self.account.name)

		frappe.set_user("Administrator")
		with patch("marketplace_integrations.integrations.shopify.inventory_pull.frappe.enqueue") as enqueue:
			enqueue.return_value = object()
			self.account.db_set("shopify_inventory_push_enabled", 0, update_modified=False)
			self.assertEqual(inventory_pull.queue_inventory_pull(self.account.name), {"queued": True})
		self.assertEqual(enqueue.call_args.kwargs["queue"], "long")
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])

	def test_queue_rejects_non_shopify_account(self):
		self.account.db_set("platform", "Talabat", update_modified=False)
		with self.assertRaisesRegex(frappe.ValidationError, "not a Shopify account"):
			inventory_pull.queue_inventory_pull(self.account.name)

	def test_queue_reports_already_running(self):
		with patch(
			"marketplace_integrations.integrations.shopify.inventory_pull.frappe.enqueue", return_value=None
		):
			with self.assertRaisesRegex(frappe.ValidationError, "already running"):
				inventory_pull.queue_inventory_pull(self.account.name)
