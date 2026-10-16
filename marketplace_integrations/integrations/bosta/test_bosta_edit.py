# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Real-database tests of Bosta Edit (#273); only the HTTP layer is faked."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api import bosta_shipments
from marketplace_integrations.integrations.bosta import editing
from marketplace_integrations.marketplace_integrations.doctype.bosta_shipment.bosta_shipment import (
	save_as_system,
)
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin, FakeResponse, created

NEW = {
	"receiver_phone": "01099999999",
	"city": "Giza",
	"district_id": "new-district",
	"address_first_line": "99 New Street, Dokki",
}


class IntegrationTestBostaEdit(BostaFixtureMixin, IntegrationTestCase):
	def _live(self, state_code=24, tracking="TRK-E"):
		order = self.make_order()
		self.http.queue["create"].append(created(tracking, f"D-{tracking}"))
		self.create(order)
		shipment = self.shipment_for(order)
		frappe.db.set_value("Bosta Shipment", shipment.name, "bosta_state_code", state_code)
		frappe.db.commit()
		shipment.reload()
		return order, shipment

	def _ok(self):
		return FakeResponse(200, {"success": True, "message": "Done successfully.", "data": {"_id": "x"}})

	def _edit(self, shipment, **overrides):
		return bosta_shipments.edit_shipment(shipment.name, **{**NEW, **overrides})

	def _addresses(self, order):
		return frappe.get_all("Address", filters={"address_title": order.name}, pluck="name")

	def _comment_count(self, order):
		return frappe.db.count("Comment", {"reference_doctype": "Sales Order", "reference_name": order.name})

	def _unchanged(self, order, shipment):
		shipment.reload()
		self.assertEqual(
			(shipment.receiver_phone, shipment.city, shipment.district_id, shipment.address_first_line),
			("01000000000", "Cairo", "district-id", "12 Example Street"),
		)
		self.assertEqual(self._addresses(order), [])
		self.assertFalse(frappe.db.get_value("Sales Order", order.name, "shipping_address_name"))

	# -- request ----------------------------------------------------------------------------

	def test_the_put_body_is_exactly_the_phone_and_the_dropoff_address(self):
		_, shipment = self._live()
		self.http.queue["update"].append(self._ok())

		self._edit(shipment)

		self.assertEqual(
			self.http.calls_of("update"),
			[
				{
					"receiver": {"phone": "01099999999"},
					"dropOffAddress": {
						"city": "Giza",
						"districtId": "new-district",
						"firstLine": "99 New Street, Dokki",
					},
				}
			],
		)

	def test_the_request_goes_to_the_tracking_number_with_put(self):
		_, shipment = self._live(tracking="TRK-PUT")
		with patch(
			"marketplace_integrations.integrations.bosta.client.BostaClient.update_delivery",
			return_value=self._ok(),
		) as update:
			self._edit(shipment)
		update.assert_called_once()
		self.assertEqual(update.call_args.args[0], "TRK-PUT")

	# -- refusals ---------------------------------------------------------------------------

	def test_refused_cases_never_reach_bosta(self):
		order, shipment = self._live()
		for code in (0, 40, 45, 46, 48, 49, 60, 100, 101, 102, 103, 104, 105):
			with self.subTest(state=code):
				frappe.db.set_value("Bosta Shipment", shipment.name, "bosta_state_code", code)
				with self.assertRaises(frappe.ValidationError) as caught:
					self._edit(shipment)
				if code == 0:
					self.assertIn("Refresh from Bosta first", str(caught.exception))
		frappe.db.set_value("Bosta Shipment", shipment.name, {"bosta_state_code": 24, "status": "Canceled"})
		with self.assertRaises(frappe.ValidationError):
			self._edit(shipment)
		frappe.db.set_value("Bosta Shipment", shipment.name, {"status": "Completed", "tracking_number": ""})
		with self.assertRaises(frappe.ValidationError):
			self._edit(shipment)
		self.assertEqual(self.http.calls_of("update"), [])
		self.assertEqual(self._addresses(order), [])

	def test_invalid_input_never_reaches_bosta(self):
		order, shipment = self._live()
		for overrides in (
			{"receiver_phone": " "},
			{"city": ""},
			{"district_id": ""},
			{"address_first_line": "12345"},
			{"address_first_line": "x" * 241},
		):
			with self.subTest(overrides=overrides), self.assertRaises(frappe.ValidationError):
				self._edit(shipment, **overrides)
		self.assertEqual(self.http.calls_of("update"), [])
		self._unchanged(order, shipment)

	def test_every_documented_editable_state_is_sent(self):
		_, shipment = self._live()
		for code in (10, 11, 20, 21, 22, 24, 30, 41, 47):
			with self.subTest(state=code):
				frappe.db.set_value("Bosta Shipment", shipment.name, "bosta_state_code", code)
				self.http.queue["update"].append(self._ok())
				self._edit(shipment)
		self.assertEqual(len(self.http.calls_of("update")), 9)

	def test_creator_only_and_post_only(self):
		_, shipment = self._live()
		frappe.set_user(self.make_user("Sales User"))
		try:
			with self.assertRaises(frappe.PermissionError):
				self._edit(shipment)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self.http.calls_of("update"), [])
		self.assertIn(bosta_shipments.edit_shipment, frappe.whitelisted)
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[bosta_shipments.edit_shipment], ["POST"]
		)

	# -- failure leaves everything unchanged -------------------------------------------------

	def test_a_bosta_refusal_or_a_network_error_changes_nothing(self):
		order, shipment = self._live()
		for queued in (
			FakeResponse(400, {"success": False, "message": "<img src=x onerror=1> bad district"}),
			frappe.ValidationError("Failed to reach Bosta."),
		):
			with self.subTest(queued=str(queued)):
				self.http.queue["update"].append(queued)
				with self.assertRaises(frappe.ValidationError) as caught:
					self._edit(shipment)
				self.assertNotIn("<img", str(caught.exception))
				self.assertIn("Check the delivery in Bosta", str(caught.exception))
				self._unchanged(order, shipment)

	def test_a_failure_recording_after_bosta_accepted_changes_nothing_in_erpnext(self):
		order, shipment = self._live()
		self.http.queue["update"].append(self._ok())
		with (
			patch.object(editing, "save_as_system", side_effect=RuntimeError("boom")),
			self.assertRaises(frappe.ValidationError) as caught,
		):
			self._edit(shipment)
		self.assertIn("Bosta accepted the change", str(caught.exception))
		self._unchanged(order, shipment)

	def test_a_shipment_canceled_while_bosta_was_updating_is_not_written(self):
		order, shipment = self._live()
		self.http.queue["update"].append(self._ok())
		real_put = self.http.__call__

		def cancel_meanwhile(method, url, **kwargs):
			response = real_put(method, url, **kwargs)
			frappe.db.set_value("Bosta Shipment", shipment.name, "status", "Canceled")
			frappe.db.commit()
			return response

		with (
			patch(
				"marketplace_integrations.integrations.bosta.client.http_request",
				side_effect=cancel_meanwhile,
			),
			self.assertRaises(frappe.ValidationError) as caught,
		):
			self._edit(shipment)

		self.assertIn("canceled in the meantime", str(caught.exception))
		self._unchanged(order, shipment)

	# -- success ----------------------------------------------------------------------------

	def test_success_updates_only_the_bosta_shipment(self):
		order, shipment = self._live()
		address_before = frappe.db.get_value(
			"Sales Order", order.name, ["shipping_address_name", "shipping_address", "modified"], as_dict=True
		)
		note_modified = frappe.db.get_value("Delivery Note", shipment.delivery_note, "modified")
		comments_before = self._comment_count(order)
		self.http.queue["update"].append(self._ok())

		self._edit(shipment)

		shipment.reload()
		self.assertEqual(
			(
				shipment.receiver_phone,
				shipment.city,
				shipment.district_id,
				shipment.address_first_line,
				shipment.receiver_name,
			),
			("01099999999", "Giza", "new-district", "99 New Street, Dokki", self._receiver()),
		)
		self.assertFalse(shipment.city_id or shipment.district_name)
		# The Sales Order (address fields, timestamp), Addresses and Delivery Note are untouched.
		self.assertEqual(
			frappe.db.get_value(
				"Sales Order",
				order.name,
				["shipping_address_name", "shipping_address", "modified"],
				as_dict=True,
			),
			address_before,
		)
		self.assertEqual(self._addresses(order), [])
		self.assertEqual(self._comment_count(order), comments_before)
		self.assertEqual(
			frappe.db.get_value("Delivery Note", shipment.delivery_note, "modified"), note_modified
		)

	# -- locked fields ---------------------------------------------------------------------

	def test_receiver_and_address_are_locked_once_there_is_a_tracking_number(self):
		_, shipment = self._live()
		manager = self.make_user("Sales Manager", "System Manager")
		for fieldname, value in (
			("receiver_name", "Someone Else"),
			("receiver_phone", "0100"),
			("city", "Giza"),
			("district_id", "other"),
			("address_first_line", "Changed Street 99"),
		):
			for user in ("Administrator", manager):
				with self.subTest(field=fieldname, user=user):
					frappe.set_user(user)
					try:
						doc = frappe.get_doc("Bosta Shipment", shipment.name)
						doc.set(fieldname, value)
						with self.assertRaises(frappe.ValidationError):
							doc.save()
					finally:
						frappe.set_user("Administrator")
		# Other fields still save, and the system write flag still edits the locked ones.
		doc = frappe.get_doc("Bosta Shipment", shipment.name)
		doc.last_error = ""
		save_as_system(doc)
		doc.receiver_phone = "0100"
		save_as_system(doc)
		self.assertEqual(frappe.db.get_value("Bosta Shipment", shipment.name, "receiver_phone"), "0100")

	def test_shipments_without_a_tracking_number_stay_editable_and_retryable(self):
		order = self.make_order()
		self.http.queue["create"].append(FakeResponse(500, {"success": False, "message": "no"}))
		self.http.queue["search"].append(FakeResponse(200, {"success": True, "data": []}))
		with self.assertRaises(frappe.ValidationError):
			self.create(order)
		shipment = self.shipment_for(order)
		self.assertEqual((shipment.status, shipment.tracking_number), ("Bosta Failed", None))
		shipment.receiver_phone = "0100"
		shipment.save()
		# A retry with corrected details still replaces them and goes through.
		self.http.queue["create"].append(created("TRK-R", "D-R"))
		self.create(order, receiver_phone="01011111111", address_first_line="Corrected Street 1")
		shipment.reload()
		self.assertEqual(
			(shipment.tracking_number, shipment.receiver_phone, shipment.address_first_line),
			("TRK-R", "01011111111", "Corrected Street 1"),
		)

	def test_the_form_fields_are_read_only_once_there_is_a_tracking_number(self):
		meta = frappe.get_meta("Bosta Shipment")
		for fieldname in (
			"receiver_name",
			"receiver_phone",
			"city",
			"district_id",
			"city_id",
			"district_name",
			"address_first_line",
		):
			self.assertEqual(meta.get_field(fieldname).read_only_depends_on, "eval:doc.tracking_number")
