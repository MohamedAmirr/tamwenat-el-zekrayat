# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from types import SimpleNamespace

from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.talabat.order_reconciliation import build_evidence


class UnitTestTalabatOrderReconciliation(UnitTestCase):
	"""Contract fixtures: documented ``order.Order.status`` values (openapi.json) -> evidence."""

	def _order(self, status, post_picked_up=0, line_ids=("line-1", "line-2")):
		return SimpleNamespace(
			external_status=status,
			post_picked_up=post_picked_up,
			items=[SimpleNamespace(external_item_id=line_id) for line_id in line_ids],
		)

	def test_received_permits_change_and_no_line_has_started_fulfillment(self):
		evidence = build_evidence(self._order("RECEIVED"))

		self.assertIs(evidence["lifecycle_permits_change"], True)
		self.assertEqual(evidence["fulfillment_started"], {"line-1": False, "line-2": False})
		self.assertNotIn("cancellation", evidence)
		self.assertEqual(evidence["source"], "marketplace")

	def test_picked_packed_and_later_statuses_forbid_change_and_mark_every_line_started(self):
		for status in ("READY_FOR_PICKUP", "DISPATCHED", "DELIVERED"):
			with self.subTest(status=status):
				evidence = build_evidence(self._order(status))

				self.assertIs(evidence["lifecycle_permits_change"], False)
				self.assertIn(status, evidence["lifecycle_reason"])
				self.assertEqual(evidence["fulfillment_started"], {"line-1": True, "line-2": True})
				self.assertNotIn("cancellation", evidence)

	def test_cancelled_is_full_cancellation(self):
		evidence = build_evidence(self._order("CANCELLED"))

		self.assertEqual(evidence["cancellation"], "full")
		self.assertNotIn("cancelled_after_fulfillment", evidence)
		self.assertNotIn("lifecycle_permits_change", evidence)

	def test_cancelled_after_pickup_is_flagged(self):
		evidence = build_evidence(self._order("CANCELLED", post_picked_up=1))

		self.assertEqual(evidence["cancellation"], "full")
		self.assertTrue(evidence["cancelled_after_fulfillment"])

	def test_post_picked_up_without_cancellation_is_ignored(self):
		evidence = build_evidence(self._order("RECEIVED", post_picked_up=1))

		self.assertNotIn("cancelled_after_fulfillment", evidence)
		self.assertNotIn("cancellation", evidence)

	def test_unrecognized_or_blank_status_is_an_unknown_stage(self):
		# "CANCELED" is the single spelling variant in the docs' prose; it is not accepted.
		for status in ("CANCELED", "", None, "SOMETHING_NEW", "received"):
			with self.subTest(status=status):
				evidence = build_evidence(self._order(status))

				self.assertIn("lifecycle_permits_change", evidence)
				self.assertIsNone(evidence["lifecycle_permits_change"])
				self.assertTrue(evidence["lifecycle_reason"])
				self.assertNotIn("cancellation", evidence)

	def test_item_level_statuses_are_not_evidence(self):
		order = self._order("RECEIVED")
		for status in ("IN_CART", "NOT_FOUND", "REPLACED", "ADDITION", "NOT_PROCESSED"):
			order.items[0].external_status = status
			self.assertEqual(build_evidence(order), build_evidence(self._order("RECEIVED")))

	def test_build_evidence_is_pure(self):
		order = self._order("DISPATCHED")
		before = (order.external_status, order.post_picked_up, [row.external_item_id for row in order.items])

		build_evidence(order)

		self.assertEqual(
			before,
			(order.external_status, order.post_picked_up, [row.external_item_id for row in order.items]),
		)
