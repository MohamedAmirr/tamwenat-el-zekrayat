# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from copy import deepcopy

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

	def test_cancelled_or_repriced_erp_order_needs_attention_even_when_marketplace_is_unchanged(self):
		for field, value in (
			("status", "Cancelled"),
			("status", "Closed"),
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

	def test_closed_erp_order_without_delivery_needs_attention(self):
		self.erp_state.update({"status": "Closed"})
		result = plan(self.previous, self._current(), {"cancellation": "full"}, self.erp_state)
		self.assertEqual(result["outcome"], NEEDS_ATTENTION)

	def test_inactive_erp_order_without_cancellation_evidence_needs_attention(self):
		for status in ("Cancelled", "Closed"):
			with self.subTest(status=status):
				erp_state = deepcopy(self.erp_state)
				erp_state["status"] = status
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
		self.assertEqual(
			plan(self.previous, self._current(), evidence, self.erp_state)["outcome"], NEEDS_ATTENTION
		)
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

	def test_this_module_is_platform_neutral(self):
		import inspect

		from marketplace_integrations.services import sales_order_reconciliation

		source = inspect.getsource(sales_order_reconciliation).lower()
		for platform in ("talabat", "shopify", "noon"):
			self.assertNotIn(platform, source)

	def _current(self):
		current = deepcopy(self.previous)
		current["items"][0].pop("sales_order_item")
		return current
