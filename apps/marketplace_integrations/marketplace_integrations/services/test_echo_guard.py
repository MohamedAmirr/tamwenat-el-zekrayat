# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Expected-change marker (#364): hashes per platform, the consume/clear rules and the wiring.

The DB class needs no Company test records: it creates a Marketplace Account and Marketplace
Order on the stand-in connection and mocks the Sales Order sync.  It deletes what it creates
in ``tearDown`` (that commits).
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from marketplace_integrations.api.talabat_orders import _item_update_payload
from marketplace_integrations.integrations.noon import expected_change as noon_echo
from marketplace_integrations.services import echo_guard

# Same structural stand-in connection the other tests use.
STAND_IN_CONNECTION_DOCTYPE = "UOM"
STAND_IN_CONNECTION = "Nos"


def _sent_item(sku, status="IN_CART", quantity=2, pricing_type="UNIT", **pricing):
	return {
		"_id": f"id-{sku}",
		"sku": sku,
		"status": status,
		"pricing": {"pricing_type": pricing_type, "quantity": quantity, **pricing},
	}


def _incoming_item(sku, status="IN_CART", quantity=2, pricing_type="UNIT", **fields):
	return {
		"external_sku": sku,
		"external_status": status,
		"pricing_type": pricing_type,
		"fulfilled_quantity": quantity,
		**fields,
	}


def _incoming(status, items=(), reason=None):
	return {"external_status": status, "cancellation_reason": reason, "items": list(items)}


class UnitTestEchoGuardHashes(UnitTestCase):
	def test_talabat_update_cart_matches_when_the_echo_has_the_same_items(self):
		sent = echo_guard.talabat_sent_hash(
			{"status": "UPDATE_CART", "items": [_sent_item("A"), _sent_item("B", "NOT_FOUND")]}
		)
		incoming = _incoming(
			"UPDATE_CART", [_incoming_item("B", "NOT_FOUND", None), _incoming_item("A")]
		)
		self.assertEqual(sent, echo_guard.talabat_incoming_hash(incoming))

	def test_talabat_addition_and_in_cart_are_the_same_class(self):
		sent = echo_guard.talabat_sent_hash(
			{"status": "UPDATE_CART", "items": [_sent_item("A"), _sent_item("NEW", "ADDITION", 1)]}
		)
		for echoed in ("ADDITION", "IN_CART"):
			incoming = _incoming("UPDATE_CART", [_incoming_item("A"), _incoming_item("NEW", echoed, 1)])
			self.assertEqual(sent, echo_guard.talabat_incoming_hash(incoming), echoed)

	def test_talabat_update_cart_differs_on_quantity_status_or_missing_item(self):
		sent = echo_guard.talabat_sent_hash({"status": "UPDATE_CART", "items": [_sent_item("A"), _sent_item("B")]})
		for incoming in (
			_incoming("UPDATE_CART", [_incoming_item("A", quantity=1), _incoming_item("B")]),
			_incoming("UPDATE_CART", [_incoming_item("A"), _incoming_item("B", "NOT_FOUND", None)]),
			_incoming("UPDATE_CART", [_incoming_item("A")]),
			_incoming("RECEIVED", [_incoming_item("A"), _incoming_item("B")]),
		):
			self.assertNotEqual(sent, echo_guard.talabat_incoming_hash(incoming))

	def test_talabat_kg_items_compare_weight_and_ignore_weighted_pieces(self):
		sent = echo_guard.talabat_sent_hash(
			{
				"status": "UPDATE_CART",
				"items": [_sent_item("K", pricing_type="KG", quantity=None, weight=1.5, weighted_pieces=0)],
			}
		)
		same = _incoming_item("K", pricing_type="KG", quantity=None, fulfilled_weight=1.5)
		with_pieces = _incoming_item(
			"K", pricing_type="KG", quantity=None, fulfilled_weight=1.5, fulfilled_weighted_pieces=3
		)
		other = _incoming_item("K", pricing_type="KG", quantity=None, fulfilled_weight=1.0)
		self.assertEqual(sent, echo_guard.talabat_incoming_hash(_incoming("UPDATE_CART", [same])))
		self.assertEqual(sent, echo_guard.talabat_incoming_hash(_incoming("UPDATE_CART", [with_pieces])))
		self.assertNotEqual(sent, echo_guard.talabat_incoming_hash(_incoming("UPDATE_CART", [other])))

	def test_talabat_hash_matches_the_payload_the_ui_builds_for_a_kg_line(self):
		row = SimpleNamespace(
			external_item_id="id-K",
			external_sku="K",
			external_status="IN_CART",
			pricing_type="KG",
			fulfilled_weight=1.5,
			fulfilled_weighted_pieces=None,
			fulfilled_quantity=None,
			fulfilled_unit_price=10,
			replaced_external_item_id=None,
		)
		sent = echo_guard.talabat_sent_hash({"status": "UPDATE_CART", "items": [_item_update_payload(row)]})
		# Talabat echoes the weight; it may omit the weighted pieces the UI always sends.
		echoed = _incoming_item("K", pricing_type="KG", quantity=None, fulfilled_weight=1.5)
		self.assertEqual(sent, echo_guard.talabat_incoming_hash(_incoming("UPDATE_CART", [echoed])))

	def test_a_real_shaped_talabat_echo_payload_matches_the_sent_hash_through_the_mapper(self):
		from marketplace_integrations.integrations.talabat.order_mapper import normalize_order

		sent = echo_guard.talabat_sent_hash(
			{
				"status": "UPDATE_CART",
				"items": [_sent_item("A", quantity=2), _sent_item("B", "NOT_FOUND"), _sent_item("N", "ADDITION", 1)],
			}
		)
		raw = {
			"order_id": "order-echo-1",
			"external_order_id": "MERCH-1",
			"status": "UPDATE_CART",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {},
			"cancellation": {},
			"items": [
				{
					"_id": "id-A",
					"sku": "A",
					"status": "IN_CART",
					"original_pricing": {"pricing_type": "UNIT", "quantity": 3},
					"pricing": {"pricing_type": "UNIT", "quantity": 2, "unit_price": 5, "total_price": 10},
				},
				{
					"_id": "id-B",
					"sku": "B",
					"status": "NOT_FOUND",
					"original_pricing": {"pricing_type": "UNIT", "quantity": 1},
					"pricing": {"pricing_type": "UNIT"},
				},
				{
					"_id": "id-N",
					"sku": "N",
					"status": "IN_CART",
					"pricing": {"pricing_type": "UNIT", "quantity": 1, "unit_price": 7, "total_price": 7},
				},
			],
		}
		self.assertEqual(sent, echo_guard.talabat_incoming_hash(normalize_order(raw)))

		cancelled = dict(raw, status="CANCELLED", cancellation={"reason": "CLOSED", "cancelled_by": "VENDOR"})
		cancel_sent = echo_guard.talabat_sent_hash({"status": "CANCELLED", "cancellation": {"reason": "CLOSED"}})
		self.assertEqual(cancel_sent, echo_guard.talabat_incoming_hash(normalize_order(cancelled)))

	def test_talabat_cancel_compares_status_and_reason_only(self):
		sent = echo_guard.talabat_sent_hash(
			{"status": "CANCELLED", "cancellation": {"reason": "CLOSED"}, "items": [_sent_item("A")]}
		)
		self.assertEqual(sent, echo_guard.talabat_incoming_hash(_incoming("CANCELLED", [], "CLOSED")))
		self.assertEqual(
			sent, echo_guard.talabat_incoming_hash(_incoming("CANCELLED", [_incoming_item("A", "NOT_FOUND")], "CLOSED"))
		)
		self.assertNotEqual(sent, echo_guard.talabat_incoming_hash(_incoming("CANCELLED", [], "TOO_BUSY")))
		self.assertNotEqual(sent, echo_guard.talabat_incoming_hash(_incoming("RECEIVED", [], None)))

	def test_talabat_update_cart_with_every_item_not_found_expects_the_automatic_cancel(self):
		sent = echo_guard.talabat_sent_hash(
			{"status": "UPDATE_CART", "items": [_sent_item("A", "NOT_FOUND"), _sent_item("B", "NOT_FOUND")]}
		)
		self.assertEqual(sent, echo_guard.talabat_incoming_hash(_incoming("CANCELLED", [], "ITEM_UNAVAILABLE")))
		self.assertNotEqual(sent, echo_guard.talabat_incoming_hash(_incoming("CANCELLED", [], "CLOSED")))

	def test_talabat_updates_not_covered_have_no_hash(self):
		self.assertIsNone(echo_guard.talabat_sent_hash({"status": "DISPATCHED", "items": []}))
		no_sku = {"_id": "x", "status": "IN_CART", "pricing": {"pricing_type": "UNIT", "quantity": 1}}
		self.assertIsNone(echo_guard.talabat_sent_hash({"status": "UPDATE_CART", "items": [no_sku]}))

	def test_noon_hash_matches_only_when_every_sent_unit_is_out_of_stock(self):
		raw = {
			"items": [
				{"mp_item_nr": 1, "integration_status": "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"},
				{"mp_item_nr": 2, "integration_status": "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"},
				{"mp_item_nr": 3, "integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED"},
			]
		}
		sent = noon_echo.sent_hash(["2", "1"])
		self.assertEqual(sent, noon_echo.incoming_hash(raw, ["1", "2"]))
		# A unit we did not send may be in any state.
		self.assertNotEqual(noon_echo.sent_hash(["1", "3"]), noon_echo.incoming_hash(raw, ["1", "3"]))
		raw["items"][1]["integration_status"] = "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED"
		self.assertNotEqual(sent, noon_echo.incoming_hash(raw, ["1", "2"]))

	def test_noon_hash_treats_a_unit_missing_from_the_payload_as_different(self):
		raw = {"items": [{"mp_item_nr": 1, "integration_status": "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"}]}
		self.assertNotEqual(noon_echo.sent_hash(["1", "2"]), noon_echo.incoming_hash(raw, ["1", "2"]))


class IntegrationTestEchoGuard(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Echo Guard {self.suffix}",
				"platform": "Talabat",
				"external_account_id": f"echo-guard-{self.suffix}",
				"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
				"connection": STAND_IN_CONNECTION,
			}
		).insert()
		self.account = account.name
		self.external_order_id = f"echo-order-{self.suffix}"
		order = frappe.get_doc(
			{
				"doctype": "Marketplace Order",
				"marketplace_account": self.account,
				"platform": "Talabat",
				"external_order_id": self.external_order_id,
			}
		)
		order.flags.skip_sync_status_update = True
		order.insert()
		self.order = order.name

	def tearDown(self):
		frappe.db.delete("Marketplace Order", {"marketplace_account": self.account})
		frappe.db.delete("Marketplace Account", {"name": self.account})
		frappe.db.commit()

	def marker(self):
		return frappe.db.get_value(
			"Marketplace Order", self.order, ["expected_change_hash", "expected_change_scope"], as_dict=True
		)

	def consume(self, incoming):
		return echo_guard.consume_expected_change(
			self.account, self.external_order_id, lambda scope: incoming
		)

	def test_marker_then_identical_incoming_state_is_an_echo_and_clears_the_marker(self):
		echo_guard.record_expected_change(self.order, "h" * 64, '["1"]')

		self.assertTrue(self.consume("h" * 64))

		self.assertFalse(self.marker().expected_change_hash)
		self.assertFalse(self.marker().expected_change_scope)

	def test_marker_then_different_incoming_state_is_a_real_change_and_clears_the_marker(self):
		echo_guard.record_expected_change(self.order, "h" * 64, None)

		self.assertFalse(self.consume("x" * 64))

		self.assertFalse(self.marker().expected_change_hash)

	def test_no_marker_is_not_an_echo_and_changes_nothing(self):
		self.assertFalse(self.consume("h" * 64))
		self.assertFalse(echo_guard.consume_expected_change(self.account, "unknown-order", lambda scope: "h"))
		self.assertFalse(self.marker().expected_change_hash)

	def test_scope_is_passed_to_the_incoming_hash(self):
		echo_guard.record_expected_change(self.order, "h" * 64, '["7"]')
		seen = []
		echo_guard.consume_expected_change(
			self.account, self.external_order_id, lambda scope: seen.append(scope)
		)
		self.assertEqual(seen, ['["7"]'])

	def test_a_stale_copy_cannot_blank_the_marker(self):
		stale = frappe.get_doc("Marketplace Order", self.order)
		echo_guard.record_expected_change(self.order, "h" * 64, '["1"]')
		stale.flags.skip_sync_status_update = True

		stale.save()

		self.assertEqual(self.marker().expected_change_hash, "h" * 64)

	def upsert_with_echo_guard(self, incoming, linked=True):
		normalized = {"external_order_id": self.external_order_id, "items": [], "latest_payload": {}}
		order = frappe._dict(name=self.order)
		if linked:
			frappe.db.set_value("Marketplace Order", self.order, "erpnext_sales_order", "SO-ECHO-FIXTURE")
		with (
			patch.object(echo_guard, "upsert_marketplace_order", return_value=order) as upsert,
			patch.object(echo_guard, "finish_echo") as finish,
		):
			result = echo_guard.upsert_checking_echo(
				self.account, normalized, lambda scope: incoming, "Talabat Webhook", "fingerprint"
			)
		return result, upsert, finish

	def test_echo_of_a_linked_order_skips_the_replan_and_queues_the_sales_order_sync(self):
		echo_guard.record_expected_change(self.order, "h" * 64, None)

		(_order, echo), upsert, finish = self.upsert_with_echo_guard("h" * 64)

		self.assertTrue(echo)
		self.assertTrue(upsert.call_args.kwargs["skip_status_replan"])
		finish.assert_called_once_with(self.order, "fingerprint", "Talabat Webhook")
		self.assertFalse(self.marker().expected_change_hash)

	def test_a_different_incoming_state_takes_the_normal_path_and_clears_the_marker(self):
		echo_guard.record_expected_change(self.order, "h" * 64, None)

		(_order, echo), upsert, finish = self.upsert_with_echo_guard("x" * 64)

		self.assertFalse(echo)
		self.assertFalse(upsert.call_args.kwargs["skip_status_replan"])
		finish.assert_not_called()
		self.assertFalse(self.marker().expected_change_hash)

	def test_no_marker_takes_the_normal_path(self):
		(_order, echo), upsert, finish = self.upsert_with_echo_guard("h" * 64)

		self.assertFalse(echo)
		self.assertFalse(upsert.call_args.kwargs["skip_status_replan"])
		finish.assert_not_called()

	def test_an_unlinked_order_takes_the_normal_path_but_clears_the_marker(self):
		echo_guard.record_expected_change(self.order, "h" * 64, None)

		(_order, echo), upsert, finish = self.upsert_with_echo_guard("h" * 64, linked=False)

		self.assertFalse(echo)
		self.assertFalse(upsert.call_args.kwargs["skip_status_replan"])
		finish.assert_not_called()
		self.assertFalse(self.marker().expected_change_hash)

	def test_a_stale_copy_cannot_bring_back_a_cleared_marker(self):
		echo_guard.record_expected_change(self.order, "h" * 64, '["1"]')
		stale = frappe.get_doc("Marketplace Order", self.order)
		echo_guard.clear_expected_change(self.order)
		stale.flags.skip_sync_status_update = True

		stale.save()

		self.assertFalse(self.marker().expected_change_hash)
		self.assertFalse(self.marker().expected_change_scope)

	def test_talabat_bump_flag_bumps_a_new_order_but_not_an_echo_or_unchanged_order(self):
		normalized = {"external_order_id": self.external_order_id, "items": [], "latest_payload": {}}
		order = frappe._dict(name=self.order)
		with (
			patch.object(echo_guard, "upsert_marketplace_order", return_value=order),
			patch("marketplace_integrations.services.sales_order_sync_debounce.bump_sync_debounce") as bump,
			patch(
				"marketplace_integrations.services.sales_order_sync_debounce.order_evidence_fingerprint",
				return_value="same",
			),
			patch("marketplace_integrations.services.sales_order_sync_debounce.existing_order_fingerprint"),
		):
			# A new order (no fingerprint before): bumped.
			echo_guard.upsert_talabat_order(self.account, normalized, "Manual", bump=True)
			bump.assert_called_once_with(self.order, "Manual")
			bump.reset_mock()
			# An unchanged existing order: not bumped.
			echo_guard.upsert_talabat_order(self.account, normalized, "Manual", fingerprint_before="same", bump=True)
			bump.assert_not_called()
			# Changed: bumped.
			echo_guard.upsert_talabat_order(self.account, normalized, "Manual", fingerprint_before="old", bump=True)
			bump.assert_called_once()

	def test_record_talabat_update_stores_the_marker_and_does_not_commit(self):
		payload = {"status": "UPDATE_CART", "items": [_sent_item("A")]}
		with patch("marketplace_integrations.services.echo_guard.frappe.db.commit") as commit:
			echo_guard.record_talabat_update(self.order, payload)

		commit.assert_not_called()
		self.assertEqual(self.marker().expected_change_hash, echo_guard.talabat_sent_hash(payload))

	def test_record_talabat_update_leaves_an_uncovered_update_alone(self):
		echo_guard.record_expected_change(self.order, "h" * 64, None)
		echo_guard.record_talabat_update(self.order, {"status": "DISPATCHED", "items": []})

		self.assertEqual(self.marker().expected_change_hash, "h" * 64)

	def test_noon_marker_stores_the_sent_units_and_matches_the_echo(self):
		noon_echo.record_out_of_stock(self.order, ["2", "1", "2"])
		self.assertEqual(frappe.parse_json(self.marker().expected_change_scope), ["1", "2"])
		raw = {
			"items": [
				{"mp_item_nr": 1, "integration_status": "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"},
				{"mp_item_nr": 2, "integration_status": "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"},
			]
		}

		self.assertTrue(
			echo_guard.consume_expected_change(
				self.account, self.external_order_id, noon_echo.incoming_matcher(raw)
			)
		)

	def test_noon_marker_does_not_match_when_a_unit_is_still_acknowledged(self):
		noon_echo.record_out_of_stock(self.order, ["1"])
		raw = {"items": [{"mp_item_nr": 1, "integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED"}]}

		self.assertFalse(
			echo_guard.consume_expected_change(
				self.account, self.external_order_id, noon_echo.incoming_matcher(raw)
			)
		)
		self.assertFalse(self.marker().expected_change_hash)

	def test_finish_echo_does_nothing_when_the_evidence_did_not_change(self):
		with (
			patch(
				"marketplace_integrations.services.sales_order_sync_debounce.order_evidence_fingerprint",
				return_value="same",
			),
			patch("marketplace_integrations.services.echo_guard.frappe.enqueue") as enqueue,
		):
			echo_guard.finish_echo(self.order, "same", "Talabat Webhook")
		enqueue.assert_not_called()

	def test_finish_echo_falls_back_to_the_debounce_when_a_sync_job_is_already_queued(self):
		with (
			patch(
				"marketplace_integrations.services.sales_order_sync_debounce.order_evidence_fingerprint",
				return_value="after",
			),
			patch("marketplace_integrations.services.echo_guard.is_job_enqueued", return_value=True),
			patch("marketplace_integrations.services.sales_order_sync_debounce.bump_sync_debounce") as bump,
			patch("marketplace_integrations.services.echo_guard.frappe.enqueue") as enqueue,
		):
			echo_guard.finish_echo(self.order, "before", "Talabat Webhook")

		enqueue.assert_not_called()
		bump.assert_called_once_with(self.order, "Talabat Webhook")

	def test_run_echo_sync_returns_quietly_when_the_order_was_deleted(self):
		with (
			patch("marketplace_integrations.services.sales_order_sync.sync_sales_order") as sync,
			patch("marketplace_integrations.services.echo_guard.frappe.log_error") as log_error,
		):
			echo_guard.run_echo_sync("MKTORD-DOES-NOT-EXIST", "Talabat Webhook")

		sync.assert_not_called()
		log_error.assert_not_called()

	def test_finish_echo_queues_the_sync_after_commit_and_never_runs_it_inline(self):
		with (
			patch(
				"marketplace_integrations.services.sales_order_sync_debounce.order_evidence_fingerprint",
				return_value="after",
			),
			patch("marketplace_integrations.services.sales_order_sync.sync_sales_order") as sync,
			patch("marketplace_integrations.services.echo_guard.is_job_enqueued", return_value=False),
			patch("marketplace_integrations.services.echo_guard.frappe.enqueue") as enqueue,
		):
			echo_guard.finish_echo(self.order, "before", "Talabat Webhook")

		sync.assert_not_called()
		kwargs = enqueue.call_args.kwargs
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["job_id"], f"marketplace-echo-sync-{self.order}")
		self.assertEqual(kwargs["marketplace_order"], self.order)
