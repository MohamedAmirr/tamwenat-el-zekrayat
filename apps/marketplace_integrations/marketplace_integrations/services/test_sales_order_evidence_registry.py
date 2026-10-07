# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from types import SimpleNamespace

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.services.sales_order_sync import (
	_RECONCILIATION_EVIDENCE_BUILDERS,
	_reconciliation_evidence,
	_uses_stored_snapshot,
)


class UnitTestSalesOrderEvidenceRegistry(UnitTestCase):
	def _order(self, platform, **fields):
		values = {
			"platform": platform,
			"external_status": None,
			"post_picked_up": 0,
			"cancelled_at": None,
			"refunds": None,
			"last_synced_snapshot": None,
			"items": [],
		}
		values.update(fields)
		return SimpleNamespace(**values)

	def test_every_registered_builder_resolves(self):
		for platform, path in _RECONCILIATION_EVIDENCE_BUILDERS.items():
			with self.subTest(platform=platform):
				self.assertTrue(callable(frappe.get_attr(path)))

	def test_shopify_evidence_is_the_unchanged_cancelled_at_and_refund_output(self):
		order = self._order("Shopify", cancelled_at="2026-10-05", refunds=frappe.as_json([{"id": "r1"}]))

		self.assertEqual(_reconciliation_evidence(order), {"cancellation": "full", "refund": True})
		self.assertEqual(_reconciliation_evidence(self._order("Shopify")), {})

	def test_talabat_cancelled_is_full_cancellation_and_a_blank_platform_is_talabat(self):
		for platform in ("Talabat", "", None):
			with self.subTest(platform=platform):
				evidence = _reconciliation_evidence(self._order(platform, external_status="CANCELLED"))

				self.assertEqual(evidence["cancellation"], "full")

	def test_only_the_owning_platform_interprets_its_fields(self):
		# A Talabat status on a Shopify order, or a Shopify cancelled_at on a Talabat order, is not evidence.
		self.assertEqual(_reconciliation_evidence(self._order("Shopify", external_status="CANCELLED")), {})
		self.assertNotIn(
			"cancellation", _reconciliation_evidence(self._order("Talabat", cancelled_at="2026-10-05"))
		)

	def test_noon_reads_unit_data_not_the_order_status_and_an_unregistered_platform_has_none(self):
		# No per-unit payload: unknown stage, and the Talabat-style status is never interpreted.
		evidence = _reconciliation_evidence(
			self._order(
				"Noon",
				external_status="CANCELLED",
				latest_payload=None,
				noon_shipments=[],
				noon_shipment_units=[],
			)
		)

		self.assertNotIn("cancellation", evidence)
		self.assertIsNone(evidence["lifecycle_permits_change"])
		self.assertEqual(_reconciliation_evidence(self._order("Other")), {})

	def test_only_full_cancellation_uses_the_stored_baseline_for_a_submitted_order(self):
		self.assertTrue(_uses_stored_snapshot(1, {"cancellation": "full"}))
		for evidence in ({}, {"refund": True}, {"return": True}, {"lifecycle_permits_change": False}):
			with self.subTest(evidence=evidence):
				self.assertFalse(_uses_stored_snapshot(1, evidence))
		self.assertFalse(_uses_stored_snapshot(0, {"cancellation": "full"}))
