# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bosta pickup locations for the Bosta Settings picker (#276).

Nothing is created or stored: Bosta Settings is replaced by a stub and only the HTTP layer
is faked. Response bodies mirror the GET /pickup-locations example in
docs/bosta-integration/docs.yaml.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api import bosta_settings
from marketplace_integrations.integrations.bosta import locations
from marketplace_integrations.integrations.bosta.client import BostaClient
from marketplace_integrations.tests.bosta_fixtures import FakeResponse

PAGE_1 = {
	"success": True,
	"data": {
		"total": 2,
		"pages": 2,
		"page": 1,
		"list": [
			{
				"_id": "yfWPU0tP2",
				"locationName": "business101",
				"address": {"firstLine": "el-mohandseen", "city": {"_id": "c1", "name": "cairo"}},
				"isDefault": True,
			},
			{"locationName": "No id"},
		],
	},
}
PAGE_2 = {
	"success": True,
	"data": {
		"pages": 2,
		"page": 2,
		"list": [
			{
				"_id": "5f0a7def4a839b00139d6203",
				"locationName": "Original Business Location",
				"address": {"firstLine": "1st settlement, New Cairo, Egypt"},
			}
		],
	},
}


class IntegrationTestBostaPickupLocations(IntegrationTestCase):
	def setUp(self):
		self.responses = []
		self.requests = []
		self.api_key = "api-key"
		self.enabled = 0

		def fake_http(method, url, **kwargs):
			self.requests.append((method, url, kwargs.get("params"), kwargs["headers"]["Authorization"]))
			item = self.responses.pop(0)
			if isinstance(item, Exception):
				raise item
			return item

		stub = SimpleNamespace(enabled=self.enabled, get_password=lambda *args, **kwargs: self.api_key)
		for patcher in (
			patch("marketplace_integrations.integrations.bosta.client.http_request", side_effect=fake_http),
			patch(
				"marketplace_integrations.integrations.bosta.client.BostaClient.get_settings",
				return_value=stub,
			),
		):
			patcher.start()
			self.addCleanup(patcher.stop)
		self.addCleanup(frappe.set_user, "Administrator")

	def test_every_page_is_fetched_and_shaped_while_bosta_is_still_disabled(self):
		self.responses += [FakeResponse(200, PAGE_1), FakeResponse(200, PAGE_2)]

		result = locations.get_pickup_locations()

		self.assertEqual(
			result,
			[
				{
					"id": "yfWPU0tP2",
					"name": "business101",
					"address": "el-mohandseen",
					"city": "cairo",
					"is_default": True,
				},
				{
					"id": "5f0a7def4a839b00139d6203",
					"name": "Original Business Location",
					"address": "1st settlement, New Cairo, Egypt",
					"city": "",
					"is_default": False,
				},
			],
		)
		self.assertEqual([request[2] for request in self.requests], [{"page": 1}, {"page": 2}])
		self.assertTrue(all(r[0] == "GET" and r[1].endswith("/pickup-locations") for r in self.requests))
		self.assertEqual({r[3] for r in self.requests}, {"api-key"})

	def test_other_operations_still_require_bosta_to_be_enabled(self):
		with self.assertRaises(frappe.ValidationError):
			BostaClient().ensure_ready()

	def test_no_saved_api_key_is_rejected_before_any_request(self):
		self.api_key = ""
		with self.assertRaises(frappe.ValidationError):
			locations.get_pickup_locations()
		self.assertEqual(self.requests, [])

	def test_a_bosta_error_is_raised_escaped_and_not_retried(self):
		self.responses.append(
			FakeResponse(401, {"success": False, "message": "<img src=x onerror=alert(1)>"})
		)
		with self.assertRaises(frappe.ValidationError) as raised:
			locations.get_pickup_locations()
		self.assertNotIn("<img", str(raised.exception))
		self.assertIn("&lt;img", str(raised.exception))
		self.assertEqual(len(self.requests), 1)

	def test_an_unrecognised_response_is_rejected(self):
		self.responses.append(FakeResponse(200, {"success": True, "data": {"list": "nope"}}))
		with self.assertRaises(frappe.ValidationError):
			locations.get_pickup_locations()

	def test_an_empty_list_returns_nothing(self):
		self.responses.append(FakeResponse(200, {"success": True, "data": {"list": [], "pages": 0}}))
		self.assertEqual(locations.get_pickup_locations(), [])

	def test_the_page_walk_is_capped_at_twenty_requests(self):
		endless = {"success": True, "data": {"pages": 999, "page": 1, "list": [{"_id": "a"}]}}
		self.responses += [FakeResponse(200, endless) for _ in range(30)]

		locations.get_pickup_locations()

		self.assertEqual(len(self.requests), 20)

	def test_a_location_repeated_across_pages_is_returned_once(self):
		page = {"success": True, "data": {"pages": 2, "list": [{"_id": "dup", "locationName": "A"}]}}
		self.responses += [FakeResponse(200, page), FakeResponse(200, page)]

		self.assertEqual([item["id"] for item in locations.get_pickup_locations()], ["dup"])

	def test_the_endpoint_is_for_system_managers_only(self):
		self.responses.append(FakeResponse(200, PAGE_2 | {"data": {**PAGE_2["data"], "pages": 1}}))
		self.assertEqual(len(bosta_settings.get_pickup_locations()), 1)

		user = f"bosta-picker-{frappe.generate_hash(length=8)}@example.com"
		self.assertFalse(frappe.db.exists("User", user))
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": user,
				"first_name": "Picker",
				"send_welcome_email": 0,
				"roles": [{"role": "Sales User"}],
			}
		).insert(ignore_permissions=True)
		self.addCleanup(self._delete_user, doc.name)
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			bosta_settings.get_pickup_locations()
		self.assertEqual(len(self.requests), 1)

	def _delete_user(self, name):
		frappe.set_user("Administrator")
		frappe.delete_doc("User", name, force=True, ignore_permissions=True, delete_permanently=True)
		frappe.db.delete("Comment", {"reference_doctype": "User", "reference_name": name})
		frappe.db.commit()
