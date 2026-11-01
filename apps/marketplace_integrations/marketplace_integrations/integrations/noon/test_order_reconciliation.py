# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Contract fixtures for the Noon evidence adapter (#178 part 3).

Hand-written, SimpleNamespace-based: ``build_evidence()`` is pure, so no DB is
involved.  Unit statuses use Noon's documented verbose enum spellings, exactly as
they appear in ``latest_payload.items[]``.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from marketplace_integrations.integrations.noon.order_reconciliation import (
	_has_noon_return,
)
from marketplace_integrations.integrations.noon.order_reconciliation import (
	build_evidence as _build_evidence,
)
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
HAS_RETURN = "marketplace_integrations.integrations.noon.order_reconciliation._has_noon_return"


STARTED_UNITS = "marketplace_integrations.integrations.noon.order_reconciliation._started_shipment_mp_item_nrs"


def build_evidence(order, has_return=False):
	"""Pure-logic tests never touch the DB: the one Noon Return lookup and the units the order's Noon
	Delivery Notes started (``order.started``) are stubbed."""
	with (
		patch(HAS_RETURN, return_value=has_return),
		patch(STARTED_UNITS, return_value=set(getattr(order, "started", ()))),
	):
		return _build_evidence(order)


def _unit(mp_item_nr, sku="SKU-A", mp_status=CONFIRMED, integration_status=ACKNOWLEDGED):
	return {
		"mp_item_nr": mp_item_nr,
		"partner_sku": sku,
		"mp_status": mp_status,
		"integration_status": integration_status,
	}


def _order(units, started=()):
	"""``started``: the ``mp_item_nr`` of the units a live Noon Delivery Note of the order started."""
	return SimpleNamespace(
		name="MO-164",
		platform="Noon",
		latest_payload={"items": units},
		started=list(started),
	)


class UnitTestNoonOrderReconciliation(UnitTestCase):
	def test_before_fulfillment_nothing_started_and_changes_are_permitted(self):
		evidence = build_evidence(_order([_unit("1"), _unit("2")]))

		self.assertEqual(evidence["fulfillment_started"], {"sku:SKU-A": False})
		self.assertIs(evidence["lifecycle_permits_change"], True)
		self.assertNotIn("cancellation", evidence)
		self.assertEqual(evidence["source"], "marketplace")

	def test_unit_of_a_live_noon_delivery_note_has_started_fulfillment(self):
		evidence = build_evidence(_order([_unit("1"), _unit("2", sku="SKU-B")], started=["1"]))

		self.assertEqual(evidence["fulfillment_started"], {"sku:SKU-A": True, "sku:SKU-B": False})
		self.assertIs(evidence["lifecycle_permits_change"], True)

	def test_the_started_units_are_those_of_the_live_noon_notes_of_the_order(self):
		from marketplace_integrations.integrations.noon import order_reconciliation

		adapter = "marketplace_integrations.integrations.noon.delivery_note_shipment.started_units_of_order"
		with patch(adapter, return_value={"1"}) as started:
			self.assertEqual(order_reconciliation._started_shipment_mp_item_nrs(_order([])), {"1"})
		started.assert_called_once_with("MO-164")

	def test_shipped_unit_has_started_even_without_a_delivery_note(self):
		evidence = build_evidence(_order([_unit("1", integration_status=SHIPPED)]))

		self.assertEqual(evidence["fulfillment_started"], {"sku:SKU-A": True})
		self.assertIs(evidence["lifecycle_permits_change"], False)
		self.assertTrue(evidence["lifecycle_reason"])

	def test_either_source_means_started_when_shipped_and_the_delivery_notes_disagree(self):
		# Unit 1 SHIPPED at Noon but on no live note; unit 2 ACKNOWLEDGED but on a live note.
		evidence = build_evidence(
			_order(
				[_unit("1", sku="SKU-A", integration_status=SHIPPED), _unit("2", sku="SKU-B")],
				started=["2"],
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
		for kwargs, started in (
			({"integration_status": SHIPPED}, ()),
			({}, ["1"]),
		):
			with self.subTest(started=bool(started)):
				evidence = build_evidence(_order([_unit("1", mp_status=CANCELLED, **kwargs)], started))

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

	def test_noon_never_produces_refund_evidence(self):
		for units in ([_unit("1")], [_unit("1", mp_status=CANCELLED, integration_status=SHIPPED)]):
			evidence = build_evidence(_order(units))

			self.assertNotIn("refund", evidence)

	def test_order_without_a_noon_return_has_no_return_evidence(self):
		for units in ([_unit("1")], [_unit("1", mp_status=CANCELLED)], []):
			with self.subTest(units=len(units)):
				self.assertNotIn("return", build_evidence(_order(units)))

	def test_a_linked_noon_return_is_return_evidence_whatever_the_unit_state(self):
		for units in (
			[_unit("1")],
			[_unit("1", mp_status=CANCELLED)],
			[_unit("1", integration_status=UNSPECIFIED)],
			[],
		):
			with self.subTest(units=len(units)):
				self.assertIs(build_evidence(_order(units), has_return=True)["return"], True)

	def test_adapter_does_not_mutate_the_order(self):
		order = _order([_unit("1")], started=["1"])
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
		evidence = build_evidence(_order([_unit("1"), _unit("2")], started=["1"]))

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

	def test_a_noon_return_on_a_synced_order_is_a_compensating_transaction(self):
		evidence = build_evidence(_order([_unit("1")]), has_return=True)

		# Nothing delivered or billed: return evidence still always needs a compensating
		# transaction, and the plan never edits the Sales Order.
		result = plan(self._snapshot(2), self._snapshot(2), evidence, self._erp_state())

		self.assertEqual(result["outcome"], UNSUPPORTED_COMPENSATING_TRANSACTION)

	def test_without_a_noon_return_the_same_order_is_no_action(self):
		result = plan(
			self._snapshot(2), self._snapshot(2), build_evidence(_order([_unit("1")])), self._erp_state()
		)

		self.assertEqual(result["outcome"], NO_ACTION)


class IntegrationTestNoonReturnLookup(IntegrationTestCase):
	"""#262: return evidence lasts only while a linked Noon Return is unresolved."""

	ORDER = SimpleNamespace(name="MO-164")

	def _evidence(self, returns, submitted=()):
		"""``submitted`` is the set of (doctype, name) pairs with docstatus 1."""

		def docstatus(doctype, name, fieldname):
			return 1 if (doctype, name) in submitted else 0

		with patch("frappe.get_all", return_value=returns) as get_all:
			with patch("frappe.db.get_value", side_effect=docstatus):
				result = _has_noon_return(self.ORDER)
		get_all.assert_called_once()
		self.assertEqual(get_all.call_args.kwargs["filters"], {"marketplace_order": "MO-164"})
		return result

	def _row(self, outcome=None, sales_return=None, credit_note=None):
		return SimpleNamespace(inspection_outcome=outcome, sales_return=sales_return, credit_note=credit_note)

	def test_no_linked_returns_means_no_evidence(self):
		self.assertFalse(self._evidence([]))
		self.assertFalse(_has_noon_return(SimpleNamespace()))

	def test_an_uninspected_return_is_unresolved(self):
		self.assertTrue(self._evidence([self._row()]))

	def test_sellable_resolves_only_with_a_submitted_return_and_credit_note(self):
		row = self._row("Sellable", "DN-1", "SI-1")
		self.assertTrue(self._evidence([row]))
		self.assertTrue(self._evidence([row], {("Delivery Note", "DN-1")}))
		self.assertTrue(self._evidence([row], {("Sales Invoice", "SI-1")}))
		self.assertFalse(self._evidence([row], {("Delivery Note", "DN-1"), ("Sales Invoice", "SI-1")}))

	def test_damaged_resolves_with_only_a_submitted_credit_note(self):
		row = self._row("Damaged", None, "SI-2")
		self.assertTrue(self._evidence([row]))
		self.assertFalse(self._evidence([row], {("Sales Invoice", "SI-2")}))

	def test_any_unresolved_return_keeps_the_evidence(self):
		resolved = self._row("Damaged", None, "SI-2")
		self.assertTrue(self._evidence([resolved, self._row()], {("Sales Invoice", "SI-2")}))
