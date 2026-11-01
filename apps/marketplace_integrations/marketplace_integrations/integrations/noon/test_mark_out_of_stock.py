# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Mark as Out of Stock on a Sales Order item row (#416): the server refusals (a Delivery Note, a non-FBPI
warehouse), the reservation and job, the worker's outcomes (success, definite failure, Unknown then Retry that
asks Noon first and never sends UpdateOrder twice, Noon confirmed but the local step failed) and the
Integration Request log. Noon HTTP is mocked, never live."""

import json
from unittest.mock import MagicMock, patch

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

from marketplace_integrations.integrations.noon import delivery_note_shipment, expected_change, fulfillment
from marketplace_integrations.integrations.noon.test_fulfillment import (
	IntegrationTestNoonFulfillmentBase,
	_confirmed_acknowledged,
)
from marketplace_integrations.patches.v0_0 import create_noon_transporter_supplier
from marketplace_integrations.tests.handover_fixture import SubmittedOrderFixture

_MODULE = "marketplace_integrations.integrations.noon.fulfillment"
_CLIENT = f"{_MODULE}.NoonClient"
_GET_ORDER = f"{_MODULE}.get_fbpi_order"
_IMPORT = f"{_MODULE}.import_order"
_ENQUEUE = f"{_MODULE}.frappe.enqueue"
_SYNC = "marketplace_integrations.services.sales_order_sync.sync_sales_order"
_OOS = "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"


def _response(status=200, message=""):
	response = MagicMock(status_code=status)
	response.message = message
	return response


def _noon_order(fbpi_order_nr, *units):
	"""GetFbpiOrder answer: ``units`` are (mp_item_nr, integration_status)."""
	return {
		"fbpi_order_nr": fbpi_order_nr,
		"items": [{**_confirmed_acknowledged(nr), "integration_status": status} for nr, status in units],
	}


class IntegrationTestNoonMarkOutOfStock(IntegrationTestNoonFulfillmentBase):
	def setUp(self):
		super().setUp()
		self.fixtures = []
		self._fbpi_ready_account()
		# a Noon draft note needs the carrier Supplier (the migration patch creates it)
		self.created_supplier = False
		if not delivery_note_shipment.transporter_name():
			create_noon_transporter_supplier.execute()
			self.created_supplier = True
			self.created_supplier_name = delivery_note_shipment.transporter_name()

	def tearDown(self):
		frappe.set_user("Administrator")
		for fixture in self.fixtures:
			for name in fixture.delivery_notes():
				frappe.db.set_value("Delivery Note", name, "custom_integration_status", "Cancelled", update_modified=False)
			frappe.db.delete("Integration Request", {"reference_doctype": "Sales Order", "reference_docname": fixture.name})
			fixture.cleanup()
		frappe.db.commit()
		super().tearDown()
		if self.created_supplier:
			frappe.delete_doc("Supplier", self.created_supplier_name, force=True, ignore_permissions=True)
			frappe.db.commit()

	# ---- helpers -------------------------------------------------------------------------

	def _fbpi_ready_account(self, **overrides):
		values = {
			"noon_fbpi_code": "FBPI",
			"noon_fulfillment_system_code": "FBPI",
			"noon_warehouse_active": 1,
			"noon_warehouse_checked_on": frappe.utils.now_datetime(),
		}
		values.update(overrides)
		frappe.db.set_value("Marketplace Account", self.account.name, values, update_modified=False)
		frappe.db.commit()

	def _order(self, rows, payload_skus):
		fixture = SubmittedOrderFixture(self.account, rows)
		self.fixtures.append(fixture)
		items = [_confirmed_acknowledged(index, sku) for index, sku in enumerate(payload_skus, start=1)]
		frappe.db.set_value(
			"Marketplace Order",
			fixture.marketplace_order.name,
			"latest_payload",
			frappe.as_json({"fbpi_order_nr": fixture.marketplace_order.external_order_id, "items": items}),
		)
		frappe.db.commit()
		return fixture

	def _reserve(self, fixture, units):
		frappe.db.set_value(
			"Marketplace Order", fixture.marketplace_order.name, "noon_pending_oos_units", frappe.as_json(sorted(units))
		)
		frappe.db.commit()

	def _pending(self, fixture):
		order = frappe.get_doc("Marketplace Order", fixture.marketplace_order.name)
		return fulfillment._pending_oos_units(order)

	def _press(self, fixture, sku="SKU-A"):
		"""The request path with the job queue mocked: ``(result, enqueue mock)``."""
		with patch(_ENQUEUE) as enqueue:
			enqueue.return_value = MagicMock()
			result = fulfillment.mark_row_out_of_stock(fixture.name, fixture.row_for(sku).name)
		return result, enqueue

	def _work(
		self, fixture, units, *, check_first=False, send=(), reads=(), sync_result=None, request_id="req-1"
	):
		"""Run the worker with Noon mocked. ``send`` answers UpdateOrder, ``reads`` GetFbpiOrder (an exception
		is raised). Returns the call names in order, and the mocks."""
		calls = []
		send, reads = iter(send), iter(reads)

		def update_order(*_args, **_kwargs):
			calls.append("UpdateOrder")
			answer = next(send)
			if isinstance(answer, Exception):
				raise answer
			return answer

		def get_order(*_args, **_kwargs):
			calls.append("GetFbpiOrder")
			answer = next(reads)
			if isinstance(answer, Exception):
				raise answer
			return answer

		def sync(*_args, **_kwargs):
			calls.append("Sync")
			return sync_result or {"ok": True}

		def import_order(*_args, **_kwargs):
			calls.append("Import")
			return (None, [])

		with (
			patch(_CLIENT) as client_class,
			patch(_GET_ORDER, side_effect=get_order),
			patch(_IMPORT, side_effect=import_order) as import_mock,
			patch(_SYNC, side_effect=sync),
		):
			client = client_class.return_value
			client.request.side_effect = update_order
			client.error_message.side_effect = lambda response: response.message
			fulfillment.run_mark_out_of_stock(
				fixture.marketplace_order.name, units, fixture.name, request_id, check_first
			)
		return calls, client, import_mock

	def _logs(self, fixture):
		return frappe.get_all(
			"Integration Request",
			filters={
				"reference_doctype": "Sales Order",
				"reference_docname": fixture.name,
				"custom_operation_type": "Out of Stock",
			},
			fields=["custom_result", "custom_correlation_id", "custom_http_status", "error", "data"],
			order_by="creation",
		)

	def _result(self, fixture, request_id="req-1"):
		return fulfillment.out_of_stock_result(fixture.name, request_id)

	# ---- refusals ------------------------------------------------------------------------

	def test_refused_for_an_item_in_a_draft_note_and_names_the_note(self):
		fixture = self._order([("SKU-A", 1)], ["SKU-A"])
		note = make_delivery_note(fixture.name)
		note.insert()
		frappe.db.commit()
		row = fixture.row_for("SKU-A")

		self.assertEqual(fulfillment.out_of_stock_rows(fixture.name), [])
		with patch(_ENQUEUE) as enqueue, self.assertRaises(frappe.ValidationError) as caught:
			fulfillment.mark_row_out_of_stock(fixture.name, row.name)
		self.assertIn(note.name, str(caught.exception))
		enqueue.assert_not_called()
		self.assertEqual(self._pending(fixture), set())

	def test_refused_for_an_item_in_a_note_with_a_created_shipment(self):
		fixture = self._order([("SKU-A", 1)], ["SKU-A"])
		note = make_delivery_note(fixture.name)
		note.insert()
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{
				"custom_integration_status": "Created",
				"custom_carrier_request_snapshot": json.dumps({"mp_item_nrs": ["1"]}),
			},
			update_modified=False,
		)
		frappe.db.commit()
		with patch(_ENQUEUE), self.assertRaises(frappe.ValidationError) as caught:
			fulfillment.mark_row_out_of_stock(fixture.name, fixture.row_for("SKU-A").name)
		self.assertIn(note.name, str(caught.exception))

	def test_a_row_without_a_live_note_is_offered(self):
		fixture = self._order([("SKU-A", 1), ("SKU-B", 1)], ["SKU-A", "SKU-B"])
		self.assertEqual(
			sorted(fulfillment.out_of_stock_rows(fixture.name)),
			sorted([fixture.row_for("SKU-A").name, fixture.row_for("SKU-B").name]),
		)

	def test_refused_unless_the_warehouse_is_fbpi(self):
		fixture = self._order([("SKU-A", 1)], ["SKU-A"])
		for overrides in ({"noon_fbpi_code": ""}, {"noon_fulfillment_system_code": "OTHER"}, {"noon_warehouse_active": 0}):
			self._fbpi_ready_account(**overrides)
			with patch(_ENQUEUE) as enqueue, self.assertRaises(frappe.ValidationError):
				fulfillment.mark_row_out_of_stock(fixture.name, fixture.row_for("SKU-A").name)
			enqueue.assert_not_called()
			self.assertEqual(self._pending(fixture), set())
			self._fbpi_ready_account()

	def test_refused_for_a_row_that_is_not_on_the_sales_order(self):
		fixture = self._order([("SKU-A", 1)], ["SKU-A"])
		with patch(_ENQUEUE), self.assertRaises(frappe.ValidationError):
			fulfillment.mark_row_out_of_stock(fixture.name, "no-such-row")

	# ---- request path --------------------------------------------------------------------

	def test_press_reserves_the_units_and_queues_one_job_with_a_stable_id(self):
		fixture = self._order([("SKU-A", 2)], ["SKU-A", "SKU-A"])
		result, enqueue = self._press(fixture)
		self.assertTrue(result["queued"])
		self.assertTrue(result["request_id"])
		self.assertEqual(self._pending(fixture), {"1", "2"})
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["mp_item_nrs"], ["1", "2"])
		self.assertIs(kwargs["check_first"], False)
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["sales_order"], fixture.name)
		first_job_id = kwargs["job_id"]
		# the same press again (the reservation still holds): same job id, and it is a Retry that asks Noon first
		_again, enqueue = self._press(fixture)
		self.assertEqual(enqueue.call_args.kwargs["job_id"], first_job_id)
		self.assertIs(enqueue.call_args.kwargs["check_first"], True)

	def test_enqueue_failure_and_a_deduplicated_job_release_the_reservation(self):
		fixture = self._order([("SKU-A", 1)], ["SKU-A"])
		with patch(_ENQUEUE, side_effect=RuntimeError("queue unavailable")):
			with self.assertRaisesRegex(RuntimeError, "queue unavailable"):
				fulfillment.mark_row_out_of_stock(fixture.name, fixture.row_for("SKU-A").name)
		self.assertEqual(self._pending(fixture), set())
		with patch(_ENQUEUE, return_value=None):
			result = fulfillment.mark_row_out_of_stock(fixture.name, fixture.row_for("SKU-A").name)
		self.assertFalse(result["queued"])
		self.assertEqual(self._pending(fixture), set())

	def test_a_sales_order_writer_without_marketplace_order_rights_can_press_again_and_hit_a_dedup(self):
		fixture = self._order([("SKU-A", 1)], ["SKU-A"])
		email = f"noon-oos-sales-user-{frappe.generate_hash(length=6)}@example.com"
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Noon Oos Test",
				"send_welcome_email": 0,
				"roles": [{"role": "Sales User"}],
			}
		).insert()
		frappe.db.commit()
		try:
			frappe.set_user(email)
			row = fixture.row_for("SKU-A").name
			# The first press and the "already queued" press (the job is deduplicated away) both write the
			# order as the system; the second reserves nothing new and releases nothing.
			with patch(_ENQUEUE, return_value=MagicMock()):
				fulfillment.mark_row_out_of_stock(fixture.name, row)
			with patch(_ENQUEUE, return_value=None):
				result = fulfillment.mark_row_out_of_stock(fixture.name, row)
			self.assertFalse(result["queued"])
			self.assertEqual(self._pending(fixture), {"1"})  # the running job keeps its reservation
			# a first press whose job is dropped releases only what it reserved, also without order rights
			frappe.set_user("Administrator")
			self._reserve(fixture, [])
			frappe.set_user(email)
			with patch(_ENQUEUE, return_value=None):
				fulfillment.mark_row_out_of_stock(fixture.name, row)
			self.assertEqual(self._pending(fixture), set())
		finally:
			frappe.set_user("Administrator")
			frappe.delete_doc("User", user.name, force=True, ignore_permissions=True)
			frappe.db.commit()

	# ---- worker --------------------------------------------------------------------------

	def test_success_sends_update_order_once_then_refreshes_and_runs_the_local_step(self):
		fixture = self._order([("SKU-A", 1)], ["SKU-A"])
		self._reserve(fixture, ["1"])
		nr = fixture.marketplace_order.external_order_id
		calls, client, import_mock = self._work(
			fixture, ["1"], send=[_response(200)], reads=[_noon_order(nr, ("1", _OOS))]
		)
		self.assertEqual(calls, ["UpdateOrder", "GetFbpiOrder", "Import", "Sync"])
		payload = client.request.call_args  # the mock saw the one UpdateOrder
		self.assertEqual(payload.args[0], "POST")
		self.assertEqual(
			payload.kwargs["json"],
			{
				"fbpi_order_nr": nr,
				"items": [{"mp_item_nr": "1", "status": fulfillment.UPDATE_ORDER_ITEM_STATUS_OUT_OF_STOCK}],
			},
		)
		self.assertIs(import_mock.call_args.kwargs["echo_guard"], False)
		self.assertEqual(self._result(fixture)["state"], "done")
		self.assertEqual(self._pending(fixture), set())
		marker = frappe.db.get_value(
			"Marketplace Order", fixture.marketplace_order.name, ["expected_change_hash", "expected_change_scope"], as_dict=True
		)
		self.assertEqual(marker.expected_change_hash, expected_change.sent_hash(["1"]))
		logs = self._logs(fixture)
		self.assertEqual([log.custom_result for log in logs], ["Success", "Success"])
		self.assertTrue(all(log.custom_correlation_id == "req-1" for log in logs))

	def test_definite_failure_keeps_the_row_releases_the_reservation_and_shows_noons_text(self):
		fixture = self._order([("SKU-A", 1)], ["SKU-A"])
		self._reserve(fixture, ["1"])
		calls, _client, _import = self._work(fixture, ["1"], send=[_response(400, "Order already shipped")])
		self.assertEqual(calls, ["UpdateOrder"])
		result = self._result(fixture)
		self.assertEqual(result, {"state": "failed", "message": "Order already shipped"})
		self.assertEqual(self._pending(fixture), set())
		self.assertEqual([log.custom_result for log in self._logs(fixture)], ["Failed"])
		self.assertFalse(frappe.db.get_value("Marketplace Order", fixture.marketplace_order.name, "expected_change_hash"))

	def test_unknown_keeps_the_reservation_and_retry_asks_noon_first_then_sends(self):
		fixture = self._order([("SKU-A", 1)], ["SKU-A"])
		self._reserve(fixture, ["1"])
		nr = fixture.marketplace_order.external_order_id
		calls, _client, _import = self._work(fixture, ["1"], send=[_response(503, "unavailable")])
		self.assertEqual(calls, ["UpdateOrder"])
		self.assertEqual(self._result(fixture)["state"], "unknown")
		self.assertEqual(self._pending(fixture), {"1"})
		self.assertEqual([log.custom_result for log in self._logs(fixture)], ["Unknown"])
		# Retry: GetFbpiOrder first; Noon does not have it out of stock, so UpdateOrder is sent now
		calls, _client, _import = self._work(
			fixture,
			["1"],
			check_first=True,
			request_id="req-2",
			reads=[
				_noon_order(nr, ("1", fulfillment.INTEGRATION_STATUS_ACKNOWLEDGED)),
				_noon_order(nr, ("1", _OOS)),
			],
			send=[_response(200)],
		)
		self.assertEqual(calls, ["GetFbpiOrder", "UpdateOrder", "GetFbpiOrder", "Import", "Sync"])
		self.assertEqual(self._result(fixture, "req-2")["state"], "done")
		self.assertEqual(self._pending(fixture), set())

	def test_unknown_then_retry_finds_the_unit_already_out_of_stock_and_sends_no_second_update_order(self):
		fixture = self._order([("SKU-A", 1)], ["SKU-A"])
		self._reserve(fixture, ["1"])
		nr = fixture.marketplace_order.external_order_id
		self._work(fixture, ["1"], send=[ConnectionError("dropped")])
		self.assertEqual(self._result(fixture)["state"], "unknown")
		calls, client, _import = self._work(
			fixture,
			["1"],
			check_first=True,
			request_id="req-2",
			reads=[_noon_order(nr, ("1", _OOS))],
		)
		self.assertEqual(calls, ["GetFbpiOrder", "Import", "Sync"])
		client.request.assert_not_called()
		self.assertEqual(self._result(fixture, "req-2")["state"], "done")
		self.assertEqual(self._pending(fixture), set())

	def test_a_failed_lookup_on_retry_is_unknown_and_sends_nothing(self):
		fixture = self._order([("SKU-A", 1)], ["SKU-A"])
		self._reserve(fixture, ["1"])
		calls, client, _import = self._work(
			fixture, ["1"], check_first=True, reads=[frappe.ValidationError("Noon down")]
		)
		self.assertEqual(calls, ["GetFbpiOrder"])
		client.request.assert_not_called()
		self.assertEqual(self._result(fixture)["state"], "unknown")
		self.assertEqual(self._pending(fixture), {"1"})

	def test_noon_confirmed_but_the_local_step_failed_keeps_the_marker_and_retry_repeats_only_the_local_step(self):
		fixture = self._order([("SKU-A", 1)], ["SKU-A"])
		self._reserve(fixture, ["1"])
		nr = fixture.marketplace_order.external_order_id
		calls, _client, _import = self._work(
			fixture,
			["1"],
			send=[_response(200)],
			reads=[_noon_order(nr, ("1", _OOS))],
			sync_result={"ok": False, "error": "ERPNext refused to remove the row"},
		)
		self.assertEqual(calls, ["UpdateOrder", "GetFbpiOrder", "Import", "Sync"])
		self.assertEqual(
			self._result(fixture), {"state": "local_failed", "message": "ERPNext refused to remove the row"}
		)
		self.assertEqual(self._pending(fixture), {"1"})  # Needs Attention lists it
		# Retry: Noon shows the unit out of stock, UpdateOrder is not sent again
		calls, client, _import = self._work(
			fixture, ["1"], check_first=True, request_id="req-2", reads=[_noon_order(nr, ("1", _OOS))]
		)
		self.assertEqual(calls, ["GetFbpiOrder", "Import", "Sync"])
		client.request.assert_not_called()
		self.assertEqual(self._result(fixture, "req-2")["state"], "done")
		self.assertEqual(self._pending(fixture), set())

	def test_units_a_note_started_after_the_reservation_are_not_sent(self):
		fixture = self._order([("SKU-A", 1)], ["SKU-A"])
		self._reserve(fixture, ["1"])
		with patch(
			"marketplace_integrations.integrations.noon.delivery_note_shipment.started_units_of_order",
			return_value={"1"},
		):
			calls, client, _import = self._work(fixture, ["1"])
		self.assertEqual(calls, [])
		client.request.assert_not_called()
		self.assertEqual(self._result(fixture)["state"], "failed")
		self.assertEqual(self._pending(fixture), set())

	def test_release_stuck_reservation_asks_noon_and_keeps_units_noon_already_marked(self):
		fixture = self._order([("SKU-A", 1), ("SKU-B", 1)], ["SKU-A", "SKU-B"])
		self._reserve(fixture, ["1", "2"])
		name = fixture.marketplace_order.name
		nr = fixture.marketplace_order.external_order_id
		with patch(_ENQUEUE) as enqueue:
			enqueue.return_value = MagicMock()
			self.assertEqual(fulfillment.release_stuck_oos_reservation(name), {"queued": True})
		self.assertEqual(self._pending(fixture), {"1", "2"})  # nothing cleared by the request itself

		with patch(_GET_ORDER, return_value=_noon_order(nr, ("1", _OOS), ("2", "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED"))):
			fulfillment.run_release_stuck_oos(name)
		self.assertEqual(self._pending(fixture), {"1"})  # Noon acted on unit 1: it stays for the Retry

		# an unusable Noon answer frees nothing
		with patch(_GET_ORDER, side_effect=frappe.ValidationError("Noon down")):
			fulfillment.run_release_stuck_oos(name)
		self.assertEqual(self._pending(fixture), {"1"})

	def test_release_stuck_reservation_is_refused_while_an_out_of_stock_job_runs(self):
		fixture = self._order([("SKU-A", 1)], ["SKU-A"])
		self._reserve(fixture, ["1"])
		name = fixture.marketplace_order.name
		fulfillment._oos_inflight_add(name, "req-live")
		self.addCleanup(fulfillment._oos_inflight_remove, name, "req-live")
		with patch(_ENQUEUE) as enqueue:
			with self.assertRaises(frappe.ValidationError):
				fulfillment.release_stuck_oos_reservation(name)
			enqueue.assert_not_called()
		# a worker that starts after the press still frees nothing while the marker is set
		with patch(_GET_ORDER, return_value=_noon_order(fixture.marketplace_order.external_order_id)) as read:
			fulfillment.run_release_stuck_oos(name)
			read.assert_not_called()
		self.assertEqual(self._pending(fixture), {"1"})

	def test_the_log_holds_no_secrets_or_customer_data(self):
		fixture = self._order([("SKU-A", 1)], ["SKU-A"])
		self._reserve(fixture, ["1"])
		nr = fixture.marketplace_order.external_order_id
		order = _noon_order(nr, ("1", _OOS))
		order["customer"] = {"name": "Private Person", "phone": "01000000000"}
		self._work(fixture, ["1"], send=[_response(200)], reads=[order])
		dump = json.dumps([dict(log) for log in self._logs(fixture)], default=str)
		self.assertNotIn("Private Person", dump)
		self.assertNotIn("01000000000", dump)
		self.assertNotIn("private_key", dump)
