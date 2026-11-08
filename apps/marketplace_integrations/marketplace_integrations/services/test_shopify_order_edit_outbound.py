# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the Shopify item-edit flow (#356): preview, send, echo marker, API gates.

``frappe.db.commit`` is patched in every test that reaches the send path (the service
commits on purpose; a real commit would make fixture rows permanent on the shared test
site, see ``docs/frappe-conventions.md``, Tests).  No Shopify call is ever made:
``commit_order_edit`` and ``import_order`` are always mocked.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from marketplace_integrations.api import marketplace_outbound as outbound_api
from marketplace_integrations.integrations.shopify.order_edit import OrderEditOutcomeUnknown
from marketplace_integrations.integrations.shopify.test_products import make_item, make_shopify_account
from marketplace_integrations.services import marketplace_outbound as outbound
from marketplace_integrations.services import sales_order_sync
from marketplace_integrations.services.linked_sales_order_fixture import LinkedSalesOrderFixture
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.tests.utils import make_role_user

COMMIT_TARGET = "marketplace_integrations.integrations.shopify.order_edit.commit_order_edit"
IMPORT_TARGET = "marketplace_integrations.integrations.shopify.orders.import_order"
LINE_ID = "gid://shopify/LineItem/1"
LINE_ID_B = "gid://shopify/LineItem/2"
OPEN_STATUS = "To Deliver and Bill"


class IntegrationTestShopifyOrderEditOutbound(IntegrationTestCase):
	def setUp(self):
		self._original_lang = frappe.local.lang
		frappe.local.lang = "en"
		self.suffix = frappe.generate_hash(length=8)
		self.company = frappe.defaults.get_global_default("company")
		self.account = self._make_account()
		self.fixture = LinkedSalesOrderFixture()
		self.orders = []
		self.extra_items = []
		self.order = self._shopify_order()

	def tearDown(self):
		frappe.local.lang = self._original_lang
		frappe.db.set_value(
			"Sales Order", self.fixture.name, {"docstatus": 0, "custom_marketplace_order": None}
		)
		self.fixture.cleanup()
		for name in self.orders:
			frappe.db.delete("Marketplace Sync Log", {"marketplace_order": name})
			frappe.delete_doc("Marketplace Order", name, force=True, ignore_permissions=True)
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": self.account.name})
		for item_code in self.extra_items:
			frappe.delete_doc("Item", item_code, force=True, ignore_permissions=True)
		frappe.delete_doc("Marketplace Account", self.account.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Shopify Connection", self.account.connection, force=True, ignore_permissions=True)

	# -- fixtures -----------------------------------------------------------

	def _make_account(self):
		return make_shopify_account(f"Edit Account {self.suffix}")

	def _shopify_order(self, platform="Shopify"):
		order = upsert_marketplace_order(
			self.account.name,
			{
				"external_order_id": f"edit-order-{self.suffix}-{frappe.generate_hash(length=4)}",
				"latest_payload": {"order_id": "external-order"},
				"external_status": "RECEIVED",
				"items": [],
			},
		)
		self.orders.append(order.name)
		sales_order = frappe.get_doc("Sales Order", self.fixture.name)
		row = sales_order.items[0]
		baseline = {
			"items": [
				{
					"external_item_id": LINE_ID,
					"sales_order_item": row.name,
					"item_code": row.item_code,
					"qty": 1,
					"uom": row.uom,
					"fulfilled_total_price": 10,
				}
			],
			"shipping": 5,
		}
		doc = frappe.get_doc("Marketplace Order", order.name)
		doc.append(
			"items",
			{
				"external_item_id": LINE_ID,
				"fulfillment_quantities_known": 1,
				"current_quantity": 1,
				"unfulfilled_quantity": 1,
			},
		)
		doc.flags.skip_sync_status_update = True
		doc.save()
		frappe.db.set_value(
			"Marketplace Order",
			order.name,
			{
				"platform": platform,
				"shop_currency": sales_order.currency,
				"last_synced_hash": "baseline-hash",
				"last_synced_snapshot": frappe.as_json(baseline),
			},
		)
		frappe.db.set_value(
			"Sales Order",
			self.fixture.name,
			{"custom_marketplace_order": order.name, "docstatus": 1, "status": OPEN_STATUS},
		)
		return order

	def _add_row(self, item_code, qty=1, rate=20, **fields):
		"""Add a Sales Order row (the fixture Sales Order is submitted by db write, so reopen it briefly)."""
		frappe.db.set_value("Sales Order", self.fixture.name, "docstatus", 0)
		sales_order = frappe.get_doc("Sales Order", self.fixture.name)
		sales_order.append(
			"items",
			{
				"item_code": item_code,
				"qty": qty,
				"rate": rate,
				"uom": "Nos",
				"warehouse": sales_order.items[0].warehouse,
				**fields,
			},
		)
		sales_order.save()
		frappe.db.set_value("Sales Order", self.fixture.name, {"docstatus": 1, "status": OPEN_STATUS})
		return sales_order.items[-1].name

	def _make_item(self, label):
		item_code = make_item(f"EDIT-{label}-{self.suffix}")
		self.extra_items.append(item_code)
		return item_code

	def _make_mapping(self, item_code, variant_id):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": item_code,
				"external_reference": variant_id,
				"external_variant_id": variant_id,
				"external_product_id": "gid://shopify/Product/1",
			}
		).insert()

	def _set_row(self, **fields):
		row = frappe.get_doc("Sales Order", self.fixture.name).items[0].name
		frappe.db.set_value("Sales Order Item", row, fields)

	def _set_baseline(self, mutate):
		snapshot = frappe.parse_json(
			frappe.db.get_value("Marketplace Order", self.order.name, "last_synced_snapshot")
		)
		mutate(snapshot)
		frappe.db.set_value(
			"Marketplace Order", self.order.name, "last_synced_snapshot", frappe.as_json(snapshot)
		)

	def _set_order(self, **fields):
		frappe.db.set_value("Marketplace Order", self.order.name, fields)

	def _preview(self):
		return outbound.get_shopify_order_edit_preview(self.fixture.name)

	def _marker(self):
		return frappe.db.get_value(
			"Marketplace Order",
			self.order.name,
			["outbound_edit_fingerprint", "outbound_edit_state"],
			as_dict=True,
		)

	def _logs(self):
		return frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": self.order.name, "sync_type": "Order Action"},
			fields=["status", "request_data", "traceback"],
		)

	def _send(self, fingerprint, **patches):
		with (
			patch(COMMIT_TARGET, **patches) as commit_edit,
			patch(IMPORT_TARGET, return_value=frappe.get_doc("Marketplace Order", self.order.name)),
			patch.object(sales_order_sync, "rebaseline_after_outbound_edit"),
			patch.object(frappe.db, "commit"),
		):
			try:
				result = outbound.send_shopify_order_edit(self.fixture.name, fingerprint)
			finally:
				self.commit_edit = commit_edit
		return result

	def _eligible_preview(self):
		self._set_row(qty=2)
		preview = self._preview()
		self.assertEqual(preview["state"], outbound.ELIGIBLE, preview["reasons"])
		return preview

	# -- preview ------------------------------------------------------------

	def test_a_quantity_change_is_an_eligible_preview_with_fingerprint_and_like_for_like_totals(self):
		self._set_row(qty=2, amount=20)

		preview = self._preview()

		self.assertEqual(preview["state"], outbound.ELIGIBLE)
		self.assertEqual(
			preview["changes"],
			[
				{
					"kind": "set_quantity",
					"external_item_id": LINE_ID,
					"quantity": 2.0,
					"label": preview["changes"][0]["label"],
					"old_quantity": 1.0,
				}
			],
		)
		self.assertEqual(len(preview["fingerprint"]), 64)
		# Baseline items plus shipping (10 + 5) against the Sales Order's own line amounts plus shipping now.
		self.assertEqual((preview["old_total"], preview["new_total"]), (15, 25))
		self.assertEqual(preview["total_difference"], 10)

	def test_the_fingerprint_changes_with_the_change_list(self):
		first = self._eligible_preview()["fingerprint"]
		self._set_row(qty=3)
		self.assertNotEqual(self._preview()["fingerprint"], first)

	def test_every_blocking_condition_blocks_the_preview(self):
		cases = {
			"cancelled": lambda: self._set_order(cancelled_at=frappe.utils.now_datetime()),
			"refunded": lambda: self._set_order(external_financial_status="REFUNDED"),
			"partially refunded": lambda: self._set_order(external_financial_status="PARTIALLY_REFUNDED"),
			"currency": lambda: self._set_order(shop_currency="ZZZ"),
			"fulfilled": lambda: frappe.db.set_value(
				"Marketplace Order Item",
				{"parent": self.order.name},
				{"current_quantity": 1, "unfulfilled_quantity": 0},
			),
			"unknown fulfillment": lambda: frappe.db.set_value(
				"Marketplace Order Item", {"parent": self.order.name}, "fulfillment_quantities_known", 0
			),
			"delivered": lambda: self._set_row(delivered_qty=1),
			"billed": lambda: self._set_row(billed_amt=5),
		}
		for name, make_blocking in cases.items():
			with self.subTest(name):
				self.setUp()
				try:
					self._set_row(qty=2)
					make_blocking()
					preview = self._preview()
					self.assertEqual(preview["state"], outbound.BLOCKED)
					self.assertTrue(preview["reasons"])
				finally:
					self.tearDown()

	def test_a_changed_line_on_a_draft_delivery_note_blocks(self):
		self._set_row(qty=2)
		real_exists = frappe.db.exists

		def exists(doctype, *args, **kwargs):
			if doctype == "Delivery Note Item":
				return True
			return real_exists(doctype, *args, **kwargs)

		with patch.object(frappe.db, "exists", side_effect=exists):
			preview = self._preview()

		self.assertEqual(preview["state"], outbound.BLOCKED)
		self.assertIn("draft Delivery Note", " ".join(preview["reasons"]))

	def test_no_baseline_is_blocked_without_crashing(self):
		for value in (None, "[]", "{}"):
			with self.subTest(snapshot=value):
				self._set_order(last_synced_snapshot=value)
				preview = self._preview()
				self.assertEqual(preview["state"], outbound.BLOCKED)
				self.assertEqual(preview["changes"], [])
				self.assertIn("baseline", " ".join(preview["reasons"]))

	def test_fractional_or_negative_quantities_are_blocked_before_any_cast(self):
		for qty in (1.5, -1):
			with self.subTest(qty=qty):
				self._set_row(qty=qty)
				preview = self._preview()
				self.assertEqual(preview["state"], outbound.BLOCKED)
				self.assertIn("whole non-negative", " ".join(preview["reasons"]))
				# The unrounded value is kept: nothing silently turned 1.5 into 1.
				self.assertEqual(preview["changes"][0]["quantity"], qty)

	def test_swapping_the_item_or_uom_on_a_synced_line_is_blocked(self):
		self._set_baseline(lambda snapshot: snapshot["items"][0].update({"item_code": "SOME-OTHER-ITEM"}))
		self.assertEqual(self._preview()["state"], outbound.BLOCKED)
		self._set_baseline(
			lambda snapshot: snapshot["items"][0].update({"item_code": self._row_item(), "uom": "Box"})
		)
		preview = self._preview()
		self.assertEqual(preview["state"], outbound.BLOCKED)
		self.assertIn("cannot be swapped", " ".join(preview["reasons"]))

	def _row_item(self):
		return frappe.get_doc("Sales Order", self.fixture.name).items[0].item_code

	def test_a_legacy_baseline_without_row_identities_is_blocked(self):
		self._set_baseline(lambda snapshot: snapshot["items"][0].update({"sales_order_item": None}))
		self._set_row(qty=2)
		preview = self._preview()
		self.assertEqual(preview["state"], outbound.BLOCKED)
		self.assertEqual(preview["changes"], [])
		self.assertIn("synchronize the order again", " ".join(preview["reasons"]))

	def test_any_marker_blocks_the_preview_whatever_its_fingerprint_or_state(self):
		self._set_row(qty=2)
		expected = {
			"in_flight": "may or may not have reached Shopify",
			"completed": "already sent to Shopify",
		}
		for state, text in expected.items():
			for fingerprint in (self._preview()["fingerprint"], "a-different-fingerprint"):
				with self.subTest(state=state, fingerprint=fingerprint[:6]):
					self._set_order(outbound_edit_fingerprint=fingerprint, outbound_edit_state=state)
					preview = self._preview()
					self.assertEqual(preview["state"], outbound.BLOCKED)
					self.assertIn(text, " ".join(preview["reasons"]))

	def test_a_reduction_is_a_set_quantity_change(self):
		self._set_baseline(lambda snapshot: snapshot["items"][0].update({"qty": 3}))
		self._set_row(qty=2)
		preview = self._preview()
		self.assertEqual(preview["state"], outbound.ELIGIBLE, preview["reasons"])
		change = preview["changes"][0]
		self.assertEqual((change["kind"], change["quantity"], change["old_quantity"]), ("set_quantity", 2, 3))

	def test_a_deleted_row_is_a_set_quantity_zero_change(self):
		deleted = self._add_row(self._row_item(), qty=1)
		self._set_baseline(
			lambda snapshot: snapshot["items"].append(
				{
					"external_item_id": LINE_ID_B,
					"sales_order_item": deleted,
					"item_code": self._row_item(),
					"qty": 1,
					"uom": "Nos",
					"fulfilled_total_price": 20,
				}
			)
		)
		doc = frappe.get_doc("Marketplace Order", self.order.name)
		doc.append(
			"items",
			{
				"external_item_id": LINE_ID_B,
				"fulfillment_quantities_known": 1,
				"current_quantity": 1,
				"unfulfilled_quantity": 1,
			},
		)
		doc.flags.skip_sync_status_update = True
		doc.save()
		frappe.db.delete("Sales Order Item", {"name": deleted})

		preview = self._preview()

		self.assertEqual(preview["state"], outbound.ELIGIBLE, preview["reasons"])
		self.assertEqual(
			[
				(c["kind"], c["external_item_id"], c["quantity"], c["old_quantity"])
				for c in preview["changes"]
			],
			[("set_quantity", LINE_ID_B, 0, 1)],
		)

	def test_a_new_row_with_one_enabled_mapping_is_an_add_variant_change(self):
		item = self._make_item("ADD")
		self._make_mapping(item, "gid://shopify/ProductVariant/55")
		self._add_row(item, qty=2)
		preview = self._preview()
		self.assertEqual(preview["state"], outbound.ELIGIBLE, preview["reasons"])
		change = preview["changes"][0]
		self.assertEqual(
			(change["kind"], change["external_variant_id"], change["quantity"], change["old_quantity"]),
			("add_variant", "gid://shopify/ProductVariant/55", 2, 0),
		)

	def test_a_new_row_without_exactly_one_mapping_is_blocked(self):
		item = self._make_item("MAP")
		self._add_row(item)
		for label, variants in (
			("missing", []),
			("ambiguous", ["gid://shopify/ProductVariant/1", "gid://shopify/ProductVariant/2"]),
		):
			with self.subTest(label):
				for variant in variants:
					self._make_mapping(item, variant)
				preview = self._preview()
				self.assertEqual(preview["state"], outbound.BLOCKED)
				self.assertIn("exactly one enabled Shopify variant mapping", " ".join(preview["reasons"]))
				self.assertEqual(preview["changes"], [])

	def test_a_new_row_with_a_uom_conversion_other_than_one_is_blocked(self):
		item = self._make_item("CONV")
		self._make_mapping(item, "gid://shopify/ProductVariant/66")
		row = self._add_row(item)
		frappe.db.set_value("Sales Order Item", row, "conversion_factor", 12)
		preview = self._preview()
		self.assertEqual(preview["state"], outbound.BLOCKED)
		self.assertIn("conversion of 1", " ".join(preview["reasons"]))

	def test_a_new_row_that_is_delivered_billed_or_on_a_draft_delivery_note_is_blocked(self):
		item = self._make_item("DEL")
		self._make_mapping(item, "gid://shopify/ProductVariant/77")
		row = self._add_row(item)
		frappe.db.set_value("Sales Order Item", row, "delivered_qty", 1)
		self.assertIn("delivered quantity", " ".join(self._preview()["reasons"]))
		frappe.db.set_value("Sales Order Item", row, {"delivered_qty": 0, "billed_amt": 5})
		self.assertIn("billed amount", " ".join(self._preview()["reasons"]))
		frappe.db.set_value("Sales Order Item", row, "billed_amt", 0)
		real_exists = frappe.db.exists

		def exists(doctype, *args, **kwargs):
			return True if doctype == "Delivery Note Item" else real_exists(doctype, *args, **kwargs)

		with patch.object(frappe.db, "exists", side_effect=exists):
			self.assertIn("draft Delivery Note", " ".join(self._preview()["reasons"]))

	def test_discount_allocations_on_any_line_block_the_preview(self):
		self._set_row(qty=2)
		items = {"parent": self.order.name}
		frappe.db.set_value("Marketplace Order Item", items, "promotions", "[]")
		self.assertEqual(self._preview()["state"], outbound.ELIGIBLE)
		for value in ('[{"allocatedAmountSet": {"shopMoney": {"amount": "1.0"}}}]',):
			with self.subTest(value=value):
				frappe.db.set_value("Marketplace Order Item", items, "promotions", value)
				preview = self._preview()
				self.assertEqual(preview["state"], outbound.BLOCKED)
				self.assertIn("discounts on its items", " ".join(preview["reasons"]))

	def test_the_sales_order_must_be_submitted_and_open(self):
		self._set_row(qty=2)
		for label, fields in (
			("draft", {"docstatus": 0}),
			("closed", {"status": "Closed"}),
			("on hold", {"status": "On Hold"}),
		):
			with self.subTest(label):
				frappe.db.set_value("Sales Order", self.fixture.name, fields)
				preview = self._preview()
				self.assertEqual(preview["state"], outbound.BLOCKED)
				self.assertIn("submitted Sales Order", " ".join(preview["reasons"]))
				frappe.db.set_value("Sales Order", self.fixture.name, {"docstatus": 1, "status": OPEN_STATUS})

	def test_non_shopify_and_unlinked_orders_are_not_applicable(self):
		self._set_row(qty=2)
		for platform in ("Talabat", "Noon"):
			with self.subTest(platform=platform):
				self._set_order(platform=platform)
				self.assertEqual(self._preview()["state"], outbound.NOT_APPLICABLE)
		frappe.db.set_value("Sales Order", self.fixture.name, "custom_marketplace_order", None)
		self.assertEqual(self._preview()["state"], outbound.NOT_APPLICABLE)

	# -- send ---------------------------------------------------------------

	def test_send_marks_in_flight_before_the_call_then_completes_and_logs_success(self):
		preview = self._eligible_preview()
		seen = {}

		def commit_edit(account, order_gid, changes, note):
			seen["marker"] = self._marker()
			return {"order_id": order_gid, "success_messages": ["done"]}

		result = self._send(preview["fingerprint"], side_effect=commit_edit)

		self.assertEqual(seen["marker"].outbound_edit_state, "in_flight")
		self.assertEqual(seen["marker"].outbound_edit_fingerprint, preview["fingerprint"])
		self.assertEqual(
			result,
			{"ok": True, "refreshed": True, "applied": True, "apply_error": None, "success_messages": ["done"]},
		)
		self.assertEqual(self._marker().outbound_edit_state, "completed")
		logs = self._logs()
		self.assertEqual([log.status for log in logs], ["Success"])
		self.assertEqual(frappe.parse_json(logs[0].request_data)["action"], "shopify_order_edit")

	def test_a_fingerprint_other_than_the_previewed_one_is_rejected_without_a_call(self):
		self._eligible_preview()
		with self.assertRaises(frappe.ValidationError):
			self._send("0" * 64, return_value={"success_messages": []})
		self.commit_edit.assert_not_called()
		self.assertIsNone(self._marker().outbound_edit_fingerprint)
		self.assertEqual(self._logs(), [])

	def test_a_repeated_send_makes_no_second_mutation(self):
		preview = self._eligible_preview()
		self._send(preview["fingerprint"], return_value={"success_messages": []})
		self.commit_edit.assert_called_once()

		with self.assertRaises(frappe.ValidationError):
			self._send(preview["fingerprint"], return_value={"success_messages": []})
		self.commit_edit.assert_not_called()
		self.assertEqual(len(self._logs()), 1)

	def test_a_confirmed_failure_logs_one_error_clears_the_marker_and_allows_a_retry(self):
		preview = self._eligible_preview()
		with self.assertRaises(frappe.ValidationError):
			self._send(preview["fingerprint"], side_effect=frappe.ValidationError("Rejected"))

		self.assertEqual([log.status for log in self._logs()], ["Error"])
		self.assertIn("Rejected", self._logs()[0].traceback)
		self.assertIsNone(self._marker().outbound_edit_fingerprint)
		self.assertEqual(self._preview()["fingerprint"], preview["fingerprint"])
		self._send(preview["fingerprint"], return_value={"success_messages": []})
		self.commit_edit.assert_called_once()

	def test_an_ambiguous_commit_failure_keeps_the_marker_so_the_changes_are_not_resent(self):
		preview = self._eligible_preview()
		with self.assertRaises(OrderEditOutcomeUnknown):
			self._send(preview["fingerprint"], side_effect=OrderEditOutcomeUnknown("unknown"))

		self.assertEqual(self._marker().outbound_edit_state, "in_flight")
		self.assertEqual([log.status for log in self._logs()], ["Error"])
		with self.assertRaises(frappe.ValidationError):
			self._send(preview["fingerprint"], return_value={"success_messages": []})
		self.commit_edit.assert_not_called()

	def test_a_refresh_failure_keeps_the_success_row_and_a_completed_marker(self):
		preview = self._eligible_preview()
		with (
			patch(COMMIT_TARGET, return_value={"success_messages": ["done"]}),
			patch(IMPORT_TARGET, side_effect=RuntimeError("boom")),
			patch.object(frappe, "log_error") as log_error,
			patch.object(frappe.db, "commit"),
		):
			result = outbound.send_shopify_order_edit(self.fixture.name, preview["fingerprint"])

		self.assertEqual(result["refreshed"], False)
		self.assertTrue(result["ok"])
		self.assertEqual(self._marker().outbound_edit_state, "completed")
		self.assertEqual([log.status for log in self._logs()], ["Success"])
		log_error.assert_called_once()

	def test_send_never_runs_for_a_non_shopify_order(self):
		self._set_row(qty=2)
		for platform in ("Talabat", "Noon"):
			with self.subTest(platform=platform):
				self._set_order(platform=platform)
				with self.assertRaises(frappe.ValidationError):
					self._send("anything", return_value={"success_messages": []})
				self.commit_edit.assert_not_called()
				self.assertIsNone(self._marker().outbound_edit_fingerprint)

	def test_a_confirmed_failure_does_not_clear_a_marker_another_writer_moved(self):
		preview = self._eligible_preview()

		def commit_edit(account, order_gid, changes, note):
			self._set_order(outbound_edit_fingerprint="other-writer", outbound_edit_state="in_flight")
			raise frappe.ValidationError("Rejected")

		with self.assertRaises(frappe.ValidationError):
			self._send(preview["fingerprint"], side_effect=commit_edit)
		self.assertEqual(self._marker().outbound_edit_fingerprint, "other-writer")

	def test_completion_only_applies_to_the_marker_this_call_set(self):
		preview = self._eligible_preview()

		def commit_edit(account, order_gid, changes, note):
			self._set_order(outbound_edit_fingerprint="other-writer", outbound_edit_state="in_flight")
			return {"success_messages": []}

		self._send(preview["fingerprint"], side_effect=commit_edit)
		marker = self._marker()
		self.assertEqual(
			(marker.outbound_edit_fingerprint, marker.outbound_edit_state), ("other-writer", "in_flight")
		)

	def test_send_is_refused_while_any_marker_is_set(self):
		preview = self._eligible_preview()
		self._set_order(outbound_edit_fingerprint="a-different-fingerprint", outbound_edit_state="in_flight")
		with self.assertRaises(frappe.ValidationError):
			self._send(preview["fingerprint"], return_value={"success_messages": []})
		self.commit_edit.assert_not_called()

	def test_send_is_refused_for_a_draft_sales_order(self):
		preview = self._eligible_preview()
		frappe.db.set_value("Sales Order", self.fixture.name, "docstatus", 0)
		with self.assertRaises(frappe.ValidationError):
			self._send(preview["fingerprint"], return_value={"success_messages": []})
		self.commit_edit.assert_not_called()

	def test_a_successful_refresh_rebaselines_through_the_shared_helper(self):
		preview = self._eligible_preview()
		refreshed = frappe.get_doc("Marketplace Order", self.order.name)
		with (
			patch(COMMIT_TARGET, return_value={"success_messages": []}),
			patch(IMPORT_TARGET, return_value=refreshed),
			patch.object(sales_order_sync, "rebaseline_after_outbound_edit") as rebaseline,
			patch.object(frappe.db, "commit"),
		):
			outbound.send_shopify_order_edit(self.fixture.name, preview["fingerprint"])

		rebaseline.assert_called_once()
		self.assertIs(rebaseline.call_args.args[0], refreshed)

	# -- re-baseline and echo marker ----------------------------------------
	# These run the real planner, pairing and save; only the snapshot builder (which needs a
	# fully mapped order) is replaced by the snapshot Shopify's refreshed order would produce.

	def _line(self, external_item_id, row, qty, **overrides):
		return {
			"external_item_id": external_item_id,
			"item_code": row.item_code,
			"qty": qty,
			"uom": row.uom,
			"warehouse": row.warehouse,
			"effective_rate": row.rate,
			"fulfilled_total_price": flt(row.rate) * qty,
			**overrides,
		}

	def _rebaseline(self, refreshed_lines, marker_state="completed"):
		self._set_order(outbound_edit_fingerprint="f" * 64, outbound_edit_state=marker_state)
		order = frappe.get_doc("Marketplace Order", self.order.name)
		sales_order = frappe.get_doc("Sales Order", self.fixture.name)
		with patch.object(
			sales_order_sync, "build_sales_order_snapshot", return_value={"items": refreshed_lines}
		):
			result = sales_order_sync.rebaseline_after_outbound_edit(order, sales_order)
		return result, frappe.get_doc("Marketplace Order", self.order.name)

	def _baseline_of(self, *lines):
		frappe.db.set_value(
			"Marketplace Order",
			self.order.name,
			"last_synced_snapshot",
			frappe.as_json({"items": list(lines)}),
		)

	def _assert_rebaselined(self, result, order, expected_lines):
		self.assertEqual(result["ok"], True, order.last_sync_error)
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertIsNone(order.outbound_edit_fingerprint)
		self.assertIsNone(order.outbound_edit_state)
		stored = frappe.parse_json(order.last_synced_snapshot)["items"]
		self.assertEqual(
			[(line["external_item_id"], line["qty"], bool(line["sales_order_item"])) for line in stored],
			expected_lines,
		)

	def test_rebaseline_accepts_a_reduced_quantity(self):
		row = frappe.get_doc("Sales Order", self.fixture.name).items[0]
		self._baseline_of(self._line(LINE_ID, row, 3, sales_order_item=row.name))
		self._set_row(qty=2, amount=20)
		row = frappe.get_doc("Sales Order", self.fixture.name).items[0]

		result, order = self._rebaseline([self._line(LINE_ID, row, 2)])

		self._assert_rebaselined(result, order, [(LINE_ID, 2, True)])

	def test_rebaseline_accepts_an_increased_quantity(self):
		row = frappe.get_doc("Sales Order", self.fixture.name).items[0]
		self._set_row(qty=2, amount=20)
		row = frappe.get_doc("Sales Order", self.fixture.name).items[0]

		result, order = self._rebaseline([self._line(LINE_ID, row, 2)])

		self._assert_rebaselined(result, order, [(LINE_ID, 2, True)])

	def test_rebaseline_accepts_a_removed_line(self):
		kept = frappe.get_doc("Sales Order", self.fixture.name).items[0]
		removed = self._add_row(kept.item_code, qty=1, rate=20)
		removed_row = frappe.get_doc("Sales Order", self.fixture.name).items[1]
		self._baseline_of(
			self._line(LINE_ID, kept, 1, sales_order_item=kept.name),
			self._line(LINE_ID_B, removed_row, 1, sales_order_item=removed),
		)
		frappe.db.delete("Sales Order Item", {"name": removed})
		# Shopify keeps the line at quantity 0.
		refreshed = [self._line(LINE_ID, kept, 1), self._line(LINE_ID_B, removed_row, 0)]

		result, order = self._rebaseline(refreshed)

		self._assert_rebaselined(result, order, [(LINE_ID, 1, True), (LINE_ID_B, 0, False)])

	def test_rebaseline_accepts_an_added_line_that_has_a_new_shopify_line_id(self):
		kept = frappe.get_doc("Sales Order", self.fixture.name).items[0]
		self._add_row(kept.item_code, qty=1, rate=20)
		added = frappe.get_doc("Sales Order", self.fixture.name).items[1]
		# The baseline knows only the first line; Shopify answers with a new line id for the add.
		refreshed = [self._line(LINE_ID, kept, 1), self._line("gid://shopify/LineItem/99", added, 1)]

		result, order = self._rebaseline(refreshed)

		self._assert_rebaselined(result, order, [(LINE_ID, 1, True), ("gid://shopify/LineItem/99", 1, True)])

	def test_rebaseline_marks_modified_after_sync_when_erpnext_and_shopify_disagree(self):
		row = frappe.get_doc("Sales Order", self.fixture.name).items[0]
		# ERPNext still holds quantity 1; Shopify now shows 2.
		result, order = self._rebaseline([self._line(LINE_ID, row, 2)])

		self.assertEqual(result, {"ok": False})
		self.assertEqual(order.erpnext_sync_status, "Needs Action")
		self.assertIsNone(order.outbound_edit_fingerprint)

	def test_rebaseline_marks_modified_after_sync_when_the_rows_cannot_be_paired(self):
		row = frappe.get_doc("Sales Order", self.fixture.name).items[0]
		refreshed = [self._line(LINE_ID, row, 1), self._line(LINE_ID_B, row, 1)]  # one row too few in ERPNext

		result, order = self._rebaseline(refreshed)

		self.assertEqual(result, {"ok": False})
		self.assertEqual(order.erpnext_sync_status, "Needs Action")
		self.assertIn("do not match", order.last_sync_error)
		self.assertIsNone(order.outbound_edit_fingerprint)

	def test_the_marker_state_decides_the_planner_evidence(self):
		for state, key in (
			("completed", "outbound_edit_requested"),
			("in_flight", "outbound_edit_in_flight"),
			(None, "outbound_edit_in_flight"),
		):
			with self.subTest(state=state):
				self._set_order(outbound_edit_fingerprint="f" * 64, outbound_edit_state=state)
				evidence = sales_order_sync._reconciliation_evidence(
					frappe.get_doc("Marketplace Order", self.order.name)
				)
				self.assertTrue(evidence[key])
				self.assertEqual(sum(k.startswith("outbound_edit") for k in evidence), 1)

	def test_a_stale_save_cannot_wipe_the_marker(self):
		self._set_order(outbound_edit_fingerprint="f" * 64, outbound_edit_state="completed")
		stale = frappe.get_doc("Marketplace Order", self.order.name)
		stale.outbound_edit_fingerprint = None
		stale.flags.skip_sync_status_update = True
		stale.save()
		self.assertEqual(self._marker().outbound_edit_fingerprint, "f" * 64)

	def _status_with(self, marker_state, refreshed_qty):
		"""Judge the order the way a webhook import or form load does, against a Shopify snapshot."""
		row = frappe.get_doc("Sales Order", self.fixture.name).items[0]
		self._baseline_of(self._line(LINE_ID, row, 1, sales_order_item=row.name))
		self._set_row(qty=2, amount=20)  # ERPNext holds the staff edit
		frappe.db.set_value(
			"Marketplace Order",
			self.order.name,
			{"erpnext_sales_order": self.fixture.name, "last_synced_hash": "h"},
		)
		self._set_order(outbound_edit_fingerprint="f" * 64, outbound_edit_state=marker_state)
		order = frappe.get_doc("Marketplace Order", self.order.name)
		with patch.object(
			sales_order_sync,
			"build_sales_order_snapshot",
			return_value={"items": [self._line(LINE_ID, row, refreshed_qty)]},
		):
			sales_order_sync.set_sync_status_from_current_snapshot(order)
		return order

	def test_a_completed_marker_keeps_the_erp_quantities_and_the_marker_until_shopify_catches_up(self):
		order = self._status_with("completed", 1)
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(order.outbound_edit_fingerprint, "f" * 64)

	def test_an_in_flight_marker_is_never_synced_and_says_the_outcome_is_unknown(self):
		order = self._status_with("in_flight", 1)
		self.assertEqual(order.erpnext_sync_status, "Needs Action")
		self.assertIn("outcome is unknown", order.last_sync_error)
		self.assertEqual(order.outbound_edit_fingerprint, "f" * 64)

	def test_the_marker_is_released_once_shopify_shows_the_new_quantities(self):
		order = self._status_with("completed", 2)
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertIsNone(order.outbound_edit_fingerprint)

	# -- API permissions ----------------------------------------------------

	def _call_as(self, email, method, *args):
		frappe.set_user(email)
		try:
			return getattr(outbound_api, method)(*args)
		finally:
			frappe.set_user("Administrator")

	def _user(self, label, perms, platform_role=True):
		email, _role = make_role_user(self, label, perms)
		if platform_role:
			# Marketplace Order rows are filtered by the account's platform (Shopify here).
			frappe.get_doc("User", email).add_roles("Shopify User")
		return email

	def test_the_old_send_endpoints_are_gone_edit_order_replaced_them(self):
		# #500: the old "edit rows, then send" flow is closed by the row guard, so its endpoints were removed.
		self.assertFalse(hasattr(outbound_api, "get_shopify_order_edit_preview"))
		self.assertFalse(hasattr(outbound_api, "send_shopify_order_edit"))

	def test_clearing_the_edit_marker_is_system_manager_only_and_logged(self):
		self._set_order(outbound_edit_fingerprint="f" * 64, outbound_edit_state="in_flight")
		writer = self._user("Edit clear writer", {"Sales Order": ["read", "write"]})
		with self.assertRaises(frappe.PermissionError):
			self._call_as(writer, "clear_shopify_edit_marker", self.order.name)
		self.assertEqual(self._marker().outbound_edit_fingerprint, "f" * 64)

		manager = self._user("Edit clear manager", {"Sales Order": ["read"]}, False)
		frappe.get_doc("User", manager).add_roles("System Manager")
		self.assertEqual(self._call_as(manager, "clear_shopify_edit_marker", self.order.name), {"ok": True})

		marker = self._marker()
		self.assertEqual((marker.outbound_edit_fingerprint, marker.outbound_edit_state), (None, None))
		comments = frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": "Marketplace Order",
				"reference_name": self.order.name,
				"comment_type": "Info",
			},
			pluck="content",
		)
		self.assertTrue(any(manager in content and "in_flight" in content for content in comments), comments)
		with self.assertRaises(frappe.ValidationError):
			self._call_as(manager, "clear_shopify_edit_marker", self.order.name)  # nothing left to clear

	def test_the_api_methods_are_whitelisted_for_post(self):
		for method in (
			outbound_api.get_order_edit_preview,
			outbound_api.edit_order,
			outbound_api.clear_shopify_edit_marker,
		):
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["POST"])
