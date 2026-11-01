# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Customer Return Pickup (Bosta type 25, epic #413, #411). Only the HTTP layer is faked."""

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.bosta import delivery_note_shipment as shipment
from marketplace_integrations.integrations.bosta import statuses
from marketplace_integrations.services import carrier_shipments
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin, FakeResponse, created

PICKUP = "Customer Return Pickup"


def stored(note, *fields):
	return frappe.db.get_value("Delivery Note", note.name, list(fields), as_dict=True)


class IntegrationTestBostaReturnPickup(BostaFixtureMixin, IntegrationTestCase):
	def _submitted(self, qty=1):
		"""A submitted outbound Bosta note (Created, as Submit requires)."""
		note = self.make_note(order=self.make_order(qty=qty))
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Created", "custom_carrier_delivery_id": "OUT-1", "lr_no": "TRK-OUT"},
		)
		note = frappe.get_doc("Delivery Note", note.name)
		note.submit()
		frappe.db.commit()
		return note

	def _return(self, source=PICKUP, returned_cod=500, qty=1, **values):
		"""A saved draft Sales Return of a submitted Bosta note, made the way staff make it."""
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

		original = self._submitted(qty)
		draft = make_sales_return(original.name)
		draft.transporter = self.supplier
		draft.shipping_address_name = self.address.name
		draft.contact_mobile = "01000000000"
		draft.custom_bosta_city = "EG-01"
		draft.custom_bosta_city_name = "Cairo"
		if source:
			draft.custom_return_source = source
			draft.custom_returned_cod = returned_cod
		draft.update(values)
		if source == shipment.RETURNED_BY_BOSTA:
			draft.flags[shipment.AUTOMATIC_RETURN_FLAG] = True  # only the app makes this source
		draft.insert()
		frappe.db.commit()
		return draft

	def test_a_person_cannot_set_or_clear_returned_by_bosta(self):
		with self.assertRaises(frappe.ValidationError):
			self._return(source=None, custom_return_source="Returned by Bosta")

		automatic = self._return(source="Returned by Bosta")
		person = frappe.get_doc("Delivery Note", automatic.name)
		person.custom_return_source = ""
		with self.assertRaises(frappe.ValidationError):
			person.save()

		plain = frappe.get_doc("Delivery Note", self._return(source=None).name)
		plain.custom_return_source = "Returned by Bosta"
		with self.assertRaises(frappe.ValidationError):
			plain.save()

	def test_a_new_pickup_return_does_not_keep_the_original_deliverys_tracking_number(self):
		draft = self._return()
		values = stored(draft, "lr_no", "lr_date")
		self.assertFalse(values.lr_no)
		self.assertFalse(values.lr_date)

	def test_marking_a_saved_return_as_pickup_later_also_clears_the_copied_tracking_number(self):
		draft = self._return(source=None)
		frappe.db.set_value("Delivery Note", draft.name, {"lr_no": "TRK-OUT", "lr_date": "2026-11-01"})
		draft = frappe.get_doc("Delivery Note", draft.name)
		draft.custom_return_source = PICKUP
		draft.custom_returned_cod = 0
		draft.save()
		values = stored(draft, "lr_no", "lr_date")
		self.assertFalse(values.lr_no)
		self.assertFalse(values.lr_date)

	# -- who owns a return -------------------------------------------------------------------

	def test_only_a_pickup_return_belongs_to_bosta(self):
		pickup = self._return()
		plain = self._return(source=None)
		by_bosta = self._return(source="Returned by Bosta")

		self.assertTrue(shipment.applies_to(pickup))
		self.assertFalse(shipment.applies_to(plain))
		self.assertFalse(shipment.applies_to(by_bosta))
		self.assertTrue(shipment.applies_to(self._submitted()))

	def test_applies_to_accepts_a_plain_dict_with_a_real_transporter(self):
		"""Needs Attention judges notes by ``{"is_return", "transporter"}`` dicts, not documents."""
		self.assertTrue(shipment.applies_to({"is_return": 0, "transporter": self.supplier}))
		self.assertFalse(shipment.applies_to({"is_return": 1, "transporter": self.supplier}))
		self.assertFalse(shipment.applies_to({"is_return": 0, "transporter": None}))

	# -- payload -----------------------------------------------------------------------------

	def test_the_request_is_type_25_with_a_negative_cod_and_the_normal_addresses(self):
		draft = self._return(returned_cod=500)

		payload = shipment.build_payload(draft)

		self.assertEqual(payload["type"], 25)
		self.assertEqual(payload["cod"], -500)
		self.assertEqual(payload["dropOffAddress"], {"city": "EG-01", "firstLine": "12 Example Street"})
		self.assertEqual(payload["businessLocationId"], "LOC-1")
		self.assertNotIn("returnAddress", payload)
		self.assertEqual(payload["businessReference"], draft.name)
		self.assertEqual(payload["uniqueBusinessReference"], draft.name)

	def test_a_returned_cod_of_zero_sends_zero(self):
		draft = self._return(returned_cod=0)

		self.assertEqual(shipment.build_payload(draft)["cod"], 0)

	def test_a_normal_note_still_sends_type_10_and_its_positive_cod(self):
		note = self.make_note(cod=120)

		payload = shipment.build_payload(note)

		self.assertEqual((payload["type"], payload["cod"]), (10, 120))

	def test_a_returned_cod_below_zero_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._return(returned_cod=-1)

		draft = self._return(returned_cod=10)
		draft.custom_returned_cod = -5
		with self.assertRaises(frappe.ValidationError):
			draft.save()

	# -- request through the neutral state machine --------------------------------------------

	def test_the_request_creates_the_pickup_as_a_return_pickup_operation(self):
		draft = self._return(returned_cod=300)

		result = carrier_shipments.request_create(draft.name)

		self.assertEqual(result, {"status": "Creating", "queued": True})
		kwargs = self.enqueue.call_args.kwargs
		self.assertEqual(kwargs["job_id"], f"bosta-create|{draft.name}")
		self.assertFalse(kwargs["lookup_first"])
		self.http.queue["create"].append(created(tracking="TRK-PICKUP", delivery_id="PICK-1"))

		carrier_shipments.run_create(draft.name, log=kwargs["log"], lookup_first=False)

		values = stored(draft, "custom_integration_status", "lr_no", "custom_carrier_delivery_id")
		self.assertEqual(values.custom_integration_status, "Created")
		self.assertEqual(values.lr_no, "TRK-PICKUP")
		sent = self.http.calls_of("create")
		self.assertEqual(len(sent), 1)
		self.assertEqual((sent[0]["type"], sent[0]["cod"]), (25, -300))
		logs = frappe.get_all(
			"Integration Request",
			filters={"reference_docname": draft.name},
			fields=["custom_operation_type", "custom_result", "custom_external_reference"],
		)
		self.assertEqual([(r.custom_operation_type, r.custom_result) for r in logs], [("Return Pickup", "Success")])
		self.assertEqual(logs[0].custom_external_reference, draft.name)
		# no label job for a return pickup
		self.assertNotIn("bosta-label|" + draft.name, [c.kwargs.get("job_id") for c in self.enqueue.call_args_list])

	def test_a_second_request_after_created_creates_nothing_more(self):
		draft = self._return()
		carrier_shipments.request_create(draft.name)
		self.http.queue["create"].append(created(tracking="TRK-PICKUP", delivery_id="PICK-1"))
		carrier_shipments.run_create(draft.name, log=self.enqueue.call_args.kwargs["log"], lookup_first=False)

		again = carrier_shipments.request_create(draft.name)

		self.assertEqual(again, {"status": "Created", "queued": False})
		self.assertEqual(len(self.http.calls_of("create")), 1)

	def test_a_retry_after_an_unknown_result_looks_the_pickup_up_first(self):
		draft = self._return()
		frappe.db.set_value(
			"Delivery Note",
			draft.name,
			{"custom_integration_status": "Creation Unknown", "custom_carrier_reference": draft.name},
		)
		frappe.db.commit()

		carrier_shipments.request_create(draft.name)

		self.assertTrue(self.enqueue.call_args.kwargs["lookup_first"])

	def test_a_return_that_is_not_marked_for_pickup_cannot_be_requested(self):
		plain = self._return(source=None)

		with self.assertRaises(frappe.ValidationError):
			carrier_shipments.request_create(plain.name)
		self.assertEqual(self.http.sent(), [])

	# -- submit: the return stays submittable --------------------------------------------------

	def test_a_plain_return_and_a_returned_by_bosta_draft_submit_without_a_shipment(self):
		for source in (None, "Returned by Bosta"):
			draft = self._return(source=source)
			draft.submit()
			self.assertEqual(draft.docstatus, 1)
			self.assertEqual(self.http.sent(), [])

	def test_a_pickup_return_submits_unless_the_pickup_is_still_being_requested(self):
		not_started = self._return()
		not_started.submit()
		self.assertEqual(not_started.docstatus, 1)

		sending = self._return()
		frappe.db.set_value("Delivery Note", sending.name, "custom_integration_status", "Creating")
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc("Delivery Note", sending.name).submit()

	def test_an_outbound_note_still_needs_its_shipment_to_submit(self):
		note = self.make_note()

		with self.assertRaises(frappe.ValidationError):
			note.submit()

	# -- guards and events ---------------------------------------------------------------------

	def test_the_returned_cod_and_source_are_locked_once_the_pickup_started(self):
		draft = self._return(returned_cod=100)
		frappe.db.set_value("Delivery Note", draft.name, "custom_integration_status", "Created")
		draft = frappe.get_doc("Delivery Note", draft.name)
		draft.custom_returned_cod = 200

		with self.assertRaises(frappe.ValidationError):
			draft.save()

	def _started(self, draft, delivery_id="PICK-9", tracking="TRK-PICK-9", **values):
		frappe.db.set_value(
			"Delivery Note",
			draft.name,
			{
				"custom_integration_status": "Created",
				"custom_carrier_delivery_id": delivery_id,
				"lr_no": tracking,
				**values,
			},
		)
		frappe.db.commit()

	def test_a_pickup_return_is_matched_by_a_webhook_but_a_plain_return_is_not(self):
		draft = self._return()
		self._started(draft)
		plain = self._return(source=None)
		self._started(plain, delivery_id="PLAIN-9", tracking="TRK-PLAIN-9")

		self.assertEqual(statuses._note_for("PICK-9", "TRK-PICK-9"), draft.name)
		self.assertIsNone(statuses._note_for("PLAIN-9", "TRK-PLAIN-9"))

	def test_the_returned_states_end_a_pickup_but_not_a_delivery(self):
		self.assertIn("Returned to business", statuses.terminal_state_names(True))
		self.assertIn("Returned to stock", statuses.terminal_state_names(True))
		self.assertNotIn("Returned to business", statuses.terminal_state_names(False))
		self.assertNotIn("Returned to business", statuses.terminal_state_names())
		self.assertEqual(shipment.terminal_states(True), statuses.terminal_state_names(True))

	def test_a_returned_state_on_a_pickup_makes_no_draft_return_and_is_not_polled_again(self):
		from unittest.mock import patch

		from marketplace_integrations.integrations.bosta import returns

		draft = self._return()
		self._started(draft)
		draft = frappe.get_doc("Delivery Note", draft.name)
		draft.submit()
		frappe.db.commit()

		with patch.object(returns, "queue_returned_draft") as queue:
			statuses._write_state(draft.name, 46)

		queue.assert_not_called()
		self.assertNotIn(draft.name, statuses._open_notes())

	def test_needs_attention_lists_a_failed_or_exceptional_pickup_but_not_a_returned_one(self):
		from marketplace_integrations.services import marketplace_attention as attention

		failed = self._return()
		frappe.db.set_value(
			"Delivery Note",
			failed.name,
			{"custom_integration_status": "Creation Failed", "custom_last_integration_error": "no"},
		)
		exception = self._return()
		self._started(exception, delivery_id="PICK-EX", tracking="TRK-EX", custom_shipping_status="Exception")
		lost = self._return()
		self._started(lost, delivery_id="PICK-LOST", tracking="TRK-LOST", custom_shipping_status="Lost")
		returned = self._return()
		self._started(returned, delivery_id="PICK-RET", tracking="TRK-RET", custom_shipping_status="Returned to business")
		frappe.db.commit()

		def listed(kind):
			return {row["reference_name"] for row in attention._collect_carrier_items(kind)}

		self.assertIn(failed.name, listed(attention.CARRIER_CREATION_PROBLEM))
		exceptions = listed(attention.CARRIER_EXCEPTION)
		self.assertIn(exception.name, exceptions)
		self.assertIn(lost.name, exceptions)
		self.assertNotIn(returned.name, listed(attention.CARRIER_RETURN))
		self.assertNotIn(returned.name, listed(attention.CARRIER_EXCEPTION))

	def test_a_created_pickup_return_may_have_its_quantity_reduced_never_raised(self):
		draft = self._return(qty=4)
		self._started(draft)
		stored_qty = frappe.get_doc("Delivery Note", draft.name).items[0].qty  # negative on a return
		self.assertEqual(stored_qty, -4)

		lower = frappe.get_doc("Delivery Note", draft.name)
		lower.items[0].qty = -2
		lower.save()
		self.assertEqual(frappe.get_doc("Delivery Note", draft.name).items[0].qty, -2)

		higher = frappe.get_doc("Delivery Note", draft.name)
		higher.items[0].qty = -3  # more than the -2 now stored
		with self.assertRaises(frappe.ValidationError):
			higher.save()

		removed = frappe.get_doc("Delivery Note", draft.name)
		removed.items = []
		with self.assertRaises(frappe.ValidationError):
			removed.save()

	def test_a_pickup_return_is_not_in_the_order_summary(self):
		draft = self._return()
		self._started(draft)
		order = frappe.db.get_value("Delivery Note Item", {"parent": draft.return_against}, "against_sales_order")
		statuses.update_sales_order_summary(order)
		summary = frappe.db.get_value("Sales Order", order, "custom_shipping_status_summary")
		self.assertEqual(frappe.parse_json(summary or "{}"), {"Created": 1})

	def test_a_refused_pickup_is_creation_failed_and_can_be_requested_again(self):
		draft = self._return()
		carrier_shipments.request_create(draft.name)
		self.http.queue["create"].append(FakeResponse(400, {"success": False, "message": "no"}))

		carrier_shipments.run_create(draft.name, log=self.enqueue.call_args.kwargs["log"], lookup_first=False)

		self.assertEqual(stored(draft, "custom_integration_status").custom_integration_status, "Creation Failed")
		self.assertEqual(carrier_shipments.request_create(draft.name)["queued"], True)
