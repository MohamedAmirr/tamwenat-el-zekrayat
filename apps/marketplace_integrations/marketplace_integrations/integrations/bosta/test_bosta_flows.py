# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Real-database tests of the Bosta create / check / cancel flows; only HTTP is faked."""

from unittest.mock import patch

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from marketplace_integrations.api import bosta_shipments
from marketplace_integrations.integrations.bosta import deliveries
from marketplace_integrations.marketplace_integrations.doctype.bosta_shipment.bosta_shipment import (
	save_as_system,
)
from marketplace_integrations.tests.bosta_fixtures import (
	BostaFixtureMixin,
	FakeResponse,
	created,
)


class IntegrationTestBostaCreate(BostaFixtureMixin, IntegrationTestCase):
	def test_creates_delivery_and_submits_the_delivery_note(self):
		order = self.make_order(rate=150)
		self.http.queue["create"].append(created())

		result = self.create(order)

		payload = self.http.calls_of("create")[0]
		self.assertEqual(payload["cod"], 150)
		self.assertTrue(payload["businessReference"])
		self.assertEqual(payload["businessReference"], payload["uniqueBusinessReference"])
		shipment = frappe.get_doc("Bosta Shipment", result["shipment"])
		self.assertEqual(payload["businessReference"], f"BOSTA-{shipment.name}")
		self.assertEqual((shipment.status, shipment.tracking_number), ("Completed", "TRK-1"))
		self.assertEqual(shipment.active_sales_order, order.name)
		self.assertEqual(frappe.db.get_value("Delivery Note", result["delivery_note"], "docstatus"), 1)

	def test_duplicate_click_makes_no_second_request_or_shipment(self):
		order = self.make_order()
		self.http.queue["create"].append(created())
		self.create(order)

		with self.assertRaises(frappe.ValidationError):
			self.create(order)

		self.assertEqual(len(self.http.sent()), 1)
		self.assertEqual(frappe.db.count("Bosta Shipment", {"sales_order": order.name}), 1)

	def test_delivery_note_failure_is_savepointed_and_resumed_without_a_second_bosta_call(self):
		order = self.make_order()
		self.http.queue["create"].append(created())

		def insert_then_fail(*args, **kwargs):
			delivery_note = make_delivery_note(*args, **kwargs)
			delivery_note.insert()
			raise frappe.ValidationError("stock problem")

		with patch.object(deliveries, "make_delivery_note", side_effect=insert_then_fail):
			with self.assertRaises(frappe.ValidationError):
				self.create(order)

		shipment = self.shipment_for(order)
		self.assertEqual(shipment.status, "Delivery Note Failed")
		self.assertIn("stock problem", shipment.delivery_note_error)
		self.assertFalse(frappe.db.exists("Delivery Note Item", {"against_sales_order": order.name}))

		result = self.create(order)

		self.assertEqual(len(self.http.sent()), 1)
		shipment.reload()
		self.assertEqual((shipment.status, shipment.delivery_note), ("Completed", result["delivery_note"]))
		self.assertFalse(shipment.delivery_note_error)

	def test_failing_bosta_call_surfaces_escaped_message_and_is_retryable(self):
		order = self.make_order()
		self.http.queue["create"].append(
			FakeResponse(400, {"success": False, "message": "<img src=x onerror=1> cod"})
		)

		with self.assertRaises(frappe.ValidationError) as raised:
			self.create(order)

		self.assertNotIn("<img", str(raised.exception))
		self.assertIn("&lt;img", str(raised.exception))
		shipment = self.shipment_for(order)
		self.assertEqual(shipment.status, "Bosta Failed")
		self.assertIn("HTTP 400", shipment.last_error)

		# Retry searches first, finds nothing, then sends again on the same shipment.
		self.http.queue["search"].append(FakeResponse(200, {"success": True, "data": []}))
		self.http.queue["create"].append(created("TRK-2", "DELIVERY-2"))
		self.create(order)

		self.assertEqual(frappe.db.count("Bosta Shipment", {"sales_order": order.name}), 1)
		shipment.reload()
		self.assertEqual(
			(shipment.status, shipment.tracking_number, shipment.last_error), ("Completed", "TRK-2", "")
		)

	def test_retry_of_a_failed_shipment_reuses_the_row_and_applies_the_new_dialog_values(self):
		order = self.make_order()
		self.http.queue["create"].append(FakeResponse(400, {"success": False, "message": "bad"}))
		with self.assertRaises(frappe.ValidationError):
			self.create(order)
		original = self.shipment_for(order)

		self.http.queue["search"].append(FakeResponse(200, {"success": True, "data": []}))
		self.http.queue["create"].append(created("TRK-3", "DELIVERY-3"))
		self.create(order, city_id="city-alex", address_first_line="Elsewhere 1")

		retried = self.shipment_for(order)
		self.assertEqual(retried.name, original.name)
		self.assertEqual(
			(retried.city, retried.city_id, retried.city_code, retried.address_first_line),
			("Alexandria", "city-alex", "EG-02", "Elsewhere 1"),
		)
		self.assertEqual(retried.status, "Completed")

	def test_a_failed_shipment_shows_the_cod_that_was_sent_including_on_a_resend(self):
		order = self.make_order(rate=500)
		self.http.queue["create"].append(FakeResponse(400, {"success": False, "message": "bad"}))
		with self.assertRaises(frappe.ValidationError):
			self.create(order)
		self.assertEqual(self.shipment_for(order).cod_amount, 500)

		# An advance payment arrives; the re-send carries and records the new COD.
		frappe.db.set_value("Sales Order", order.name, "advance_paid", 200)
		frappe.db.commit()
		self.http.queue["search"].append(FakeResponse(200, {"success": True, "data": []}))
		self.http.queue["create"].append(FakeResponse(400, {"success": False, "message": "bad again"}))
		with self.assertRaises(frappe.ValidationError):
			self.create(order)

		self.assertEqual(self.http.calls_of("create")[-1]["cod"], 300)
		self.assertEqual(self.shipment_for(order).cod_amount, 300)

	def test_network_exception_does_not_leave_the_shipment_pending(self):
		order = self.make_order()
		self.http.queue["create"].append(frappe.ValidationError("Failed to reach Bosta."))

		with self.assertRaises(frappe.ValidationError):
			self.create(order)

		shipment = self.shipment_for(order)
		self.assertEqual(shipment.status, "Bosta Failed")
		self.assertIn("Check Bosta", shipment.last_error)

	def test_check_bosta_links_a_delivery_that_exists_and_continues(self):
		order = self.make_order()
		self.http.queue["create"].append(frappe.ValidationError("Failed to reach Bosta."))
		with self.assertRaises(frappe.ValidationError):
			self.create(order)
		shipment = self.shipment_for(order)
		self.http.queue["search"].append(
			FakeResponse(
				200,
				{
					"success": True,
					"data": [
						{
							"_id": "DELIVERY-9",
							"trackingNumber": "TRK-9",
							"businessReference": shipment.unique_business_reference,
						}
					],
				},
			)
		)

		result = deliveries.check_bosta_shipment(shipment.name)

		self.assertTrue(result["found"])
		self.assertEqual(
			self.http.calls_of("search"), [{"businessReference": shipment.unique_business_reference}]
		)
		shipment.reload()
		self.assertEqual((shipment.status, shipment.tracking_number), ("Completed", "TRK-9"))
		self.assertEqual(len(self.http.calls_of("create")), 1)

	def test_check_bosta_not_found_marks_failed_so_a_retry_is_allowed(self):
		order = self.make_order()
		shipment = self.shipment_for(order) or self._pending_shipment(order)
		self.http.queue["search"].append(FakeResponse(200, {"success": True, "data": []}))

		result = deliveries.check_bosta_shipment(shipment.name)

		self.assertFalse(result["found"])
		shipment.reload()
		self.assertEqual(shipment.status, "Bosta Failed")
		self.assertIn("A retry is allowed", shipment.last_error)

	def test_check_bosta_refuses_a_fresh_pending_shipment_and_unrecognised_search_replies(self):
		order = self.make_order()
		shipment = self._pending_shipment(order, minutes_old=0)
		with self.assertRaises(frappe.ValidationError):
			deliveries.check_bosta_shipment(shipment.name)
		self.assertEqual(self.http.sent(), [])

		frappe.db.set_value(
			"Bosta Shipment", shipment.name, "modified", "2020-01-01 00:00:00", update_modified=False
		)
		self.http.queue["search"].append(
			FakeResponse(200, {"success": True, "data": {"deliveries": [{"x": 1}]}})
		)
		with self.assertRaises(frappe.ValidationError):
			deliveries.check_bosta_shipment(shipment.name)
		self.assertEqual(frappe.db.get_value("Bosta Shipment", shipment.name, "status"), "Pending Bosta")

	def test_cod_above_bosta_limit_is_rejected_before_any_request(self):
		order = self.make_order(rate=30001)

		with self.assertRaises(frappe.ValidationError):
			self.create(order)

		self.assertEqual(self.http.sent(), [])
		self.assertIsNone(self.shipment_for(order))

	def test_cod_is_the_outstanding_amount(self):
		partly_paid = self.make_order(rate=500, advance_paid=200)
		paid = self.make_order(rate=500, advance_paid=500)
		self.http.queue["create"] += [created("TRK-A", "D-A"), created("TRK-B", "D-B")]

		self.create(partly_paid)
		self.create(paid)

		self.assertEqual([call["cod"] for call in self.http.calls_of("create")], [300, 0])
		defaults = deliveries.get_sales_order_defaults(self.make_order(rate=500, advance_paid=200).name)
		self.assertEqual(
			(defaults["cod_amount"], defaults["order_total"], defaults["advance_paid"]), (300, 500, 200)
		)

	def test_ineligible_orders_are_rejected_before_any_request(self):
		closed = self.make_order()
		frappe.db.set_value("Sales Order", closed.name, "status", "Closed")
		on_hold = self.make_order()
		frappe.db.set_value("Sales Order", on_hold.name, "status", "On Hold")
		delivered = self.make_order()
		frappe.db.set_value("Sales Order Item", delivered.items[0].name, "delivered_qty", 1)
		draft = self.make_order(submit=False)

		for order in (closed, on_hold, delivered, draft):
			with self.subTest(order=order.name), self.assertRaises(frappe.ValidationError):
				self.create(order)
			self.assertIsNone(self.shipment_for(order))
		self.assertEqual(self.http.sent(), [])

	def test_permission_matrix(self):
		# The Delivery Note step needs ERPNext's own rights: Delivery Note create (Stock User)
		# and Account read (Accounts User). Sales Manager gets both extra roles here; see the
		# next test for what happens without them.
		users = {
			"Sales User": self.make_user("Sales User"),
			"Sales Manager": self.make_user("Sales Manager", "Stock User", "Accounts User"),
			"Stock User": self.make_user("Stock User", "Accounts User"),
		}
		cases = [("Sales User", False), ("Sales Manager", True), ("Stock User", True), ("Guest", False)]
		for role, allowed in cases:
			order = self.make_order()
			if allowed:
				self.http.queue["create"].append(created(f"TRK-{role}", f"D-{role}"))
			frappe.set_user(users.get(role, "Guest"))
			try:
				with self.subTest(role=role):
					if allowed:
						result = self.create(order)
						self.assertEqual(result["tracking_number"], f"TRK-{role}")
						# The real user stays the Delivery Note's owner.
						self.assertEqual(
							frappe.db.get_value("Delivery Note", result["delivery_note"], "owner"),
							users[role],
						)
					else:
						with self.assertRaises(frappe.PermissionError):
							self.create(order)
			finally:
				frappe.set_user("Administrator")
			self.assertEqual(bool(self.shipment_for(order)), allowed)

	def test_creator_without_account_read_ends_delivery_note_failed_and_is_resumable(self):
		# OPEN DECISION: ERPNext refuses a Delivery Note for a user without Account read.
		manager = self.make_user("Sales Manager")
		order = self.make_order()
		self.http.queue["create"].append(created())

		frappe.set_user(manager)
		try:
			with self.assertRaises(frappe.PermissionError):
				self.create(order)
		finally:
			frappe.set_user("Administrator")

		shipment = self.shipment_for(order)
		self.assertEqual((shipment.status, shipment.tracking_number), ("Delivery Note Failed", "TRK-1"))
		self.create(order)  # an administrator resumes without a second Bosta call
		self.assertEqual(len(self.http.sent()), 1)
		self.assertEqual(self.shipment_for(order).status, "Completed")

	def test_configuration_failure_persists_nothing_and_is_not_an_ambiguous_send(self):
		order = self.make_order()
		stub = type("S", (), {"enabled": 0, "get_password": lambda *a, **k: ""})()
		with (
			patch(
				"marketplace_integrations.integrations.bosta.client.BostaClient.get_settings",
				return_value=stub,
			),
			self.assertRaises(frappe.ValidationError) as raised,
		):
			self.create(order)
		self.assertIn("disabled", str(raised.exception))
		self.assertNotIn("may or may not", str(raised.exception))
		self.assertIsNone(self.shipment_for(order))

	def test_expected_cod_and_currency_are_checked(self):
		order = self.make_order(rate=500, advance_paid=200)
		with self.assertRaises(frappe.ValidationError) as raised:
			self.create(order, expected_cod=500)
		self.assertIn("Reopen the dialog", str(raised.exception))
		self.assertEqual(self.http.sent(), [])

		self.http.queue["create"].append(created())
		self.create(order, expected_cod=300)
		self.assertEqual(self.http.calls_of("create")[0]["cod"], 300)

		foreign = self.make_order()
		company_currency = frappe.get_cached_value("Company", foreign.company, "default_currency")
		other = frappe.get_all("Currency", filters={"name": ["!=", company_currency]}, pluck="name", limit=1)[
			0
		]
		frappe.db.set_value("Sales Order", foreign.name, "currency", other)
		with self.assertRaises(frappe.ValidationError) as raised:
			self.create(foreign)
		self.assertIn("company currency", str(raised.exception))
		self.assertEqual(len(self.http.sent()), 1)

	def test_a_stale_caller_cannot_build_a_second_delivery_note(self):
		order = self.make_order()
		self.http.queue["create"].append(created())
		with (
			patch.object(deliveries, "make_delivery_note", side_effect=frappe.ValidationError("boom")),
			self.assertRaises(frappe.ValidationError),
		):
			self.create(order)
		stale = self.shipment_for(order)  # decided while status was "Delivery Note Failed"
		first = self.create(order)  # a concurrent resume wins

		again = deliveries._create_delivery_note(stale, order.name)

		self.assertEqual(again["delivery_note"], first["delivery_note"])
		self.assertEqual(
			len(frappe.get_all("Delivery Note Item", {"against_sales_order": order.name}, pluck="parent")), 1
		)
		self.assertEqual(self.shipment_for(order).status, "Completed")  # not demoted
		with self.assertRaises(frappe.ValidationError):
			deliveries.check_bosta_shipment(stale.name)  # nothing left to check

	def test_a_cancel_between_bosta_created_and_the_delivery_note_step_wins(self):
		from marketplace_integrations.integrations.bosta import statuses

		order = self.make_order()
		self.http.queue["create"].append(created("TRK-C", "D-C"))
		with (
			patch.object(deliveries, "make_delivery_note", side_effect=frappe.ValidationError("boom")),
			self.assertRaises(frappe.ValidationError),
		):
			self.create(order)
		shipment = self.shipment_for(order)
		shipment.status = "Bosta Created"
		shipment.delivery_note_error = ""
		save_as_system(shipment)
		stale = frappe.get_doc("Bosta Shipment", shipment.name)
		statuses.process_webhook_event(
			{"_id": "D-C", "trackingNumber": "TRK-C", "state": 49, "type": "SEND", "timeStamp": 5000}
		)

		with self.assertRaises(frappe.ValidationError):
			deliveries._create_delivery_note(stale, order.name)

		self.assertEqual(self.shipment_for(order, status="Canceled").name, shipment.name)
		self.assertFalse(frappe.db.exists("Delivery Note Item", {"against_sales_order": order.name}))

	def test_system_fields_cannot_be_written_by_non_admins(self):
		order = self.make_order()
		self.http.queue["create"].append(created())
		self.create(order)
		shipment = self.shipment_for(order)
		manager = self.make_user("Sales Manager")

		frappe.set_user(manager)
		try:
			for fieldname, value in (("status", "Canceled"), ("tracking_number", "X"), ("delivery_note", "")):
				shipment.reload()
				setattr(shipment, fieldname, value)
				with self.subTest(field=fieldname), self.assertRaises(frappe.ValidationError):
					shipment.save()
			# The receiver and address are locked once Bosta has the delivery (#273).
			shipment.reload()
			shipment.receiver_phone = "01111111111"
			with self.assertRaises(frappe.ValidationError):
				shipment.save()
			with self.assertRaises(frappe.ValidationError):
				frappe.get_doc(
					{
						"doctype": "Bosta Shipment",
						"sales_order": order.name,
						"status": "Completed",
						"receiver_name": self._receiver(),
						"receiver_phone": "0100",
						"city": "Cairo",
						"city_id": "city-cairo",
						"city_code": "EG-01",
						"address_first_line": "12 Example Street",
					}
				).insert()
		finally:
			frappe.set_user("Administrator")

	def test_dialog_defaults_and_city_is_required(self):
		order = self.make_order(rate=500, advance_paid=500)
		defaults = bosta_shipments.get_create_defaults(order.name)
		self.assertEqual(
			(defaults["cod_amount"], defaults["order_total"], defaults["advance_paid"]), (0, 500, 500)
		)
		self.assertEqual(defaults["receiver_name"], self.customer.customer_name)

		# A missing city is refused before anything is sent, and no district is asked for.
		self.assertNotIn("district_id", defaults)
		with self.assertRaises(frappe.ValidationError) as raised:
			bosta_shipments.create_shipment(
				order.name, self._receiver(), "01000000000", "", "12 Example Street"
			)
		self.assertIn("Choose a Bosta city", str(raised.exception))
		self.assertEqual(self.http.sent(), [])

	def test_only_one_active_shipment_per_sales_order_but_a_new_one_after_cancel(self):
		order = self.make_order()
		self.http.queue["create"].append(created())
		self.create(order)
		first = self.shipment_for(order)

		def candidate():
			return frappe.get_doc(
				{
					"doctype": "Bosta Shipment",
					"sales_order": order.name,
					"receiver_name": self._receiver(),
					"receiver_phone": "0100",
					"city": "Cairo",
					"city_id": "city-cairo",
					"city_code": "EG-01",
					"address_first_line": "12 Example Street",
				}
			)

		# Friendly message from validate...
		with self.assertRaises(frappe.ValidationError) as raised:
			save_as_system(candidate(), insert=True)
		self.assertIn("already has an active", str(raised.exception))
		# ...and the database-level unique index, even with validation bypassed.
		bypass = candidate()
		bypass.active_sales_order = order.name
		bypass.flags.ignore_validate = True
		with self.assertRaises(frappe.UniqueValidationError):
			save_as_system(bypass, insert=True)
		frappe.db.rollback()

		# A System Manager re-activating a Canceled shipment gets the friendly message too.
		first.status = "Canceled"
		save_as_system(first)
		second = save_as_system(candidate(), insert=True)
		self.assertEqual(frappe.db.get_value("Bosta Shipment", second.name, "active_sales_order"), order.name)
		first.reload()
		first.status = "Completed"
		with self.assertRaises(frappe.ValidationError) as raised:
			save_as_system(first)
		self.assertIn("already has an active", str(raised.exception))
		frappe.db.rollback()

	def test_several_canceled_shipments_coexist_for_one_sales_order(self):
		order = self.make_order()
		names = []
		for _ in range(2):
			shipment = frappe.get_doc(
				{
					"doctype": "Bosta Shipment",
					"sales_order": order.name,
					"receiver_name": self._receiver(),
					"receiver_phone": "0100",
					"city": "Cairo",
					"city_id": "city-cairo",
					"city_code": "EG-01",
					"address_first_line": "12 Example Street",
					"status": "Canceled",
				}
			)
			names.append(save_as_system(shipment, insert=True).name)
		self.assertEqual(len(set(names)), 2)
		self.assertEqual(
			[frappe.db.get_value("Bosta Shipment", n, "active_sales_order") for n in names], [None, None]
		)
		frappe.db.commit()

	def _pending_shipment(self, order, minutes_old=10):
		shipment = frappe.get_doc(
			{
				"doctype": "Bosta Shipment",
				"sales_order": order.name,
				"receiver_name": self._receiver(),
				"receiver_phone": "01000000000",
				"city": "Cairo",
				"city_id": "city-cairo",
				"city_code": "EG-01",
				"address_first_line": "12 Example Street",
				"bosta_requested_on": now_datetime(),
			}
		)
		save_as_system(shipment, insert=True)
		if minutes_old:
			frappe.db.set_value("Bosta Shipment", shipment.name, "bosta_requested_on", "2020-01-01 00:00:00")
		frappe.db.commit()
		return shipment
