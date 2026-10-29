# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from marketplace_integrations.api import bosta_shipments
from marketplace_integrations.integrations.bosta import deliveries
from marketplace_integrations.marketplace_integrations.doctype.bosta_shipment.bosta_shipment import (
	save_as_system,
)
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin, created


class UnitTestBostaShipments(UnitTestCase):
	def test_print_a4_arabic_label_delegates_to_the_label_service(self):
		expected = {"file_url": "/private/files/bosta-awb.pdf"}
		with patch.object(bosta_shipments, "create_a4_arabic_label", return_value=expected) as create:
			self.assertEqual(bosta_shipments.print_a4_arabic_label("BOSTA-SHP-00001"), expected)

		create.assert_called_once_with("BOSTA-SHP-00001")


class IntegrationTestBostaShipmentSummary(BostaFixtureMixin, IntegrationTestCase):
	"""get_sales_order_shipment on a real database (#246); no HTTP is involved."""

	def test_sales_order_bosta_status_is_a_read_only_list_and_standard_filter_field(self):
		field = frappe.get_meta("Sales Order").get_field("custom_delivery_status")
		self.assertTrue(field.read_only)
		self.assertTrue(field.in_list_view)
		self.assertTrue(field.in_standard_filter)
		# The Bosta-only field stays, but the list shows the platform-neutral one (#378).
		bosta_field = frappe.get_meta("Sales Order").get_field("custom_bosta_delivery_status")
		self.assertTrue(bosta_field.read_only)
		self.assertFalse(bosta_field.in_list_view)

	def _shipment(self, order, status, **values):
		fields = {
			"doctype": "Bosta Shipment",
			"sales_order": order.name,
			"receiver_name": self._receiver(),
			"receiver_phone": "01000000000",
			"city": "Cairo",
			"city_id": "city-cairo",
			"city_code": "EG-01",
			"address_first_line": "12 Example Street",
			"status": status,
			**values,
		}
		shipment = save_as_system(frappe.get_doc(fields), insert=True)
		frappe.db.commit()
		return shipment

	def _as(self, email, order):
		frappe.set_user(email)
		try:
			return bosta_shipments.get_sales_order_shipment(order.name)
		finally:
			frappe.set_user("Administrator")

	def test_order_without_a_shipment_returns_nothing(self):
		self.assertIsNone(bosta_shipments.get_sales_order_shipment(self.make_order().name))

	def test_active_shipment_summary(self):
		order = self.make_order()
		shipment = self._shipment(
			order,
			"Bosta Created",
			tracking_number="TRK-9",
			bosta_state_value="Picked up",
			shipment_fee=87,
		)
		self.assertEqual(
			bosta_shipments.get_sales_order_shipment(order.name),
			{
				"name": shipment.name,
				"status": "Bosta Created",
				"tracking_number": "TRK-9",
				"bosta_state": "Picked up",
				"bosta_state_code": 0,
				"active": True,
				"last_update": "",
				"receiver_name": self._receiver(),
				"receiver_phone": "01000000000",
				"city": "Cairo",
				"city_id": "city-cairo",
				"address_first_line": "12 Example Street",
				"cod_amount": 0,
				"shipment_fee": 87,
				"delivery_note": "",
				"delivery_note_error": "",
				"delivery_note_status": "",
				"handover_status": "Awaiting Handover",
				"handover_confirmed_by": "",
				"handover_confirmed_on": "",
				"stock_posting_status": "Not Posted",
				"created_on": str(shipment.creation),
				"allocations": [],
				"shipments": [
					{
						"name": shipment.name,
						"status": "Bosta Created",
						"tracking_number": "TRK-9",
						"bosta_state": "Picked up",
						"bosta_state_code": 0,
						"active": True,
						"last_update": "",
						"receiver_name": self._receiver(),
						"receiver_phone": "01000000000",
						"city": "Cairo",
						"city_id": "city-cairo",
						"address_first_line": "12 Example Street",
						"cod_amount": 0,
						"shipment_fee": 87,
						"delivery_note": "",
						"delivery_note_error": "",
						"delivery_note_status": "",
						"handover_status": "Awaiting Handover",
						"handover_confirmed_by": "",
						"handover_confirmed_on": "",
						"stock_posting_status": "Not Posted",
						"created_on": str(shipment.creation),
						"allocations": [],
					}
				],
			},
		)

	def test_summary_has_exactly_the_fields_the_form_uses(self):
		order = self.make_order()
		self._shipment(order, "Pending Bosta")
		self.assertEqual(
			set(bosta_shipments.get_sales_order_shipment(order.name)),
			{
				"name",
				"status",
				"tracking_number",
				"bosta_state",
				"bosta_state_code",
				"active",
				"last_update",
				"receiver_name",
				"receiver_phone",
				"city",
				"city_id",
				"address_first_line",
				"cod_amount",
				"shipment_fee",
				"delivery_note",
				"delivery_note_error",
				"delivery_note_status",
				"handover_status",
				"handover_confirmed_by",
				"handover_confirmed_on",
				"stock_posting_status",
				"created_on",
				"allocations",
				"shipments",
			},
		)

	def test_delivery_note_error_is_returned_for_a_failed_delivery_note(self):
		order = self.make_order()
		self._shipment(order, "Delivery Note Failed", delivery_note_error="stock problem")
		self.assertEqual(
			bosta_shipments.get_sales_order_shipment(order.name)["delivery_note_error"], "stock problem"
		)

	def test_historical_shipments_are_all_returned_with_latest_at_top_level(self):
		order = self.make_order()
		older = self._shipment(order, "Canceled", tracking_number="OLD")
		latest = self._shipment(order, "Canceled", tracking_number="NEWER")
		summary = bosta_shipments.get_sales_order_shipment(order.name)
		self.assertEqual(summary["name"], latest.name)
		self.assertEqual((summary["status"], summary["active"]), ("Canceled", False))
		self.assertEqual([row["name"] for row in summary["shipments"]], [latest.name, older.name])

	def test_summary_and_sales_order_status_prefer_latest_non_canceled_shipment(self):
		order = self.make_order()
		active = self._shipment(order, "Bosta Created", bosta_state_code=45, bosta_state_value="Delivered")
		self._shipment(order, "Canceled", bosta_state_code=0, bosta_state_value="")

		summary = bosta_shipments.get_sales_order_shipment(order.name)

		self.assertEqual(summary["name"], active.name)
		self.assertEqual(frappe.db.get_value("Sales Order", order.name, "custom_bosta_delivery_status"), "Delivered")
		self.assertEqual(frappe.db.get_value("Sales Order", order.name, "custom_delivery_status"), "Delivered")
		frappe.db.set_value("Sales Order", order.name, "custom_bosta_delivery_status", "", update_modified=False)
		from marketplace_integrations.patches.v0_0.backfill_sales_order_bosta_delivery_status import execute

		execute()
		self.assertEqual(frappe.db.get_value("Sales Order", order.name, "custom_bosta_delivery_status"), "Delivered")

	def test_canceled_shipment_falls_back_to_its_status_when_state_is_unknown(self):
		order = self.make_order()
		shipment = self._shipment(order, "Bosta Created", bosta_state_code=0, bosta_state_value="")
		self.assertEqual(
			frappe.db.get_value("Sales Order", order.name, "custom_bosta_delivery_status"), "Bosta Created"
		)

		shipment.status = "Canceled"
		shipment.bosta_state_value = ""
		shipment.bosta_state_code = 0
		save_as_system(shipment)

		self.assertEqual(
			frappe.db.get_value("Sales Order", order.name, "custom_bosta_delivery_status"), "Canceled"
		)

	def test_canceled_then_new_active_returns_the_active_one(self):
		order = self.make_order()
		self._shipment(order, "Canceled", tracking_number="OLD")
		active = self._shipment(order, "Pending Bosta")
		summary = bosta_shipments.get_sales_order_shipment(order.name)
		self.assertEqual((summary["name"], summary["active"]), (active.name, True))

	def test_summary_is_for_the_requested_order_only(self):
		other = self.make_order()
		self._shipment(other, "Pending Bosta")
		self.assertIsNone(bosta_shipments.get_sales_order_shipment(self.make_order().name))

	def test_permission_matrix(self):
		order = self.make_order()
		shipment = self._shipment(order, "Completed", tracking_number="TRK-1")
		users = {
			"Sales Manager": self.make_user("Sales Manager"),
			"Stock User": self.make_user("Stock User"),
			"Sales User": self.make_user("Sales User"),
			# Reads Sales Orders but has no Bosta Shipment permission at all.
			"Accounts User": self.make_user("Accounts User"),
		}
		for role in ("Sales Manager", "Stock User", "Sales User"):
			with self.subTest(role=role):
				summary = self._as(users[role], order)
				self.assertEqual((summary["name"], summary["tracking_number"]), (shipment.name, "TRK-1"))
		with self.subTest(role="Accounts User"):
			self.assertTrue(
				frappe.has_permission("Sales Order", "read", doc=order.name, user=users["Accounts User"])
			)
			self.assertFalse(frappe.has_permission("Bosta Shipment", "read", user=users["Accounts User"]))
			self.assertIsNone(self._as(users["Accounts User"], order))
			frappe.db.set_value("Sales Order", order.name, "custom_bosta_delivery_status", "Delivered")
			frappe.set_user(users["Accounts User"])
			try:
				row = frappe.get_list(
					"Sales Order", filters={"name": order.name}, fields=["custom_bosta_delivery_status"]
				)
			finally:
				frappe.set_user("Administrator")
			self.assertEqual(row[0].custom_bosta_delivery_status, "Delivered")

	def test_summary_carries_the_cod_update_time_and_delivery_note(self):
		order = self.make_order(rate=150)
		self.http.queue["create"].append(created())
		self.create(order)
		summary = bosta_shipments.get_sales_order_shipment(order.name)
		shipment = self.shipment_for(order)
		self.assertEqual(summary["cod_amount"], 150)
		self.assertEqual(shipment.cod_amount, 150)
		self.assertEqual(summary["delivery_note"], shipment.delivery_note)
		self.assertEqual(summary["delivery_note_status"], "To Bill")
		self.assertEqual(frappe.db.get_value("Delivery Note", shipment.delivery_note, "status"), "To Bill")

		frappe.db.set_value("Bosta Shipment", shipment.name, "last_bosta_status_on", "2026-10-16 10:30:00")
		self.assertTrue(
			bosta_shipments.get_sales_order_shipment(order.name)["last_update"].startswith("2026-10-16 10:30")
		)

	def test_delivery_note_status_is_only_returned_to_users_who_can_read_it(self):
		order = self.make_order()
		self.http.queue["create"].append(created())
		self.create(order)
		reader = self.make_user("Stock User")
		self.assertTrue(frappe.has_permission("Delivery Note", "read", user=reader))
		self.assertEqual(self._as(reader, order)["delivery_note_status"], "To Bill")

		real = frappe.has_permission

		def deny_delivery_notes(doctype, *args, **kwargs):
			return False if doctype == "Delivery Note" else real(doctype, *args, **kwargs)

		with patch.object(deliveries.frappe, "has_permission", side_effect=deny_delivery_notes):
			hidden = self._as(reader, order)
		self.assertTrue(hidden["delivery_note"])
		self.assertEqual(hidden["delivery_note_status"], "")

	def test_a_user_who_cannot_read_the_sales_order_is_refused(self):
		order = self.make_order()
		self._shipment(order, "Completed", tracking_number="TRK-1")
		for user in ("Guest", self.make_user("Website Manager")):
			with self.subTest(user=user):
				frappe.set_user(user)
				try:
					with self.assertRaises(frappe.PermissionError):
						bosta_shipments.get_sales_order_shipment(order.name)
				finally:
					frappe.set_user("Administrator")

	def test_the_method_is_post_only_and_whitelisted(self):
		self.assertIn(bosta_shipments.get_sales_order_shipment, frappe.whitelisted)
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[bosta_shipments.get_sales_order_shipment],
			["POST"],
		)
