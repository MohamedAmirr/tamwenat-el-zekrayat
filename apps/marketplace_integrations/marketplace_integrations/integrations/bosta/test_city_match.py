# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.bosta import city_match

DIRECTORY = [
	{"id": "city-cairo", "name": "Cairo", "name_ar": "القاهرة", "code": "EG-01"},
	{"id": "city-alex", "name": "Alexandria", "name_ar": "الإسكندرية", "code": "EG-02"},
	{"id": "city-giza", "name": "Giza", "name_ar": "الجيزة", "code": "EG-25"},
]


class UnitTestBostaCityMatch(UnitTestCase):
	def setUp(self):
		patcher = patch(
			"marketplace_integrations.integrations.bosta.city_match.locations.get_directory",
			return_value=DIRECTORY,
		)
		self.get_directory = patcher.start()
		self.addCleanup(patcher.stop)

	def test_egypt_shopify_province_maps_to_bosta_city_and_is_trusted(self):
		self.assertEqual(
			city_match.resolve_city({"country_code": "EG", "province_code": "C", "hints": ["Nasr City"]}),
			{"city_id": "city-cairo", "city_name": "Cairo", "from_shopify": True},
		)

	def test_shopify_only_province_uses_its_bosta_city(self):
		# "SU" (6th of October) is Shopify-only; Bosta has it under Giza (EG-25).
		self.assertEqual(
			city_match.resolve_city({"country_code": "EG", "province_code": "su"}),
			{"city_id": "city-giza", "city_name": "Giza", "from_shopify": True},
		)

	def test_province_code_is_ignored_outside_egypt(self):
		self.assertEqual(
			city_match.resolve_city({"country_code": "AE", "province_code": "C", "hints": []}),
			{},
		)

	def test_province_not_in_the_map_falls_back_to_the_hints(self):
		self.assertEqual(
			city_match.resolve_city({"country_code": "EG", "province_code": "ZZ", "hints": ["Alexandria"]}),
			{"city_id": "city-alex", "city_name": "Alexandria", "from_shopify": False},
		)

	def test_hint_matches_the_english_name_case_and_punctuation_insensitively(self):
		self.assertEqual(
			city_match.resolve_city({"hints": ["  GIZA, "]}),
			{"city_id": "city-giza", "city_name": "Giza", "from_shopify": False},
		)

	def test_hint_matches_the_arabic_name_across_alef_and_teh_marbuta_variants(self):
		# Teh marbuta and alef with hamza normalise to the directory spelling.
		self.assertEqual(
			city_match.resolve_city({"hints": ["القاهره"]})["city_id"],
			"city-cairo",
		)
		self.assertEqual(
			city_match.resolve_city({"hints": ["الاسكندرية"]})["city_id"],
			"city-alex",
		)

	def test_hint_matches_the_bosta_city_code(self):
		self.assertEqual(
			city_match.resolve_city({"hints": ["eg-25"]})["city_id"],
			"city-giza",
		)

	def test_hint_with_no_match_gives_an_empty_result(self):
		self.assertEqual(city_match.resolve_city({"hints": ["Nowhere Town"]}), {})

	def test_address_without_hints_or_province_gives_empty_and_never_reads_the_directory(self):
		self.assertEqual(city_match.resolve_city({}), {})
		self.get_directory.assert_not_called()
