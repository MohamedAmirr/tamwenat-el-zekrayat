# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.bosta.client import BostaClient


class FakeSettings:
	def __init__(self, *, enabled=1, api_key="bosta-test-key"):
		self.enabled = enabled
		self.api_key = api_key

	def get_password(self, fieldname, raise_exception=False):
		assert fieldname == "api_key"
		return self.api_key


class FakeResponse:
	def __init__(self, status_code=200, body=None, text=""):
		self.status_code = status_code
		self.body = body
		self.text = text

	def json(self):
		if self.body is None:
			raise ValueError("not JSON")
		return self.body


class UnitTestBostaClient(UnitTestCase):
	def setUp(self):
		self.settings = FakeSettings()
		self.client = BostaClient(settings=self.settings)

	def test_create_delivery_passes_workflow_payload_unchanged(self):
		payload = {"delivery": {"value": "unchanged"}}
		response = FakeResponse(201, {"success": True})

		with patch(
			"marketplace_integrations.integrations.bosta.client.http_request", return_value=response
		) as request:
			result = self.client.create_delivery(payload)

		self.assertIs(result, response)
		args, kwargs = request.call_args
		self.assertEqual(args[:2], ("POST", "https://app.bosta.co/api/v2/deliveries?apiVersion=1"))
		self.assertEqual(kwargs["json"], payload)
		self.assertEqual(
			kwargs["headers"], {"Authorization": "bosta-test-key", "Content-Type": "application/json"}
		)
		self.assertFalse(kwargs["include_exception_details"])

	def test_terminate_delivery_sends_a_bodyless_delete_to_the_documented_path(self):
		response = FakeResponse(200, {"success": True})
		with patch(
			"marketplace_integrations.integrations.bosta.client.http_request", return_value=response
		) as request:
			self.assertIs(self.client.terminate_delivery(" 5108002 "), response)

		args, kwargs = request.call_args
		self.assertEqual(
			args[:2], ("DELETE", "https://app.bosta.co/api/v2/deliveries/business/5108002/terminate")
		)
		self.assertNotIn("json", kwargs)
		self.assertNotIn("data", kwargs)

	def test_terminate_delivery_requires_a_tracking_number_and_quotes_it(self):
		with patch("marketplace_integrations.integrations.bosta.client.http_request") as request:
			with self.assertRaises(frappe.ValidationError):
				self.client.terminate_delivery("  ")
			request.assert_not_called()
			self.client.terminate_delivery("a/b")
		self.assertTrue(request.call_args.args[1].endswith("/business/a%2Fb/terminate"))

	def test_create_delivery_returns_failed_response_for_caller_to_handle(self):
		response = FakeResponse(400, {"message": "invalid receiver"})
		with patch("marketplace_integrations.integrations.bosta.client.http_request", return_value=response):
			result = self.client.create_delivery({"verified": True})

		self.assertIs(result, response)
		self.assertIn("HTTP 400", self.client.error_message(response))

	def test_a4_arabic_awb_uses_only_documented_fixed_options(self):
		response = FakeResponse(200)
		with patch(
			"marketplace_integrations.integrations.bosta.client.http_request", return_value=response
		) as request:
			self.client.get_a4_arabic_awb(["77873113", "77873114"])

		args, kwargs = request.call_args
		self.assertEqual(args[:2], ("POST", "https://app.bosta.co/api/v2/deliveries/mass-awb"))
		self.assertEqual(
			kwargs["json"],
			{"trackingNumbers": "77873113,77873114", "requestedAwbType": "A4", "lang": "ar"},
		)

	def test_a4_arabic_awb_rejects_more_than_fifty_tracking_numbers_before_request(self):
		tracking_numbers = [str(index) for index in range(51)]
		with patch("marketplace_integrations.integrations.bosta.client.http_request") as request:
			with self.assertRaises(frappe.ValidationError):
				self.client.get_a4_arabic_awb(tracking_numbers)
		request.assert_not_called()

	def test_a4_arabic_awb_accepts_fifty_tracking_numbers(self):
		tracking_numbers = [str(index) for index in range(50)]
		response = FakeResponse(200)
		with patch(
			"marketplace_integrations.integrations.bosta.client.http_request", return_value=response
		) as request:
			self.client.get_a4_arabic_awb(tracking_numbers)

		self.assertEqual(request.call_args.kwargs["json"]["trackingNumbers"], ",".join(tracking_numbers))

	def test_get_delivery_quotes_the_tracking_number(self):
		response = FakeResponse(200, {"success": True, "data": {}})
		with patch(
			"marketplace_integrations.integrations.bosta.client.http_request", return_value=response
		) as request:
			self.client.get_delivery("BOS/ 123")

		args, _kwargs = request.call_args
		self.assertEqual(args[:2], ("GET", "https://app.bosta.co/api/v2/deliveries/business/BOS%2F%20123"))

	def test_request_refuses_disabled_or_incomplete_settings_without_network_call(self):
		self.settings.enabled = 0
		with patch("marketplace_integrations.integrations.bosta.client.http_request") as request:
			with self.assertRaises(frappe.ValidationError):
				self.client.create_delivery({})
		request.assert_not_called()

		self.settings.enabled = 1
		self.settings.api_key = ""
		with patch("marketplace_integrations.integrations.bosta.client.http_request") as request:
			with self.assertRaises(frappe.ValidationError):
				self.client.create_delivery({})
		request.assert_not_called()

	def test_rejects_non_object_delivery_payload_and_blank_tracking_number(self):
		with self.assertRaises(frappe.ValidationError):
			self.client.create_delivery([])
		with self.assertRaises(frappe.ValidationError):
			self.client.get_a4_arabic_awb(" ")
		with self.assertRaises(frappe.ValidationError):
			self.client.get_delivery(" ")
