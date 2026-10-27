# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the shared draft Delivery Note service (#368)."""

import frappe

from marketplace_integrations.integrations.noon.test_fulfillment import IntegrationTestNoonFulfillmentBase
from marketplace_integrations.services import sync_status
from marketplace_integrations.services.delivery_note_drafts import create_draft_delivery_note
from marketplace_integrations.services.handover_fixture import SubmittedOrderFixture
from marketplace_integrations.tests.utils import make_role_user


class IntegrationTestDraftDeliveryNote(IntegrationTestNoonFulfillmentBase):
	def setUp(self):
		super().setUp()
		self.fixtures = []

	def tearDown(self):
		frappe.set_user("Administrator")
		for fixture in self.fixtures:
			fixture.cleanup()
		frappe.db.commit()
		super().tearDown()

	def _order(self, rows, **kwargs):
		fixture = SubmittedOrderFixture(self.account, rows, **kwargs)
		self.fixtures.append(fixture)
		return fixture

	def _dn_rows(self, name):
		return frappe.get_doc("Delivery Note", name).items

	def test_creates_one_draft_limited_to_the_given_units(self):
		fx = self._order([("A", 3), ("B", 2)])
		result = create_draft_delivery_note(fx.marketplace_order.name, [{"external_sku": "A", "qty": 2}])

		self.assertTrue(result["delivery_note"])
		self.assertEqual(fx.delivery_notes(), [result["delivery_note"]])
		dn = frappe.get_doc("Delivery Note", result["delivery_note"])
		self.assertEqual(dn.docstatus, 0)  # never submitted
		self.assertEqual([(row.item_code, row.qty) for row in dn.items], [(fx.items["A"].name, 2)])
		self.assertEqual(dn.items[0].so_detail, fx.row_for("A").name)

	def test_counts_units_per_sku_and_ignores_zero_quantities(self):
		fx = self._order([("A", 5)])
		result = create_draft_delivery_note(
			fx.marketplace_order.name,
			[
				{"external_sku": "A", "qty": 1},
				{"external_sku": "A", "qty": 1},
				{"external_sku": "A", "qty": 0},
			],
		)
		self.assertEqual([row.qty for row in self._dn_rows(result["delivery_note"])], [2])

	def test_explicit_item_needs_no_mapping(self):
		fx = self._order([("A", 2)], unmapped=("A",))
		result = create_draft_delivery_note(fx.marketplace_order.name, [{"item": fx.items["A"].name, "qty": 1}])
		self.assertTrue(result["delivery_note"])
		self.assertEqual(result["unresolved"], [])

	def test_quantity_is_capped_and_the_excess_is_reported_and_queues_a_sync(self):
		fx = self._order([("A", 3)])
		result = create_draft_delivery_note(fx.marketplace_order.name, [{"external_sku": "A", "qty": 5}])
		self.assertEqual([row.qty for row in self._dn_rows(result["delivery_note"])], [3])
		item = fx.items["A"].name
		self.assertEqual(
			result["unresolved"], [{"external_sku": "A", "item": item, "qty": 2, "reason": "excess"}]
		)
		# The Sales Order is behind the platform order: it is queued for the normal debounced sync.
		order = frappe.db.get_value(
			"Marketplace Order",
			fx.marketplace_order.name,
			["erpnext_sync_status", "pending_sync_trigger_source", "sync_debounce_until"],
			as_dict=True,
		)
		self.assertEqual(order.erpnext_sync_status, sync_status.SYNCING)
		self.assertEqual(order.pending_sync_trigger_source, "ERPNext Document Change")
		self.assertTrue(order.sync_debounce_until)

	def test_queue_sync_false_reports_the_excess_without_queueing_a_sync(self):
		fx = self._order([("A", 1)])
		before = frappe.db.get_value("Marketplace Order", fx.marketplace_order.name, "erpnext_sync_status")
		result = create_draft_delivery_note(
			fx.marketplace_order.name, [{"external_sku": "A", "qty": 2}], queue_sync=False
		)
		self.assertEqual(result["unresolved"][0]["reason"], "excess")
		self.assertEqual(
			frappe.db.get_value("Marketplace Order", fx.marketplace_order.name, "erpnext_sync_status"), before
		)

	def test_delivered_quantity_reduces_the_cap(self):
		fx = self._order([("A", 3)])
		frappe.db.set_value("Sales Order Item", fx.row_for("A").name, "delivered_qty", 2)
		result = create_draft_delivery_note(fx.marketplace_order.name, [{"external_sku": "A", "qty": 3}])
		self.assertEqual([row.qty for row in self._dn_rows(result["delivery_note"])], [1])

	def test_second_call_never_over_delivers_and_creates_no_second_note(self):
		fx = self._order([("A", 3)])
		units = [{"external_sku": "A", "qty": 3}]
		first = create_draft_delivery_note(fx.marketplace_order.name, units)
		second = create_draft_delivery_note(fx.marketplace_order.name, units)

		self.assertTrue(first["delivery_note"])
		self.assertIsNone(second["delivery_note"])
		self.assertEqual([(e["reason"], e["qty"]) for e in second["unresolved"]], [("excess", 3)])
		self.assertEqual(fx.delivery_notes(), [first["delivery_note"]])

	def test_second_call_delivers_only_the_remainder_after_a_partial_draft(self):
		fx = self._order([("A", 3)])
		create_draft_delivery_note(fx.marketplace_order.name, [{"external_sku": "A", "qty": 1}])
		second = create_draft_delivery_note(fx.marketplace_order.name, [{"external_sku": "A", "qty": 5}])
		self.assertEqual([row.qty for row in self._dn_rows(second["delivery_note"])], [2])
		self.assertEqual(len(fx.delivery_notes()), 2)

	def test_a_deleted_draft_frees_its_quantity(self):
		fx = self._order([("A", 2)])
		first = create_draft_delivery_note(fx.marketplace_order.name, [{"external_sku": "A", "qty": 2}])
		frappe.delete_doc("Delivery Note", first["delivery_note"], force=True)
		again = create_draft_delivery_note(fx.marketplace_order.name, [{"external_sku": "A", "qty": 2}])
		self.assertTrue(again["delivery_note"])

	def test_unmapped_unit_is_reported_and_the_mapped_unit_is_still_drafted(self):
		fx = self._order([("A", 2)])
		before = frappe.db.get_value("Marketplace Order", fx.marketplace_order.name, "erpnext_sync_status")
		result = create_draft_delivery_note(
			fx.marketplace_order.name,
			[{"external_sku": "A", "qty": 1}, {"external_sku": "NO-SUCH-SKU", "qty": 1}],
		)
		self.assertTrue(result["delivery_note"])
		self.assertEqual(
			result["unresolved"], [{"external_sku": "NO-SUCH-SKU", "item": "", "qty": 1, "reason": "unmapped"}]
		)
		# The service writes no status (the caller persists the record) and an unmapped unit
		# needs a mapping, not a sync.
		self.assertEqual(
			frappe.db.get_value("Marketplace Order", fx.marketplace_order.name, "erpnext_sync_status"), before
		)

	def test_unit_with_no_matching_sales_order_row_is_skipped(self):
		fx = self._order([("A", 2)])
		# An Item that is mapped but not on this order's Sales Order.
		other = self._order([("OTHER", 1)])
		result = create_draft_delivery_note(
			fx.marketplace_order.name, [{"item": other.items["OTHER"].name, "qty": 1}]
		)
		self.assertIsNone(result["delivery_note"])
		self.assertEqual(
			[(e["item"], e["reason"]) for e in result["unresolved"]], [(other.items["OTHER"].name, "not_on_order")]
		)
		status = frappe.db.get_value("Marketplace Order", fx.marketplace_order.name, "erpnext_sync_status")
		self.assertEqual(status, sync_status.SYNCING)  # behind the platform order: sync queued
		self.assertEqual(fx.delivery_notes(), [])

	def test_all_units_skipped_creates_no_note(self):
		fx = self._order([("A", 2)])
		result = create_draft_delivery_note(fx.marketplace_order.name, [{"external_sku": "UNKNOWN", "qty": 2}])
		self.assertIsNone(result["delivery_note"])
		self.assertEqual(fx.delivery_notes(), [])

	def test_a_clean_call_leaves_the_sync_status_alone(self):
		fx = self._order([("A", 2)])
		before = frappe.db.get_value("Marketplace Order", fx.marketplace_order.name, "erpnext_sync_status")
		create_draft_delivery_note(fx.marketplace_order.name, [{"external_sku": "A", "qty": 1}])
		self.assertEqual(
			frappe.db.get_value("Marketplace Order", fx.marketplace_order.name, "erpnext_sync_status"), before
		)

	def test_order_without_a_sales_order_is_refused(self):
		fx = self._order([("A", 1)])
		frappe.db.set_value("Marketplace Order", fx.marketplace_order.name, "erpnext_sales_order", None)
		with self.assertRaises(frappe.ValidationError):
			create_draft_delivery_note(fx.marketplace_order.name, [{"external_sku": "A", "qty": 1}])

	def test_user_without_delivery_note_create_permission_is_refused(self):
		fx = self._order([("A", 1)])
		email, _role = make_role_user(self, "No DN", {"Marketplace Order": ["write"]})
		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				create_draft_delivery_note(fx.marketplace_order.name, [{"external_sku": "A", "qty": 1}])
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(fx.delivery_notes(), [])
