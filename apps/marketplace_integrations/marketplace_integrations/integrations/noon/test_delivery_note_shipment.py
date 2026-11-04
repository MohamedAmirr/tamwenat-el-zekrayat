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
from marketplace_integrations.integrations.noon import fulfillment
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
		self._fbpi_ready_account()
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

	def _fbpi_ready_account(self, **overrides):
		"""The account as after Check Noon Warehouse (#416)."""
		values = {
			"noon_fulfillment_system_code": "fbpi",
			"noon_warehouse_active": 1,
			"noon_warehouse_checked_on": frappe.utils.now_datetime(),
		}
		values.update(overrides)
		frappe.db.set_value("Marketplace Account", self.account.name, values, update_modified=False)
		frappe.db.commit()

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
		# Deleting the newest note gives its name back to the series, so a later test (or run) gets the same
		# name: the name-keyed cache flags (invoice upload running, refresh again) and the Integration Request
		# log rows (a "Cancel / Success" row hides a Carrier Cancelled item) must not carry over.
		logs = {"reference_doctype": "Delivery Note", "reference_docname": note.name}
		frappe.db.delete("Integration Request", logs)
		self.addCleanup(frappe.db.delete, "Integration Request", logs)
		for flag in (noon._INVOICE_INFLIGHT, noon._REFRESH_AGAIN):
			self.addCleanup(frappe.cache().delete_value, flag.format(note.name))
		return note

	def _note(self, name):
		return frappe.get_doc("Delivery Note", name)

	def _found(self, fixture, awbs=None, units=("1",), order_nr=None):
		"""A GetShipment 200 for ``fixture``'s order (``fbpi_order_nr`` is part of the answer)."""
		return _response(
			200,
			{
				"fbpi_order_nr": order_nr or fixture.marketplace_order.external_order_id,
				"awbs": awbs or [],
				"items": [{"mp_item_nr": unit} for unit in units],
			},
		)

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
		self.assertEqual(
			noon.problem_states(), {"Carrier Exception": [noon.SHIPPING_MIXED, noon.SHIPPING_CANCELLED]}
		)
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

	def test_validate_ready_refuses_a_warehouse_that_is_not_fbpi_ready(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="TYPED-1")
		cases = (
			({"noon_fulfillment_system_code": "", "noon_warehouse_checked_on": None}, "never checked"),
			({"noon_fulfillment_system_code": "OTHER"}, "OTHER, not FBPI"),
			({"noon_warehouse_active": 0}, "not active"),
		)
		for overrides, reason in cases:
			self._fbpi_ready_account(**overrides)
			with self.assertRaisesRegex(frappe.ValidationError, reason):
				noon.validate_ready(self._note(note.name))
			with patch(_CLIENT) as client_class:
				with self.assertRaises(frappe.ValidationError):
					carrier_shipments.request_create(note.name)
			client_class.return_value.request.assert_not_called()
			self.assertEqual(self._note(note.name).custom_integration_status, "Not Created")
			self._fbpi_ready_account()
		noon.validate_ready(self._note(note.name))  # ready again

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
		found = self._found(fx, [{"courier": "noon", "awb_nr": "STICKER-1"}])
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
			client.request.side_effect = [self._found(fx)]
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

	def test_cancelling_a_note_never_calls_an_order_level_path_including_the_auto_cancel(self):
		# Noon documents that CancelShipment also cancels the shipment's items; ERPNext only ever calls the
		# shipment cancel, never UpdateOrder or another order-level path (the Sales Order follows by sync).
		update_path = fulfillment.FBPI_ORDER_UPDATE_PATH.rsplit("/", 1)[-1]
		cancel_path = noon.SHIPMENT_CANCEL_PATH.rsplit("/", 1)[-1]
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		draft = self._created_note(fx)
		_result, client, _get = self._cancel(draft, _response(200))
		self.assertEqual(_paths(client), [cancel_path])
		self.assertNotIn(update_path, _paths(client))

		fx = self._order([("SKU-B", 1)], ["SKU-B"])
		submitted = self._created_note(fx)
		self._submit_flags(submitted)
		with (
			patch(_CLIENT) as client_class,
			patch(_GET_ORDER, return_value={"items": [{"mp_item_nr": "1", "mp_status": "MP_ITEM_STATUS_CONFIRMED"}]}),
			patch("frappe.model.document.Document.cancel"),
			patch.object(carrier_shipments, "_check_native_cancel_not_blocked"),
			patch.object(carrier_shipments, "_notify"),
		):
			client = client_class.return_value
			client.request.side_effect = [_response(200)]
			client.error_message.side_effect = lambda response: response.message
			carrier_shipments.cancel(submitted.name)
			carrier_shipments._auto_cancel(submitted.name)  # the job queued after a refresh found the cancel
		self.assertEqual(_paths(client), [cancel_path])
		frappe.db.set_value("Delivery Note", submitted.name, "docstatus", 0, update_modified=False)
		frappe.db.set_value("Delivery Note Item", {"parent": submitted.name}, "docstatus", 0, update_modified=False)
		frappe.db.commit()

	def test_after_a_cancel_shipment_200_the_same_units_are_shippable_again_by_a_new_note(self):
		# Assumes Noon keeps the units CONFIRMED + ACKNOWLEDGED after CancelShipment (UNVERIFIED: the docs say the
		# items are cancelled too). Documents what ERPNext does under that payload.
		fx = self._order([("SKU-A", 2)], ["SKU-A", "SKU-A"])
		first = self._draft_note(fx, lr_no="AWB-ONE")
		frappe.db.set_value("Delivery Note Item", {"parent": first.name}, "qty", 1)
		frappe.db.set_value(
			"Delivery Note",
			first.name,
			{
				"custom_integration_status": "Created",
				"custom_carrier_reference": first.name,
				"custom_carrier_request_snapshot": json.dumps({"mp_item_nrs": ["1"]}),
			},
			update_modified=False,
		)
		second = self._draft_note(fx, lr_no="AWB-TWO")
		frappe.db.set_value("Delivery Note Item", {"parent": second.name}, "qty", 1)
		frappe.db.commit()
		self.assertEqual([unit["mp_item_nr"] for unit in noon._pick_units(self._note(second.name))], ["2"])
		self.assertEqual(noon.started_units_of_order(fx.marketplace_order.name), {"1"})
		result, _client, _get = self._cancel(first, _response(200))
		self.assertEqual(result["status"], "Cancelled")
		self.assertEqual(noon.started_units_of_order(fx.marketplace_order.name), set())
		self.assertEqual([unit["mp_item_nr"] for unit in noon._pick_units(self._note(second.name))], ["1"])

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

	def test_a_draft_note_of_another_carrier_is_not_a_live_noon_shipment_to_bosta(self):
		from marketplace_integrations.integrations.bosta import delivery_note_shipment as bosta

		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		self._created_note(fx)
		bosta.refuse_sales_order_cancel(frappe.get_doc("Sales Order", fx.name))

	# ---- status: derivation, refresh, webhook, nightly -------------------------------------------------

	@staticmethod
	def _unit(nr, integration="SHIPPED", mp="MP_ITEM_STATUS_CONFIRMED", sku="SKU-A"):
		return {
			"mp_item_nr": nr,
			"partner_sku": sku,
			"mp_status": mp,
			"integration_status": f"INTEGRATION_ITEM_STATUS_{integration}",
		}

	def _derive(self, units, order_items, shipment_nrs=None):
		doc = frappe._dict(
			custom_carrier_request_snapshot=json.dumps({"mp_item_nrs": units}),
			items=[frappe._dict(name="row-1", item_code="ITEM-A")],
		)
		shipment = {"items": [{"mp_item_nr": nr} for nr in (shipment_nrs if shipment_nrs is not None else units)]}
		with patch.object(noon, "resolve_item", return_value="ITEM-A"):
			return noon.derive_status(doc, shipment, {"items": order_items}, "ACC")

	def test_the_derivation_table(self):
		unit = self._unit
		cancelled = {"integration": "ACKNOWLEDGED", "mp": "MP_ITEM_STATUS_CANCELLED"}
		cases = [
			([unit("1", "ACKNOWLEDGED")], "Acknowledged", None),
			([unit("1"), unit("2")], "Shipped", None),
			([unit("1", "OUT_OF_STOCK")], "Out of Stock", None),
			([unit("1", **cancelled), unit("2", **cancelled)], "Cancelled", "cancelled"),
			([unit("1"), unit("2", "ACKNOWLEDGED")], "Mixed", "different statuses"),
			([unit("1"), unit("2", **cancelled)], "Mixed", "cancelled 1 of 2"),
		]
		for order_items, expected, error_text in cases:
			derived = self._derive([i["mp_item_nr"] for i in order_items], order_items)
			self.assertEqual(derived.shipping_status, expected)
			# the one row of the Item shows the same word (all units belong to it)
			self.assertEqual(derived.rows, {"row-1": expected})
			if error_text:
				self.assertIn(error_text.lower(), (derived.error or "").lower())
			else:
				self.assertIsNone(derived.error)

	def test_an_unknown_unit_status_changes_nothing_and_items_that_differ_set_an_error(self):
		odd = self._derive(["1"], [{"mp_item_nr": "1", "partner_sku": "SKU-A", "integration_status": "INTEGRATION_ITEM_STATUS_UNSPECIFIED"}])
		self.assertIsNone(odd.shipping_status)
		self.assertEqual(odd.rows, {})
		self.assertTrue(odd.error)
		differ = self._derive(["1"], [self._unit("1")], shipment_nrs=["1", "9"])
		self.assertEqual(differ.shipping_status, "Shipped")
		self.assertIn("differ", differ.error)

	def _refresh(self, note, shipment, order_items):
		with patch(_CLIENT) as client_class, patch(_GET_ORDER, return_value={"items": order_items}) as get_order:
			client = client_class.return_value
			client.request.side_effect = [shipment]
			client.error_message.side_effect = lambda response: response.message
			result = noon.refresh(note.name)
		return result, client, get_order

	def test_refresh_writes_status_row_status_and_sync_time_and_never_edits_items(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		rows_before = [(r.item_code, r.qty) for r in self._note(note.name).items]
		shipment = self._found(fx)
		result, client, get_order = self._refresh(note, shipment, [self._unit("1")])
		self.assertEqual(result, "Shipped")
		self.assertEqual(_paths(client), ["get"])  # GetShipment, then the order (mocked) once
		get_order.assert_called_once()
		stored = self._note(note.name)
		self.assertEqual(stored.custom_shipping_status, "Shipped")
		self.assertEqual(stored.items[0].custom_noon_item_status, "Shipped")
		self.assertTrue(stored.custom_last_sync_at)
		self.assertFalse(stored.custom_last_integration_error)
		self.assertEqual([(r.item_code, r.qty) for r in stored.items], rows_before)

	def test_a_noon_note_is_counted_in_the_sales_order_summary_and_a_refresh_updates_it(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		summary = lambda: frappe.db.get_value("Sales Order", fx.name, "custom_shipping_status_summary")  # noqa: E731
		frappe.db.set_value("Delivery Note", note.name, "custom_shipping_status", "Acknowledged", update_modified=False)
		noon.after_created(note.name, {})
		self.assertEqual(json.loads(summary()), {"Acknowledged": 1})

		self._refresh(note, self._found(fx), [self._unit("1")])
		self.assertEqual(json.loads(summary()), {"Shipped": 1})

		# a Cancelled shipment is not live: it leaves the summary
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled", update_modified=False)
		noon.after_cancelled(note.name)
		self.assertIsNone(summary())

	def test_refresh_shows_mixed_and_an_item_difference_sets_the_error(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A", "SKU-A"])
		note = self._created_note(fx, units=("1", "2"))
		shipment = self._found(fx)
		self._refresh(note, shipment, [self._unit("1"), self._unit("2", "ACKNOWLEDGED")])
		stored = self._note(note.name)
		self.assertEqual(stored.custom_shipping_status, "Mixed")
		self.assertEqual(stored.items[0].custom_noon_item_status, "Mixed")
		self.assertIn("differ", stored.custom_last_integration_error)

	def test_a_shipment_missing_at_noon_sets_the_error_and_keeps_the_status(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		self._refresh(note, _not_found(), [self._unit("1")])
		stored = self._note(note.name)
		self.assertTrue(stored.custom_last_integration_error)
		self.assertNotEqual(stored.custom_shipping_status, "Shipped")

	def test_refresh_resolves_a_cancellation_unknown_when_noon_shows_every_unit_cancelled(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancellation Unknown", update_modified=False)
		result, client, _get = self._refresh(
			note, None, [self._unit("1", "ACKNOWLEDGED", mp=_CANCELLED)]
		)
		self.assertEqual(result, "Cancelled")
		self.assertEqual(self._note(note.name).custom_integration_status, "Cancelled")
		client.request.assert_not_called()  # no GetShipment, never a cancel call

	def test_a_cancellation_unknown_note_whose_units_stay_confirmed_is_never_marked_cancelled(self):
		# The stuck state: if Noon cancelled the shipment but kept the units CONFIRMED (UNVERIFIED), a refresh
		# cannot prove the cancel, so the note stays Cancellation Unknown (the person retries or checks).
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancellation Unknown", update_modified=False)
		frappe.db.commit()
		with patch.object(noon, "_cancel_note_natively") as native:
			_result, client, _get = self._refresh(note, None, [self._unit("1", "ACKNOWLEDGED")])
		self.assertEqual(self._note(note.name).custom_integration_status, "Cancellation Unknown")
		native.assert_not_called()
		client.request.assert_not_called()

	def _submit_flags(self, note):
		frappe.db.set_value("Delivery Note", note.name, "docstatus", 1, update_modified=False)
		frappe.db.set_value("Delivery Note Item", {"parent": note.name}, "docstatus", 1, update_modified=False)
		frappe.db.commit()

	def test_the_webhook_queues_one_deduplicated_refresh_per_submitted_note_and_ignores_drafts(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		draft = self._created_note(fx)
		with patch("frappe.enqueue") as enqueue:
			self.assertEqual(noon.enqueue_note_refreshes(fx.marketplace_order.name), [])
		enqueue.assert_not_called()
		self._submit_flags(draft)
		with patch("frappe.enqueue") as enqueue:
			self.assertEqual(noon.enqueue_note_refreshes(fx.marketplace_order.name), [draft.name])
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["job_id"], f"noon-refresh|{draft.name}")
		self.assertTrue(kwargs["deduplicate"])
		self.assertTrue(kwargs["enqueue_after_commit"])

	def test_the_nightly_reconcile_is_one_deduplicated_long_job(self):
		with patch("frappe.enqueue") as enqueue:
			noon.reconcile()
		kwargs = enqueue.call_args.kwargs
		self.assertEqual((kwargs["job_id"], kwargs["queue"], kwargs["deduplicate"]), ("noon-reconcile", "long", True))

	def test_the_nightly_reconcile_survives_one_notes_failure(self):
		# One SKU per order: a mapping is unique per account and SKU.
		fx_one = self._order([("SKU-A", 1)], ["SKU-A"])
		fx_two = self._order([("SKU-B", 1)], ["SKU-B"])
		one, two = self._created_note(fx_one), self._created_note(fx_two)
		seen = []

		def reconcile_note(note, _service):
			seen.append(note.name)
			if note.name == one.name:
				raise RuntimeError("boom")

		with patch.object(noon, "_reconcile_note", side_effect=reconcile_note):
			noon.run_reconcile()
		self.assertIn(one.name, seen)
		self.assertIn(two.name, seen)

	def test_the_nightly_reconcile_stops_at_shipped_or_cancelled_and_after_60_days(self):
		notes = {}
		for key in ("acknowledged", "shipped", "cancelled", "old", "cancelled_submitted"):
			sku = f"SKU-{key}"  # one SKU per order: a mapping is unique per account and SKU
			notes[key] = self._created_note(self._order([(sku, 1)], [sku]))
		for key, status in (
			("acknowledged", "Acknowledged"),
			("shipped", "Shipped"),
			("cancelled", "Cancelled"),
			("old", "Acknowledged"),
			("cancelled_submitted", "Cancelled"),
		):
			frappe.db.set_value("Delivery Note", notes[key].name, "custom_shipping_status", status, update_modified=False)
		# Noon shows it cancelled but the note is still Created and submitted: it must be refreshed so it
		# becomes Cancelled and is cancelled natively.
		self._submit_flags(notes["cancelled_submitted"])
		frappe.db.set_value(
			"Delivery Note", notes["old"].name, "creation", frappe.utils.add_days(frappe.utils.now_datetime(), -61), update_modified=False
		)
		frappe.db.commit()
		seen = []

		with patch.object(noon, "_reconcile_note", side_effect=lambda note, _service: seen.append(note.name)):
			noon.run_reconcile()

		self.assertIn(notes["acknowledged"].name, seen)
		self.assertIn(notes["cancelled_submitted"].name, seen)
		for key in ("shipped", "cancelled", "old"):
			self.assertNotIn(notes[key].name, seen)
		frappe.db.set_value("Delivery Note", notes["cancelled_submitted"].name, "docstatus", 0, update_modified=False)
		frappe.db.set_value("Delivery Note Item", {"parent": notes["cancelled_submitted"].name}, "docstatus", 0, update_modified=False)
		frappe.db.commit()

	def test_refresh_cancels_natively_only_when_it_moved_the_note_into_cancelled_or_retries_a_blocked_one(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		self._submit_flags(note)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled", update_modified=False)
		frappe.db.commit()
		# Cancelled by this app's own Cancel (no error): a refresh leaves it to the person cancelling it.
		with patch.object(noon, "_cancel_note_natively") as native:
			noon.refresh(note.name)
		native.assert_not_called()
		# Cancelled with the error a blocked automatic cancel left: the refresh retries.
		frappe.db.set_value("Delivery Note", note.name, "custom_last_integration_error", "blocked", update_modified=False)
		frappe.db.commit()
		with patch.object(noon, "_cancel_note_natively") as native:
			noon.refresh(note.name)
		native.assert_called_once_with(note.name)
		frappe.db.set_value("Delivery Note", note.name, "docstatus", 0, update_modified=False)
		frappe.db.set_value("Delivery Note Item", {"parent": note.name}, "docstatus", 0, update_modified=False)
		frappe.db.commit()

	def test_a_shipment_of_another_order_or_with_a_foreign_unit_is_never_adopted(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="STICKER-1")
		self._creating(note)
		doc = self._note(note.name)
		for shipment in (self._found(fx, order_nr="SOME-OTHER-ORDER"), self._found(fx, units=("1", "99"))):
			with patch(_CLIENT) as client_class:
				client_class.return_value.request.side_effect = [shipment]
				with self.assertRaises(noon.Unknown):
					noon.find_remote(doc)

	def test_needs_attention_lists_an_error_a_refresh_wrote_on_a_shipped_note(self):
		from marketplace_integrations.services import marketplace_attention as attention

		note = self._created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_shipping_status": "Shipped", "custom_last_integration_error": "The items of the Noon shipment differ"},
			update_modified=False,
		)
		frappe.db.commit()
		found = [i for i in attention._collect_carrier_items(attention.CARRIER_EXCEPTION) if i["reference_name"] == note.name]
		self.assertEqual(len(found), 1)

	def test_a_clean_refresh_clears_only_the_error_a_refresh_wrote(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A", "SKU-A"])
		note = self._created_note(fx)
		# a refusal written by the cancel stays through a clean refresh
		frappe.db.set_value("Delivery Note", note.name, "custom_last_integration_error", "Noon refused the cancel", update_modified=False)
		frappe.db.commit()
		self._refresh(note, self._found(fx), [self._unit("1")])
		self.assertEqual(self._note(note.name).custom_last_integration_error, "Noon refused the cancel")
		# the refresh's own error is written, then cleared by the next clean refresh
		# (unit 2 is on the Noon order, so the shipment is ours; it is not one of the units the note sent)
		self._refresh(note, self._found(fx, units=("1", "2")), [self._unit("1")])
		self.assertIn("differ",self._note(note.name).custom_last_integration_error)
		self._refresh(note, self._found(fx), [self._unit("1")])
		self.assertFalse(self._note(note.name).custom_last_integration_error)

	def test_a_webhook_during_a_running_refresh_makes_the_job_read_again(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		calls = []

		def refresh(name):
			calls.append(name)
			if len(calls) == 1:  # the webhook arrives while the first read runs (its job is dropped by the queue)
				frappe.cache().set_value(noon._REFRESH_AGAIN.format(name), 1)

		with patch.object(noon, "refresh", side_effect=refresh):
			noon.run_refresh(note.name)
		self.assertEqual(calls, [note.name, note.name])

	def test_mark_out_of_stock_and_reconciliation_treat_an_unresolved_create_as_started(self):
		from marketplace_integrations.integrations.noon import order_reconciliation

		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		order = frappe.get_doc("Marketplace Order", fx.marketplace_order.name)
		self.assertTrue(order_reconciliation.build_evidence(order)["lifecycle_permits_change"])

		note = self._draft_note(fx, lr_no="TYPED-1")
		for status in ("Creating", "Creation Unknown"):
			frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", status, update_modified=False)
			frappe.db.commit()
			self.assertTrue(noon.has_unresolved_create(order.name))
			evidence = order_reconciliation.build_evidence(order)
			self.assertEqual(evidence["fulfillment_started"], {"sku:SKU-A": True})
			self.assertFalse(evidence["lifecycle_permits_change"])
			row = frappe.db.get_value("Sales Order Item", {"parent": fx.name}, "name")
			with self.assertRaises(frappe.ValidationError):
				fulfillment.mark_row_out_of_stock(fx.name, row)

	def test_the_nightly_reconcile_resolves_an_unknown_create_by_lookup_only(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="AWB-N1")
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Creation Unknown", "custom_carrier_reference": note.name},
			update_modified=False,
		)
		frappe.db.commit()
		shipment = self._found(fx, [{"awb_nr": "AWB-N1"}])
		with patch(_CLIENT) as client_class:
			client = client_class.return_value
			client.request.side_effect = [shipment]
			client.error_message.side_effect = lambda response: response.message
			noon.run_reconcile()
		self.assertEqual(_paths(client), ["get"])  # GetShipment only, never CreateShipment
		self.assertEqual(self._note(note.name).custom_integration_status, "Created")

	def test_a_draft_that_holds_a_noon_shipment_gets_the_error_for_needs_attention(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="AWB-N2")
		frappe.db.set_value("Delivery Note", note.name, "custom_carrier_reference", note.name, update_modified=False)
		frappe.db.commit()
		shipment = self._found(fx)
		with patch(_CLIENT) as client_class:
			client = client_class.return_value
			client.request.side_effect = [shipment]
			noon.run_reconcile()
		self.assertIn("holds a Noon shipment", self._note(note.name).custom_last_integration_error)
		self.assertEqual(self._note(note.name).custom_integration_status, "Not Created")

	def test_needs_attention_lists_mixed_cancelled_and_draft_holds_rows_for_noon(self):
		from marketplace_integrations.services import marketplace_attention as attention

		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_shipping_status": "Mixed", "custom_last_integration_error": "units differ"},
			update_modified=False,
		)
		frappe.db.commit()
		found = [i for i in attention._collect_carrier_items(attention.CARRIER_EXCEPTION) if i["reference_name"] == note.name]
		self.assertEqual(len(found), 1)
		# Noon never has a carrier delivery id: a cancelled note still appears
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled", update_modified=False)
		frappe.db.commit()
		found = [i for i in attention._collect_carrier_items(attention.CARRIER_CANCELLED) if i["reference_name"] == note.name]
		self.assertEqual(len(found), 1)
		# a draft that holds a Noon shipment (Not Created with the error) is a creation problem
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Not Created", "custom_last_integration_error": "This draft holds a Noon shipment"},
			update_modified=False,
		)
		frappe.db.commit()
		found = [i for i in attention._collect_carrier_items(attention.CARRIER_CREATION_PROBLEM) if i["reference_name"] == note.name]
		self.assertEqual(len(found), 1)

	# ---- shipment invoice (#416) ------------------------------------------------------------------

	_PDF = (
		b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
		b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 3 3]>>endobj\nxref\n0 4\n0000000000 65535 f \n"
		b"0000000009 00000 n \n0000000052 00000 n \n0000000101 00000 n \ntrailer<</Size 4/Root 1 0 R>>\n"
		b"startxref\n249\n%%EOF"
	)

	def _submitted_created_note(self, fixture):
		"""A Created Noon note marked submitted (the database row only: nothing here posts stock)."""
		note = self._created_note(fixture)
		frappe.db.set_value("Delivery Note", note.name, "docstatus", 1, update_modified=False)
		frappe.db.commit()
		self.addCleanup(frappe.db.delete, "File", {"attached_to_doctype": "Delivery Note", "attached_to_name": note.name})
		self.addCleanup(frappe.db.delete, "Integration Request", {"reference_docname": note.name, "custom_operation_type": "Invoice"})
		return note

	def _loose_pdf(self):
		return frappe.get_doc(
			{"doctype": "File", "file_name": "shipment-invoice.pdf", "content": self._PDF, "is_private": 1}
		).insert()

	def _press(self, note, file_doc, key=None, invoice_nr="INV-1"):
		with patch("marketplace_integrations.integrations.noon.delivery_note_shipment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			result = noon.upload_invoice_for_note(note.name, file_doc.file_url, invoice_nr, "2026-11-01", key)
		return result, enqueue

	def _run_invoice(self, note, key, status=200, file_url=None):
		file_url = file_url or frappe.get_all(
			"File", filters={"attached_to_doctype": "Delivery Note", "attached_to_name": note.name}, pluck="file_url"
		)[0]
		with patch(_CLIENT) as client_class:
			client = client_class.return_value
			client.request.return_value = _response(status, message="refused <b>x</b>")
			client.error_message.side_effect = lambda response: response.message
			noon.run_upload_invoice(note.name, file_url, "INV-1", "2026-11-01", key)
		return client

	def test_the_invoice_is_a_private_file_on_the_note_and_the_job_uses_the_shipment_number(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		result, enqueue = self._press(note, self._loose_pdf())
		self.assertTrue(result["queued"])
		attached = frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Delivery Note", "attached_to_name": note.name},
			fields=["file_url", "is_private"],
		)
		self.assertEqual([(row.is_private, row.file_url) for row in attached], [(1, result["file_url"])])
		self.assertEqual(enqueue.call_args.kwargs["idempotency_key"], result["idempotency_key"])
		client = self._run_invoice(note, result["idempotency_key"])
		args, kwargs = client.request.call_args
		self.assertEqual(args, ("POST", "/fbpi/v1/shipment/invoice/upload"))
		self.assertEqual(kwargs["json"]["integration_shipment_nr"], note.name)
		self.assertEqual(kwargs["json"]["warehouse_code"], self.account.noon_warehouse_code)
		self.assertEqual(kwargs["json"]["idempotency_key"], result["idempotency_key"])
		self.assertEqual(kwargs["json"]["issued_at"], "2026-11-01")

	def test_success_records_the_upload_time_and_logs_without_secrets(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		result, _enqueue = self._press(note, self._loose_pdf())
		client = self._run_invoice(note, result["idempotency_key"])
		stored = self._note(note.name)
		self.assertTrue(stored.custom_noon_invoice_uploaded_on)
		self.assertEqual(frappe.cache().get_value(f"noon-invoice-result|{result['idempotency_key']}")["state"], "done")
		rows = frappe.get_all(
			"Integration Request",
			filters={"custom_operation_type": "Invoice", "reference_doctype": "Delivery Note", "reference_docname": note.name},
			fields=["*"],
		)
		self.assertEqual(len(rows), 1)
		dump = json.dumps(rows[0], default=str)
		self.assertEqual((rows[0].custom_result, rows[0].custom_external_reference), ("Success", note.name))
		self.assertNotIn(client.request.call_args.kwargs["json"]["invoice"], dump)
		self.assertNotIn("test-key-id", dump)

	def test_retry_reuses_the_key_and_a_new_press_makes_a_new_key(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		first, _enqueue = self._press(note, self._loose_pdf())
		self._run_invoice(note, first["idempotency_key"], status=500)
		failed = self._note(note.name)
		self.assertFalse(failed.custom_noon_invoice_uploaded_on, "a failure records no upload")
		retry, enqueue = self._press(note, frappe._dict(file_url=first["file_url"]), key=first["idempotency_key"])
		self.assertEqual(retry["idempotency_key"], first["idempotency_key"])
		self.assertEqual(enqueue.call_args.kwargs["idempotency_key"], first["idempotency_key"])
		self.assertEqual(
			frappe.cache().get_value(f"noon-invoice-result|{first['idempotency_key']}")["state"], "pending"
		)
		self.assertEqual(
			len(frappe.get_all("File", filters={"attached_to_doctype": "Delivery Note", "attached_to_name": note.name})), 1
		)
		self._run_invoice(note, first["idempotency_key"])
		second, _enqueue = self._press(note, self._loose_pdf(), invoice_nr="INV-2")
		self.assertNotEqual(second["idempotency_key"], first["idempotency_key"])

	def test_a_second_upload_replaces_the_time_of_the_first(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		first, _enqueue = self._press(note, self._loose_pdf())
		self._run_invoice(note, first["idempotency_key"])
		frappe.db.set_value("Delivery Note", note.name, "custom_noon_invoice_uploaded_on", "2026-01-01 10:00:00", update_modified=False)
		second, _enqueue = self._press(note, self._loose_pdf(), invoice_nr="INV-2")
		self._run_invoice(note, second["idempotency_key"])
		self.assertGreater(str(self._note(note.name).custom_noon_invoice_uploaded_on), "2026-01-02")

	def test_a_double_press_while_an_upload_runs_is_refused_and_the_job_clears_the_marker(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		first, _enqueue = self._press(note, self._loose_pdf())
		with self.assertRaises(frappe.ValidationError):
			self._press(note, self._loose_pdf(), invoice_nr="INV-2")
		# the same key (a Retry of the running press) is not a second upload
		retry, _enqueue = self._press(note, frappe._dict(file_url=first["file_url"]), key=first["idempotency_key"])
		self.assertEqual(retry["idempotency_key"], first["idempotency_key"])
		self._run_invoice(note, first["idempotency_key"])
		second, _enqueue = self._press(note, self._loose_pdf(), invoice_nr="INV-2")
		self.assertNotEqual(second["idempotency_key"], first["idempotency_key"])
		self._run_invoice(note, second["idempotency_key"])

	def test_a_key_is_bound_to_its_document(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._submitted_created_note(fx)
		other = self._submitted_created_note(self._order([("SKU-B", 1)], ["SKU-B"]))
		first, _enqueue = self._press(note, self._loose_pdf())
		fulfillment.set_invoice_result(first["idempotency_key"], "failed", "Noon said no")
		self.assertEqual(fulfillment.invoice_result(first["idempotency_key"], "Delivery Note", note.name)["state"], "failed")
		self.assertEqual(fulfillment.invoice_result(first["idempotency_key"], "Delivery Note", other.name)["state"], "pending")
		with self.assertRaises(frappe.ValidationError):
			self._press(other, frappe._dict(file_url=first["file_url"]), key=first["idempotency_key"])
		self._run_invoice(note, first["idempotency_key"])

	def test_a_loose_upload_that_shares_the_attached_copys_path_is_removed_but_the_file_stays(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		loose = self._loose_pdf()
		result, _enqueue = self._press(note, loose)
		self.assertFalse(frappe.db.exists("File", loose.name), "the orphan loose row is gone")
		self.assertTrue(frappe.get_all("File", filters={"file_url": result["file_url"]}), "the attached copy remains")
		self.assertTrue(frappe.get_doc("File", {"file_url": result["file_url"]}).get_content())
		self._run_invoice(note, result["idempotency_key"])

	def test_a_failure_shows_in_the_result_escaped_and_never_changes_the_note(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		result, _enqueue = self._press(note, self._loose_pdf())
		self._run_invoice(note, result["idempotency_key"], status=400)
		outcome = frappe.cache().get_value(f"noon-invoice-result|{result['idempotency_key']}")
		self.assertEqual(outcome["state"], "failed")
		self.assertNotIn("<b>", outcome["message"])
		stored = self._note(note.name)
		self.assertEqual((stored.docstatus, stored.custom_integration_status), (1, "Created"))
		# Submit and Cancel stay available: nothing in the invoice path touches the note's status
		self.assertFalse(stored.custom_noon_invoice_uploaded_on)

	def test_the_press_is_refused_for_what_is_not_a_created_submitted_noon_note(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		draft = self._created_note(fx)  # Created but still a draft
		with self.assertRaises(frappe.ValidationError):
			self._press(draft, self._loose_pdf())
		frappe.db.set_value("Delivery Note", draft.name, {"docstatus": 1, "custom_integration_status": "Cancellation Unknown"}, update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			self._press(draft, self._loose_pdf())
		self.assertFalse(frappe.get_all("File", filters={"attached_to_doctype": "Delivery Note", "attached_to_name": draft.name}))

	def test_the_press_is_refused_unless_the_warehouse_is_fbpi_ready(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		for overrides, reason in (
			({"noon_fulfillment_system_code": "OTHER"}, "OTHER, not FBPI"),
			({"noon_warehouse_active": 0}, "not active"),
		):
			self._fbpi_ready_account(**overrides)
			with self.assertRaisesRegex(frappe.ValidationError, reason):
				self._press(note, self._loose_pdf())
			self._fbpi_ready_account()
		self.assertFalse(frappe.get_all("File", filters={"attached_to_doctype": "Delivery Note", "attached_to_name": note.name}))

	def test_the_press_refuses_a_missing_number_date_a_bad_key_and_a_non_pdf(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		with patch("marketplace_integrations.integrations.noon.delivery_note_shipment.frappe.enqueue") as enqueue:
			with self.assertRaises(frappe.ValidationError):
				noon.upload_invoice_for_note(note.name, self._loose_pdf().file_url, " ", "2026-11-01")
			with self.assertRaises(frappe.ValidationError):
				noon.upload_invoice_for_note(note.name, self._loose_pdf().file_url, "INV-1", "")
			with self.assertRaises(frappe.ValidationError):
				noon.upload_invoice_for_note(note.name, self._loose_pdf().file_url, "INV-1", "2026-11-01", "bad key!")
			not_pdf = frappe.get_doc(
				{"doctype": "File", "file_name": "x.bin", "content": b"not a pdf", "is_private": 1}
			).insert()
			with self.assertRaises(frappe.ValidationError):
				noon.upload_invoice_for_note(note.name, not_pdf.file_url, "INV-1", "2026-11-01")
		enqueue.assert_not_called()

	def test_the_endpoint_needs_write_on_the_delivery_note(self):
		from marketplace_integrations.api import noon_fulfillment
		from marketplace_integrations.tests.utils import make_role_user

		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		writer, _role = make_role_user(self, "Noon Invoice Note Writer", {"Delivery Note": ["read", "write"]})
		reader, _role = make_role_user(self, "Noon Invoice Note Reader", {"Delivery Note": ["read"]})
		with patch.object(noon, "upload_invoice_for_note", return_value={"queued": True}) as upload:
			with self.set_user(writer):
				noon_fulfillment.upload_delivery_note_invoice(note.name, "/private/files/i.pdf", "INV-1", "2026-11-01")
			upload.assert_called_once_with(note.name, "/private/files/i.pdf", "INV-1", "2026-11-01", None)
			with self.set_user(reader), self.assertRaises(frappe.PermissionError):
				noon_fulfillment.upload_delivery_note_invoice(note.name, "/private/files/i.pdf", "INV-1", "2026-11-01")
		self.assertEqual(upload.call_count, 1)

	def test_amending_a_note_does_not_carry_the_invoice_record(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="TYPED-1")
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_noon_invoice_uploaded_on": "2026-11-01 10:00:00"},
			update_modified=False,
		)
		new = frappe.copy_doc(self._note(note.name), ignore_no_copy=False)  # Amend copies even no_copy fields
		new.lr_no = None  # the typed AWB belongs to the old note (the duplicate AWB rule is not under test)
		new.custom_noon_generate_awb = 1
		noon.guard_changes(new)
		self.assertFalse(new.custom_noon_invoice_uploaded_on)
