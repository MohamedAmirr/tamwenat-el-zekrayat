# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from copy import deepcopy

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.services.sales_order_reconciliation import (
	CANCEL_ORDER,
	CLOSE_REMAINDER,
	NEEDS_ATTENTION,
	NO_ACTION,
	UNSUPPORTED_COMPENSATING_TRANSACTION,
	UPDATE_SUBMITTED_ORDER,
	plan,
	snapshot_advanced_from,
)


class UnitTestSalesOrderReconciliation(UnitTestCase):
	def setUp(self):
		# plan() returns _()-translated merchant-readable text, and many tests below
		# assert on literal English substrings of it. Pin the language so those
		# assertions hold regardless of the site's default language (#172).
		self._original_lang = frappe.local.lang
		frappe.local.lang = "en"
		self.previous = {
			"company": "Test Company",
			"customer": "Test Customer",
			"selling_price_list": "Standard Selling",
			"transaction_date": "2026-10-05",
			"delivery_date": "2026-10-06",
			"items": [
				{
					"external_item_id": "line-1",
					"sales_order_item": "SOI-1",
					"item_code": "ITEM-1",
					"qty": 2,
					"uom": "Nos",
					"warehouse": "Main Warehouse",
					"effective_rate": 50,
					"fulfilled_total_price": 100,
				}
			],
		}
		self.erp_state = {
			"docstatus": 1,
			"status": "To Deliver and Bill",
			"per_delivered": 0,
			"per_billed": 0,
			"lines": [
				{
					"sales_order_item": "SOI-1",
					"item_code": "ITEM-1",
					"uom": "Nos",
					"warehouse": "Main Warehouse",
					"qty": 2,
					"delivered_qty": 0,
					"billed_amt": 0,
					"amount": 100,
				}
			],
		}

	def tearDown(self):
		frappe.local.lang = self._original_lang

	def test_unchanged_effectively_equivalent_state_needs_no_action(self):
		result = plan(self.previous, self._current(), {}, self.erp_state)

		self.assertEqual(result["outcome"], NO_ACTION)

	def test_quantity_change_before_execution_is_an_advisory_update(self):
		current = self._current()
		current["items"][0]["qty"] = 3
		current["items"][0]["fulfilled_total_price"] = 150

		result = plan(self.previous, current, {}, self.erp_state)

		self.assertEqual(result["outcome"], UPDATE_SUBMITTED_ORDER)
		self.assertEqual(result["changes"][0]["field"], "qty")

	def test_line_reduction_below_existing_advance_needs_attention(self):
		current = self._current()
		current["items"][0]["qty"] = 1
		current["items"][0]["effective_rate"] = 40
		current["items"][0]["fulfilled_total_price"] = 40
		self.erp_state.update({"grand_total": 100, "advance_paid": 60})

		result = plan(self.previous, current, {}, self.erp_state)

		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertIn("advance of 60", result["message"])

	def test_requested_quantity_below_delivered_quantity_needs_attention(self):
		current = self._current()
		current["items"][0]["qty"] = 1
		self.erp_state["lines"][0]["delivered_qty"] = 2

		result = plan(self.previous, current, {}, self.erp_state)

		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertIn("delivered", result["message"])

	def test_billed_line_needs_attention(self):
		current = self._current()
		current["items"][0]["qty"] = 1
		self.erp_state["lines"][0]["billed_amt"] = 100

		result = plan(self.previous, current, {}, self.erp_state)

		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertIn("billed", result["message"])

	def test_structural_change_needs_attention(self):
		current = self._current()
		current["customer"] = "Other Customer"

		result = plan(self.previous, current, {}, self.erp_state)

		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertEqual(result["changes"][0]["kind"], "header_changed")

	def test_cancelled_or_drifted_erp_order_needs_attention_even_when_marketplace_is_unchanged(self):
		for field, value in (
			("status", "Cancelled"),
			("amount", 120),
			("item_code", "ITEM-2"),
		):
			with self.subTest(field=field):
				erp_state = deepcopy(self.erp_state)
				if field == "status":
					erp_state[field] = value
				else:
					erp_state["lines"][0][field] = value
				result = plan(self.previous, self._current(), {}, erp_state)
				self.assertEqual(result["outcome"], NEEDS_ATTENTION)

	def test_return_requires_compensating_transaction(self):
		result = plan(self.previous, self._current(), {"return": True}, self.erp_state)
		self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)

	def test_refund_with_delivery_or_billing_requires_compensating_transaction(self):
		for field, value in (("delivered_qty", 1), ("billed_amt", 50)):
			with self.subTest(field=field):
				erp_state = deepcopy(self.erp_state)
				erp_state["lines"][0][field] = value
				result = plan(self.previous, self._current(), {"refund": True}, erp_state)
				self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)

	def test_refund_without_delivery_or_billing_falls_through_to_cancel(self):
		evidence = {"refund": True, "cancellation": "full"}
		result = plan(self.previous, self._current(), evidence, self.erp_state)
		self.assertEqual(result["outcome"], CANCEL_ORDER)

	def test_refund_without_delivery_or_billing_uses_the_normal_outcome(self):
		result = plan(self.previous, self._current(), {"refund": True}, self.erp_state)
		self.assertEqual(result["outcome"], NO_ACTION)

	def test_cancelled_erp_order_with_full_cancellation_evidence_is_converged(self):
		for docstatus, status in ((1, "Cancelled"), (2, "Cancelled")):
			with self.subTest(docstatus=docstatus):
				erp_state = deepcopy(self.erp_state)
				erp_state.update({"docstatus": docstatus, "status": status})
				result = plan(self.previous, self._current(), {"cancellation": "full"}, erp_state)
				self.assertEqual(result["outcome"], NO_ACTION)

	def test_closed_erp_order_with_cancellation_and_nothing_left_to_deliver_is_converged(self):
		self.erp_state.update({"status": "Closed"})
		self.erp_state["lines"][0]["delivered_qty"] = 2
		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)
		self.assertEqual(result["outcome"], NO_ACTION)

	def test_closed_erp_order_with_delivery_converges_on_replay(self):
		self.erp_state.update({"status": "Closed"})
		self.erp_state["lines"][0]["delivered_qty"] = 1
		# #179: a Closed order with delivery is what CLOSE_REMAINDER leaves, so it converges.
		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)
		self.assertEqual(result["outcome"], NO_ACTION)

	def test_closed_erp_order_without_delivery_and_full_cancellation_is_cancelled_after_reopen(self):
		self.erp_state.update({"status": "Closed"})
		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)
		self.assertEqual(result["outcome"], CANCEL_ORDER)
		self.assertTrue(result["reopen"])

	def test_echo_of_an_outbound_cancel_on_a_closed_order_is_no_action_and_not_reopened(self):
		self.erp_state.update({"status": "Closed"})
		evidence = {"cancellation": "full", "outbound_cancel_requested": True}
		result = plan(self.previous, self._current(), evidence, self.erp_state)
		self.assertEqual(result["outcome"], NO_ACTION)
		self.assertFalse(result.get("reopen"))

	def test_echo_also_holds_for_the_stored_snapshot_and_a_zero_quantity_current_snapshot(self):
		self.erp_state.update({"status": "Closed"})
		evidence = {"cancellation": "full", "outbound_cancel_requested": True}
		for current in ({}, self._current()):
			with self.subTest(current=current):
				self.assertEqual(plan(self.previous, current, evidence, self.erp_state)["outcome"], NO_ACTION)

	def test_outbound_marker_without_platform_cancellation_is_not_an_echo(self):
		self.erp_state.update({"status": "Closed"})
		result = plan(self.previous, self._current(), {"outbound_cancel_requested": True}, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)

	def test_platform_cancellation_without_the_marker_still_reopens_and_cancels(self):
		self.erp_state.update({"status": "Closed"})
		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)
		self.assertEqual(result["outcome"], CANCEL_ORDER)
		self.assertTrue(result["reopen"])

	def test_echo_rule_needs_a_closed_order_with_no_delivery_or_billing(self):
		# With delivery the order already converges; the marker adds nothing and nothing is reopened.
		evidence = {"cancellation": "full", "outbound_cancel_requested": True}
		delivered = deepcopy(self.erp_state)
		delivered.update({"status": "Closed"})
		delivered["lines"][0]["delivered_qty"] = 1
		self.assertEqual(plan(self.previous, self._current(), evidence, delivered)["outcome"], NO_ACTION)
		# An open order is not an echo case: the normal cancel applies.
		self.assertEqual(
			plan(self.previous, self._current(), evidence, self.erp_state)["outcome"], CANCEL_ORDER
		)
		# A billed (undelivered) Closed order is not reopenable: attention, not NO_ACTION.
		billed = deepcopy(self.erp_state)
		billed.update({"status": "Closed"})
		billed["lines"][0]["billed_amt"] = 10
		self.assertEqual(plan(self.previous, self._current(), evidence, billed)["outcome"], NEEDS_ATTENTION)

	def test_inactive_order_advisory_appends_the_platform_instruction_when_given(self):
		erp_state = deepcopy(self.erp_state)
		erp_state.update({"docstatus": 2, "status": "Cancelled"})
		plain = plan(self.previous, self._current(), {}, erp_state)["message"]
		hinted = plan(
			self.previous,
			self._current(),
			{"inactive_order_instruction": "Cancel it on the platform."},
			erp_state,
		)
		self.assertEqual(hinted["outcome"], NEEDS_ATTENTION)
		self.assertEqual(hinted["message"], f"{plain} Cancel it on the platform.")

	def test_closed_erp_order_with_advance_payment_is_not_reopened_for_cancellation(self):
		self.erp_state.update({"status": "Closed", "advance_paid": 10})
		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertFalse(result.get("reopen"))

	def test_closed_erp_order_with_qty_or_rate_change_is_updated_after_reopen(self):
		self.erp_state.update({"status": "Closed"})
		for field, value in (("qty", 1), ("effective_rate", 60)):
			with self.subTest(field=field):
				current = self._current()
				current["items"][0][field] = value
				current["items"][0]["fulfilled_total_price"] = (
					current["items"][0]["qty"] * current["items"][0]["effective_rate"]
				)
				result = plan(self.previous, current, {}, self.erp_state)
				self.assertEqual(result["outcome"], UPDATE_SUBMITTED_ORDER)
				self.assertTrue(result["reopen"])

	def test_closed_erp_order_update_respects_the_lifecycle_gate(self):
		self.erp_state.update({"status": "Closed"})
		current = self._current()
		current["items"][0]["qty"] = 1
		result = plan(
			self.previous,
			current,
			{"lifecycle_permits_change": False, "lifecycle_reason": "Too late."},
			self.erp_state,
		)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertFalse(result.get("reopen"))

	def test_closed_erp_order_item_uom_or_warehouse_change_needs_attention(self):
		self.erp_state.update({"status": "Closed"})
		for field, value in (("item_code", "ITEM-2"), ("uom", "Box"), ("warehouse", "Other")):
			with self.subTest(field=field):
				current = self._current()
				current["items"][0][field] = value
				result = plan(self.previous, current, {}, self.erp_state)
				self.assertEqual(result["outcome"], NEEDS_ATTENTION)
				self.assertFalse(result.get("reopen"))

	def test_closed_erp_order_structural_changes_need_attention(self):
		self.erp_state.update({"status": "Closed"})
		header = self._current()
		header["customer"] = "Other Customer"
		added = self._current()
		added["items"].append({**added["items"][0], "external_item_id": "line-2"})
		for current in (header, added):
			result = plan(self.previous, current, {}, self.erp_state)
			self.assertEqual(result["outcome"], NEEDS_ATTENTION)
			self.assertFalse(result.get("reopen"))

	def test_closed_erp_order_equivalent_to_the_marketplace_needs_attention_without_reopen(self):
		self.erp_state.update({"status": "Closed"})
		result = plan(self.previous, self._current(), {}, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertIn("still expects fulfillment", result["message"])
		self.assertIn("reopen it or review the order", result["message"])
		self.assertNotIn("reopen", result)

	def test_closed_undelivered_order_with_all_zero_snapshot_is_cancelled_after_reopen(self):
		self.erp_state.update({"status": "Closed"})
		current = self._current()
		for line in current["items"]:
			line["qty"] = 0
		result = plan(self.previous, current, {}, self.erp_state)
		self.assertEqual(result["outcome"], CANCEL_ORDER)
		self.assertTrue(result["reopen"])

	def test_closed_delivered_order_equivalent_to_the_marketplace_is_unchanged(self):
		self.erp_state.update({"status": "Closed"})
		self.erp_state["lines"][0]["delivered_qty"] = 1
		# #179 behavior is unchanged: not reopened; converged only with cancellation evidence.
		result = plan(self.previous, self._current(), {}, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertNotIn("reopen", result)
		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)
		self.assertEqual(result["outcome"], NO_ACTION)

	def test_closed_erp_order_with_delivery_is_never_reopened_for_a_change(self):
		self.erp_state.update({"status": "Closed"})
		self.erp_state["lines"][0]["delivered_qty"] = 1
		current = self._current()
		current["items"][0]["qty"] = 3
		result = plan(self.previous, current, {}, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertFalse(result.get("reopen"))

	def test_closed_erp_order_refund_or_return_evidence_keeps_priority(self):
		self.erp_state.update({"status": "Closed"})
		result = plan(self.previous, self._current(), {"return": True}, self.erp_state)
		self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)
		self.erp_state["lines"][0]["delivered_qty"] = 1
		result = plan(self.previous, self._current(), {"refund": True}, self.erp_state)
		self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)

	def test_cancelled_erp_order_is_never_reopened(self):
		for docstatus, status in ((1, "Cancelled"), (2, "Cancelled")):
			with self.subTest(docstatus=docstatus):
				erp_state = deepcopy(self.erp_state)
				erp_state.update({"docstatus": docstatus, "status": status})
				current = self._current()
				current["items"][0]["qty"] = 1
				for evidence, candidate in (({}, current), ({"cancellation": "full"}, current)):
					result = plan(self.previous, candidate, evidence, erp_state)
					self.assertFalse(result.get("reopen"))
				self.assertEqual(plan(self.previous, current, {}, erp_state)["outcome"], NEEDS_ATTENTION)

	def test_cancelled_erp_order_without_cancellation_evidence_needs_attention(self):
		erp_state = deepcopy(self.erp_state)
		erp_state["status"] = "Cancelled"
		result = plan(self.previous, self._current(), {}, erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)

	def test_full_cancellation_before_execution_is_advisory_cancel(self):
		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)

		self.assertEqual(result["outcome"], CANCEL_ORDER)

	def test_billed_and_delivered_unchanged_order_needs_no_action(self):
		self.erp_state["lines"][0]["billed_amt"] = 100
		self.erp_state["lines"][0]["delivered_qty"] = 2

		result = plan(self.previous, self._current(), {}, self.erp_state)

		self.assertEqual(result["outcome"], NO_ACTION)

	def test_cancellation_of_billed_order_needs_attention(self):
		self.erp_state["lines"][0]["billed_amt"] = 100
		self.erp_state["lines"][0]["delivered_qty"] = 1

		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)

		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertIn("billed", result["message"])

	def test_cancellation_of_fully_delivered_billed_order_needs_compensating_transaction(self):
		self.erp_state["lines"][0]["billed_amt"] = 100
		self.erp_state["lines"][0]["delivered_qty"] = 2

		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)

		self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)

	def test_cancellation_with_outstanding_remainder_closes_remainder(self):
		self.erp_state["lines"][0]["delivered_qty"] = 1

		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)

		self.assertEqual(result["outcome"], CLOSE_REMAINDER)

	def test_cancellation_after_full_delivery_needs_compensating_transaction(self):
		self.erp_state["lines"][0]["delivered_qty"] = 2

		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)

		self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)
		self.assertIn("compensating transaction", result["message"])

	def test_erp_already_matching_changed_marketplace_state_converges_even_when_billed_and_delivered(self):
		current = self._current()
		current["items"][0]["qty"] = 3
		current["items"][0]["fulfilled_total_price"] = 150
		self.erp_state["lines"][0].update({"qty": 3, "amount": 150, "delivered_qty": 3, "billed_amt": 150})

		result = plan(self.previous, current, {}, self.erp_state)

		self.assertEqual(result["outcome"], NO_ACTION)

	def test_equivalence_does_not_hide_cancellation_evidence(self):
		self.erp_state["lines"][0]["delivered_qty"] = 2

		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)

		self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)

	def test_fractional_line_is_compared_by_amount_at_currency_precision(self):
		# 0.75 x 13.33 = 9.9975, which ERPNext posts as 10.00 and which matches the total.
		self.previous["items"][0].update(
			{"qty": 0.75, "effective_rate": 13.333333, "fulfilled_total_price": 10}
		)
		self.erp_state["lines"][0].update({"qty": 0.75, "amount": 10.0})
		self.erp_state["precision"] = {"qty": 3, "amount": 2}

		result = plan(self.previous, self._current(), {}, self.erp_state)

		self.assertEqual(result["outcome"], NO_ACTION)

	def test_amount_drift_at_currency_precision_is_not_equivalent(self):
		# 3 x 13.33 = 39.99 posts as 39.99, not the 40.00 marketplace total.
		self.previous["items"][0].update({"qty": 3, "effective_rate": 13.333333, "fulfilled_total_price": 40})
		self.erp_state["lines"][0].update({"qty": 3, "amount": 39.99})
		self.erp_state["precision"] = {"qty": 3, "amount": 2}

		result = plan(self.previous, self._current(), {}, self.erp_state)

		self.assertEqual(result["outcome"], NEEDS_ATTENTION)

	def test_advanced_snapshot_keeps_line_identities_and_refund_ids(self):
		self.previous["refund_ids"] = ["r1"]
		current = self._current()
		current["items"][0]["qty"] = 3

		advanced = snapshot_advanced_from(self.previous, current)

		self.assertEqual(advanced["items"][0]["sales_order_item"], "SOI-1")
		self.assertEqual(advanced["items"][0]["qty"], 3)
		self.assertEqual(advanced["refund_ids"], ["r1"])
		self.assertEqual(plan(advanced, current, {}, self.erp_state)["changes"], [])

	def _quantity_change(self):
		current = self._current()
		current["items"][0]["qty"] = 3
		current["items"][0]["fulfilled_total_price"] = 150
		return current

	def test_evidence_without_the_new_keys_is_unchanged(self):
		for evidence in ({}, {"source": "marketplace"}):
			with self.subTest(evidence=evidence):
				result = plan(self.previous, self._quantity_change(), evidence, self.erp_state)
				self.assertEqual(result["outcome"], UPDATE_SUBMITTED_ORDER)

	def test_permitting_lifecycle_before_fulfillment_allows_the_update(self):
		evidence = {"lifecycle_permits_change": True, "fulfillment_started": {"line-1": False}}
		result = plan(self.previous, self._quantity_change(), evidence, self.erp_state)
		self.assertEqual(result["outcome"], UPDATE_SUBMITTED_ORDER)

	def test_lifecycle_that_forbids_change_needs_attention_with_its_reason(self):
		evidence = {"lifecycle_permits_change": False, "lifecycle_reason": "Order already handed over."}
		result = plan(self.previous, self._quantity_change(), evidence, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertEqual(result["message"], "Order already handed over.")

	def test_lifecycle_that_forbids_change_is_irrelevant_when_nothing_changed(self):
		evidence = {"lifecycle_permits_change": False, "lifecycle_reason": "Order already handed over."}
		result = plan(self.previous, self._current(), evidence, self.erp_state)
		self.assertEqual(result["outcome"], NO_ACTION)

	def test_mapping_sourced_change_is_not_blamed_on_the_lifecycle(self):
		current = self._current()
		current["items"][0]["warehouse"] = "Other Warehouse"
		for evidence in (
			{"lifecycle_permits_change": False, "lifecycle_reason": "Order already handed over."},
			{"lifecycle_permits_change": None, "lifecycle_reason": "Unknown stage."},
			{"lifecycle_permits_change": True, "fulfillment_started": {"line-1": True}},
		):
			with self.subTest(evidence=evidence):
				result = plan(self.previous, current, evidence, self.erp_state)
				# The lifecycle is not blamed; the mapping gate (D4) reports it instead.
				self.assertEqual(result["outcome"], NEEDS_ATTENTION)
				self.assertIn("cannot be applied automatically", result["message"])

	def test_marketplace_sourced_change_alongside_a_mapping_change_is_still_gated(self):
		current = self._quantity_change()
		current["items"][0]["warehouse"] = "Other Warehouse"
		evidence = {"lifecycle_permits_change": False, "lifecycle_reason": "Order already handed over."}
		result = plan(self.previous, current, evidence, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertEqual(result["message"], "Order already handed over.")

	def test_refund_and_return_keep_priority_over_the_lifecycle_gate(self):
		erp_state = deepcopy(self.erp_state)
		erp_state["lines"][0]["delivered_qty"] = 1
		for base in ({"refund": True}, {"return": True}):
			with self.subTest(evidence=base):
				evidence = {**base, "lifecycle_permits_change": False, "lifecycle_reason": "Handed over."}
				result = plan(self.previous, self._quantity_change(), evidence, erp_state)
				self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)

	def test_changed_line_with_fulfillment_started_needs_attention(self):
		for started in ({"line-1": True}, True):
			with self.subTest(started=started):
				evidence = {"lifecycle_permits_change": True, "fulfillment_started": started}
				result = plan(self.previous, self._quantity_change(), evidence, self.erp_state)
				self.assertEqual(result["outcome"], NEEDS_ATTENTION)
				self.assertIn("line-1", result["message"])

	def test_fulfillment_started_on_another_line_does_not_block(self):
		evidence = {"fulfillment_started": {"line-2": True}}
		result = plan(self.previous, self._quantity_change(), evidence, self.erp_state)
		self.assertEqual(result["outcome"], UPDATE_SUBMITTED_ORDER)

	def test_unknown_lifecycle_meeting_a_change_needs_attention(self):
		evidence = {"lifecycle_permits_change": None, "lifecycle_reason": "Unknown stage."}
		result = plan(self.previous, self._quantity_change(), evidence, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertEqual(result["message"], "Unknown stage.")

	def test_unknown_lifecycle_without_a_change_needs_no_action(self):
		result = plan(self.previous, self._current(), {"lifecycle_permits_change": None}, self.erp_state)
		self.assertEqual(result["outcome"], NO_ACTION)

	def test_cancellation_is_not_blocked_by_the_lifecycle_gate(self):
		evidence = {"cancellation": "full", "lifecycle_permits_change": None}
		result = plan(self.previous, self._current(), evidence, self.erp_state)
		self.assertEqual(result["outcome"], CANCEL_ORDER)

	def test_cancellation_after_fulfillment_depends_on_erp_delivery(self):
		evidence = {"cancellation": "full", "cancelled_after_fulfillment": True}
		result = plan(self.previous, self._current(), evidence, self.erp_state)
		self.assertEqual(result["outcome"], CANCEL_ORDER)
		self.assertIn("Check what happened to the parcel", result["message"])
		for delivered_qty in (1, 2):
			with self.subTest(delivered_qty=delivered_qty):
				erp_state = deepcopy(self.erp_state)
				erp_state["lines"][0]["delivered_qty"] = delivered_qty
				result = plan(self.previous, self._current(), evidence, erp_state)
				self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)

	def test_post_pickup_cancellation_converges_when_erp_order_already_cancelled(self):
		evidence = {"cancellation": "full", "cancelled_after_fulfillment": True}
		for docstatus, status in ((1, "Cancelled"), (2, "Cancelled")):
			with self.subTest(docstatus=docstatus):
				erp_state = deepcopy(self.erp_state)
				erp_state.update({"docstatus": docstatus, "status": status})
				erp_state["lines"][0]["delivered_qty"] = 1  # delivery must not override convergence
				result = plan(self.previous, self._current(), evidence, erp_state)
				self.assertEqual(result["outcome"], NO_ACTION)

	def test_post_pickup_cancellation_converges_when_erp_order_closed_without_remainder(self):
		self.erp_state.update({"status": "Closed"})
		self.erp_state["lines"][0]["delivered_qty"] = 2
		evidence = {"cancellation": "full", "cancelled_after_fulfillment": True}
		result = plan(self.previous, self._current(), evidence, self.erp_state)
		self.assertEqual(result["outcome"], NO_ACTION)

	def test_post_pickup_cancellation_with_closed_remainder_converges(self):
		self.erp_state.update({"status": "Closed"})
		self.erp_state["lines"][0]["delivered_qty"] = 1
		evidence = {"cancellation": "full", "cancelled_after_fulfillment": True}
		result = plan(self.previous, self._current(), evidence, self.erp_state)
		self.assertEqual(result["outcome"], NO_ACTION)

	def test_cancellation_after_fulfillment_with_billing_but_no_delivery_needs_attention(self):
		self.erp_state["lines"][0]["billed_amt"] = 50
		evidence = {"cancellation": "full", "cancelled_after_fulfillment": True}
		result = plan(self.previous, self._current(), evidence, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)

	def test_cancellation_after_fulfillment_with_advance_payment_needs_attention(self):
		self.erp_state["advance_paid"] = 100
		evidence = {"cancellation": "full", "cancelled_after_fulfillment": True}
		result = plan(self.previous, self._current(), evidence, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)

	def test_plain_cancellation_is_unchanged_by_the_new_keys(self):
		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)
		self.assertEqual(result["outcome"], CANCEL_ORDER)

	def test_advance_payment_blocks_cancellation(self):
		self.erp_state["advance_paid"] = 50
		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertIn("advance payment", result["message"])

	def test_all_lines_zero_cancels_when_nothing_is_delivered(self):
		current = self._current()
		current["items"][0]["qty"] = 0
		result = plan(self.previous, current, {}, self.erp_state)
		self.assertEqual(result["outcome"], CANCEL_ORDER)

	def test_all_lines_zero_closes_remainder_after_delivery(self):
		current = self._current()
		current["items"][0]["qty"] = 0
		self.erp_state["lines"][0]["delivered_qty"] = 1
		result = plan(self.previous, current, {"cancellation": "full"}, self.erp_state)
		self.assertEqual(result["outcome"], CLOSE_REMAINDER)

	def test_all_lines_zero_after_delivery_without_cancellation_needs_attention(self):
		current = self._current()
		current["items"][0]["qty"] = 0
		self.erp_state["lines"][0]["delivered_qty"] = 1
		result = plan(self.previous, current, {}, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertIn("explicit cancellation is required", result["message"])

	def _assert_mapping_change_needs_attention(self, field, value):
		current = self._current()
		current["items"][0][field] = value
		result = plan(self.previous, current, {}, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertIn("cannot be applied automatically", result["message"])

	def test_item_code_change_needs_attention(self):
		self._assert_mapping_change_needs_attention("item_code", "OTHER-ITEM")

	def test_uom_change_needs_attention(self):
		self._assert_mapping_change_needs_attention("uom", "Box")

	def test_warehouse_change_needs_attention(self):
		self._assert_mapping_change_needs_attention("warehouse", "Other WH - X")

	def test_all_lines_zero_with_billing_and_remainder_needs_attention(self):
		current = self._current()
		current["items"][0]["qty"] = 0
		self.erp_state["lines"][0]["delivered_qty"] = 1
		self.erp_state["lines"][0]["billed_amt"] = 50
		result = plan(self.previous, current, {"cancellation": "full"}, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)

	def test_zero_quantity_line_is_expected_absent_from_erp(self):
		self.previous["items"].append(
			{"external_item_id": "line-2", "qty": 0, "item_code": "ITEM-2", "sales_order_item": None}
		)
		result = plan(self.previous, self._current(), {}, self.erp_state)
		self.assertEqual(result["outcome"], NO_ACTION)

	def test_line_returning_from_zero_to_positive_needs_attention(self):
		self.previous["items"][0]["qty"] = 0
		current = self._current()
		current["items"][0]["qty"] = 2
		result = plan(self.previous, current, {}, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)

	def test_quantity_reduction_after_partial_delivery_needs_attention(self):
		current = self._current()
		current["items"][0]["qty"] = 1
		current["items"][0]["fulfilled_total_price"] = 50
		self.erp_state["lines"][0]["delivered_qty"] = 2
		result = plan(self.previous, current, {}, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)

	def test_execute_rejects_a_non_executable_outcome(self):
		from marketplace_integrations.services.sales_order_reconciliation import execute

		with self.assertRaises(ValueError):
			execute({"outcome": NO_ACTION}, "SO-1")

	def test_reopen_checks_permission_before_reopening_and_comments_after(self):
		from unittest.mock import MagicMock

		from marketplace_integrations.services.sales_order_reconciliation import reopen_closed_order

		order = MagicMock()
		order.check_permission.side_effect = PermissionError
		with self.assertRaises(PermissionError):
			reopen_closed_order(order, CANCEL_ORDER, "MKT-1")
		order.update_status.assert_not_called()

		order = MagicMock()
		reopen_closed_order(order, CANCEL_ORDER, "MKT-1")
		order.check_permission.assert_called_once_with("submit")
		order.update_status.assert_called_once_with("Draft")
		order.reload.assert_called_once()
		self.assertIn("MKT-1", order.add_comment.call_args.args[1])

	def test_reopen_plan_message_says_the_order_will_be_reopened_first(self):
		self.erp_state.update({"status": "Closed"})
		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)
		self.assertIn("reopened first", result["message"])

	def _linked(self, delivery_notes=(), invoices=(), **line_fields):
		self.erp_state["lines"][0].update(line_fields)
		self.erp_state["linked_documents"] = {
			"Delivery Note": list(delivery_notes),
			"Sales Invoice": list(invoices),
		}

	def test_refund_after_delivery_names_the_delivery_note(self):
		self._linked(delivery_notes=["DN-0001"], delivered_qty=2)

		result = plan(self.previous, self._current(), {"refund": True}, self.erp_state)

		self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)
		self.assertEqual(
			result["message"],
			"The marketplace refunded this order, but it was already delivered in ERPNext (Delivery Note DN-0001). "
			"The Sales Order was not changed. If the goods came back, create a Sales Return against "
			"Delivery Note DN-0001 (a compensating transaction), otherwise review the order manually.",
		)

	def test_refund_after_invoice_names_the_sales_invoice(self):
		self._linked(invoices=["SI-0001"], billed_amt=100)

		result = plan(self.previous, self._current(), {"refund": True}, self.erp_state)

		self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)
		self.assertEqual(
			result["message"],
			"The marketplace refunded this order, but it was already invoiced in ERPNext (Sales Invoice SI-0001). "
			"The Sales Order was not changed. Create a Credit Note against Sales Invoice SI-0001 for the "
			"refunded amount (a compensating transaction), or review the order manually.",
		)

	def test_refund_after_delivery_and_invoice_names_both_and_stays_short(self):
		self._linked(
			delivery_notes=["DN-0001", "DN-0002"], invoices=["SI-0001"], delivered_qty=2, billed_amt=100
		)

		result = plan(self.previous, self._current(), {"refund": True}, self.erp_state)

		self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)
		for text in ("Delivery Notes DN-0001, DN-0002", "Sales Invoice SI-0001", "compensating"):
			self.assertIn(text, result["message"])
		self.assertLess(len(result["message"]), 450)

	def test_more_than_three_documents_list_three_and_say_and_more(self):
		self._linked(
			delivery_notes=["DN-1", "DN-2", "DN-3", "DN-4"], invoices=["SI-1", "SI-2"], delivered_qty=2
		)

		result = plan(self.previous, self._current(), {"refund": True}, self.erp_state)

		self.assertIn("Delivery Notes DN-1, DN-2, DN-3, and more", result["message"])
		self.assertNotIn("DN-4", result["message"])
		self.assertIn("Sales Invoices SI-1, SI-2)", result["message"])

	def test_three_documents_are_listed_without_and_more(self):
		self._linked(delivery_notes=["DN-1", "DN-2", "DN-3"], delivered_qty=2)

		result = plan(self.previous, self._current(), {"refund": True}, self.erp_state)

		self.assertIn("Delivery Notes DN-1, DN-2, DN-3)", result["message"])
		self.assertNotIn("and more", result["message"])

	def test_longest_realistic_both_message_fits_the_stored_500_characters(self):
		self._linked(
			delivery_notes=[f"DN-{index}".ljust(30, "X") for index in range(4)],
			invoices=[f"SI-{index}".ljust(30, "X") for index in range(4)],
			delivered_qty=2,
			billed_amt=100,
		)

		for evidence in ({"refund": True}, {"return": True}):
			with self.subTest(evidence=evidence):
				message = plan(self.previous, self._current(), evidence, self.erp_state)["message"]

				self.assertLess(len(message), 500)
				self.assertEqual(message.count("and more"), 2)
				self.assertTrue(message.endswith("or review the order manually."))

	def test_return_evidence_is_not_worded_as_a_refund(self):
		self._linked(delivery_notes=["DN-0001"], delivered_qty=2)

		result = plan(self.previous, self._current(), {"return": True}, self.erp_state)

		self.assertIn("reported a return", result["message"])
		self.assertNotIn("refunded", result["message"])

	def test_cancellation_after_invoice_names_the_sales_invoice(self):
		self._linked(invoices=["SI-0001"], billed_amt=100, delivered_qty=1)

		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)

		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertEqual(
			result["message"],
			"The marketplace cancelled this order, but Sales Invoice SI-0001 already bills it in ERPNext, "
			"so the Sales Order cannot be cancelled automatically. Cancel the Sales Invoice (or issue a "
			"Credit Note if it is submitted and paid), then run the update again.",
		)

	def test_cancellation_after_full_delivery_names_the_delivery_note(self):
		self._linked(delivery_notes=["DN-0001"], delivered_qty=2)

		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)

		self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)
		self.assertEqual(
			result["message"],
			"The marketplace cancelled this order, but Delivery Note DN-0001 already delivered all of it. "
			"Create a Sales Return against Delivery Note DN-0001 for the goods that came back, "
			"or review the order manually.",
		)

	def test_blocked_update_after_delivery_names_the_documents(self):
		current = self._current()
		current["items"][0]["qty"] = 3
		current["items"][0]["fulfilled_total_price"] = 150
		self._linked(delivery_notes=["DN-0001"], invoices=["SI-0001"], delivered_qty=1)

		result = plan(self.previous, current, {}, self.erp_state)

		self.assertEqual(result["outcome"], NEEDS_ATTENTION)
		self.assertIn("(Delivery Note DN-0001; Sales Invoice SI-0001)", result["message"])

	def test_missing_or_empty_linked_documents_keep_the_generic_text(self):
		self.erp_state["lines"][0].update(delivered_qty=2, billed_amt=100)
		for linked in (None, {}, {"Delivery Note": [], "Sales Invoice": []}):
			with self.subTest(linked=linked):
				state = deepcopy(self.erp_state)
				if linked is not None:
					state["linked_documents"] = linked

				refund = plan(self.previous, self._current(), {"refund": True}, state)
				cancel = plan(self.previous, self._current(), {"cancellation": "full"}, state)

				self.assertEqual(
					refund["message"],
					"Marketplace return or refund evidence needs a compensating ERP transaction.",
				)
				self.assertEqual(
					cancel["message"],
					"The cancelled marketplace order is already fully delivered in ERPNext; "
					"a return or other compensating transaction is needed.",
				)

	def test_this_module_is_platform_neutral(self):
		import inspect

		from marketplace_integrations.services import sales_order_reconciliation

		source = inspect.getsource(sales_order_reconciliation).lower()
		for platform in ("talabat", "shopify", "noon"):
			self.assertNotIn(platform, source)

	def test_shipping_charge_unchanged_needs_no_action(self):
		self.previous["shipping"] = 419.22
		current = self._current()
		current["shipping"] = 419.22

		self.assertEqual(plan(self.previous, current, {}, self.erp_state)["outcome"], NO_ACTION)

	def test_changed_or_new_shipping_on_a_submitted_order_needs_attention_and_executes_nothing(self):
		cases = {"refunded": (419.22, None), "changed": (419.22, 100.0), "never synced": (None, 419.22)}
		for label, (previous_shipping, current_shipping) in cases.items():
			with self.subTest(case=label):
				previous = deepcopy(self.previous)
				current = self._current()
				if previous_shipping is not None:
					previous["shipping"] = previous_shipping
				if current_shipping is not None:
					current["shipping"] = current_shipping

				result = plan(previous, current, {}, self.erp_state)

				self.assertEqual(result["outcome"], NEEDS_ATTENTION)
				self.assertIn("cancel and amend", result["message"])
				self.assertEqual([change["field"] for change in result["changes"]], ["shipping"])

	def _current(self):
		current = deepcopy(self.previous)
		current["items"][0].pop("sales_order_item")
		return current
