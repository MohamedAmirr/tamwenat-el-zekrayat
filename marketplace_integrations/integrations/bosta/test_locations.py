# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bosta city list, city code directory, ``get_city`` and the dialog address prefill (#245, #318).

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


def directory_with(*entries):
	return {"success": True, "data": {"list": list(entries)}}


CAIRO_ENTRY = {
	"cityId": "city-cairo",
	"cityName": "Cairo",
	"cityOtherName": "القاهرة",
	"cityCode": "EG-01",
	"districts": [{"districtId": "d-5th", "districtName": "5th Settlement"}],
}


class IntegrationTestBostaLocations(BostaFixtureMixin, IntegrationTestCase):
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

	def test_directory_keeps_only_city_id_name_and_code_and_is_cached(self):
		self.http.queue["all_districts"].append(
			FakeResponse(200, directory_with(CAIRO_ENTRY, {"cityName": "No id", "districts": []}))
		)

		first = locations.get_directory()
		second = locations.get_directory()

		self.assertEqual(
			first,
			[{"id": "city-cairo", "name": "Cairo", "name_ar": "القاهرة", "code": "EG-01"}],
		)
		self.assertEqual(second, first)
		self.assertEqual(len(self.http.calls_of("all_districts")), 1)

	def test_a_failed_directory_fetch_blocks_every_later_lookup_for_this_request_and_a_minute(self):
		self.http.queue["all_districts"].append(FakeResponse(500, {"success": False}))
		with self.assertRaises(frappe.ValidationError):
			locations.get_directory()
		with self.assertRaises(locations.BostaLookupUnavailable):
			locations.get_directory()
		frappe.local.bosta_lookup_failed = False  # a new request, still within the minute
		with self.assertRaises(locations.BostaLookupUnavailable):
			locations.get_directory()
		self.assertEqual(len(self.http.calls_of("all_districts")), 1)

	def test_a_failed_directory_fetch_caches_nothing(self):
		self.http.queue["all_districts"].append(FakeResponse(500, {"success": False}))
		with self.assertRaises(frappe.ValidationError):
			locations.get_directory()
		self.assertIsNone(frappe.cache().get_value(locations.DIRECTORY_CACHE_KEY))

	def test_refresh_reloads_cities_and_drops_the_cached_directory(self):
		self.http.queue["cities"] += [FakeResponse(200, CITIES), FakeResponse(200, CITIES)]
		locations.get_cities()
		locations.get_directory()

		locations.get_cities(refresh=True)
		locations.get_directory()

		self.assertEqual(len(self.http.calls_of("cities")), 2)
		self.assertEqual(len(self.http.calls_of("all_districts")), 2)

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

	def test_get_city_returns_the_list_name_and_the_directory_code(self):
		self.http.queue["cities"].append(
			FakeResponse(
				200, {"success": True, "data": {"list": [{"_id": "city-cairo", "name": "Cairo (Bosta)"}]}}
			)
		)
		self.http.queue["all_districts"].append(FakeResponse(200, directory_with(CAIRO_ENTRY)))

		self.assertEqual(
			locations.get_city("city-cairo"),
			{"id": "city-cairo", "name": "Cairo (Bosta)", "code": "EG-01"},
		)

	def test_get_city_refuses_an_empty_id_without_any_call(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Choose a Bosta city"):
			locations.get_city("  ")
		self.assertEqual(self.http.calls, [])

	def test_get_city_refuses_a_city_bosta_does_not_list(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Unknown Bosta city"):
			locations.get_city("city-nowhere")
		self.assertEqual(self.http.calls_of("create"), [])

	def test_get_city_refuses_a_city_without_a_code_and_never_sends_its_name(self):
		self.http.queue["all_districts"].append(
			FakeResponse(
				200,
				directory_with({"cityId": "city-cairo", "cityName": "Cairo", "cityCode": ""}),
			)
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Bosta has no city code for Cairo"):
			locations.get_city("city-cairo")

	def test_get_city_refuses_when_the_directory_cannot_be_loaded(self):
		self.http.queue["all_districts"].append(FakeResponse(500, {"success": False}))
		with self.assertRaises(frappe.ValidationError):
			locations.get_city("city-cairo")
		# The next lookup in this request fails fast with the friendly message.
		with self.assertRaisesRegex(frappe.ValidationError, "Could not load the Bosta city codes"):
			locations.get_city("city-cairo")
		self.assertEqual(len(self.http.calls_of("all_districts")), 1)

	def test_whitelisted_methods_require_a_creator_role(self):
		reader = self.make_user("Sales User")
		creator = self.make_user("Stock User")

		frappe.set_user(reader)
		try:
			with self.assertRaises(frappe.PermissionError):
				bosta_shipments.get_cities()
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self.http.calls, [])

		frappe.set_user(creator)
		try:
			self.assertEqual(len(bosta_shipments.get_cities("0")), 3)
		finally:
			frappe.set_user("Administrator")

	def test_create_sends_the_city_code_and_the_row_stores_city_id_code_and_name(self):
		order = self.make_order()
		self.http.queue["create"].append(created())

		bosta_shipments.create_shipment(
			order.name, self._receiver(), "01000000000", "city-cairo", "12 Example Street"
		)

		self.assertEqual(
			self.http.calls_of("create")[0]["dropOffAddress"],
			{"city": "EG-01", "firstLine": "12 Example Street"},
		)
		shipment = self.shipment_for(order)
		self.assertEqual(
			(shipment.city, shipment.city_id, shipment.city_code),
			("Cairo", "city-cairo", "EG-01"),
		)

	def test_create_refuses_a_missing_city_before_any_row_or_bosta_call(self):
		order = self.make_order()
		with self.assertRaisesRegex(frappe.ValidationError, "Choose a Bosta city"):
			bosta_shipments.create_shipment(order.name, self._receiver(), "0100", " ", "12 Example Street")
		self.assertEqual(self.http.calls, [])
		self.assertIsNone(self.shipment_for(order))

	def test_create_refuses_an_unknown_city_before_any_row_or_bosta_call(self):
		order = self.make_order()
		with self.assertRaisesRegex(frappe.ValidationError, "Unknown Bosta city"):
			bosta_shipments.create_shipment(
				order.name, self._receiver(), "0100", "city-nowhere", "12 Example Street"
			)
		self.assertEqual(self.http.calls_of("create"), [])
		self.assertIsNone(self.shipment_for(order))

	def test_create_refuses_a_city_without_a_code_before_any_row_or_bosta_call(self):
		order = self.make_order()
		self.http.queue["all_districts"].append(
			FakeResponse(200, directory_with({"cityId": "city-cairo", "cityName": "Cairo", "cityCode": ""}))
		)
		with self.assertRaisesRegex(frappe.ValidationError, "no city code"):
			bosta_shipments.create_shipment(
				order.name, self._receiver(), "0100", "city-cairo", "12 Example Street"
			)
		self.assertEqual(self.http.calls_of("create"), [])
		self.assertIsNone(self.shipment_for(order))

	def test_omitted_address_gives_the_friendly_message_not_a_type_error(self):
		order = self.make_order()
		with self.assertRaisesRegex(frappe.ValidationError, "Enter the address first line"):
			bosta_shipments.create_shipment(order.name, self._receiver(), "0100", "city-cairo")
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

	def _marketplace_patch(self, shipping_address=None, platform="Shopify", phone="", address=""):
		"""Stand in for the Marketplace Order reads (building a real one needs a Marketplace Account)."""
		real_get_value = frappe.db.get_value
		shipping = frappe.as_json(shipping_address) if shipping_address else None

		def get_value(doctype, *args, **kwargs):
			if doctype == "Marketplace Order":
				if isinstance(args[1], list):
					return frappe._dict(
						platform=platform, customer_phone_number=phone, shipping_address=shipping
					)
				return address
			return real_get_value(doctype, *args, **kwargs)

		return patch.object(frappe.db, "get_value", side_effect=get_value)

	def test_shipping_address_prefills_first_line_and_city_name_and_id_but_no_district(self):
		order = self.make_order()
		address = self._address(address_line2="Floor 3")
		frappe.db.set_value("Sales Order", order.name, "shipping_address_name", address.name)

		defaults = self._defaults(order)

		self.assertEqual(defaults["address_first_line"], "12 Example Street, Floor 3")
		self.assertEqual(defaults["address_city"], "Cairo")
		self.assertEqual((defaults["city_id"], defaults["city_name"]), ("city-cairo", "Cairo"))
		self.assertFalse(defaults["from_shopify"])
		for key in ("district", "district_id", "district_name"):
			self.assertNotIn(key, defaults)

	def test_an_address_naming_a_city_by_its_arabic_name_prefills_that_city(self):
		order = self.make_order()
		address = self._address(city="الجيزة", address_line1="Building 4")
		frappe.db.set_value("Sales Order", order.name, "shipping_address_name", address.name)

		defaults = self._defaults(order)

		self.assertEqual((defaults["city_id"], defaults["city_name"]), ("city-giza", "Giza"))

	def test_a_shopify_egypt_order_prefills_its_city_as_trusted(self):
		order = self.make_order()
		frappe.db.set_value("Sales Order", order.name, "custom_marketplace_order", "MKTORD-FAKE")
		shipping = {"countryCodeV2": "EG", "provinceCode": "SU", "city": "6th of October"}

		with self._marketplace_patch(shipping_address=shipping):
			defaults = self._defaults(order)

		self.assertEqual(
			(defaults["city_id"], defaults["city_name"], defaults["from_shopify"]),
			("city-giza", "Giza", True),
		)

	def test_an_unreachable_directory_only_loses_the_prefill_without_a_popup_or_a_second_try(self):
		order = self.make_order()
		other = self.make_order()
		address = self._address(address_line1="5th Settlement")
		frappe.db.set_value("Sales Order", order.name, "shipping_address_name", address.name)
		frappe.db.set_value("Sales Order", other.name, "shipping_address_name", address.name)
		self.http.queue["all_districts"].append(FakeResponse(500, {"success": False}))
		frappe.local.message_log = []

		defaults = self._defaults(order)
		other_defaults = self._defaults(other)

		self.assertEqual(frappe.local.message_log, [])
		self.assertEqual(len(self.http.calls_of("all_districts")), 1)
		self.assertEqual(defaults["address_first_line"], "5th Settlement")
		for prefilled in (defaults, other_defaults):
			self.assertNotIn("city_id", prefilled)
			self.assertNotIn("district_id", prefilled)

	def test_without_a_shipping_address_the_fields_are_blank(self):
		defaults = self._defaults(self.make_order())
		self.assertEqual((defaults["address_first_line"], defaults["address_city"]), ("", ""))
		self.assertNotIn("city_id", defaults)

	def test_marketplace_order_address_is_the_fallback_without_a_city(self):
		order = self.make_order()
		frappe.db.set_value("Sales Order", order.name, "custom_marketplace_order", "MKTORD-FAKE")

		with self._marketplace_patch(address=" 123 Main St, Cairo "):
			defaults = self._defaults(order)

		self.assertEqual(defaults["address_first_line"], "123 Main St, Cairo")
		self.assertEqual(defaults["address_city"], "")
		self.assertEqual(self.http.calls_of("all_districts"), [])

	def test_receiver_phone_falls_back_to_the_shopify_buyer_phone_only(self):
		order = self.make_order()
		self.assertEqual(self._defaults(order)["receiver_phone"], "")
		frappe.db.set_value("Sales Order", order.name, "custom_marketplace_order", "MKTORD-FAKE")
		real_get_value = frappe.db.get_value
		platform = ["Shopify"]

		def get_value(doctype, *args, **kwargs):
			if doctype == "Marketplace Order":  # building a real one needs a Marketplace Account
				return frappe._dict(platform=platform[0], customer_phone_number=" +201013271104 ")
			return real_get_value(doctype, *args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=get_value):
			self.assertEqual(self._defaults(order)["receiver_phone"], "+201013271104")
			frappe.db.set_value("Sales Order", order.name, "contact_mobile", "01000000000")
			self.assertEqual(self._defaults(order)["receiver_phone"], "01000000000")
			# Another platform's buyer phone is not used.
			frappe.db.set_value("Sales Order", order.name, "contact_mobile", "")
			platform[0] = "Talabat"
			self.assertEqual(self._defaults(order)["receiver_phone"], "")

	def test_shipping_address_wins_over_the_marketplace_order(self):
		order = self.make_order()
		address = self._address()
		frappe.db.set_value("Sales Order", order.name, "shipping_address_name", address.name)
		frappe.db.set_value("Sales Order", order.name, "custom_marketplace_order", "MKTORD-FAKE")

		self.assertEqual(self._defaults(order)["address_first_line"], "12 Example Street")
