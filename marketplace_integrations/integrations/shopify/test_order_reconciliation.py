# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from copy import deepcopy
from types import SimpleNamespace

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.shopify.order_reconciliation import (
	build_evidence,
	fulfillment_started,
	refund_ids,
	unreflected_refund_ids,
)
from marketplace_integrations.services.sales_order_reconciliation import (
	NEEDS_ATTENTION,
	UNSUPPORTED_COMPENSATING_TRANSACTION,
	UPDATE_SUBMITTED_ORDER,
	plan,
)


class UnitTestShopifyOrderReconciliation(UnitTestCase):
	def _order(self, cancelled_at=None, refunds=None, baseline=None, items=None):
		return SimpleNamespace(
			items=items,
			cancelled_at=cancelled_at,
			refunds=frappe.as_json(refunds) if refunds is not None else None,
			last_synced_snapshot=frappe.as_json(baseline) if baseline is not None else None,
		)

	def test_no_cancellation_or_refund_means_no_evidence(self):
		self.assertEqual(build_evidence(self._order()), {})

	def test_cancelled_at_is_full_cancellation(self):
		self.assertEqual(build_evidence(self._order(cancelled_at="2026-10-05")), {"cancellation": "full"})

	def test_refund_not_in_baseline_is_refund_evidence(self):
		order = self._order(refunds=[{"id": "r1"}, {"id": "r2"}], baseline={"refund_ids": ["r1"]})

		self.assertEqual(unreflected_refund_ids(order), ["r2"])
		self.assertEqual(build_evidence(order), {"refund": True})

	def test_refund_already_in_baseline_is_not_evidence(self):
		order = self._order(refunds=[{"id": "r1"}], baseline={"refund_ids": ["r1"]})

		self.assertEqual(build_evidence(order), {})

	def test_legacy_baseline_without_refund_ids_treats_every_refund_as_new(self):
		order = self._order(refunds=[{"id": "r1"}], baseline={"items": []})

		self.assertEqual(unreflected_refund_ids(order), ["r1"])

	def test_no_baseline_treats_every_refund_as_new(self):
		self.assertEqual(unreflected_refund_ids(self._order(refunds=[{"id": "r1"}])), ["r1"])

	def test_cancellation_and_refund_combine(self):
		order = self._order(cancelled_at="2026-10-05", refunds=[{"id": "r1"}])

		self.assertEqual(build_evidence(order), {"cancellation": "full", "refund": True})

	def test_refund_ids_tolerates_malformed_values(self):
		for refunds in (None, "", "not json", "{}", "[]"):
			with self.subTest(refunds=refunds):
				self.assertEqual(refund_ids(SimpleNamespace(refunds=refunds)), [])
		self.assertEqual(refund_ids(SimpleNamespace(refunds='["plain-id"]')), ["plain-id"])


def _line(line_id, current_quantity, unfulfilled_quantity, known=1, non_fulfillable_quantity=0):
	"""A line as persisted from Shopify's LineItem.currentQuantity/unfulfilledQuantity/nonFulfillableQuantity."""
	return SimpleNamespace(
		external_item_id=line_id,
		current_quantity=current_quantity,
		unfulfilled_quantity=unfulfilled_quantity,
		non_fulfillable_quantity=non_fulfillable_quantity,
		fulfillment_quantities_known=known,
	)


class UnitTestShopifyFulfillmentEvidence(UnitTestCase):
	"""Contract fixtures hand-written from shopify.dev's LineItem field docs (not live-verified)."""

	def _order(self, items, cancelled_at=None, refunds=None, baseline=None):
		return SimpleNamespace(
			items=items,
			cancelled_at=cancelled_at,
			refunds=frappe.as_json(refunds) if refunds is not None else None,
			last_synced_snapshot=frappe.as_json(baseline) if baseline is not None else None,
		)

	def test_before_fulfillment_permits_change_and_no_line_has_started(self):
		evidence = build_evidence(self._order([_line("l1", 2, 2), _line("l2", 1, 1)]))

		self.assertIs(evidence["lifecycle_permits_change"], True)
		self.assertEqual(evidence["fulfillment_started"], {"l1": False, "l2": False})
		self.assertEqual(evidence["source"], "marketplace")

	def test_partially_fulfilled_line_has_started_and_order_still_permits_other_lines(self):
		evidence = build_evidence(self._order([_line("l1", 2, 1), _line("l2", 1, 1)]))

		self.assertIs(evidence["lifecycle_permits_change"], True)
		self.assertEqual(evidence["fulfillment_started"], {"l1": True, "l2": False})

	def test_fully_fulfilled_order_forbids_change_with_a_reason(self):
		evidence = build_evidence(self._order([_line("l1", 2, 0), _line("l2", 1, 0)]))

		self.assertIs(evidence["lifecycle_permits_change"], False)
		self.assertTrue(evidence["lifecycle_reason"])
		self.assertEqual(evidence["fulfillment_started"], {"l1": True, "l2": True})

	def test_cancelled_order_is_full_cancellation_alongside_line_evidence(self):
		evidence = build_evidence(self._order([_line("l1", 2, 2)], cancelled_at="2026-10-05"))

		self.assertEqual(evidence["cancellation"], "full")
		self.assertEqual(evidence["fulfillment_started"], {"l1": False})

	def test_refunded_units_leave_current_quantity_and_do_not_count_as_fulfilled(self):
		# 3 ordered, 1 refunded (current 2), nothing fulfilled.
		evidence = build_evidence(self._order([_line("l1", 2, 2, non_fulfillable_quantity=1)]))

		self.assertEqual(evidence["fulfillment_started"], {"l1": False})

	def test_fully_refunded_line_is_not_started(self):
		self.assertIs(fulfillment_started(_line("l1", 0, 0)), False)

	def test_unfulfilled_above_current_is_floored_not_started(self):
		self.assertIs(fulfillment_started(_line("l1", 1, 2)), False)

	def test_row_without_known_quantities_is_unknown_not_fulfilled(self):
		# A pre-#178 row: both quantities read back 0, which would look fully fulfilled.
		unknown = _line("l1", 2, 0, known=0)

		self.assertIsNone(fulfillment_started(unknown))
		evidence = build_evidence(self._order([unknown, _line("l2", 1, 1)]))
		self.assertIsNone(evidence["lifecycle_permits_change"])
		self.assertTrue(evidence["lifecycle_reason"])
		self.assertEqual(evidence["fulfillment_started"], {"l2": False})

	def test_known_refund_id_is_not_new_evidence_but_a_new_one_is(self):
		items = [_line("l1", 2, 2)]
		known = build_evidence(self._order(items, refunds=[{"id": "r1"}], baseline={"refund_ids": ["r1"]}))
		both = [{"id": "r1"}, {"id": "r2"}]
		new = build_evidence(self._order(items, refunds=both, baseline={"refund_ids": ["r1"]}))

		self.assertNotIn("refund", known)
		self.assertTrue(new["refund"])


class UnitTestShopifyEvidenceThroughPlanner(UnitTestCase):
	def setUp(self):
		self.previous = {
			"company": "Test Company",
			"items": [
				{
					"external_item_id": "l1",
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

	def _evidence(self, items, refunds=None):
		order = SimpleNamespace(
			items=items,
			cancelled_at=None,
			refunds=frappe.as_json(refunds) if refunds is not None else None,
			last_synced_snapshot=None,
		)
		return build_evidence(order)

	def _changed(self):
		current = deepcopy(self.previous)
		current["items"][0].pop("sales_order_item")
		current["items"][0]["qty"] = 1
		current["items"][0]["fulfilled_total_price"] = 50
		return current

	def test_change_before_fulfillment_is_an_update(self):
		result = plan(self.previous, self._changed(), self._evidence([_line("l1", 1, 1)]), self.erp_state)

		self.assertEqual(result["outcome"], UPDATE_SUBMITTED_ORDER)

	def test_change_to_a_partially_or_fully_fulfilled_line_needs_attention(self):
		for items in ([_line("l1", 1, 0)], [_line("l1", 2, 1)]):
			with self.subTest(unfulfilled=items[0].unfulfilled_quantity):
				result = plan(self.previous, self._changed(), self._evidence(items), self.erp_state)

				self.assertEqual(result["outcome"], NEEDS_ATTENTION)

	def test_change_on_a_row_with_unknown_fulfillment_needs_attention(self):
		evidence = self._evidence([_line("l1", 1, 0, known=0)])

		result = plan(self.previous, self._changed(), evidence, self.erp_state)

		self.assertEqual(result["outcome"], NEEDS_ATTENTION)

	def test_refund_with_delivery_or_billing_is_a_compensating_transaction(self):
		evidence = self._evidence([_line("l1", 1, 1)], refunds=[{"id": "r1"}])
		for field, value in (("delivered_qty", 1), ("billed_amt", 50)):
			with self.subTest(field=field):
				erp_state = deepcopy(self.erp_state)
				erp_state["lines"][0][field] = value

				result = plan(self.previous, self._changed(), evidence, erp_state)

				self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)
