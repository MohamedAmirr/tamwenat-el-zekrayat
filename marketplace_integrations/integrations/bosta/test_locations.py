# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bosta city/district lists and the dialog address prefill (#245).

Real database and cache; only the HTTP layer is faked. Response bodies mirror the
examples in docs/bosta-integration/docs.yaml.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api import bosta_shipments
from marketplace_integrations.integrations.bosta import deliveries, locations
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin, FakeResponse, created

CITIES = {
	"success": True,
	"data": {
		"list": [
			{"_id": "city-cairo", "name": "Cairo", "nameAr": "القاهرة", "dropOffAvailability": True},
			{"_id": "city-alex", "name": "Alexandria", "nameAr": "الإسكندرية"},
			{"name": "No id"},
		]
	},
}
DISTRICTS = {
	"success": True,
	"data": [
		{
			"zoneName": "New Cairo",
			"districtId": "d-1",
			"districtName": "1st Settlement",
			"districtOtherName": "التجمع الاول",
			"dropOffAvailability": True,
		},
		{"zoneName": "Z", "districtId": "d-2", "districtName": "No Drop Off", "dropOffAvailability": False},
		{"zoneName": "Z", "districtId": "d-3", "districtName": "Maadi"},
	],
}


class IntegrationTestBostaLocations(BostaFixtureMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self._clear_cache()
		self.addCleanup(self._clear_cache)

	def _clear_cache(self):
		cache = frappe.cache()
		cache.delete_value(locations.CITIES_CACHE_KEY)
		for city_id in ("city-cairo", "city-alex"):
			cache.delete_value(locations.DISTRICTS_CACHE_KEY.format(city_id=city_id))

	def test_cities_use_the_documented_shape_and_second_call_is_served_from_cache(self):
		self.http.queue["cities"].append(FakeResponse(200, CITIES))

		first = locations.get_cities()
		second = locations.get_cities()

		self.assertEqual(
			first,
			[
				{"id": "city-cairo", "name": "Cairo", "name_ar": "القاهرة"},
				{"id": "city-alex", "name": "Alexandria", "name_ar": "الإسكندرية"},
			],
		)
		self.assertEqual(second, first)
		self.assertEqual(len(self.http.calls_of("cities")), 1)

	def test_districts_exclude_no_drop_off_and_are_cached_per_city(self):
		self.http.queue["cities"].append(FakeResponse(200, CITIES))
		self.http.queue["districts"].append(FakeResponse(200, DISTRICTS))

		districts = locations.get_districts("city-cairo")
		again = locations.get_districts("city-cairo")

		self.assertEqual([d["id"] for d in districts], ["d-1", "d-3"])
		self.assertEqual(
			districts[0],
			{"id": "d-1", "name": "1st Settlement", "name_ar": "التجمع الاول", "zone": "New Cairo"},
		)
		self.assertEqual(again, districts)
		self.assertEqual(len(self.http.calls_of("districts")), 1)
		self.assertTrue(self.http.calls_of("districts")[0].endswith("/cities/city-cairo/districts"))

	def test_refresh_reloads_cities_and_drops_cached_districts(self):
		self.http.queue["cities"] += [FakeResponse(200, CITIES), FakeResponse(200, CITIES)]
		self.http.queue["districts"] += [FakeResponse(200, DISTRICTS), FakeResponse(200, DISTRICTS)]
		locations.get_cities()
		locations.get_districts("city-cairo")

		locations.get_cities(refresh=True)
		locations.get_districts("city-cairo")

		self.assertEqual(len(self.http.calls_of("cities")), 2)
		self.assertEqual(len(self.http.calls_of("districts")), 2)

	def test_districts_refresh_reloads_only_that_city(self):
		self.http.queue["cities"].append(FakeResponse(200, CITIES))
		self.http.queue["districts"] += [FakeResponse(200, DISTRICTS), FakeResponse(200, DISTRICTS)]
		locations.get_districts("city-cairo")
		locations.get_districts("city-cairo", refresh=True)
		self.assertEqual(len(self.http.calls_of("districts")), 2)

	def test_unknown_city_ids_are_rejected_without_a_districts_call_or_cache_entry(self):
		self.http.queue["cities"].append(FakeResponse(200, CITIES))
		for bad in ("not-a-city", "..", "../cities", "", "  "):
			with self.assertRaises(frappe.ValidationError, msg=repr(bad)):
				locations.get_districts(bad)
			with self.assertRaises(frappe.ValidationError, msg=repr(bad)):
				locations.get_districts(bad, refresh=True)
			self.assertIsNone(
				frappe.cache().get_value(locations.DISTRICTS_CACHE_KEY.format(city_id=bad.strip()))
			)
		self.assertEqual(self.http.calls_of("districts"), [])

	def test_bosta_failures_raise_and_are_not_cached(self):
		self.http.queue["cities"] += [
			FakeResponse(500, {"success": False, "message": "Internal Server Error"}),
			frappe.ValidationError("Failed to reach Bosta."),
			FakeResponse(200, {"success": True, "data": {"unexpected": 1}}),
			FakeResponse(200, CITIES),
		]
		for _attempt in range(3):
			with self.assertRaises(frappe.ValidationError):
				locations.get_cities()
		self.assertEqual(len(locations.get_cities()), 2)

	def test_a_failed_refresh_keeps_the_previous_lists(self):
		self.http.queue["cities"] += [FakeResponse(200, CITIES), FakeResponse(500, {"success": False})]
		locations.get_cities()
		with self.assertRaises(frappe.ValidationError):
			locations.get_cities(refresh=True)
		self.assertEqual(len(locations.get_cities()), 2)
		self.assertEqual(len(self.http.calls_of("cities")), 2)

	def test_whitelisted_methods_require_a_creator_role(self):
		self.http.queue["cities"].append(FakeResponse(200, CITIES))
		self.http.queue["districts"].append(FakeResponse(200, DISTRICTS))
		reader = self.make_user("Sales User")
		creator = self.make_user("Stock User")

		frappe.set_user(reader)
		try:
			with self.assertRaises(frappe.PermissionError):
				bosta_shipments.get_cities()
			with self.assertRaises(frappe.PermissionError):
				bosta_shipments.get_districts("city-cairo")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self.http.calls, [])

		frappe.set_user(creator)
		try:
			self.assertEqual(len(bosta_shipments.get_cities("0")), 2)
			self.assertEqual(len(bosta_shipments.get_districts("city-cairo")), 2)
		finally:
			frappe.set_user("Administrator")

	def test_create_sends_the_district_id_and_city_name_and_row_stores_them(self):
		order = self.make_order()
		self.http.queue["create"].append(created())

		bosta_shipments.create_shipment(
			order.name, self._receiver(), "01000000000", "Cairo", "d-1", "12 Example Street"
		)

		address = self.http.calls_of("create")[0]["dropOffAddress"]
		self.assertEqual(address, {"city": "Cairo", "districtId": "d-1", "firstLine": "12 Example Street"})
		shipment = self.shipment_for(order)
		self.assertEqual((shipment.city, shipment.district_id), ("Cairo", "d-1"))

	def test_create_requires_a_district(self):
		order = self.make_order()
		with self.assertRaises(frappe.ValidationError):
			bosta_shipments.create_shipment(
				order.name, self._receiver(), "0100", "Cairo", " ", "12 Example Street"
			)
		with self.assertRaises(TypeError):  # the City ID / District Name path is gone
			bosta_shipments.create_shipment(
				order.name, self._receiver(), "0100", "Cairo", "d-1", "12 Example Street", city_id="c"
			)
		self.assertEqual(self.http.calls, [])
		self.assertIsNone(self.shipment_for(order))

	def test_omitted_district_or_address_gives_the_friendly_message_not_a_type_error(self):
		order = self.make_order()
		with self.assertRaisesRegex(frappe.ValidationError, "Choose a Bosta district"):
			bosta_shipments.create_shipment(order.name, self._receiver(), "0100", "Cairo")
		with self.assertRaisesRegex(frappe.ValidationError, "address first line"):
			bosta_shipments.create_shipment(order.name, self._receiver(), "0100", "Cairo", "d-1")
		self.assertEqual(self.http.calls, [])
		self.assertIsNone(self.shipment_for(order))


class IntegrationTestBostaAddressPrefill(BostaFixtureMixin, IntegrationTestCase):
	def _address(self, **values):
		address = frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": f"Bosta Test {self.suffix}",
				"address_type": "Shipping",
				"address_line1": "12 Example Street",
				"city": "Cairo",
				"country": frappe.db.get_value("Country", {}, "name"),
				**values,
			}
		).insert()
		self._track(address)
		frappe.db.commit()
		return address

	def _defaults(self, order):
		return deliveries.get_sales_order_defaults(order.name)

	def test_shipping_address_prefills_first_line_and_source_city_but_no_district(self):
		order = self.make_order()
		address = self._address(address_line2="Floor 3")
		frappe.db.set_value("Sales Order", order.name, "shipping_address_name", address.name)

		defaults = self._defaults(order)

		self.assertEqual(defaults["address_first_line"], "12 Example Street, Floor 3")
		self.assertEqual(defaults["address_city"], "Cairo")
		self.assertNotIn("district", defaults)
		self.assertNotIn("district_id", defaults)

	def test_without_a_shipping_address_the_fields_are_blank(self):
		defaults = self._defaults(self.make_order())
		self.assertEqual((defaults["address_first_line"], defaults["address_city"]), ("", ""))

	def test_marketplace_order_address_is_the_fallback_without_a_city(self):
		order = self.make_order()
		frappe.db.set_value("Sales Order", order.name, "custom_marketplace_order", "MKTORD-FAKE")
		real_get_value = frappe.db.get_value

		def get_value(doctype, *args, **kwargs):
			if doctype == "Marketplace Order":  # building a real one needs a Marketplace Account
				return " 123 Main St, Cairo "
			return real_get_value(doctype, *args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=get_value):
			defaults = self._defaults(order)

		self.assertEqual(defaults["address_first_line"], "123 Main St, Cairo")
		self.assertEqual(defaults["address_city"], "")

	def test_shipping_address_wins_over_the_marketplace_order(self):
		order = self.make_order()
		address = self._address()
		frappe.db.set_value("Sales Order", order.name, "shipping_address_name", address.name)
		frappe.db.set_value("Sales Order", order.name, "custom_marketplace_order", "MKTORD-FAKE")

		self.assertEqual(self._defaults(order)["address_first_line"], "12 Example Street")
