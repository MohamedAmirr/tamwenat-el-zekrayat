# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Edit Order and the Shopify-originated edit rules (epic #502, #500/#501): the COD re-check after the COD was sent,
the server guard on the item rows, the draft Delivery Note rule and the holds. Mocked at the service boundary: the
rules are decisions about documents, so the documents are stand-ins."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.bosta import delivery_note_shipment as bosta
from marketplace_integrations.integrations.bosta import editing
from marketplace_integrations.services import order_edit, order_reservation


def make_note(cod_amount, snapshot_cod, status="Created"):
	return SimpleNamespace(
		name="DN-1",
		custom_integration_status=status,
		custom_cod_amount=cod_amount,
		custom_carrier_request_snapshot=json.dumps({"cod": snapshot_cod}),
		custom_shipping_label=None,
	)


class IntegrationTestOrderEditStaleCod(IntegrationTestCase):
	"""Bosta holds the new COD but ERPNext's ``custom_cod_amount`` is still the old one: a Retry must not push it back."""

	def test_retry_is_refused_while_the_order_has_an_unsettled_edit_marker(self):
		values = frappe._dict(custom_cod_amount=100, custom_carrier_request_snapshot=json.dumps({"cod": 200}))
		with (
			patch.object(frappe.db, "get_value", return_value=values),
			patch.object(editing, "_order_with_open_edit", return_value="MKT-1"),
			patch.object(editing, "update_cod") as update,
			self.assertRaises(frappe.ValidationError) as caught,
		):
			editing.sync_cod("DN-1")
		update.assert_not_called()
		self.assertIn("MKT-1", str(caught.exception))

	def test_retry_without_a_marker_sends_the_cod_as_before(self):
		values = frappe._dict(custom_cod_amount=100, custom_carrier_request_snapshot=json.dumps({"cod": 200}))
		with (
			patch.object(frappe.db, "get_value", return_value=values),
			patch.object(editing, "_order_with_open_edit", return_value=None),
			patch.object(editing, "update_cod", return_value=frappe._dict(outcome="updated", message="")) as update,
		):
			self.assertEqual(editing.sync_cod("DN-1"), {"status": "updated"})
		update.assert_called_once_with("DN-1", 100)

	def test_a_failed_local_apply_after_shopify_took_the_edit_keeps_the_cod_bosta_has(self):
		plan = frappe._dict(note="DN-1", old=100.0, new=200.0)
		with (
			patch.object(order_edit.outbound, "_as_sales_order", return_value=SimpleNamespace(name="SO-1", get=lambda key: "MKT-1")),
			patch.object(order_edit, "is_shopify_order", return_value=True),
			patch.object(order_edit, "_parse_items", return_value=frappe._dict(qty={}, add=[])),
			patch.object(frappe, "get_doc"),
			patch.object(
				order_edit,
				"_preview",
				return_value={"state": order_edit.outbound.ELIGIBLE, "reasons": [], "fingerprint": "fp", "cod": plan},
			),
			patch.object(order_edit, "_dry_run"),
			patch.object(order_edit, "_update_bosta_cod"),
			patch.object(order_edit.outbound, "send_shopify_order_edit", return_value={"ok": True, "applied": False}),
			patch.object(frappe.db, "set_value") as set_value,
			patch.object(frappe.db, "commit"),
		):
			result = order_edit.edit_order("SO-1", [], "fp")
		self.assertFalse(result["applied"])
		set_value.assert_called_once_with("Delivery Note", "DN-1", "custom_cod_amount", 200.0, update_modified=False)


class IntegrationTestOrderEditCodRecheck(IntegrationTestCase):
	"""``edit_order`` sends the new COD to Bosta first; the re-check under the Marketplace Order lock must not read
	that update (snapshot at the new COD, ``custom_cod_amount`` at the old one) as "COD differs"."""

	def _plan(self, note, sent=None):
		reasons = []
		# ``flt(x, 2)`` reads the System Settings rounding method; with ``frappe.db.get_value`` patched below a cold
		# cache would read the stand-in as the setting and round everything to 0.
		frappe.get_system_settings("rounding_method")
		with (
			patch.object(order_edit, "_bosta_note", return_value=note),
			patch.object(editing, "cod_edit_blocker", return_value=None),
			patch.object(order_edit, "_whole_order_note", return_value=True),
			patch.object(
				order_edit,
				"_proposed",
				return_value=SimpleNamespace(
					calculate_taxes_and_totals=lambda: None, grand_total=200, rounded_total=200
				),
			),
			patch.object(frappe.db, "get_value", return_value=frappe._dict(advance_paid=0)),
			patch.object(bosta, "cod_from_totals", return_value=200),
			patch.object(bosta, "_check_cod"),
		):
			plan = order_edit.cod_plan(SimpleNamespace(name="SO-1"), frappe._dict(), reasons, sent=sent)
		return plan, reasons

	def test_a_snapshot_the_edit_itself_moved_to_the_new_cod_blocks_nothing(self):
		note = make_note(cod_amount=100, snapshot_cod=200)
		sent = frappe._dict(note="DN-1", old=100, new=200)

		plan, reasons = self._plan(note, sent=sent)

		self.assertEqual(reasons, [])
		self.assertEqual((plan.old, plan.new), (100, 200))

	def test_the_same_difference_without_the_sent_plan_still_blocks(self):
		note = make_note(cod_amount=100, snapshot_cod=200)

		plan, reasons = self._plan(note)

		self.assertIsNone(plan)
		self.assertEqual(len(reasons), 1)
		self.assertIn("differs from the COD Bosta has", reasons[0])

	def test_a_snapshot_that_is_not_the_sent_cod_still_blocks(self):
		note = make_note(cod_amount=100, snapshot_cod=150)

		plan, reasons = self._plan(note, sent=frappe._dict(note="DN-1", old=100, new=200))

		self.assertIsNone(plan)
		self.assertIn("differs from the COD Bosta has", reasons[0])

	def test_edit_order_hands_the_sent_plan_to_the_recheck_under_the_lock(self):
		plan = frappe._dict(note="DN-1", old=100, new=200, label=False)
		preview = {"state": "eligible", "fingerprint": "f", "reasons": [], "cod": plan}
		previews = []

		def fake_preview(sales_order, edit, order, listing=False, sent=None):
			previews.append(sent)
			return preview

		def fake_send(sales_order, fingerprint, preview_fn=None, apply_fn=None):
			again = preview_fn(MagicMock(), MagicMock())  # what send_shopify_order_edit does under the lock
			assert again["state"] == "eligible"
			return {"ok": True}

		with (
			patch.object(order_edit.outbound, "_as_sales_order", return_value=SimpleNamespace(name="SO-1", get=lambda f: "MKT-1")),
			patch.object(order_edit, "is_shopify_order", return_value=True),
			patch.object(order_edit, "_parse_items", return_value=frappe._dict(qty={}, add=[])),
			patch.object(frappe, "get_doc", return_value=MagicMock()),
			patch.object(order_edit, "_preview", side_effect=fake_preview),
			patch.object(order_edit, "_dry_run"),
			patch.object(order_edit, "_update_bosta_cod") as update,
			patch.object(order_edit.outbound, "send_shopify_order_edit", side_effect=fake_send),
		):
			result = order_edit.edit_order("SO-1", [], "f")

		self.assertEqual(result, {"ok": True})
		update.assert_called_once_with(plan)
		# the first preview has no sent plan, the one under the lock gets the plan that was sent
		self.assertEqual(previews, [None, plan])


class IntegrationTestOrderEditRowGuard(IntegrationTestCase):
	"""``update_child_qty_rate`` called directly must be refused for a Shopify order before pickup."""

	def setUp(self):
		self.row = frappe._dict(parenttype="Sales Order", parent="SO-1")
		frappe.flags.pop(order_edit._EDIT_FLAG, None)
		frappe.flags.pop("marketplace_syncing_orders", None)
		self.addCleanup(frappe.flags.pop, order_edit._EDIT_FLAG, None)
		self.addCleanup(frappe.flags.pop, "marketplace_syncing_orders", None)
		self.addCleanup(frappe.flags.pop, order_edit._ROWS_CHANGED_FLAG, None)

	def _guard(self):
		values = frappe._dict(docstatus=1, custom_marketplace_order="MKT-1")
		with (
			patch.object(frappe.db, "get_value", return_value=values),
			patch.object(order_edit, "is_shopify_order", return_value=True),
			patch.object(order_edit, "picked_up_note", return_value=None),
		):
			order_edit._guard_row(self.row)

	def test_a_row_edit_that_is_not_edit_order_or_shopify_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._guard()
		self.assertIn("changed with Edit Order", str(caught.exception))

	def test_edit_order_may_change_the_rows_of_its_own_order_only(self):
		frappe.flags[order_edit._EDIT_FLAG] = "SO-1"
		self._guard()
		self.assertIn("SO-1", frappe.flags[order_edit._ROWS_CHANGED_FLAG])

		frappe.flags[order_edit._EDIT_FLAG] = "SO-OTHER"
		with self.assertRaises(frappe.ValidationError):
			self._guard()

	def test_a_change_that_comes_from_shopify_is_not_refused(self):
		frappe.flags["marketplace_syncing_orders"] = {"MKT-1"}
		self._guard()

	def test_after_pickup_the_stage_lock_refuses_even_edit_order(self):
		frappe.flags[order_edit._EDIT_FLAG] = "SO-1"
		values = frappe._dict(docstatus=1, custom_marketplace_order="MKT-1")
		with (
			patch.object(frappe.db, "get_value", return_value=values),
			patch.object(order_edit, "is_shopify_order", return_value=True),
			patch.object(order_edit, "picked_up_note", return_value="DN-9"),
			self.assertRaises(frappe.ValidationError),
		):
			order_edit._guard_row(self.row)

	def test_a_draft_or_other_platform_order_is_not_guarded(self):
		with patch.object(frappe.db, "get_value", return_value=frappe._dict(docstatus=0, custom_marketplace_order="MKT-1")):
			order_edit._guard_row(self.row)
		with (
			patch.object(frappe.db, "get_value", return_value=frappe._dict(docstatus=1, custom_marketplace_order="MKT-1")),
			patch.object(order_edit, "is_shopify_order", return_value=False),
		):
			order_edit._guard_row(self.row)


class IntegrationTestOrderEditDraftNotes(IntegrationTestCase):
	"""One draft note that ships the whole order, or the edit is refused (and a Shopify change held)."""

	def _problem(self, drafts, whole=True):
		notes = [frappe._dict(name=name, docstatus=0, is_return=0) for name in drafts]
		with (
			patch.object(order_edit, "_notes", return_value=notes),
			patch.object(frappe, "get_doc", return_value=MagicMock()),
			patch.object(order_edit, "_whole_order_note", return_value=whole),
		):
			return order_edit.draft_note_problem("SO-1")

	def test_no_draft_and_one_whole_order_draft_are_fine(self):
		self.assertIsNone(self._problem([]))
		self.assertIsNone(self._problem(["DN-1"]))

	def test_more_than_one_draft_is_refused_naming_the_notes(self):
		message = self._problem(["DN-1", "DN-2"])
		self.assertIn("DN-1", message)
		self.assertIn("DN-2", message)
		self.assertIn("more than one draft Delivery Note", message)

	def test_a_draft_that_does_not_ship_the_whole_order_is_refused_naming_the_note(self):
		message = self._problem(["DN-1"], whole=False)
		self.assertIn("DN-1", message)
		self.assertIn("does not ship the whole order", message)

	def test_an_order_row_split_over_two_note_rows_is_refused(self):
		note = SimpleNamespace(items=[SimpleNamespace(so_detail="R1"), SimpleNamespace(so_detail="R1")])
		with (
			patch.object(order_edit, "_notes", return_value=[frappe._dict(name="DN-1", docstatus=0, is_return=0)]),
			patch.object(frappe, "get_doc", return_value=note),
			patch.object(order_edit, "_whole_order_note", return_value=True),
		):
			message = order_edit.draft_note_problem("SO-1")
		self.assertIn("DN-1", message)
		self.assertIn("more than one row", message)

	def test_a_submitted_or_return_note_is_not_a_draft(self):
		notes = [
			frappe._dict(name="DN-1", docstatus=1, is_return=0),
			frappe._dict(name="DN-2", docstatus=0, is_return=1),
		]
		with patch.object(order_edit, "_notes", return_value=notes):
			self.assertIsNone(order_edit.draft_note_problem("SO-1"))

	def test_whole_order_note_needs_every_undelivered_unit_and_nothing_of_another_order(self):
		order = SimpleNamespace(
			name="SO-1", items=[SimpleNamespace(name="R1", qty=3, delivered_qty=1, delivered_by_supplier=0)]
		)

		def note(qty, against="SO-1"):
			return SimpleNamespace(items=[SimpleNamespace(so_detail="R1", qty=qty, against_sales_order=against)])

		self.assertTrue(order_edit._whole_order_note(note(2), order))
		self.assertFalse(order_edit._whole_order_note(note(1), order))
		self.assertFalse(order_edit._whole_order_note(note(2, against="SO-2"), order))

	def test_the_preview_blocks_with_the_note_problem_and_asks_bosta_nothing(self):
		with (
			patch.object(order_edit, "lock_reason", return_value=None),
			patch.object(order_edit, "draft_note_problem", return_value="Delivery Note DN-1 does not ship the whole order"),
			patch.object(order_edit, "_resolve_added", return_value=[]),
			patch.object(order_edit, "_proposed", return_value=SimpleNamespace(items=[1])),
			patch.object(order_edit.outbound, "_shopify_edit_preview", return_value={"reasons": [], "changes": []}),
			patch.object(order_edit, "cod_plan") as cod_plan,
		):
			preview = order_edit._preview(SimpleNamespace(name="SO-1", items=[]), frappe._dict(qty={}, add=[]), MagicMock())

		self.assertEqual(preview["state"], order_edit.outbound.BLOCKED)
		self.assertIn("DN-1", " ".join(preview["reasons"]))
		cod_plan.assert_not_called()

	def test_a_change_from_shopify_is_held_for_a_bad_note_or_a_bosta_shipment_with_an_unknown_result(self):
		with (
			patch.object(frappe.db, "get_value", return_value="MKT-1"),
			patch.object(order_edit, "is_shopify_order", return_value=True),
			patch.object(order_edit, "lock_reason", return_value=None),
		):
			with patch.object(order_edit, "draft_note_problem", return_value="two drafts"):
				self.assertEqual(order_edit.update_hold_reason("SO-1"), "two drafts")
			with patch.object(order_edit, "draft_note_problem", return_value=None):
				for status in ("Creating", "Creation Unknown"):
					note = SimpleNamespace(name="DN-1", custom_integration_status=status)
					with patch.object(order_edit, "_bosta_note", return_value=note):
						self.assertIn("DN-1", order_edit.update_hold_reason("SO-1"))
				created = SimpleNamespace(name="DN-1", custom_integration_status="Created")
				with patch.object(order_edit, "_bosta_note", return_value=created):
					self.assertIsNone(order_edit.update_hold_reason("SO-1"))
				with patch.object(order_edit, "_bosta_note", return_value=None):
					self.assertIsNone(order_edit.update_hold_reason("SO-1"))


class IntegrationTestOrderEditHolds(IntegrationTestCase):
	def _stage(self, stage, reason_code=None):
		def get_value(doctype, name, fieldname=None, *args, **kwargs):
			if doctype == "Sales Order":
				return (stage, "MKT-1")
			return reason_code

		return patch.object(frappe.db, "get_value", side_effect=get_value)

	def test_only_a_confirmed_or_an_already_held_order_may_be_held_for_stock(self):
		from marketplace_integrations.services import sync_reason_codes

		with self._stage(order_reservation.CONFIRMED):
			self.assertTrue(order_edit._held_for_stock("SO-1"))
		with self._stage(order_reservation.WAITING_FOR_STOCK, sync_reason_codes.WAITING_FOR_STOCK_EDIT):
			self.assertTrue(order_edit._held_for_stock("SO-1"))
		with self._stage(order_reservation.WAITING_FOR_STOCK, "SOMETHING_ELSE"):
			self.assertFalse(order_edit._held_for_stock("SO-1"))
		with self._stage(order_reservation.LOCKED):
			self.assertFalse(order_edit._held_for_stock("SO-1"))

	def test_an_order_that_is_not_held_for_stock_is_never_held_by_hold_for_stock(self):
		with (
			patch.object(order_edit, "is_shopify_order", return_value=True),
			patch.object(frappe.db, "get_value", return_value="MKT-1"),
			patch.object(order_edit, "_held_for_stock", return_value=False),
			patch.object(order_reservation, "hold_for_edit") as hold,
		):
			self.assertIsNone(order_edit.hold_for_stock("SO-1", {}, {}))
		hold.assert_not_called()

	def test_a_person_s_edit_of_a_held_order_releases_nothing_on_a_shortfall(self):
		with (
			patch.object(order_reservation, "_release_reservations") as release,
			patch.object(order_reservation, "reservation_enabled", return_value=True),
			patch.object(order_reservation, "_eligible", return_value=True),
			patch.object(order_reservation, "_stock_rows", return_value=[SimpleNamespace(item_code="A", warehouse="W")]),
			patch.object(order_reservation, "_lock_bins"),
			patch.object(order_reservation, "_reserve_rows"),
			patch.object(order_reservation, "_shortfalls", return_value=[(MagicMock(), 1)]),
			patch.object(order_reservation, "_available_for", return_value=[("A", 1, 0)]),
			patch.object(order_reservation, "set_stage"),
			patch.object(order_reservation, "_open_todo"),
			patch.object(frappe.db, "get_value"),
			patch.object(frappe.db, "savepoint"),
			patch.object(frappe.db, "rollback"),
			patch.object(frappe, "get_doc", return_value=SimpleNamespace(name="SO-1")),
		):
			self.assertEqual(order_reservation.reserve_or_wait("SO-1", keep_reservation=True), order_reservation.WAITING)
			release.assert_not_called()
			self.assertEqual(order_reservation.reserve_or_wait("SO-1"), order_reservation.WAITING)
			release.assert_called_once_with("SO-1")


class IntegrationTestOrderEditInput(IntegrationTestCase):
	def _order(self):
		return SimpleNamespace(items=[SimpleNamespace(name="R1", item_code="A", idx=1)])

	def test_an_entry_that_is_not_an_object_is_a_clear_error(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			order_edit._parse_items(self._order(), ["R1"])
		self.assertIn("Each item must have", str(caught.exception))

	def test_a_client_sent_row_name_is_escaped_in_the_error(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			order_edit._parse_items(self._order(), [{"docname": "<img src=x onerror=1>", "qty": 1}])
		self.assertNotIn("<img", str(caught.exception))
		self.assertIn("&lt;img", str(caught.exception))
