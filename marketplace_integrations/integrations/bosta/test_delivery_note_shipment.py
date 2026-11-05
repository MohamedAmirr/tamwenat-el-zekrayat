# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The Bosta shipment of a Delivery Note (epic #413, #408): request, job, lookup-before-create, guards.

Real database; only the HTTP layer is faked (``tests/bosta_fixtures.py``) and ``frappe.enqueue`` is
patched, because a queued job never fires in a test: the tests call ``run_create`` themselves."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api import bosta_shipments
from marketplace_integrations.api import carrier_shipments as carrier_api
from marketplace_integrations.integrations.bosta import delivery_note_shipment as shipment
from marketplace_integrations.services import carrier_log, carrier_shipments
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin, FakeResponse, created
from marketplace_integrations.tests.utils import make_role_user

FOUND = {
	"success": True,
	"data": {
		"count": 1,
		"deliveries": [{"_id": "DELIVERY-9", "trackingNumber": "TRK-9", "businessReference": None}],
	},
}
NONE_FOUND = {"success": True, "data": {"count": 0, "deliveries": []}}


def stored(note, *fields):
	return frappe.db.get_value("Delivery Note", note.name, list(fields), as_dict=True)


class IntegrationTestBostaDeliveryNoteShipment(BostaFixtureMixin, IntegrationTestCase):
	def _start(self, note):
		"""request_create then the job body, as the worker would run it."""
		carrier_shipments.request_create(note.name)
		kwargs = self.enqueue.call_args.kwargs
		carrier_shipments.run_create(note.name, kwargs["log"], kwargs["lookup_first"])
		return kwargs["log"]

	def _found(self, note):
		body = json.loads(json.dumps(FOUND))
		body["data"]["deliveries"][0]["businessReference"] = note.name
		return FakeResponse(200, body)

	# -- new note ----------------------------------------------------------------------------

	def test_a_new_note_starts_not_created_with_the_default_cod(self):
		note = self.make_note(self.make_order(rate=100, qty=2, advance_paid=50))

		values = stored(note, "custom_integration_status", "custom_cod_amount")
		self.assertEqual(values.custom_integration_status, "Not Created")
		# The note ships the whole order, so the whole advance is deducted.
		self.assertEqual(values.custom_cod_amount, 150)

	def test_a_typed_zero_cod_means_prepaid_and_is_kept(self):
		note = self.make_note(cod=0)

		self.assertEqual(stored(note, "custom_cod_amount").custom_cod_amount, 0)

	def test_cod_above_the_bosta_limit_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_note(cod=30001)

	def test_an_amended_note_does_not_inherit_the_shipment(self):
		note = self.make_note()
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Created", "custom_carrier_delivery_id": "D-1", "lr_no": "T-1"},
		)
		# Desk Amend copies even no_copy fields; the guard runs on that unsaved copy.
		amended = frappe.copy_doc(frappe.get_doc("Delivery Note", note.name))
		amended.amended_from = note.name
		amended.custom_carrier_delivery_id = "D-1"
		amended.custom_integration_status = "Created"
		amended.lr_no = "T-1"

		shipment.guard_changes(amended)

		self.assertEqual(amended.custom_integration_status, "Not Created")
		self.assertFalse(amended.custom_carrier_delivery_id)
		self.assertFalse(amended.lr_no)

	def test_bosta_is_refused_for_a_platform_that_fulfils_its_own_orders(self):
		with patch.object(shipment, "excluded_platform", return_value="Noon"):
			with self.assertRaises(frappe.ValidationError):
				self.make_note()

	# -- COD: default until confirmed ---------------------------------------------------------

	def test_the_default_cod_is_recalculated_on_every_save_until_it_is_confirmed(self):
		note = self.make_note(self.make_order(rate=100, qty=4), confirm_cod=False)
		self.assertEqual(stored(note, "custom_cod_amount", "custom_cod_confirmed").custom_cod_confirmed, 0)
		self.assertEqual(stored(note, "custom_cod_amount").custom_cod_amount, 400)

		doc = frappe.get_doc("Delivery Note", note.name)
		doc.items[0].qty = 2
		doc.save()

		values = stored(note, "custom_cod_amount", "custom_cod_confirmed")
		self.assertEqual(values.custom_cod_amount, 200)
		self.assertEqual(values.custom_cod_confirmed, 0)

	def test_a_carrier_chosen_after_the_first_save_gets_the_default_cod(self):
		note = self.make_note(self.make_order(rate=100, qty=3), confirm_cod=False, transporter=None)
		self.assertFalse(stored(note, "custom_cod_amount").custom_cod_amount)

		doc = frappe.get_doc("Delivery Note", note.name)
		doc.transporter = self.supplier
		doc.save()

		values = stored(note, "custom_cod_amount", "custom_cod_confirmed")
		self.assertEqual(values.custom_cod_amount, 300)
		self.assertEqual(values.custom_cod_confirmed, 0)

	def test_a_changed_cod_counts_as_confirmed_and_a_typed_zero_is_accepted(self):
		note = self.make_note(self.make_order(rate=100, qty=2), confirm_cod=False)

		doc = frappe.get_doc("Delivery Note", note.name)
		doc.custom_cod_amount = 0
		doc.save()
		values = stored(note, "custom_cod_amount", "custom_cod_confirmed")
		self.assertEqual((values.custom_cod_amount, values.custom_cod_confirmed), (0, 1))

		# Confirmed: later saves keep it, whatever the items now say.
		doc = frappe.get_doc("Delivery Note", note.name)
		doc.items[0].qty = 1
		doc.save()
		self.assertEqual(stored(note, "custom_cod_amount").custom_cod_amount, 0)
		self.http.queue["create"].append(created())
		self._start(note)
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Created")

	def test_ticking_confirmed_keeps_the_default_amount(self):
		note = self.make_note(self.make_order(rate=100, qty=2), confirm_cod=False)

		doc = frappe.get_doc("Delivery Note", note.name)
		doc.custom_cod_confirmed = 1
		doc.save()
		doc.reload()
		doc.items[0].qty = 1
		doc.save()

		self.assertEqual(stored(note, "custom_cod_amount").custom_cod_amount, 200)

	def test_unticking_confirmed_goes_back_to_the_default(self):
		note = self.make_note(self.make_order(rate=100, qty=2), cod=5)
		doc = frappe.get_doc("Delivery Note", note.name)
		doc.custom_cod_confirmed = 0
		doc.save()

		self.assertEqual(stored(note, "custom_cod_amount").custom_cod_amount, 200)

	def test_a_cod_that_was_never_confirmed_cannot_be_sent(self):
		note = self.make_note(confirm_cod=False)

		with self.assertRaises(frappe.ValidationError):
			carrier_shipments.request_create(note.name)

		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Not Created")
		self.assertEqual(self.http.sent(), [])

	def test_the_default_cod_survives_a_deleted_sales_order(self):
		note = self.make_note()
		doc = frappe.get_doc("Delivery Note", note.name)
		with patch.object(shipment, "sales_order_of", return_value="SO-DOES-NOT-EXIST"):
			self.assertEqual(shipment.default_cod(doc), doc.grand_total)

	def test_the_default_cod_is_the_grand_total_not_the_rounded_total(self):
		# Shopify's total (shipping included) is the grand total; the rounded total can differ by up to 0.50.
		note = self.make_note(self.make_order(rate=555.5), confirm_cod=False)

		doc = frappe.get_doc("Delivery Note", note.name)
		self.assertEqual(doc.grand_total, 555.5)
		self.assertEqual(shipment.default_cod(doc), 555.5)
		self.assertEqual(stored(note, "custom_cod_amount").custom_cod_amount, 555.5)
		# The advance is deducted from the same grand-total basis.
		frappe.db.set_value("Sales Order", doc.items[0].against_sales_order, "advance_paid", 100)
		self.assertEqual(shipment.default_cod(doc), 455.5)

	def test_a_fully_paid_order_that_rounds_down_has_cod_zero(self):
		# ERPNext caps the recorded payment at the rounded total: 494 paid on grand 494.21 / rounded 494 is
		# fully paid, so the single note gets COD 0, not the 0.21 the grand total alone would leave.
		order = self.make_order(rate=494.21)
		frappe.db.set_value("Sales Order", order.name, {"rounded_total": 494, "advance_paid": 494})
		note = self.make_note(order, confirm_cod=False)

		doc = frappe.get_doc("Delivery Note", note.name)
		self.assertEqual(shipment.default_cod(doc), 0)
		self.assertEqual(stored(note, "custom_cod_amount").custom_cod_amount, 0)

	def test_a_part_paid_or_unpaid_order_keeps_the_grand_total_basis(self):
		order = self.make_order(rate=555.5)
		frappe.db.set_value("Sales Order", order.name, {"rounded_total": 556, "advance_paid": 200})
		doc = frappe.get_doc("Delivery Note", self.make_note(order, confirm_cod=False).name)
		self.assertEqual(shipment.default_cod(doc), 355.5)
		frappe.db.set_value("Sales Order", order.name, "advance_paid", 0)
		self.assertEqual(shipment.default_cod(doc), 555.5)

	def test_an_amended_note_must_confirm_its_cod_again(self):
		note = self.make_note(cod=77)
		amended = frappe.copy_doc(frappe.get_doc("Delivery Note", note.name))
		amended.amended_from = note.name
		amended.custom_cod_amount = 77
		amended.custom_cod_confirmed = 1

		shipment.guard_changes(amended)

		self.assertEqual(amended.custom_cod_confirmed, 0)
		self.assertNotEqual(amended.custom_cod_amount, 77)

	# -- request -----------------------------------------------------------------------------

	def test_request_queues_exactly_one_job_and_a_second_request_changes_nothing(self):
		note = self.make_note()

		first = carrier_shipments.request_create(note.name)
		second = carrier_shipments.request_create(note.name)

		self.assertEqual(first, {"status": "Creating", "queued": True})
		self.assertEqual(second["queued"], False)
		self.assertEqual(self.enqueue.call_count, 1)
		kwargs = self.enqueue.call_args.kwargs
		self.assertEqual(kwargs["job_id"], f"bosta-create|{note.name}")
		self.assertTrue(kwargs["deduplicate"])
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(frappe.db.count("Integration Request", {"reference_docname": note.name}), 1)
		self.assertEqual(stored(note, "custom_carrier_reference").custom_carrier_reference, note.name)
		self.assertEqual(self.http.sent(), [])

	def test_an_empty_city_is_filled_from_the_address_before_the_job_is_queued(self):
		note = self.make_note(city="")
		frappe.db.set_value("Delivery Note", note.name, "custom_bosta_city_name", "")

		result = carrier_shipments.request_create(note.name)

		self.assertEqual(result, {"status": "Creating", "queued": True})
		values = stored(note, "custom_bosta_city", "custom_bosta_city_name")
		self.assertEqual((values.custom_bosta_city, values.custom_bosta_city_name), ("EG-01", "Cairo"))

	def test_the_city_autofill_alone_does_not_bump_modified(self):
		# The Creating write of request_create bumps ``modified`` on purpose; the autofill must not, so a
		# form opened before it still submits.
		note = self.make_note(city="")
		frappe.db.set_value("Delivery Note", note.name, "custom_bosta_city_name", "")
		modified = stored(note, "modified").modified

		self.assertTrue(shipment.autofill_city(frappe.get_doc("Delivery Note", note.name)))

		values = stored(note, "custom_bosta_city", "modified")
		self.assertEqual(values.custom_bosta_city, "EG-01")
		self.assertEqual(values.modified, modified)

	def test_a_city_that_is_already_set_is_never_replaced_by_the_address_city(self):
		note = self.make_note(city="EG-25")

		carrier_shipments.request_create(note.name)

		self.assertEqual(stored(note, "custom_bosta_city").custom_bosta_city, "EG-25")

	def test_an_empty_city_the_address_cannot_fill_asks_the_person_without_leaving_the_note_creating(self):
		frappe.db.set_value("Address", self.address.name, "city", "Nowhere Land")
		note = self.make_note(city="")

		result = carrier_shipments.request_create(note.name)

		self.assertEqual(result, {"status": "Not Created", "queued": False, "needs_city": True})
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Not Created")
		self.assertEqual(frappe.db.count("Integration Request", {"reference_docname": note.name}), 0)
		self.assertEqual(self.enqueue.call_count, 0)

	def test_a_failed_city_lookup_asks_the_person_instead_of_failing(self):
		note = self.make_note(city="")
		self.http.queue["all_districts"].append(TimeoutError("slow"))

		result = carrier_shipments.request_create(note.name)

		self.assertTrue(result.get("needs_city"))
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Not Created")

	def test_the_validation_asks_for_a_city_instead_of_throwing_when_it_is_called_directly(self):
		frappe.db.set_value("Address", self.address.name, "city", "Nowhere Land")
		note = self.make_note(city="")

		result = shipment.validate_ready(frappe.get_doc("Delivery Note", note.name))

		self.assertEqual(result, {"needs_city": True})

	def test_request_refuses_a_stale_form(self):
		note = self.make_note()

		with self.assertRaises(frappe.TimestampMismatchError):
			carrier_shipments.request_create(note.name, "2001-01-01 00:00:00.000000")

	def test_request_needs_submit_permission_on_the_note(self):
		note = self.make_note()
		email, _role = make_role_user(self, "Bosta Reader", {"Delivery Note": ["read"]})
		frappe.set_user(email)

		with self.assertRaises(frappe.PermissionError):
			carrier_api.request_shipment(note.name)
		frappe.set_user("Administrator")

		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Not Created")

	def test_a_user_with_submit_permission_can_request(self):
		note = self.make_note()
		email, _role = make_role_user(self, "Bosta Submitter", {"Delivery Note": ["read", "write", "submit"]})
		frappe.set_user(email)

		result = carrier_api.request_shipment(note.name)
		frappe.set_user("Administrator")

		self.assertEqual(result["status"], "Creating")

	# -- the job -----------------------------------------------------------------------------

	def test_the_first_attempt_creates_without_a_lookup_and_writes_back(self):
		note = self.make_note()
		self.http.queue["create"].append(created("TRK-1", "DELIVERY-1"))

		log = self._start(note)

		values = stored(
			note, "custom_integration_status", "custom_carrier_delivery_id", "lr_no", "custom_last_sync_at"
		)
		self.assertEqual(values.custom_integration_status, "Created")
		self.assertEqual(values.custom_carrier_delivery_id, "DELIVERY-1")
		self.assertEqual(values.lr_no, "TRK-1")
		self.assertEqual([kind for kind, _ in self.http.sent()], ["create"])
		payload = self.http.calls_of("create")[0]
		self.assertEqual(payload["businessReference"], note.name)
		self.assertEqual(payload["uniqueBusinessReference"], note.name)
		self.assertEqual(payload["dropOffAddress"], {"city": "EG-01", "firstLine": "12 Example Street"})
		self.assertEqual(frappe.db.get_value("Integration Request", log, "custom_result"), "Success")

	# -- the job submits the Created note as the requester (delivery-note-first.md) ---------------

	def _start_as(self, note, email):
		"""request_shipment as ``email`` (the API passes the session user), then the job body."""
		frappe.set_user(email)
		carrier_api.request_shipment(note.name)
		frappe.set_user("Administrator")
		kwargs = self.enqueue.call_args.kwargs
		carrier_shipments.run_create(note.name, kwargs["log"], kwargs["lookup_first"], kwargs["submit_as"])
		return kwargs

	def test_the_job_submits_the_created_note_as_the_user_who_requested_it(self):
		note = self.make_note()
		# Sales User too: ERPNext reads the customer's Address on every Delivery Note save, and Stock roles cannot.
		email = self.make_user("Stock Manager", "Sales User")
		self.http.queue["create"].append(created("TRK-1", "DELIVERY-1"))

		# Frappe writes no Version row while ``frappe.in_test`` unless a save asks for it (``_save`` sets
		# ``ignore_version = frappe.in_test``); the job's own ``.submit()`` cannot be told, so ask for it here.
		real_save = frappe.model.document.Document._save

		def save_with_version(doc, *args, **kwargs):
			kwargs.setdefault("ignore_version", False)
			return real_save(doc, *args, **kwargs)

		with patch.object(frappe.model.document.Document, "_save", autospec=True, side_effect=save_with_version):
			kwargs = self._start_as(note, email)

		self.assertEqual(kwargs["submit_as"], email)
		values = stored(note, "docstatus", "custom_integration_status", "custom_last_integration_error")
		self.assertEqual((values.docstatus, values.custom_integration_status), (1, "Created"))
		self.assertFalse(values.custom_last_integration_error)
		# The Version owner is who the system notifies about a later automatic cancel (_submitter).
		self.assertEqual(carrier_shipments._submitter(frappe.get_doc("Delivery Note", note.name)), email)
		self.assertEqual(frappe.session.user, "Administrator")
		# on_submit still queues the label job (after the submit commits)
		jobs = [call.args[0] for call in self.enqueue.call_args_list if call.args]
		self.assertIn("marketplace_integrations.integrations.bosta.delivery_note_shipment.run_fetch_label", jobs)

	def test_a_job_without_submit_as_leaves_the_created_note_a_draft(self):
		note = self.make_note()
		self.http.queue["create"].append(created("TRK-1", "DELIVERY-1"))

		self._start(note)

		self.assertEqual(stored(note, "docstatus", "custom_integration_status").docstatus, 0)
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Created")

	def test_a_failed_submit_leaves_a_created_draft_with_the_reason_and_raises_nothing(self):
		note = self.make_note()
		email = self.make_user("Stock Manager", "Sales User")
		self.http.queue["create"].append(created("TRK-1", "DELIVERY-1"))

		with patch(
			"erpnext.stock.doctype.delivery_note.delivery_note.DeliveryNote.on_submit",
			side_effect=frappe.ValidationError("Negative stock for <b>ITEM-1</b>"),
		):
			self._start_as(note, email)

		values = stored(note, "docstatus", "custom_integration_status", "custom_last_integration_error")
		self.assertEqual((values.docstatus, values.custom_integration_status), (0, "Created"))
		self.assertIn("Shipment created at Bosta; the Delivery Note could not be submitted:", values.custom_last_integration_error)
		self.assertIn("Negative stock for ITEM-1", values.custom_last_integration_error)
		self.assertNotIn("<b>", values.custom_last_integration_error)
		self.assertEqual(frappe.session.user, "Administrator")

	def test_the_job_submits_without_the_system_user_so_a_user_who_cannot_submit_is_refused(self):
		note = self.make_note()
		email = self.make_user("Stock User")
		self.http.queue["create"].append(created("TRK-1", "DELIVERY-1"))
		# The API would have refused this user; a job started for him still must not submit as the system.
		carrier_shipments.request_create(note.name, submit_as=email)
		kwargs = self.enqueue.call_args.kwargs

		real_check = frappe.model.document.Document.check_permission

		def refuse_delivery_notes(doc, *args, **kwargs):
			# Only the note: the job's own Error Log insert must keep working.
			if doc.doctype == "Delivery Note":
				raise frappe.PermissionError
			return real_check(doc, *args, **kwargs)

		with patch.object(
			frappe.model.document.Document, "check_permission", autospec=True, side_effect=refuse_delivery_notes
		):
			carrier_shipments.run_create(note.name, kwargs["log"], kwargs["lookup_first"], kwargs["submit_as"])

		values = stored(note, "docstatus", "custom_integration_status", "custom_last_integration_error")
		self.assertEqual((values.docstatus, values.custom_integration_status), (0, "Created"))
		self.assertIn("could not be submitted", values.custom_last_integration_error)

	def test_check_never_submits_a_note_it_finds_created(self):
		note = self.make_note()
		self.http.queue["create"].append(TimeoutError("lost"))
		self._start(note)
		self.http.queue["search"].append(self._found(note))

		result = carrier_shipments.check(note.name)

		self.assertEqual(result["status"], "Created")
		self.assertEqual(stored(note, "docstatus").docstatus, 0)

	def test_a_retry_request_without_a_user_never_submits(self):
		note = self.make_note()
		self.http.queue["create"].append(FakeResponse(400, {"success": False, "message": "bad phone"}))
		self._start(note)
		self.http.queue["search"].append(FakeResponse(200, NONE_FOUND))
		self.http.queue["create"].append(created("TRK-2", "DELIVERY-2"))

		self._start(note)

		self.assertIsNone(self.enqueue.call_args.kwargs["submit_as"])
		self.assertEqual(stored(note, "docstatus", "custom_integration_status").docstatus, 0)

	def test_a_confirmed_zero_cod_is_sent_as_zero_and_the_differs_hint_does_not_block_it(self):
		note = self.make_note(self.make_order(rate=100, qty=2), cod=0)
		# The merchant's 0 differs from the computed 200: the form says so, the shipment still goes.
		self.assertEqual(shipment.cod_hint(note.name)["default"], 200)
		self.http.queue["create"].append(created("TRK-1", "DELIVERY-1"))

		self._start(note)

		self.assertEqual(self.http.calls_of("create")[0]["cod"], 0)
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Created")
		frappe.get_doc("Delivery Note", note.name).submit()
		self.assertEqual(stored(note, "docstatus", "custom_cod_amount").custom_cod_amount, 0)

	def test_a_definitive_refusal_is_creation_failed_and_the_message_is_stored_raw(self):
		note = self.make_note()
		self.http.queue["create"].append(
			FakeResponse(400, {"success": False, "message": "<img src=x onerror=1> bad phone"})
		)

		log = self._start(note)

		values = stored(note, "custom_integration_status", "custom_last_integration_error")
		self.assertEqual(values.custom_integration_status, "Creation Failed")
		# Stored raw: every place that renders it escapes it once (Needs Attention, the form intro).
		self.assertIn("<img src=x onerror=1>", values.custom_last_integration_error)
		self.assertNotIn("&lt;", values.custom_last_integration_error)
		self.assertEqual(frappe.db.get_value("Integration Request", log, "custom_result"), "Failed")

	def test_an_unknown_outcome_never_becomes_a_failure(self):
		cases = {
			"timeout": TimeoutError("timed out"),
			"server error": FakeResponse(500, {"success": False}),
			"rate limited": FakeResponse(429, {"success": False}),
			"malformed 2xx": FakeResponse(200, {"success": True, "data": {}}),
		}
		for label, outcome in cases.items():
			with self.subTest(label):
				note = self.make_note()
				self.http.queue["create"].append(outcome)

				log = self._start(note)

				self.assertEqual(
					stored(note, "custom_integration_status").custom_integration_status, "Creation Unknown"
				)
				self.assertEqual(frappe.db.get_value("Integration Request", log, "custom_result"), "Unknown")

	def test_a_later_attempt_looks_up_before_it_creates_and_adopts_what_it_finds(self):
		note = self.make_note()
		self.http.queue["create"].append(TimeoutError("lost"))
		self._start(note)
		self.http.queue["search"].append(self._found(note))

		self._start(note)

		self.assertEqual([kind for kind, _ in self.http.sent()], ["create", "search"])
		values = stored(note, "custom_integration_status", "custom_carrier_delivery_id", "lr_no")
		self.assertEqual(values.custom_integration_status, "Created")
		self.assertEqual(values.custom_carrier_delivery_id, "DELIVERY-9")
		self.assertEqual(values.lr_no, "TRK-9")

	def test_a_later_attempt_creates_only_when_the_lookup_finds_nothing(self):
		note = self.make_note()
		self.http.queue["create"] += [TimeoutError("lost"), created("TRK-2", "DELIVERY-2")]
		self._start(note)
		self.http.queue["search"].append(FakeResponse(200, NONE_FOUND))

		self._start(note)

		self.assertEqual([kind for kind, _ in self.http.sent()], ["create", "search", "create"])
		self.assertEqual(stored(note, "lr_no").lr_no, "TRK-2")

	def test_an_ambiguous_lookup_stays_unknown_and_never_creates(self):
		note = self.make_note()
		self.http.queue["create"].append(TimeoutError("lost"))
		self._start(note)
		both = json.loads(json.dumps(FOUND))
		both["data"]["count"] = 2
		both["data"]["deliveries"].append(dict(both["data"]["deliveries"][0], _id="OTHER"))
		for item in both["data"]["deliveries"]:
			item["businessReference"] = note.name
		self.http.queue["search"].append(FakeResponse(200, both))

		self._start(note)

		self.assertEqual([kind for kind, _ in self.http.sent()], ["create", "search"])
		self.assertEqual(
			stored(note, "custom_integration_status").custom_integration_status, "Creation Unknown"
		)

	def test_a_job_for_a_note_that_is_not_creating_does_nothing(self):
		note = self.make_note()

		carrier_shipments.run_create(note.name)

		self.assertEqual(self.http.sent(), [])

	def test_the_lookup_first_rule_survives_the_deletion_of_the_log_rows(self):
		note = self.make_note()
		self.http.queue["create"].append(TimeoutError("lost"))
		self._start(note)
		# Log Settings deletes Integration Request rows after 90 days: the decision must not need them.
		frappe.db.delete("Integration Request", {"reference_docname": note.name})
		self.http.queue["search"].append(FakeResponse(200, NONE_FOUND))
		self.http.queue["create"].append(created("TRK-3", "DELIVERY-3"))

		self._start(note)

		self.assertEqual([kind for kind, _ in self.http.sent()], ["create", "search", "create"])

	def test_the_first_attempt_asks_the_job_for_no_lookup_and_a_later_one_for_a_lookup(self):
		note = self.make_note()
		self.http.queue["create"].append(FakeResponse(400, {"success": False, "message": "bad phone"}))
		self._start(note)
		self.assertFalse(self.enqueue.call_args.kwargs["lookup_first"])

		carrier_shipments.request_create(note.name)

		self.assertTrue(self.enqueue.call_args.kwargs["lookup_first"])

	# -- Check with Bosta --------------------------------------------------------------------

	def test_check_writes_nothing_when_the_note_moved_on_during_the_call(self):
		note = self.make_note()
		self.http.queue["create"].append(TimeoutError("lost"))
		self._start(note)
		self.http.queue["search"].append(FakeResponse(200, NONE_FOUND))
		error_before = stored(note, "custom_last_integration_error").custom_last_integration_error
		real_find = shipment.find_remote_delivery

		def find_then_move_on(reference):
			result = real_find(reference)
			# While Bosta was being asked, another check found the delivery.
			frappe.db.set_value(
				"Delivery Note",
				note.name,
				{"custom_integration_status": "Created", "custom_carrier_delivery_id": "D-OTHER", "lr_no": "T-OTHER"},
			)
			frappe.db.commit()
			return result

		with patch.object(shipment, "find_remote_delivery", side_effect=find_then_move_on):
			result = carrier_shipments.check(note.name)

		self.assertFalse(result["found"])
		values = stored(note, "custom_integration_status", "lr_no", "custom_last_integration_error")
		self.assertEqual(values.custom_integration_status, "Created")
		self.assertEqual(values.lr_no, "T-OTHER")
		# The error of the lost create is still there: the late "still unknown" answer wrote nothing.
		self.assertEqual(values.custom_last_integration_error, error_before)

	def test_check_with_bosta_returns_the_raw_message_for_the_client_to_escape(self):
		note = self.make_note()
		self.http.queue["create"].append(TimeoutError("lost"))
		self._start(note)
		self.http.queue["search"].append(FakeResponse(500, {"success": False, "message": "<b>down</b>"}))

		result = carrier_shipments.check(note.name)

		self.assertIn("<b>down</b>", result["message"])
		self.assertIn("<b>down</b>", stored(note, "custom_last_integration_error").custom_last_integration_error)

	def test_check_with_bosta_only_looks_up_and_never_creates(self):
		note = self.make_note()
		self.http.queue["create"].append(TimeoutError("lost"))
		self._start(note)
		self.http.queue["search"].append(FakeResponse(200, NONE_FOUND))

		result = carrier_shipments.check(note.name)

		self.assertFalse(result["found"])
		self.assertEqual([kind for kind, _ in self.http.sent()], ["create", "search"])
		self.assertEqual(
			stored(note, "custom_integration_status").custom_integration_status, "Creation Unknown"
		)

	def test_check_with_bosta_adopts_a_found_delivery(self):
		note = self.make_note()
		self.http.queue["create"].append(TimeoutError("lost"))
		self._start(note)
		self.http.queue["search"].append(self._found(note))

		result = carrier_shipments.check(note.name)

		self.assertEqual(result["status"], "Created")
		self.assertEqual(stored(note, "lr_no").lr_no, "TRK-9")

	def test_check_with_bosta_refuses_a_note_that_is_still_being_sent(self):
		note = self.make_note()
		carrier_shipments.request_create(note.name)

		with self.assertRaises(frappe.ValidationError):
			carrier_shipments.check(note.name)

	# -- guards ------------------------------------------------------------------------------

	def test_submit_is_refused_until_the_shipment_is_created(self):
		note = self.make_note()

		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc("Delivery Note", note.name).submit()
		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "docstatus"), 0)

		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Created")
		frappe.get_doc("Delivery Note", note.name).submit()
		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "docstatus"), 1)

	def test_a_stale_copy_cannot_submit_past_the_stored_status(self):
		note = self.make_note()
		stale = frappe.get_doc("Delivery Note", note.name)
		stale.custom_integration_status = "Created"

		with self.assertRaises(frappe.ValidationError):
			stale.submit()

	def test_started_shipment_locks_the_carrier_cod_city_and_items(self):
		note = self.make_note()
		carrier_shipments.request_create(note.name)
		changes = {
			"custom_cod_amount": 5,
			"custom_cod_confirmed": 0,
			"lr_no": "SNEAKY",
			"transporter": None,
		}
		for fieldname, value in changes.items():
			with self.subTest(fieldname):
				doc = frappe.get_doc("Delivery Note", note.name)
				doc.set(fieldname, value)
				with self.assertRaises(frappe.ValidationError):
					doc.save()
		doc = frappe.get_doc("Delivery Note", note.name)
		doc.items[0].qty = doc.items[0].qty + 1
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_a_created_draft_submits_unchanged_after_the_worker_wrote_its_fields(self):
		note = self.make_note()
		self.http.queue["create"].append(created("TRK-1", "DELIVERY-1"))
		self._start(note)
		# The sanitizer HTML-escapes a posted Data value that db_set stored raw.
		frappe.db.set_value("Delivery Note", note.name, "custom_bosta_city_name", "Dokki & Giza")
		doc = frappe.get_doc("Delivery Note", note.name)
		doc.custom_bosta_city_name = "Dokki &amp; Giza"
		doc.custom_cod_amount = frappe.utils.flt(doc.custom_cod_amount) + 0.001

		doc.submit()

		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "docstatus"), 1)
		self.assertEqual(stored(note, "custom_bosta_city_name").custom_bosta_city_name, "Dokki & Giza")

	def test_a_refused_change_to_a_started_shipment_names_the_field(self):
		note = self.make_note()
		self._start(note)
		doc = frappe.get_doc("Delivery Note", note.name)
		doc.lr_no = "SNEAKY"

		with self.assertRaises(frappe.ValidationError) as caught:
			doc.submit()

		self.assertIn(frappe._(frappe.get_meta("Delivery Note").get_label("lr_no")), str(caught.exception))

	def test_started_shipment_locks_the_receiver_phone_and_address_but_db_set_still_writes(self):
		note = self.make_note()
		doc = frappe.get_doc("Delivery Note", note.name)
		doc.contact_mobile = "01011111111"
		doc.save()  # still editable before the start
		carrier_shipments.request_create(note.name)
		for fieldname, value in {"contact_mobile": "01022222222", "shipping_address_name": "X-Other"}.items():
			with self.subTest(fieldname):
				doc = frappe.get_doc("Delivery Note", note.name)
				doc.set(fieldname, value)
				with self.assertRaises(frappe.ValidationError):
					doc.save()
		# the edit dialog's write path (db_set, no validate) is not blocked
		frappe.get_doc("Delivery Note", note.name).db_set("contact_mobile", "01033333333")
		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "contact_mobile"), "01033333333")

	def test_a_failed_note_can_still_be_edited(self):
		note = self.make_note()
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Creation Failed")
		doc = frappe.get_doc("Delivery Note", note.name)
		doc.custom_bosta_city = "EG-02"

		doc.save()

		self.assertEqual(stored(note, "custom_bosta_city").custom_bosta_city, "EG-02")

	def test_a_save_cannot_change_the_integration_fields_the_stored_values_are_kept(self):
		note = self.make_note()
		frappe.db.set_value("Delivery Note", note.name, "custom_carrier_delivery_id", "D-STORED")
		doc = frappe.get_doc("Delivery Note", note.name)
		doc.custom_integration_status = "Created"
		doc.custom_carrier_delivery_id = "D-FORGED"

		doc.save()

		values = stored(note, "custom_integration_status", "custom_carrier_delivery_id")
		self.assertEqual(values.custom_integration_status, "Not Created")
		self.assertEqual(values.custom_carrier_delivery_id, "D-STORED")

	def test_a_copy_loaded_before_a_label_or_status_write_still_submits_and_keeps_the_stored_values(self):
		note = self.make_note()
		frappe.db.set_value("Delivery Note", note.name, {"custom_integration_status": "Created", "lr_no": "TRK-1"})
		frappe.db.commit()
		stale = frappe.get_doc("Delivery Note", note.name)
		# Written after the form was opened, as the label job and a webhook do (modified untouched).
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_shipping_label": "/private/files/l.pdf", "custom_shipping_status": "Delivered"},
			update_modified=False,
		)

		stale.submit()

		values = stored(note, "docstatus", "custom_shipping_label", "custom_shipping_status", "custom_integration_status")
		self.assertEqual(values.docstatus, 1)
		self.assertEqual(values.custom_shipping_label, "/private/files/l.pdf")
		self.assertEqual(values.custom_shipping_status, "Delivered")
		self.assertEqual(values.custom_integration_status, "Created")

	# -- COD stays the merchant's choice until the shipment starts --------------------------------

	def test_a_confirmed_cod_can_still_be_changed_and_a_changed_item_does_not_recompute_it(self):
		note = self.make_note(self.make_order(rate=100, qty=4), cod=400)
		doc = frappe.get_doc("Delivery Note", note.name)
		doc.custom_cod_amount = 0
		doc.save()
		self.assertEqual(stored(note, "custom_cod_amount", "custom_cod_confirmed").custom_cod_confirmed, 1)
		self.assertEqual(stored(note, "custom_cod_amount").custom_cod_amount, 0)

		doc = frappe.get_doc("Delivery Note", note.name)
		doc.custom_cod_amount = 123
		doc.items[0].qty = 2
		doc.save()

		self.assertEqual(stored(note, "custom_cod_amount").custom_cod_amount, 123)

	def test_the_cod_hint_names_the_computed_amount_only_when_it_differs_on_an_editable_note(self):
		note = self.make_note(self.make_order(rate=100, qty=4), cod=400)
		self.assertIsNone(shipment.cod_hint(note.name)["default"])

		doc = frappe.get_doc("Delivery Note", note.name)
		doc.items[0].qty = 2
		doc.save()
		self.assertEqual(stored(note, "custom_cod_amount").custom_cod_amount, 400)
		self.assertEqual(shipment.cod_hint(note.name)["default"], 200)

		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Creating")
		self.assertIsNone(shipment.cod_hint(note.name)["default"])

	def test_the_cod_hint_endpoint_needs_read_permission(self):
		note = self.make_note()
		email, _role = make_role_user(self, "Bosta No Access", {"Item": ["read"]})
		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				bosta_shipments.get_cod_hint(note.name)
		finally:
			frappe.set_user("Administrator")

	def test_request_refuses_a_note_changed_between_validation_and_the_lock(self):
		note = self.make_note()
		opened = str(stored(note, "modified").modified)

		def changed_meanwhile(doc, action="create"):
			frappe.db.set_value("Delivery Note", doc.name, "custom_bosta_city_name", "Giza")

		with patch.object(shipment, "validate_ready", side_effect=changed_meanwhile):
			with self.assertRaises(frappe.TimestampMismatchError):
				carrier_shipments.request_create(note.name, opened)
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Not Created")

	def test_request_refuses_a_cod_unconfirmed_between_validation_and_the_lock(self):
		note = self.make_note()

		real = shipment.validate_ready
		calls = []

		def unconfirmed_meanwhile(doc, action="create"):
			# the first call is the pre-lock validation; the second runs under the lock and is real
			if not calls:
				calls.append(doc.name)
				frappe.db.set_value("Delivery Note", doc.name, "custom_cod_confirmed", 0, update_modified=False)
				return None
			return real(doc, action)

		with patch.object(shipment, "validate_ready", side_effect=unconfirmed_meanwhile):
			with self.assertRaises(frappe.ValidationError):
				carrier_shipments.request_create(note.name)
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Not Created")

	def test_a_failed_replay_does_not_turn_a_created_shipment_into_unknown(self):
		from marketplace_integrations.integrations.bosta import statuses

		note = self.make_note()
		with (
			patch.object(statuses, "replay_pending_events", side_effect=RuntimeError("replay broke")),
			patch.object(statuses, "update_sales_order_summary_of"),
		):
			shipment.after_created(note.name, {"_id": "DELIVERY-1"})  # must not raise

		self.assertTrue(
			frappe.db.exists("Error Log", {"method": "Bosta: pending events could not be replayed"})
			or frappe.db.exists("Error Log", {"error": ["like", "%replay broke%"]})
		)

	def test_a_note_amended_from_a_bosta_note_loses_the_bosta_fields_when_the_carrier_changed(self):
		note = self.make_note()
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Created", "custom_carrier_delivery_id": "D-1", "lr_no": "T-1", "custom_cod_amount": 90},
		)
		amended = frappe.copy_doc(frappe.get_doc("Delivery Note", note.name))
		amended.amended_from = note.name
		amended.transporter = None
		amended.custom_carrier_delivery_id = "D-1"
		amended.custom_integration_status = "Created"
		amended.custom_shipping_status = "Delivered"
		amended.lr_no = "T-1"

		carrier_shipments.validate_delivery_note(amended)

		for fieldname in ("custom_carrier_delivery_id", "custom_integration_status", "custom_shipping_status", "lr_no"):
			self.assertFalse(amended.get(fieldname), fieldname)

	def test_cancel_and_delete_are_refused_while_the_shipment_is_live(self):
		for status in ("Creating", "Creation Unknown", "Created"):
			with self.subTest(status):
				note = self.make_note()
				frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", status)
				with self.assertRaises(frappe.ValidationError):
					frappe.delete_doc("Delivery Note", note.name)
				frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Not Created")

	def test_a_sales_order_with_a_live_draft_note_cannot_be_cancelled(self):
		order = self.make_order()
		note = self.make_note(order)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Created")

		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc("Sales Order", order.name).cancel()

	# -- label ---------------------------------------------------------------------------------

	def test_a_label_failure_leaves_the_note_created_and_fetch_label_retries(self):
		from marketplace_integrations.integrations.bosta import labels

		note = self.make_note()
		frappe.db.set_value("Delivery Note", note.name, {"custom_integration_status": "Created", "lr_no": "TRK-1"})
		frappe.db.commit()
		with patch.object(labels, "create_a4_arabic_label", side_effect=frappe.ValidationError("Bosta is down")):
			self.assertIsNone(shipment.run_fetch_label(note.name))
			with self.assertRaises(frappe.ValidationError):
				bosta_shipments.fetch_label(note.name)

		values = stored(note, "custom_integration_status", "custom_shipping_label")
		self.assertEqual(values.custom_integration_status, "Created")
		self.assertFalse(values.custom_shipping_label)
		results = frappe.get_all(
			"Integration Request",
			filters={"reference_docname": note.name, "custom_operation_type": "Label Fetch"},
			pluck="custom_result",
		)
		self.assertEqual(results, ["Failed", "Failed"])

		with patch.object(
			labels, "create_a4_arabic_label", return_value={"file_url": "/private/files/x.pdf", "file_name": "x.pdf"}
		):
			result = bosta_shipments.fetch_label(note.name)
		self.assertEqual(result["file_url"], "/private/files/x.pdf")

	def test_a_label_error_is_logged_raw_and_escaped_only_for_the_screen(self):
		from marketplace_integrations.integrations.bosta import labels

		note = self.make_note()
		frappe.db.set_value("Delivery Note", note.name, {"custom_integration_status": "Created", "lr_no": "TRK-1"})
		frappe.db.commit()
		client = type("C", (), {"get_a4_arabic_awb": lambda self, numbers: None, "error_message": lambda self, r: "bad <b> & more"})()
		with (
			patch.object(labels, "BostaClient", return_value=client),
			patch.object(labels, "is_success_response", return_value=False),
		):
			with self.assertRaises(frappe.ValidationError) as caught:
				shipment.fetch_label(note.name, raise_on_error=True)
		self.assertIn("&lt;b&gt;", str(caught.exception))
		error = frappe.get_all(
			"Integration Request",
			filters={"reference_docname": note.name, "custom_operation_type": "Label Fetch"},
			pluck="error",
		)
		self.assertEqual(error, ["bad <b> & more"])

	def test_the_label_write_does_not_change_modified(self):
		import base64
		from types import SimpleNamespace

		from marketplace_integrations.integrations.bosta import labels

		note = self.make_note()
		frappe.db.set_value("Delivery Note", note.name, {"custom_integration_status": "Created", "lr_no": "TRK-1"})
		frappe.db.commit()
		before = stored(note, "modified").modified
		response = SimpleNamespace(status_code=200, content=base64.b64encode(b"%PDF-1.7\nlabel"))
		client = SimpleNamespace(get_a4_arabic_awb=lambda numbers: response)

		file_doc = SimpleNamespace(file_url="/private/files/bosta-awb.pdf", file_name="bosta-awb.pdf")

		with (
			patch.object(labels, "BostaClient", return_value=client),
			patch.object(labels, "is_success_response", return_value=True),
			patch.object(labels, "save_file", return_value=file_doc),
		):
			labels.create_a4_arabic_label(note.name)

		values = stored(note, "modified", "custom_shipping_label")
		self.assertTrue(values.custom_shipping_label)
		self.assertEqual(values.modified, before)

	# -- log hygiene -------------------------------------------------------------------------

	def test_the_log_never_holds_the_receiver_or_the_key(self):
		note = self.make_note()
		self.http.queue["create"].append(created())

		log = self._start(note)

		row = frappe.db.get_value(
			"Integration Request", log, ["data", "output", "error", "request_headers", "url"], as_dict=True
		)
		text = json.dumps(row)
		for secret in ("01000000000", "12 Example Street", self.customer.customer_name, "api-key"):
			self.assertNotIn(secret, text)

	def test_the_log_row_carries_the_attempt_and_correlation(self):
		note = self.make_note()
		self.http.queue["create"].append(TimeoutError("lost"))
		first = self._start(note)
		self.http.queue["search"].append(FakeResponse(200, NONE_FOUND))
		self.http.queue["create"].append(created())
		second = self._start(note)

		attempts = [frappe.db.get_value("Integration Request", name, "custom_attempt_no") for name in (first, second)]
		self.assertEqual(attempts, [1, 2])
		self.assertEqual(
			frappe.db.get_value("Integration Request", first, "integration_request_service"), "Bosta"
		)
		self.assertTrue(frappe.db.get_value("Integration Request", first, "custom_correlation_id"))

	# -- framework dependency ------------------------------------------------------------------

	def test_the_form_still_waits_for_a_before_submit_promise(self):
		"""delivery_note_shipping.js returns a promise from ``before_submit``; this fails if a Frappe
		upgrade stops honouring it (re-check the browser flow, docs/frappe-best-practices.md)."""
		path = frappe.get_app_path("frappe", "public", "js", "frappe", "form", "form.js")
		with open(path) as handle:
			source = handle.read()
		# The exact pattern the shipping script relies on: validated is reset to true, the before_submit
		# trigger is awaited (its promise), and only then is frappe.validated read to stop the submit.
		self.assertRegex(
			source,
			r"frappe\.validated = true;\s*me\.script_manager\.trigger\(\"before_submit\"\)\.then\(function \(\) \{\s*"
			r"if \(!frappe\.validated\) \{\s*return me\.handle_save_fail",
		)
