# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The Noon adapter of the neutral carrier contract (#415): create on Submit through the neutral job,
GetShipment before every CreateShipment, the AWB step (one call, reused on retry), unit picking, the
duplicate AWB refusal, the locked fields and the cancel. Noon HTTP is mocked, never live."""

import json
from unittest.mock import MagicMock, patch

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

from marketplace_integrations.integrations.noon import delivery_note_shipment as noon
from marketplace_integrations.integrations.noon.test_fulfillment import (
	IntegrationTestNoonFulfillmentBase,
	_confirmed_acknowledged,
)
from marketplace_integrations.patches.v0_0 import create_noon_transporter_supplier
from marketplace_integrations.services import carrier_log, carrier_shipments
from marketplace_integrations.tests.handover_fixture import SubmittedOrderFixture

_CLIENT = "marketplace_integrations.integrations.noon.fulfillment.NoonClient"
_GET_ORDER = "marketplace_integrations.integrations.noon.fulfillment.get_fbpi_order"
_CANCELLED = "MP_ITEM_STATUS_CANCELLED"


def _response(status=200, body=None, message=None):
	response = MagicMock(status_code=status)
	response.json.return_value = body if body is not None else {}
	response.message = message
	return response


def _not_found():
	return _response(400, {"error": {"code": "INVALID_ARGUMENT", "message": "No shipment found with number"}})


def _awb(awb="NOON-AWB-1"):
	return _response(200, {"awb_nrs": [awb]})


def _paths(client):
	return [call.args[1].rsplit("/", 1)[-1] for call in client.request.call_args_list]


class IntegrationTestNoonDeliveryNoteShipment(IntegrationTestNoonFulfillmentBase):
	def setUp(self):
		super().setUp()
		self.fixtures = []
		self.created_supplier = False
		if not noon.transporter_name():
			create_noon_transporter_supplier.execute()
			self.created_supplier = True
			self.created_supplier_name = noon.transporter_name()

	def tearDown(self):
		frappe.set_user("Administrator")
		for fixture in self.fixtures:
			# the neutral cancel/delete guard refuses a note with a live shipment: close them first
			for name in fixture.delivery_notes():
				frappe.db.set_value("Delivery Note", name, "custom_integration_status", "Cancelled", update_modified=False)
			fixture.cleanup()
		frappe.db.commit()
		super().tearDown()
		if self.created_supplier:
			frappe.delete_doc("Supplier", self.created_supplier_name, force=True, ignore_permissions=True)
			frappe.db.commit()

	# ---- helpers -------------------------------------------------------------------------

	def _order(self, rows, payload_skus):
		"""A submitted Noon Sales Order of ``rows`` ([(sku, qty)]) whose Noon payload holds one CONFIRMED +
		ACKNOWLEDGED unit per entry of ``payload_skus`` (mp_item_nr 1, 2, ...)."""
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

	def _draft_note(self, fixture, lr_no=None, generate_awb=0):
		"""Save a draft Delivery Note from the Sales Order the way Create > Delivery Note does."""
		note = make_delivery_note(fixture.name)
		note.lr_no = lr_no
		note.custom_noon_generate_awb = generate_awb
		note.insert()
		frappe.db.commit()
		return note

	def _note(self, name):
		return frappe.get_doc("Delivery Note", name)

	def _creating(self, note):
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Creating", "custom_carrier_reference": note.name},
			update_modified=False,
		)
		frappe.db.commit()

	def _run(self, note, *responses, lookup_first=True):
		with patch(_CLIENT) as client_class:
			client = client_class.return_value
			client.request.side_effect = list(responses)
			client.error_message.side_effect = lambda response: response.message
			carrier_shipments.run_create(note.name, log=None, lookup_first=lookup_first)
		return client

	def _created_note(self, fixture, units=("1",)):
		"""A draft note whose shipment is Created with the given units in its snapshot."""
		note = self._draft_note(fixture, lr_no=f"AWB-{frappe.generate_hash(length=6)}")
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{
				"custom_integration_status": "Created",
				"custom_carrier_reference": note.name,
				"custom_carrier_request_snapshot": json.dumps({"mp_item_nrs": list(units)}),
			},
			update_modified=False,
		)
		frappe.db.commit()
		return note

	def _cancel(self, note, *responses, order_items=None):
		items = order_items if order_items is not None else [{"mp_item_nr": "1", "mp_status": "MP_ITEM_STATUS_CONFIRMED"}]
		with patch(_CLIENT) as client_class, patch(_GET_ORDER, return_value={"items": items}) as get_order:
			client = client_class.return_value
			client.request.side_effect = list(responses)
			client.error_message.side_effect = lambda response: response.message
			result = carrier_shipments.cancel(note.name)
		return result, client, get_order

	# ---- contract ------------------------------------------------------------------------

	def test_noon_is_a_registered_carrier_and_claims_its_notes(self):
		self.assertIs(carrier_shipments.CARRIERS["Noon"], noon)
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="TYPED-1")
		self.assertIs(carrier_shipments.adapter_for(note), noon)
		# the Needs Attention collector and the amend cleanup judge a dict by its transporter
		self.assertTrue(noon.applies_to({"is_return": 0, "transporter": noon.transporter_name()}))
		self.assertFalse(noon.applies_to({"is_return": 1, "transporter": noon.transporter_name()}))
		self.assertFalse(noon.applies_to({"is_return": 0, "transporter": None}))
		self.assertEqual(noon.problem_states(), {})
		self.assertEqual(noon.terminal_states(), ["Cancelled"])

	def test_the_first_save_sets_the_carrier_and_not_created(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="TYPED-1")
		saved = self._note(note.name)
		self.assertEqual((saved.transporter, saved.transporter_name), (noon.transporter_name(), "Noon"))
		self.assertEqual(saved.custom_integration_status, "Not Created")

	# ---- request (the neutral entry) ------------------------------------------------------------

	def test_request_create_runs_one_deduplicated_job_that_looks_up_first(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="TYPED-1")
		with patch("frappe.enqueue") as enqueue, patch(_CLIENT) as client_class:
			result = carrier_shipments.request_create(note.name)
			again = carrier_shipments.request_create(note.name)
		enqueue.assert_called_once()
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["job_id"], f"noon-create|{note.name}")
		self.assertTrue(kwargs["deduplicate"])
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertTrue(kwargs["lookup_first"])  # even the first attempt asks Noon first
		self.assertEqual((result["status"], result["queued"]), ("Creating", True))
		self.assertFalse(again["queued"])
		client_class.return_value.request.assert_not_called()
		saved = self._note(note.name)
		self.assertEqual((saved.custom_integration_status, saved.custom_carrier_reference), ("Creating", note.name))

	def test_submit_is_refused_until_the_shipment_is_created(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="TYPED-1")
		with self.assertRaises(frappe.ValidationError):
			self._note(note.name).submit()

	def test_validate_ready_refuses_what_noon_cannot_take(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx)  # no AWB, checkbox off
		with self.assertRaises(frappe.ValidationError):
			noon.validate_ready(self._note(note.name))
		frappe.db.set_value("Delivery Note", note.name, "lr_no", "TYPED-1")
		noon.validate_ready(self._note(note.name))  # now ready (nothing is sent)
		frappe.db.set_value("Delivery Note Item", {"parent": note.name}, "qty", 1.5)
		with self.assertRaises(frappe.ValidationError):
			noon.validate_ready(self._note(note.name))

	# ---- create (the job) -----------------------------------------------------------------------

	def test_get_shipment_runs_before_create_and_a_typed_awb_goes_with_courier_noon(self):
		fx = self._order([("SKU-A", 2), ("SKU-B", 1)], ["SKU-A", "SKU-B", "SKU-A"])
		note = self._draft_note(fx, lr_no="STICKER-1")
		self._creating(note)
		client = self._run(note, _not_found(), _response(200))
		self.assertEqual(_paths(client), ["get", "create"])
		sent = client.request.call_args_list[1].kwargs["json"]
		self.assertEqual(sent["integration_shipment_nr"], note.name)
		self.assertEqual(sent["awbs"], [{"courier": "noon", "awb_nr": "STICKER-1"}])
		self.assertEqual(sorted(item["mp_item_nr"] for item in sent["items"]), ["1", "2", "3"])
		saved = self._note(note.name)
		self.assertEqual((saved.custom_integration_status, saved.lr_no, saved.docstatus), ("Created", "STICKER-1", 0))
		self.assertEqual(json.loads(saved.custom_carrier_request_snapshot), {"mp_item_nrs": ["1", "2", "3"]})
		self.assertEqual(saved.custom_shipping_status, "Acknowledged")
		self.assertTrue(saved.lr_date and saved.custom_last_sync_at)

	def test_a_shipment_found_at_noon_is_created_without_a_create_call(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="STICKER-1")
		self._creating(note)
		found = _response(200, {"awbs": [{"courier": "noon", "awb_nr": "STICKER-1"}], "items": [{"mp_item_nr": "1"}]})
		client = self._run(note, found)
		self.assertEqual(_paths(client), ["get"])
		self.assertEqual(self._note(note.name).custom_integration_status, "Created")

	def test_the_checkbox_asks_noon_for_one_awb_and_commits_it_before_create(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, generate_awb=1)
		self._creating(note)
		seen = {}

		def create_sees_stored_awb(*args, **kwargs):
			if args[1].endswith("/create"):
				seen["lr_no"] = frappe.db.get_value("Delivery Note", note.name, "lr_no")
				return _response(200)
			return {"get": _not_found(), "noon-logistics-awbs/get": _awb("NOON-AWB-1")}[
				args[1].split("/v1/shipment/")[-1]
			]

		with patch(_CLIENT) as client_class:
			client = client_class.return_value
			client.request.side_effect = create_sees_stored_awb
			carrier_shipments.run_create(note.name, log=None, lookup_first=True)
		self.assertEqual(_paths(client), ["get", "get", "create"])  # GetShipment, GetNoonLogisticsAWBs, CreateShipment
		self.assertEqual(client.request.call_args_list[1].kwargs["json"]["qty"], 1)
		self.assertEqual(seen["lr_no"], "NOON-AWB-1")
		self.assertEqual(self._note(note.name).lr_no, "NOON-AWB-1")
		self.assertEqual(
			frappe.db.count(
				"Integration Request",
				{"reference_docname": note.name, "custom_operation_type": "AWB", "custom_result": "Success"},
			),
			1,
		)

	def test_a_create_failure_after_the_awb_then_retry_requests_no_second_awb(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, generate_awb=1)
		self._creating(note)
		first = self._run(note, _not_found(), _awb("NOON-AWB-9"), _response(500, message="Noon down"))
		self.assertEqual(_paths(first), ["get", "get", "create"])
		saved = self._note(note.name)
		self.assertEqual((saved.custom_integration_status, saved.lr_no), ("Creation Unknown", "NOON-AWB-9"))

		self._creating(self._note(note.name))  # Submit again: the neutral request writes Creating
		second = self._run(note, _not_found(), _response(200))
		self.assertEqual(_paths(second), ["get", "create"])  # no AWB request
		self.assertEqual(second.request.call_args_list[1].kwargs["json"]["awbs"], [{"courier": "noon", "awb_nr": "NOON-AWB-9"}])
		self.assertEqual(self._note(note.name).custom_integration_status, "Created")

	def test_a_definite_refusal_fails_and_keeps_the_draft(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="STICKER-1")
		self._creating(note)
		self._run(note, _not_found(), _response(400, message="Unit not shippable"))
		saved = self._note(note.name)
		self.assertEqual((saved.custom_integration_status, saved.docstatus), ("Creation Failed", 0))
		self.assertIn("Unit not shippable", saved.custom_last_integration_error)

	def test_a_timeout_is_unknown_never_failed(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="STICKER-1")
		self._creating(note)
		self._run(note, _not_found(), TimeoutError("timed out"))
		self.assertEqual(self._note(note.name).custom_integration_status, "Creation Unknown")

	def test_an_unrecognised_get_shipment_answer_never_creates(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="STICKER-1")
		self._creating(note)
		client = self._run(note, _response(400, {"error": {"message": "Something else"}, }, message="Something else"))
		self.assertEqual(_paths(client), ["get"])
		self.assertEqual(self._note(note.name).custom_integration_status, "Creation Unknown")

	def test_a_missing_mapping_fails_without_an_awb_or_create_call(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, generate_awb=1)
		frappe.db.set_value("Marketplace Product Mapping", fx.mappings[0].name, "enabled", 0)
		frappe.db.commit()
		self._creating(note)
		client = self._run(note, _not_found())
		self.assertEqual(_paths(client), ["get"])
		saved = self._note(note.name)
		self.assertEqual(saved.custom_integration_status, "Creation Failed")
		self.assertIn("no enabled Noon product mapping", saved.custom_last_integration_error)

	def test_check_resolves_a_found_shipment_and_never_creates(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="STICKER-1")
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Creation Unknown", "custom_carrier_reference": note.name},
			update_modified=False,
		)
		frappe.db.commit()
		with patch(_CLIENT) as client_class:
			client = client_class.return_value
			client.request.side_effect = [_not_found()]
			result = carrier_shipments.check(note.name)
			self.assertFalse(result["found"])
			self.assertEqual(_paths(client), ["get"])
			client.request.side_effect = [_response(200, {"awbs": [], "items": [{"mp_item_nr": "1"}]})]
			self.assertTrue(carrier_shipments.check(note.name)["found"])
		self.assertEqual(self._note(note.name).custom_integration_status, "Created")

	# ---- units -----------------------------------------------------------------------------------

	def test_units_another_live_note_started_are_not_taken_again(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A", "SKU-A"])
		first = self._draft_note(fx, lr_no="AWB-ONE")
		frappe.db.set_value("Delivery Note Item", {"parent": first.name}, "qty", 1)
		frappe.db.set_value(
			"Delivery Note",
			first.name,
			{"custom_integration_status": "Created", "custom_carrier_request_snapshot": json.dumps({"mp_item_nrs": ["1"]})},
			update_modified=False,
		)
		second = self._draft_note(fx, lr_no="AWB-TWO")
		frappe.db.set_value("Delivery Note Item", {"parent": second.name}, "qty", 1)
		picked = noon._pick_units(self._note(second.name))
		self.assertEqual([unit["mp_item_nr"] for unit in picked], ["2"])
		# a cancelled shipment releases its unit
		frappe.db.set_value("Delivery Note", first.name, "custom_integration_status", "Cancelled", update_modified=False)
		picked = noon._pick_units(self._note(second.name))
		self.assertEqual([unit["mp_item_nr"] for unit in picked], ["1"])

	def test_too_few_shippable_units_is_refused(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="AWB-ONE")
		with self.assertRaises(frappe.ValidationError):
			noon.validate_ready(self._note(note.name))

	# ---- AWB rules ---------------------------------------------------------------------------------

	def test_an_awb_held_by_another_live_note_is_refused_ignoring_case(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		self._draft_note(fx, lr_no="Sticker-9")
		other = self._order([("SKU-B", 1)], ["SKU-B"])
		with self.assertRaises(frappe.ValidationError):
			self._draft_note(other, lr_no="STICKER-9")
		# a cancelled holder releases the AWB
		for name in fx.delivery_notes():
			frappe.db.set_value("Delivery Note", name, "docstatus", 2, update_modified=False)
		frappe.db.commit()
		self._draft_note(other, lr_no="STICKER-9")

	def test_ticked_checkbox_with_a_typed_awb_is_refused(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		with self.assertRaises(frappe.ValidationError):
			self._draft_note(fx, lr_no="TYPED-1", generate_awb=1)

	def test_a_generated_awb_and_the_started_shipment_are_locked(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, generate_awb=1)
		frappe.db.set_value("Delivery Note", note.name, "lr_no", "NOON-AWB-1", update_modified=False)
		frappe.db.commit()
		changed = self._note(note.name)
		changed.lr_no = "OTHER"
		with self.assertRaises(frappe.ValidationError):
			changed.save()
		unticked = self._note(note.name)
		unticked.custom_noon_generate_awb = 0
		with self.assertRaises(frappe.ValidationError):
			unticked.save()
		# once started, the carrier and the rows lock too
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Created", update_modified=False)
		frappe.db.commit()
		started = self._note(note.name)
		started.items[0].qty = 1  # same quantity: allowed
		started.save()
		started = self._note(note.name)
		started.items[0].qty = 2
		with self.assertRaises(frappe.ValidationError):
			started.save()

	# ---- cancel --------------------------------------------------------------------------------------

	def test_cancel_reads_the_order_first_and_a_cancelled_shipment_makes_no_cancel_call(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		result, client, get_order = self._cancel(note, order_items=[{"mp_item_nr": "1", "mp_status": _CANCELLED}])
		get_order.assert_called_once()
		client.request.assert_not_called()
		self.assertEqual((result["status"], result["outcome"]), ("Cancelled", "cancelled"))
		saved = self._note(note.name)
		self.assertEqual((saved.custom_integration_status, saved.custom_shipping_status), ("Cancelled", "Cancelled"))

	def test_cancel_sends_cancel_shipment_with_the_shipment_number(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		result, client, _get = self._cancel(note, _response(200))
		self.assertEqual(_paths(client), ["cancel"])
		self.assertEqual(client.request.call_args.kwargs["json"]["integration_shipment_nr"], note.name)
		self.assertEqual(result["outcome"], "cancelled")

	def test_a_noon_refusal_leaves_the_note_created_with_the_reason(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		result, _client, _get = self._cancel(note, _response(400, message="Already shipped"))
		self.assertEqual((result["status"], result["outcome"]), ("Created", "refused"))
		saved = self._note(note.name)
		self.assertEqual(saved.custom_integration_status, "Created")
		self.assertIn("Already shipped", saved.custom_last_integration_error)

	def test_an_unknown_cancel_result_is_cancellation_unknown_and_retry_asks_noon_first(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		result, _client, _get = self._cancel(note, _response(500, message="Noon down"))
		self.assertEqual((result["status"], result["outcome"]), ("Cancellation Unknown", "unknown"))
		retry, client, get_order = self._cancel(note, order_items=[{"mp_item_nr": "1", "mp_status": _CANCELLED}])
		get_order.assert_called_once()  # asked first again
		client.request.assert_not_called()
		self.assertEqual(retry["status"], "Cancelled")

	def test_a_failed_order_read_never_sends_a_blind_cancel(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		with patch(_CLIENT) as client_class, patch(_GET_ORDER, side_effect=frappe.ValidationError("down")):
			result = carrier_shipments.cancel(note.name)
		client_class.return_value.request.assert_not_called()
		self.assertEqual(result["outcome"], "unknown")

	def test_after_a_failed_local_cancel_the_next_cancel_makes_no_noon_call(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled", update_modified=False)
		frappe.db.set_value("Delivery Note", note.name, "docstatus", 1, update_modified=False)
		frappe.db.commit()
		with patch(_CLIENT) as client_class, patch(_GET_ORDER) as get_order:
			result = carrier_shipments.cancel(note.name)
		client_class.return_value.request.assert_not_called()
		get_order.assert_not_called()
		self.assertEqual(result["outcome"], "cancelled")
		frappe.db.set_value("Delivery Note", note.name, "docstatus", 0, update_modified=False)

	# ---- Sales Order cancel ---------------------------------------------------------------------------

	def test_a_sales_order_with_a_draft_note_holding_a_live_shipment_cannot_be_cancelled(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		order = frappe.get_doc("Sales Order", fx.name)
		with self.assertRaises(frappe.ValidationError):
			noon.refuse_sales_order_cancel(order)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled", update_modified=False)
		noon.refuse_sales_order_cancel(order)

	# ---- placeholders --------------------------------------------------------------------------------

	def test_refresh_and_reconcile_are_phase_3_placeholders_that_call_nothing(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		with patch(_CLIENT) as client_class:
			self.assertIsNone(noon.refresh(note.name))
			self.assertIsNone(noon.reconcile())
		client_class.return_value.request.assert_not_called()
		self.assertEqual(carrier_log.CREATED, "Created")
