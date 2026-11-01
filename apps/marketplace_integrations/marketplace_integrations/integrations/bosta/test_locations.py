# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bosta city list, city code directory, and ``get_city`` (#245, #318).

Real database and cache; only the HTTP layer is faked. Response bodies mirror the
examples in docs/bosta-integration/docs.yaml.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.bosta import locations
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin, FakeResponse

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
