# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.bosta import deliveries


class UnitTestBostaDeliveries(UnitTestCase):
	def _shipment(self, **values):
		defaults = {
			"receiver_name": "Ahmed",
			"receiver_phone": "01000000000",
			"city": "Cairo",
			"district_id": "district-id",
			"city_id": "",
			"district_name": "",
			"address_first_line": "12 Example Street",
			"unique_business_reference": "BOSTA-SAL-ORD-0001",
		}
		defaults.update(values)
		return SimpleNamespace(**defaults)

	def test_build_payload_sends_only_mvp_fields_with_district_id_address(self):
		with patch(
			"marketplace_integrations.integrations.bosta.deliveries.frappe.get_single",
			return_value=SimpleNamespace(business_location_id="business-location"),
		):
			payload = deliveries._build_delivery_payload(
				self._shipment(), SimpleNamespace(grand_total=123.45)
			)

		self.assertEqual(
			payload,
			{
				"type": 10,
				"cod": 123.45,
				"receiver": {"firstName": "Ahmed", "phone": "01000000000"},
				"dropOffAddress": {
					"city": "Cairo",
					"districtId": "district-id",
					"firstLine": "12 Example Street",
				},
				"businessLocationId": "business-location",
				"uniqueBusinessReference": "BOSTA-SAL-ORD-0001",
			},
		)

	def test_build_payload_supports_the_documented_city_id_district_name_address(self):
		with patch(
			"marketplace_integrations.integrations.bosta.deliveries.frappe.get_single",
			return_value=SimpleNamespace(business_location_id="business-location"),
		):
			payload = deliveries._build_delivery_payload(
				self._shipment(district_id="", city_id="city-id", district_name="Maadi"),
				SimpleNamespace(grand_total=1),
			)

		self.assertEqual(
			payload["dropOffAddress"],
			{"city": "Cairo", "cityId": "city-id", "districtName": "Maadi", "firstLine": "12 Example Street"},
		)

	def test_accepted_response_requires_successful_http_status_and_not_false_success_flag(self):
		response = SimpleNamespace(status_code=200, json=lambda: {"success": True})
		self.assertTrue(deliveries._is_accepted_response(response))
		self.assertFalse(
			deliveries._is_accepted_response(SimpleNamespace(status_code=400, json=lambda: {"success": True}))
		)
		self.assertFalse(
			deliveries._is_accepted_response(
				SimpleNamespace(status_code=200, json=lambda: {"success": False})
			)
		)

	def test_creation_uses_the_persisted_shipment_name_for_the_unique_reference(self):
		class Shipment:
			name = "BOSTA-SHP-00001"
			status = "Pending Bosta"
			bosta_delivery_id = ""

			def insert(self):
				return self

			def save(self):
				return self

		shipment = Shipment()
		order = SimpleNamespace(name="SAL-ORD-00001", grand_total=100)
		response = SimpleNamespace(
			status_code=201,
			json=lambda: {"success": True, "data": {"_id": "bosta-id", "trackingNumber": "track"}},
		)
		client = SimpleNamespace(create_delivery=lambda payload: response)

		with (
			patch.object(deliveries, "_get_submitted_sales_order", return_value=order),
			patch.object(deliveries.frappe.db, "get_value", return_value=None),
			patch.object(deliveries.frappe.db, "commit"),
			patch.object(deliveries.frappe, "get_doc", return_value=shipment),
			patch.object(deliveries, "BostaClient", return_value=client),
			patch.object(deliveries, "_build_delivery_payload", return_value={}),
			patch.object(
				deliveries, "_create_delivery_note", return_value={"shipment": shipment.name}
			) as create_delivery_note,
		):
			result = deliveries.create_bosta_shipment(
				order.name, "Ahmed", "01000000000", "Cairo", "district-id", "", "", "12 Example Street"
			)

		self.assertEqual(shipment.unique_business_reference, "BOSTA-BOSTA-SHP-00001")
		create_delivery_note.assert_called_once_with(shipment, order)
		self.assertEqual(result, {"shipment": shipment.name})
