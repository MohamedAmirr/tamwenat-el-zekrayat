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

	def test_closed_erp_order_with_outstanding_remainder_needs_attention(self):
		self.erp_state.update({"status": "Closed"})
		self.erp_state["lines"][0]["delivered_qty"] = 1
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
		self.previous["items"][0].update({"qty": 0.75, "effective_rate": 13.333333, "fulfilled_total_price": 10})
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

	def _current(self):
		current = deepcopy(self.previous)
		current["items"][0].pop("sales_order_item")
		return current
