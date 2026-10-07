# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Contract fixtures for the Noon evidence adapter (#178 part 3).

Hand-written, SimpleNamespace-based: ``build_evidence()`` is pure, so no DB is
involved.  Unit statuses use Noon's documented verbose enum spellings, exactly as
they appear in ``latest_payload.items[]``.
"""

from types import SimpleNamespace

from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.noon.order_reconciliation import build_evidence
from marketplace_integrations.services.sales_order_reconciliation import (
	CANCEL_ORDER,
	NEEDS_ATTENTION,
	NO_ACTION,
	UNSUPPORTED_COMPENSATING_TRANSACTION,
	plan,
)

CONFIRMED = "MP_ITEM_STATUS_CONFIRMED"
CANCELLED = "MP_ITEM_STATUS_CANCELLED"
ACKNOWLEDGED = "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED"
OUT_OF_STOCK = "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"
SHIPPED = "INTEGRATION_ITEM_STATUS_SHIPPED"
UNSPECIFIED = "INTEGRATION_ITEM_STATUS_UNSPECIFIED"


def _unit(mp_item_nr, sku="SKU-A", mp_status=CONFIRMED, integration_status=ACKNOWLEDGED):
	return {
		"mp_item_nr": mp_item_nr,
		"partner_sku": sku,
		"mp_status": mp_status,
		"integration_status": integration_status,
	}


def _order(units, shipments=(), shipment_units=()):
	"""``shipments``: ``[(integration_shipment_nr, status)]``; ``shipment_units``: ``[(shipment_nr, mp_item_nr)]``."""
	return SimpleNamespace(
		platform="Noon",
		latest_payload={"items": units},
		noon_shipments=[
			SimpleNamespace(integration_shipment_nr=nr, status=status) for nr, status in shipments
		],
		noon_shipment_units=[
			SimpleNamespace(integration_shipment_nr=nr, mp_item_nr=mp_item_nr)
			for nr, mp_item_nr in shipment_units
		],
	)


class UnitTestNoonOrderReconciliation(UnitTestCase):
	def test_before_fulfillment_nothing_started_and_changes_are_permitted(self):
		evidence = build_evidence(_order([_unit("1"), _unit("2")]))

		self.assertEqual(evidence["fulfillment_started"], {"sku:SKU-A": False})
		self.assertIs(evidence["lifecycle_permits_change"], True)
		self.assertNotIn("cancellation", evidence)
		self.assertEqual(evidence["source"], "marketplace")

	def test_unit_in_a_created_shipment_has_started_fulfillment(self):
		# Cancel Pending is undocumented at Noon: counted as started (open question).
		for status in ("Created", "Cancel Pending"):
			with self.subTest(status=status):
				evidence = build_evidence(
					_order([_unit("1"), _unit("2", sku="SKU-B")], [("S-1", status)], [("S-1", "1")])
				)

				self.assertEqual(evidence["fulfillment_started"], {"sku:SKU-A": True, "sku:SKU-B": False})
				self.assertIs(evidence["lifecycle_permits_change"], True)

	def test_unit_in_a_shipment_that_never_left_has_not_started(self):
		for status in ("Pending", "Error", "Cancelled", "Discarded"):
			with self.subTest(status=status):
				evidence = build_evidence(_order([_unit("1")], [("S-1", status)], [("S-1", "1")]))

				self.assertEqual(evidence["fulfillment_started"], {"sku:SKU-A": False})

	def test_shipped_unit_has_started_even_without_a_shipment_row(self):
		evidence = build_evidence(_order([_unit("1", integration_status=SHIPPED)]))

		self.assertEqual(evidence["fulfillment_started"], {"sku:SKU-A": True})
		self.assertIs(evidence["lifecycle_permits_change"], False)
		self.assertTrue(evidence["lifecycle_reason"])

	def test_either_source_means_started_when_shipped_and_shipment_tables_disagree(self):
		# Unit 1 SHIPPED but only in a Cancelled shipment; unit 2 ACKNOWLEDGED but in a Created shipment.
		evidence = build_evidence(
			_order(
				[_unit("1", sku="SKU-A", integration_status=SHIPPED), _unit("2", sku="SKU-B")],
				[("S-1", "Cancelled"), ("S-2", "Created")],
				[("S-1", "1"), ("S-2", "2")],
			)
		)

		self.assertEqual(evidence["fulfillment_started"], {"sku:SKU-A": True, "sku:SKU-B": True})

	def test_mixed_unit_line_counts_as_started(self):
		evidence = build_evidence(
			_order([_unit("1"), _unit("2", integration_status=SHIPPED), _unit("3", sku="SKU-B")])
		)

		self.assertEqual(evidence["fulfillment_started"], {"sku:SKU-A": True, "sku:SKU-B": False})
		self.assertIs(evidence["lifecycle_permits_change"], True)

	def test_out_of_stock_and_cancelled_with_no_eligible_unit_left_is_full_cancellation(self):
		evidence = build_evidence(
			_order(
				[
					_unit("1", mp_status=CANCELLED, integration_status=OUT_OF_STOCK),
					_unit("2", mp_status=CANCELLED),
				]
			)
		)

		self.assertEqual(evidence["cancellation"], "full")
		self.assertNotIn("cancelled_after_fulfillment", evidence)

	def test_one_remaining_confirmed_unit_is_not_a_full_cancellation(self):
		evidence = build_evidence(
			_order([_unit("1", mp_status=CANCELLED), _unit("2", integration_status=OUT_OF_STOCK), _unit("3")])
		)

		self.assertNotIn("cancellation", evidence)

	def test_cancellation_after_a_unit_started_is_cancelled_after_fulfillment(self):
		for kwargs, shipments, shipment_units in (
			({"integration_status": SHIPPED}, (), ()),
			({}, [("S-1", "Created")], [("S-1", "1")]),
		):
			with self.subTest(shipments=bool(shipments)):
				evidence = build_evidence(
					_order([_unit("1", mp_status=CANCELLED, **kwargs)], shipments, shipment_units)
				)

				self.assertEqual(evidence["cancellation"], "full")
				self.assertIs(evidence["cancelled_after_fulfillment"], True)

	def test_unspecified_or_unknown_status_is_an_unknown_lifecycle_not_a_guess(self):
		for unit in (
			_unit("1", integration_status=UNSPECIFIED),
			_unit("1", mp_status="MP_ITEM_STATUS_UNSPECIFIED"),
			_unit("1", integration_status="INTEGRATION_ITEM_STATUS_SOMETHING_NEW"),
		):
			with self.subTest(unit=unit):
				evidence = build_evidence(_order([unit]))

				self.assertIsNone(evidence["lifecycle_permits_change"])
				self.assertTrue(evidence["lifecycle_reason"])
				self.assertNotIn("cancellation", evidence)

	def test_unknown_status_wins_over_an_apparent_cancellation(self):
		evidence = build_evidence(
			_order([_unit("1", mp_status=CANCELLED), _unit("2", integration_status=UNSPECIFIED)])
		)

		self.assertNotIn("cancellation", evidence)
		self.assertIsNone(evidence["lifecycle_permits_change"])

	def test_missing_unit_data_is_an_unknown_lifecycle(self):
		for payload in (None, {}, {"items": []}):
			with self.subTest(payload=payload):
				order = _order([])
				order.latest_payload = payload

				evidence = build_evidence(order)

				self.assertIsNone(evidence["lifecycle_permits_change"])
				self.assertNotIn("fulfillment_started", evidence)

	def test_noon_never_produces_return_or_refund_evidence_yet(self):
		for units in ([_unit("1")], [_unit("1", mp_status=CANCELLED, integration_status=SHIPPED)]):
			evidence = build_evidence(_order(units))

			self.assertNotIn("return", evidence)
			self.assertNotIn("refund", evidence)

	def test_adapter_does_not_mutate_the_order(self):
		order = _order([_unit("1")], [("S-1", "Created")], [("S-1", "1")])
		before = repr(vars(order))

		build_evidence(order)

		self.assertEqual(repr(vars(order)), before)


class UnitTestNoonEvidenceThroughThePlanner(UnitTestCase):
	"""Adapter output consumed by ``plan()``, with hand-written snapshots and ERP state."""

	def _snapshot(self, qty):
		return {
			"company": "C",
			"customer": "Cust",
			"selling_price_list": "PL",
			"transaction_date": "2026-10-01",
			"delivery_date": None,
			"items": [
				{
					"external_item_id": "sku:SKU-A",
					"item_code": "ITEM-A",
					"qty": qty,
					"uom": "Nos",
					"warehouse": "W",
					"fulfilled_total_price": qty * 10.0,
					"effective_rate": 10.0,
					"sales_order_item": "SOI-1",
				}
			],
		}

	def _erp_state(self, delivered_qty=0):
		return {
			"docstatus": 1,
			"status": "To Deliver and Bill",
			"lines": [
				{
					"sales_order_item": "SOI-1",
					"item_code": "ITEM-A",
					"uom": "Nos",
					"warehouse": "W",
					"qty": 2,
					"amount": 20.0,
					"delivered_qty": delivered_qty,
					"billed_amt": 0,
				}
			],
			"precision": {},
		}

	def test_changed_line_that_started_fulfillment_needs_attention(self):
		evidence = build_evidence(_order([_unit("1"), _unit("2")], [("S-1", "Created")], [("S-1", "1")]))

		result = plan(self._snapshot(2), self._snapshot(1), evidence, self._erp_state())

		self.assertEqual(result["outcome"], NEEDS_ATTENTION)

	def test_unchanged_started_order_is_no_action(self):
		evidence = build_evidence(_order([_unit("1", integration_status=SHIPPED), _unit("2")]))

		result = plan(self._snapshot(2), self._snapshot(2), evidence, self._erp_state())

		self.assertEqual(result["outcome"], NO_ACTION)

	def test_full_cancellation_before_fulfillment_cancels_the_order(self):
		evidence = build_evidence(
			_order([_unit("1", mp_status=CANCELLED), _unit("2", integration_status=OUT_OF_STOCK)])
		)

		result = plan(self._snapshot(2), self._snapshot(2), evidence, self._erp_state())

		self.assertEqual(result["outcome"], CANCEL_ORDER)

	def test_cancellation_after_fulfillment_with_delivery_is_a_compensating_transaction(self):
		evidence = build_evidence(_order([_unit("1", mp_status=CANCELLED, integration_status=SHIPPED)]))

		result = plan(self._snapshot(2), self._snapshot(2), evidence, self._erp_state(delivered_qty=2))

		self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)

	def test_no_return_evidence_comes_from_noon_yet(self):
		evidence = build_evidence(_order([_unit("1", mp_status=CANCELLED, integration_status=SHIPPED)]))

		self.assertNotIn("return", evidence)
