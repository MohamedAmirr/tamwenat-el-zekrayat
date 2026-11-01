# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the Noon Sales Order item-edit guard (#368)."""

from unittest.mock import patch

import frappe
from erpnext.controllers.accounts_controller import update_child_qty_rate

from marketplace_integrations.integrations.noon.test_fulfillment import IntegrationTestNoonFulfillmentBase
from marketplace_integrations.services import sales_order_sync
from marketplace_integrations.tests.handover_fixture import SubmittedOrderFixture

GUARD = "marketplace_integrations.services.noon_item_edit_guard"


class IntegrationTestNoonItemEditGuard(IntegrationTestNoonFulfillmentBase):
	def setUp(self):
		super().setUp()
		self.fx = SubmittedOrderFixture(self.account, [("A", 3), ("B", 2)])
		frappe.db.commit()

	def tearDown(self):
		frappe.flags.pop("marketplace_syncing_orders", None)
		self.fx.cleanup()
		frappe.db.commit()
		super().tearDown()

	def _trans_items(self, **overrides):
		"""``update_child_qty_rate`` payload for every row, with per-row ``{item: {field: value}}`` overrides."""
		rows = frappe.get_doc("Sales Order", self.fx.name).items
		payload = []
		for row in rows:
			entry = {
				"docname": row.name,
				"item_code": row.item_code,
				"description": row.description,
				"qty": row.qty,
				"rate": row.rate,
				"uom": row.uom,
				"conversion_factor": row.conversion_factor,
			}
			entry.update(overrides.get(row.item_code, {}))
			payload.append(entry)
		return payload

	def _update(self, payload):
		update_child_qty_rate("Sales Order", frappe.as_json(payload), self.fx.name)

	def _qty(self, sku):
		return self.fx.row_for(sku).qty

	def test_hooks_are_registered_on_the_item_rows(self):
		# update_child_qty_rate changes the child rows before it saves the parent, so a
		# parent-level hook would see no difference: the guard sits on Sales Order Item.
		events = frappe.get_hooks("doc_events", app_name="marketplace_integrations")["Sales Order Item"]
		self.assertIn(f"{GUARD}.block_row_change", events["before_update_after_submit"])
		self.assertIn(f"{GUARD}.block_row_add", events["before_insert"])
		self.assertIn(f"{GUARD}.block_row_remove", events["on_trash"])
		# The Noon guard is not on the parent; the only parent hook is the Bosta summary restore (#413).
		parent = frappe.get_hooks("doc_events", app_name="marketplace_integrations")["Sales Order"]
		self.assertEqual(
			parent["before_update_after_submit"],
			["marketplace_integrations.integrations.bosta.statuses.keep_stored_summary"],
		)

	def test_noon_quantity_change_is_refused_with_a_message_pointing_to_the_supported_actions(self):
		a = self.fx.items["A"].name
		with self.assertRaises(frappe.ValidationError) as caught:
			self._update(self._trans_items(**{a: {"qty": 1}}))
		self.assertIn("Mark as Out of Stock", str(caught.exception))
		self.assertEqual(self._qty("A"), 3)

	def test_noon_rate_change_is_refused(self):
		a = self.fx.items["A"].name
		with self.assertRaises(frappe.ValidationError):
			self._update(self._trans_items(**{a: {"rate": 99}}))

	def test_a_changed_pack_size_or_uom_is_refused_too(self):
		for field, value in (("conversion_factor", 2), ("stock_qty", 9), ("uom", "Box")):
			with self.subTest(field=field):
				row = frappe.get_doc("Sales Order Item", self.fx.row_for("A").name)
				row.flags.ignore_validate_update_after_submit = True
				row.set(field, value)
				with self.assertRaises(frappe.ValidationError) as caught:
					row.save()
				self.assertIn("Mark as Out of Stock", str(caught.exception))

	def test_noon_row_removal_is_refused(self):
		payload = [entry for entry in self._trans_items() if entry["item_code"] != self.fx.items["B"].name]
		with self.assertRaises(frappe.ValidationError):
			self._update(payload)
		self.assertEqual(len(frappe.get_doc("Sales Order", self.fx.name).items), 2)

	def test_noon_row_addition_is_refused(self):
		extra = SubmittedOrderFixture(self.account, [("C", 1)])
		try:
			row = frappe.get_doc("Sales Order", self.fx.name).items[0]
			payload = self._trans_items()
			payload.append(
				{
					"item_code": extra.items["C"].name,
					"qty": 1,
					"rate": 10,
					"uom": "Nos",
					"conversion_factor": 1,
					"warehouse": row.warehouse,
					"delivery_date": str(row.delivery_date),
				}
			)
			with self.assertRaises(frappe.ValidationError):
				self._update(payload)
			self.assertEqual(len(frappe.get_doc("Sales Order", self.fx.name).items), 2)
		finally:
			extra.cleanup()

	def test_the_sync_marker_lets_the_planners_own_write_through(self):
		a = self.fx.items["A"].name
		frappe.flags.marketplace_syncing_orders = {self.fx.marketplace_order.name}
		self._update(self._trans_items(**{a: {"qty": 2}}))
		self.assertEqual(self._qty("A"), 2)

	def test_the_marker_is_per_order(self):
		a = self.fx.items["A"].name
		frappe.flags.marketplace_syncing_orders = {"SOME-OTHER-ORDER"}
		with self.assertRaises(frappe.ValidationError):
			self._update(self._trans_items(**{a: {"qty": 2}}))

	def test_sync_sales_order_registers_the_marker_while_it_runs(self):
		seen = []

		def fake_sync(marketplace_order, trigger_source):
			seen.append(set(frappe.flags.get("marketplace_syncing_orders") or ()))
			return {}

		with patch.object(sales_order_sync, "_sync_sales_order", side_effect=fake_sync):
			sales_order_sync.sync_sales_order(self.fx.marketplace_order.name)
		self.assertEqual(seen, [{self.fx.marketplace_order.name}])
		self.assertNotIn(self.fx.marketplace_order.name, frappe.flags.get("marketplace_syncing_orders") or ())

	def test_non_item_fields_stay_editable_on_a_noon_order(self):
		so = frappe.get_doc("Sales Order", self.fx.name)
		so.contact_mobile = "01000000000"
		so.save()  # update after submit; header field only
		self.assertEqual(frappe.db.get_value("Sales Order", self.fx.name, "contact_mobile"), "01000000000")

	def test_shopify_order_keeps_todays_behaviour(self):
		a = self.fx.items["A"].name
		for platform, qty in (("Shopify", 2), ("Talabat", 1)):
			with self.subTest(platform=platform):
				frappe.db.set_value("Marketplace Order", self.fx.marketplace_order.name, "platform", platform)
				self._update(self._trans_items(**{a: {"qty": qty}}))
				self.assertEqual(self._qty("A"), qty)

	def test_creating_and_submitting_a_noon_sales_order_is_not_blocked(self):
		# The fixture inserts a draft with rows and submits it, as the import and sync do.
		other = SubmittedOrderFixture(self.account, [("NEW", 2)])
		try:
			self.assertEqual(frappe.db.get_value("Sales Order", other.name, "docstatus"), 1)
		finally:
			other.cleanup()

	def test_a_draft_noon_sales_order_rows_can_change(self):
		draft = SubmittedOrderFixture(self.account, [("DRAFT", 2)], submit=False)
		try:
			row = frappe.get_doc("Sales Order Item", draft.row_for("DRAFT").name)
			row.qty = 1
			row.save()
			frappe.delete_doc("Sales Order Item", row.name, force=True, ignore_permissions=True)
		finally:
			draft.cleanup()

	def test_unlinked_sales_order_is_not_affected(self):
		frappe.db.set_value("Sales Order", self.fx.name, "custom_marketplace_order", None)
		a = self.fx.items["A"].name
		self._update(self._trans_items(**{a: {"qty": 1}}))
		self.assertEqual(self._qty("A"), 1)
