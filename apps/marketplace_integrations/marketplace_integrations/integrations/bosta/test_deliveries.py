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
			payload = deliveries._build_delivery_payload(self._shipment(), 123.45)

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
				"businessReference": "BOSTA-SAL-ORD-0001",
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
				1,
			)

		self.assertEqual(
			payload["dropOffAddress"],
			{"city": "Cairo", "cityId": "city-id", "districtName": "Maadi", "firstLine": "12 Example Street"},
		)

	def test_accepted_response_requires_successful_http_status_and_not_false_success_flag(self):
		is_accepted = deliveries.is_success_response
		response = SimpleNamespace(status_code=200, json=lambda: {"success": True})
		self.assertTrue(is_accepted(response))
		self.assertFalse(is_accepted(SimpleNamespace(status_code=400, json=lambda: {"success": True})))
		self.assertFalse(is_accepted(SimpleNamespace(status_code=200, json=lambda: {"success": False})))
