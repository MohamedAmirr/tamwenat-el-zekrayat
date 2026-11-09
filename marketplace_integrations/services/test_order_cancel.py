# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The pre-flight of the cancel chain (epic #502, #501): everything that would stop a later step is refused BEFORE
step 1 (the carrier terminate), so a refused chain leaves the order untouched."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.services import order_cancel


class IntegrationTestOrderCancelPreflight(IntegrationTestCase):
	def setUp(self):
		self.order = SimpleNamespace(name="SO-1")
		self.option = {"state": "eligible", "already_requested": False, "platform_cancelled": False, "reasons": []}

	def _preflight(self, invoices=(), payments=(), drafts=(), permitted=True, picked_up=None):
		def get_all(doctype, **kwargs):
			return {"Sales Invoice Item": list(invoices), "Payment Entry Reference": list(payments)}.get(doctype, [])

		def has_permission(doctype, ptype, doc=None, throw=False):
			if not permitted or (permitted != True and (doctype, ptype) == permitted):  # noqa: E712
				if throw:
					raise frappe.PermissionError
				return False
			return True

		notes = [frappe._dict(name=name) for name in drafts]
		with (
			patch.object(order_cancel.order_edit, "picked_up_note", return_value=picked_up),
			patch.object(frappe, "get_all", side_effect=get_all),
			patch.object(frappe, "has_permission", side_effect=has_permission) as permission,
			patch.object(order_cancel, "_draft_notes", return_value=notes),
			patch.object(order_cancel, "_check_draft_note_deletable"),
			patch.object(order_cancel.outbound, "get_platform_cancel_option", return_value=self.option),
		):
			order_cancel._preflight(self.order)
		return permission

	def test_a_clean_order_passes_and_every_step_permission_is_checked(self):
		permission = self._preflight(drafts=["DN-1", "DN-2"])

		checked = {(call.args[0], call.args[1]) for call in permission.call_args_list}
		self.assertEqual(
			checked,
			{("Sales Order", "cancel"), ("Delivery Note", "write"), ("Delivery Note", "delete")},
		)

	def test_a_submitted_sales_invoice_is_refused_before_anything_is_cancelled(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._preflight(invoices=["SINV-1"])
		self.assertIn("SINV-1", str(caught.exception))
		self.assertIn("Nothing was cancelled", str(caught.exception))

	def test_a_linked_payment_entry_is_refused_before_anything_is_cancelled(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._preflight(payments=["PE-1"])
		self.assertIn("PE-1", str(caught.exception))
		self.assertIn("Nothing was cancelled", str(caught.exception))

	def test_missing_cancel_delete_or_write_permission_is_refused_up_front(self):
		for missing in (("Sales Order", "cancel"), ("Delivery Note", "write"), ("Delivery Note", "delete")):
			with self.subTest(missing=missing), self.assertRaises(frappe.PermissionError):
				self._preflight(drafts=["DN-1"], permitted=missing)

	def test_a_submitted_delivery_note_still_sends_the_order_to_the_return_flow(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._preflight(picked_up="DN-9")
		self.assertIn("DN-9", str(caught.exception))

	def test_a_refused_preflight_runs_no_step(self):
		steps = [MagicMock(), MagicMock(), MagicMock(), MagicMock()]
		sales_order = MagicMock(docstatus=1)
		sales_order.name = "SO-1"
		sales_order.get.return_value = "MKT-1"
		with (
			patch.object(frappe, "get_doc", return_value=sales_order),
			patch.object(order_cancel.order_edit, "is_shopify_order", return_value=True),
			patch.object(order_cancel, "_preflight", side_effect=frappe.ValidationError("no")),
			patch.object(order_cancel, "_cancel_shipment", steps[0]),
			patch.object(order_cancel, "_delete_draft_note", steps[1]),
			patch.object(order_cancel, "_cancel_sales_order", steps[2]),
			patch.object(order_cancel, "_cancel_on_platform", steps[3]),
			self.assertRaises(frappe.ValidationError),
		):
			order_cancel.cancel_order("SO-1")
		for step in steps:
			step.assert_not_called()


class IntegrationTestOrderCancelDraftNoteDeletable(IntegrationTestCase):
	"""Step 2 would refuse these notes, so the pre-flight refuses them before the carrier is terminated."""

	def _check(self, against=("SO-1",), links=()):
		note = SimpleNamespace(name="DN-1", items=[SimpleNamespace(against_sales_order=name) for name in against])
		with (
			patch.object(order_cancel, "_draft_notes", return_value=[frappe._dict(name="DN-1")]),
			patch.object(frappe, "get_doc", return_value=note),
			patch("frappe.model.delete_doc.get_linked_docs", return_value=list(links)),
		):
			order_cancel._check_draft_note_deletable(SimpleNamespace(name="SO-1"))

	def test_a_note_of_this_order_alone_and_unlinked_passes(self):
		self._check()

	def test_a_note_holding_rows_of_another_order_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._check(against=("SO-1", "SO-2"))
		self.assertIn("DN-1", str(caught.exception))
		self.assertIn("Nothing was cancelled", str(caught.exception))

	def test_a_note_with_a_linked_draft_document_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._check(links=[{"reference_doctype": "Shipment", "reference_docname": "SHIP-1"}])
		self.assertIn("SHIP-1", str(caught.exception))
		self.assertIn("Nothing was cancelled", str(caught.exception))
