# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""A Shopify shipping change applied to a SUBMITTED Sales Order (#534): the planner outcome, the executor
(``sales_order_sync._execute_shipping_change``) and ``services/order_shipping_change.py``. Bosta and Shopify are never
called: the Bosta COD update is replaced at the ``editing.update_cod`` boundary."""

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from marketplace_integrations.integrations.bosta import delivery_note_shipment as bosta
from marketplace_integrations.integrations.bosta import editing
from marketplace_integrations.services import carrier_log, order_edit
from marketplace_integrations.services import order_shipping_change as shipping_change
from marketplace_integrations.services import sync_reason_codes as reasons
from marketplace_integrations.services import test_sales_order_sync as base
from marketplace_integrations.services.sales_order_sync import get_current_sync_status, sync_sales_order

ITEMS = 36  # the order's line total
_Base = base.IntegrationTestSalesOrderSync


class IntegrationTestShippingChange(IntegrationTestCase):
	# The Shopify order/account builders of the Sales Order sync tests (same fixtures, no inherited tests).
	setUp = _Base.setUp
	_make_item = _Base._make_item
	_make_account = _Base._make_account
	_make_mapping = _Base._make_mapping
	_make_shopify_account = _Base._make_shopify_account
	_make_shopify_order = _Base._make_shopify_order
	_income_account = _Base._income_account
	_sync_shopify = _Base._sync_shopify
	_deliver = _Base._deliver

	# --- helpers ------------------------------------------------------------------------

	def _synced(self, shipping=50, **kwargs):
		account, order, result = self._sync_shopify(
			transaction_amount=ITEMS, shipping_total=shipping, order_total=ITEMS + shipping, **kwargs
		)
		self.assertTrue(result["ok"], result.get("error"))
		return account, order, result["sales_order"]

	def _shopify_changes_shipping(self, order, new):
		order.shipping_total = new
		order.order_total = ITEMS + new
		order.save()

	def _order_state(self, sales_order):
		return frappe.db.get_value(
			"Sales Order",
			sales_order,
			["grand_total", "rounded_total", "modified", "custom_needs_attention", "custom_attention_reason"],
			as_dict=True,
		)

	def _shipping_rows(self, sales_order, doctype="Sales Order"):
		return [
			row
			for row in frappe.get_doc(doctype, sales_order).taxes
			if row.charge_type == "Actual" and row.description == "Shipping"
		]

	def _baseline_shipping(self, order):
		order.reload()
		return frappe.parse_json(order.last_synced_snapshot).get("shipping")

	def _log_actions(self, order):
		return [
			frappe.parse_json(log.request_data).get("action")
			for log in frappe.get_all(
				"Marketplace Sync Log",
				filters={"marketplace_order": order.name},
				fields=["request_data"],
				order_by="creation asc",
			)
		]

	def _assert_untouched(self, sales_order, before):
		after = self._order_state(sales_order)
		self.assertEqual((after.grand_total, after.rounded_total, after.modified), (
			before.grand_total, before.rounded_total, before.modified))

	def _bosta_note(self, sales_order, status="Created"):
		note = self._deliver(sales_order, submit=False)
		cod = flt(frappe.db.get_value("Sales Order", sales_order, "grand_total"), 2)
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{
				"custom_integration_status": status,
				"custom_cod_amount": cod,
				"custom_carrier_request_snapshot": json.dumps({"cod": cod}),
			},
			update_modified=False,
		)
		return note.name

	@contextmanager
	def _bosta(self, sales_order, outcomes=("updated",)):
		"""Make the order's draft notes Bosta notes and replace the Bosta COD update. ``calls`` records ``(note, cod,
		the Sales Order's grand total at the moment of the call)``."""
		calls = []
		queue = list(outcomes)

		def update_cod(note, cod):
			calls.append((note, flt(cod, 2), frappe.db.get_value("Sales Order", sales_order, "grand_total")))
			outcome = queue.pop(0) if queue else "updated"
			return frappe._dict(outcome=outcome, message="Bosta says no" if outcome == "failed" else "")

		with (
			patch.object(bosta, "applies_to", return_value=True),
			patch.object(editing, "cod_edit_blocker", return_value=None),
			patch.object(editing, "update_cod", side_effect=update_cod),
		):
			yield calls

	# --- applied -----------------------------------------------------------------------

	def test_regular_to_fast_shipping_changes_the_order_the_note_and_the_baseline(self):
		_account, order, sales_order = self._synced(shipping=50)
		note = self._deliver(sales_order, submit=False)
		self.assertEqual([row.tax_amount for row in self._shipping_rows(note.name, "Delivery Note")], [50])
		self._shopify_changes_shipping(order, 80)

		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["reconciliation"]["outcome"], "UPDATE_SHIPPING")
		self.assertEqual(result["executed_action"], "UPDATE_SHIPPING")
		self.assertEqual(order.erpnext_sync_status, "Synced")
		doc = frappe.get_doc("Sales Order", sales_order)
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual([row.tax_amount for row in self._shipping_rows(sales_order)], [80])
		self.assertEqual((doc.grand_total, doc.rounded_total), (116, 116))
		self.assertEqual(doc.total_taxes_and_charges, 80)
		self.assertEqual([row.tax_amount for row in self._shipping_rows(note.name, "Delivery Note")], [80])
		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "grand_total"), 116)
		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "docstatus"), 0)
		self.assertEqual(doc.custom_needs_attention, 0)
		self.assertEqual(self._baseline_shipping(order), 80)
		self.assertEqual(self._log_actions(order)[-1], "UPDATE_SHIPPING")
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Sales Order", "reference_name": sales_order, "comment_type": "Comment"},
			pluck="content",
		)
		self.assertTrue(any("Shipping changed from" in text for text in comments), comments)

	def test_the_baseline_moved_so_a_second_run_plans_no_shipping_change(self):
		_account, order, sales_order = self._synced(shipping=50)
		self._shopify_changes_shipping(order, 80)
		sync_sales_order(order.name)
		before = self._order_state(sales_order)

		again = sync_sales_order(order.name)

		self.assertTrue(again["ok"], again.get("error"))
		self.assertEqual(again["reconciliation"]["outcome"], "NO_ACTION")
		self._assert_untouched(sales_order, before)
		self.assertEqual(len(self._shipping_rows(sales_order)), 1)
		self.assertEqual(get_current_sync_status(order.name), "Synced")

	def test_the_stock_reservation_entries_are_not_touched(self):
		original = frappe.db.get_single_value("Stock Settings", "enable_stock_reservation")
		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", 1)
		self.addCleanup(frappe.db.set_single_value, "Stock Settings", "enable_stock_reservation", original)
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"TEST-SHIP-STOCK-{self.suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}
		).insert()
		receipt = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [{"item_code": item.name, "qty": 20, "t_warehouse": self.warehouse, "basic_rate": 5}],
			}
		).insert()
		receipt.submit()
		self.item = item
		_account, order, sales_order = self._synced(shipping=50)
		reservations = lambda: sorted(  # noqa: E731
			(row.name, row.reserved_qty, row.docstatus)
			for row in frappe.get_all(
				"Stock Reservation Entry",
				filters={"voucher_no": sales_order},
				fields=["name", "reserved_qty", "docstatus"],
			)
		)
		before = reservations()
		self.assertTrue(before, "the sync should have reserved the stock")
		self._shopify_changes_shipping(order, 80)

		result = sync_sales_order(order.name)

		self.assertEqual(result["reconciliation"]["outcome"], "UPDATE_SHIPPING")
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "grand_total"), 116)
		self.assertEqual(reservations(), before)

	def test_an_order_without_shipping_gets_a_shipping_row(self):
		account, order, sales_order = self._synced(shipping=0)
		self.assertEqual(self._shipping_rows(sales_order), [])
		self._shopify_changes_shipping(order, 50)

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		rows = self._shipping_rows(sales_order)
		self.assertEqual(len(rows), 1)
		self.assertEqual(
			(rows[0].tax_amount, rows[0].account_head, rows[0].description, rows[0].charge_type),
			(50, account.erpnext_shipping_account, "Shipping", "Actual"),
		)
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "grand_total"), 86)

	def test_a_store_pickup_removes_the_shipping_row(self):
		_account, order, sales_order = self._synced(shipping=50)
		self._shopify_changes_shipping(order, 0)

		result = sync_sales_order(order.name)
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(self._shipping_rows(sales_order), [])
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "grand_total"), ITEMS)
		self.assertNotIn("shipping", frappe.parse_json(order.last_synced_snapshot))
		self.assertEqual(sync_sales_order(order.name)["reconciliation"]["outcome"], "NO_ACTION")

	def test_a_total_below_what_the_customer_paid_is_applied_and_flagged_refund_owed(self):
		_account, order, sales_order = self._synced(shipping=80)
		frappe.db.set_value("Sales Order", sales_order, "advance_paid", 116, update_modified=False)
		self._shopify_changes_shipping(order, 50)

		result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		state = self._order_state(sales_order)
		self.assertEqual(state.grand_total, 86)
		self.assertEqual(state.custom_needs_attention, 1)
		self.assertIn("Refund owed", state.custom_attention_reason)
		self.assertIn("30.00", state.custom_attention_reason)

	# --- not applied: an invoice or a pickup ---------------------------------------------

	def _assert_blocked_and_flagged(self, order, sales_order, before, result):
		order.reload()
		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(result["executed_action"], "NEEDS_ATTENTION")
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self._assert_untouched(sales_order, before)
		self.assertEqual([row.tax_amount for row in self._shipping_rows(sales_order)], [50])
		state = self._order_state(sales_order)
		self.assertEqual(state.custom_needs_attention, 1)
		self.assertIn("Credit Note / Debit Note", state.custom_attention_reason)
		self.assertIn("was not changed", state.custom_attention_reason)
		# the baseline moved: the change is reported once, by the flag
		self.assertEqual(self._baseline_shipping(order), 80)
		self.assertEqual(sync_sales_order(order.name)["reconciliation"]["outcome"], "NO_ACTION")

	def test_a_submitted_sales_invoice_leaves_the_order_unchanged_and_flags_it(self):
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		_account, order, sales_order = self._synced(shipping=50)
		invoice = make_sales_invoice(sales_order)
		invoice.insert()
		invoice.submit()
		before = self._order_state(sales_order)
		self._shopify_changes_shipping(order, 80)

		result = sync_sales_order(order.name)

		self.assertIn(invoice.name, self._order_state(sales_order).custom_attention_reason)
		self._assert_blocked_and_flagged(order, sales_order, before, result)

	def test_a_picked_up_shipment_leaves_the_order_unchanged_and_flags_it(self):
		_account, order, sales_order = self._synced(shipping=50)
		note = self._deliver(sales_order, submit=True)
		before = self._order_state(sales_order)
		self._shopify_changes_shipping(order, 80)

		result = sync_sales_order(order.name)

		self.assertIn(note.name, self._order_state(sales_order).custom_attention_reason)
		self._assert_blocked_and_flagged(order, sales_order, before, result)

	# --- Bosta ---------------------------------------------------------------------------

	def test_a_created_bosta_note_gets_the_new_cod_before_the_order_changes(self):
		_account, order, sales_order = self._synced(shipping=50)
		note = self._bosta_note(sales_order)
		self._shopify_changes_shipping(order, 80)

		with self._bosta(sales_order) as calls:
			result = sync_sales_order(order.name)

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(calls, [(note, 116.0, 86.0)])  # called once, while the order still had its old total
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "grand_total"), 116)
		self.assertEqual(frappe.db.get_value("Delivery Note", note, "custom_cod_amount"), 116)
		self.assertEqual([row.tax_amount for row in self._shipping_rows(note, "Delivery Note")], [80])

	def test_a_bosta_failure_changes_nothing_and_flags_the_order(self):
		_account, order, sales_order = self._synced(shipping=50)
		self._bosta_note(sales_order)
		before = self._order_state(sales_order)
		baseline = order.reload().last_synced_hash
		self._shopify_changes_shipping(order, 80)

		with self._bosta(sales_order, ("failed",)) as calls:
			result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(len(calls), 1)
		self.assertEqual(order.sync_reason_code, reasons.SHIPPING_CHANGE_FAILED)
		self.assertEqual(order.erpnext_sync_status, "Needs Action")
		self.assertEqual(order.last_synced_hash, baseline)
		self._assert_untouched(sales_order, before)
		state = self._order_state(sales_order)
		self.assertEqual(state.custom_needs_attention, 1)
		self.assertIn("Bosta did not accept the new COD", state.custom_attention_reason)

	def test_an_erpnext_refusal_after_the_cod_was_sent_puts_the_old_cod_back(self):
		_account, order, sales_order = self._synced(shipping=50)
		note = self._bosta_note(sales_order)
		before = self._order_state(sales_order)
		self._shopify_changes_shipping(order, 80)

		with (
			self._bosta(sales_order) as calls,
			patch.object(order_edit, "refresh_draft_note", side_effect=frappe.ValidationError("ERPNext said no")),
		):
			result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual([call[:2] for call in calls], [(note, 116.0), (note, 86.0)])
		self.assertEqual(order.sync_reason_code, reasons.SHIPPING_CHANGE_FAILED)
		self._assert_untouched(sales_order, before)
		self.assertEqual([row.tax_amount for row in self._shipping_rows(sales_order)], [50])
		self.assertEqual(self._order_state(sales_order).custom_needs_attention, 1)

	def test_a_bosta_shipment_that_is_creating_or_unknown_holds_the_change(self):
		for status in (carrier_log.CREATING, carrier_log.UNKNOWN):
			with self.subTest(status=status):
				self.setUp()
				_account, order, sales_order = self._synced(shipping=50)
				self._bosta_note(sales_order, status)
				before = self._order_state(sales_order)
				self._shopify_changes_shipping(order, 80)

				with self._bosta(sales_order) as calls:
					result = sync_sales_order(order.name)
				order.reload()

				self.assertFalse(result["ok"])
				self.assertEqual(calls, [])
				self.assertEqual(order.sync_reason_code, reasons.ORDER_LOCKED_BY_SHIPMENT)
				self.assertEqual(order.erpnext_sync_status, "Needs Action")
				self._assert_untouched(sales_order, before)
				self.assertEqual(self._order_state(sales_order).custom_needs_attention, 1)
				# the baseline is kept: a later Retry Sync applies the change
				self.assertEqual(self._baseline_shipping(order), 50)

	def test_two_draft_delivery_notes_hold_the_change_without_calling_bosta(self):
		_account, order, sales_order = self._synced(shipping=50)
		self._deliver(sales_order, submit=False)
		self._deliver(sales_order, submit=False)
		before = self._order_state(sales_order)
		self._shopify_changes_shipping(order, 80)

		with self._bosta(sales_order) as calls:
			result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(calls, [])
		self.assertEqual(order.sync_reason_code, reasons.ORDER_LOCKED_BY_SHIPMENT)
		self._assert_untouched(sales_order, before)
		self.assertEqual(self._order_state(sales_order).custom_needs_attention, 1)

	# --- a failed change is not repeated by itself --------------------------------------

	def _failed_change(self):
		_account, order, sales_order = self._synced(shipping=50)
		self._bosta_note(sales_order)
		self._shopify_changes_shipping(order, 80)
		with self._bosta(sales_order, ("failed",)):
			sync_sales_order(order.name)
		order.reload()
		self.assertEqual(order.sync_reason_code, reasons.SHIPPING_CHANGE_FAILED)
		return order, sales_order

	def test_a_failed_shipping_change_is_not_queued_by_the_form_load(self):
		order, _sales_order = self._failed_change()

		status = get_current_sync_status(order.name)
		order.reload()

		self.assertEqual(status, "Needs Action")
		self.assertEqual(order.sync_reason_code, reasons.SHIPPING_CHANGE_FAILED)

	def test_a_failed_shipping_change_is_not_rerun_by_an_automatic_sync_only_by_manual_resync(self):
		order, sales_order = self._failed_change()

		for trigger in ("Shopify Webhook", "Bin Sweep", "Recheck"):
			with self.subTest(trigger=trigger), self._bosta(sales_order) as calls:
				result = sync_sales_order(order.name, trigger_source=trigger)
				self.assertFalse(result["ok"])
				self.assertEqual(calls, [])
				self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "grand_total"), 86)

		with self._bosta(sales_order) as calls:
			result = sync_sales_order(order.name, trigger_source="Manual Resync")
		order.reload()

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(len(calls), 1)
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "grand_total"), 116)
		self.assertEqual(order.erpnext_sync_status, "Synced")

	def test_manual_trigger_also_reruns_a_failed_shipping_change(self):
		order, sales_order = self._failed_change()

		with self._bosta(sales_order):
			result = sync_sales_order(order.name, trigger_source="Manual")

		self.assertTrue(result["ok"], result.get("error"))
		self.assertEqual(frappe.db.get_value("Sales Order", sales_order, "grand_total"), 116)

	# --- permission ----------------------------------------------------------------------

	def test_a_permission_error_is_a_permission_denied_state_with_a_sync_log(self):
		_account, order, sales_order = self._synced(shipping=50)
		before = self._order_state(sales_order)
		logs = len(self._log_actions(order))
		self._shopify_changes_shipping(order, 80)

		with patch.object(shipping_change, "apply", side_effect=frappe.PermissionError("no")):
			result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(order.sync_reason_code, reasons.PERMISSION_DENIED)
		self.assertIn("permission", str(result["error"]).lower())
		self._assert_untouched(sales_order, before)
		self.assertEqual(len(self._log_actions(order)), logs + 1)
		last = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name},
			fields=["status"],
			order_by="creation desc",
			limit=1,
		)[0]
		self.assertEqual(last.status, "Invalid")


# Not a module attribute any more: the loader would collect the whole Sales Order sync suite a second time.
del _Base


class IntegrationTestShippingRowHelpers(IntegrationTestCase):
	"""``set_shipping_row`` and the reason texts, on documents that are never saved."""

	def _doc(self, rows=()):
		doc = frappe.new_doc("Sales Order")
		for amount, description in rows:
			doc.append(
				"taxes",
				{"charge_type": "Actual", "account_head": "Sales - TEZ", "description": description, "tax_amount": amount},
			)
		return doc

	def test_an_existing_row_is_updated_and_reported_changed(self):
		doc = self._doc([(50, "Shipping")])

		self.assertTrue(shipping_change.set_shipping_row(doc, 80, "Sales - TEZ"))
		self.assertEqual(shipping_change.shipping_amount(doc), 80)
		self.assertFalse(shipping_change.set_shipping_row(doc, 80, "Sales - TEZ"))

	def test_a_missing_row_is_added_with_the_sync_shape(self):
		doc = self._doc()

		self.assertTrue(shipping_change.set_shipping_row(doc, 50, "Sales - TEZ"))
		row = doc.taxes[0]
		self.assertEqual(
			(row.charge_type, row.account_head, row.description, row.tax_amount, row.included_in_print_rate),
			("Actual", "Sales - TEZ", "Shipping", 50, 0),
		)

	def test_a_missing_row_without_an_account_throws(self):
		with self.assertRaises(frappe.ValidationError):
			shipping_change.set_shipping_row(self._doc(), 50, None)

	def test_zero_removes_the_row_and_other_charges_stay(self):
		doc = self._doc([(50, "Shipping"), (7, "Packing")])

		self.assertTrue(shipping_change.set_shipping_row(doc, 0, "Sales - TEZ"))
		self.assertEqual([row.description for row in doc.taxes], ["Packing"])
		self.assertFalse(shipping_change.set_shipping_row(doc, 0, "Sales - TEZ"))

	def test_duplicate_shipping_rows_collapse_to_one(self):
		doc = self._doc([(50, "Shipping"), (20, "Shipping")])

		self.assertTrue(shipping_change.set_shipping_row(doc, 80, "Sales - TEZ"))
		self.assertEqual([row.tax_amount for row in doc.taxes], [80])

	def test_flags_after_apply(self):
		applied = {"currency": "EGP", "old": 50, "new": 80, "total": 116, "advance_paid": 0, "bosta_note": None}
		self.assertEqual(shipping_change.flags_after_apply(applied), [])

		over = shipping_change.flags_after_apply({**applied, "advance_paid": 130})
		self.assertEqual(len(over), 1)
		self.assertIn("Refund owed", over[0])
		# an advance equal to the total (within a cent) owes nothing
		self.assertEqual(shipping_change.flags_after_apply({**applied, "advance_paid": 116.004}), [])

		pickup = shipping_change.flags_after_apply({**applied, "new": 0, "old": 50, "bosta_note": "DN-1"})
		self.assertEqual(len(pickup), 1)
		self.assertIn("Bosta shipment started", pickup[0])
		self.assertEqual(shipping_change.flags_after_apply({**applied, "new": 0, "bosta_note": None}), [])
