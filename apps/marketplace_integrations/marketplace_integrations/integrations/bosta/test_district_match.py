# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.bosta import district_match


def _district(district_id, name, name_ar="", **extra):
	return {"id": district_id, "name": name, "name_ar": name_ar, **extra}


DIRECTORY = [
	{
		"id": "city-cairo",
		"name": "Cairo",
		"name_ar": "القاهرة",
		"code": "EG-01",
		"districts": [
			_district("d-5th", "5th Settlement", "التجمع الخامس"),
			_district("d-5th-3", "5th Settlement - District 3", "التجمع الخامس - الحي 3"),
			_district("d-maadi", "Maadi", "المعادي"),
			_district("d-nasr", "Nasr City", "مدينة نصر"),
			_district("d-heliopolis", "Heliopolis", "مصر الجديدة"),
			_district("d-zamalek", "Zamalek", "الزمالك"),
			_district("d-mansoura", "Mansoura", "المنصورة"),
		],
	},
	{
		"id": "city-giza",
		"name": "Giza",
		"name_ar": "الجيزة",
		"code": "EG-02",
		"districts": [
			_district("d-maadi-giza", "Maadi", "المعادي"),
			_district("d-dokki", "Dokki", "الدقي"),
			_district("d-agouza", "Agouza", "العجوزة"),
		],
	},
]


class UnitTestBostaDistrictMatch(UnitTestCase):
	def setUp(self):
		patcher = patch(
			"marketplace_integrations.integrations.bosta.district_match.locations.get_directory",
			return_value=DIRECTORY,
		)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_english_name_matches_as_whole_words_case_insensitively(self):
		self.assertEqual(
			district_match.match_district("12 Street 9, NASR CITY, Cairo", None),
			{"city_id": "city-cairo", "city_name": "Cairo", "district_id": "d-nasr"},
		)

	def test_arabic_name_matches(self):
		match = district_match.match_district("شارع 9 التجمع الخامس القاهرة", None)
		self.assertEqual(match["district_id"], "d-5th")

	def test_arabic_letter_variants_diacritics_tatweel_and_digits_are_normalized(self):
		self.assertEqual(district_match.normalize("المَعـادى"), district_match.normalize("المعادي"))
		self.assertEqual(district_match.normalize("مدينة"), district_match.normalize("مدينه"))
		self.assertEqual(district_match.normalize("أإآ"), "ااا")  # noqa: RUF001
		self.assertEqual(district_match.normalize("الحي ٣ - Floor ۴"), "الحي 3 floor 4")
		match = district_match.match_district("الزَّمالك، ش ١٠", ["Cairo"])  # noqa: RUF001
		self.assertEqual(match["district_id"], "d-zamalek")

	def test_longest_match_wins_over_a_name_contained_in_it(self):
		match = district_match.match_district("Building 4, 5th Settlement - District 3, Cairo", None)
		self.assertEqual(match["district_id"], "d-5th-3")
		match = district_match.match_district("التجمع الخامس الحي 3", None)
		self.assertEqual(match["district_id"], "d-5th-3")
		# Without the longer name only the shorter district matches.
		self.assertEqual(district_match.match_district("5th Settlement", None)["district_id"], "d-5th")

	def test_names_only_match_as_whole_words(self):
		self.assertIsNone(district_match.match_district("Maadiville compound", None))
		self.assertIsNone(district_match.match_district("Dokkigate", None))

	def test_two_different_districts_in_the_address_are_ambiguous(self):
		self.assertIsNone(district_match.match_district("Nasr City near Zamalek", None))

	def test_same_name_in_two_cities_is_ambiguous_until_a_city_hint_picks_one(self):
		self.assertIsNone(district_match.match_district("Maadi", None))
		self.assertEqual(district_match.match_district("Maadi", "Giza")["district_id"], "d-maadi-giza")
		self.assertEqual(district_match.match_district("Maadi", ["", "الجيزة"])["city_id"], "city-giza")
		self.assertEqual(district_match.match_district("Maadi", "Cairo")["district_id"], "d-maadi")

	def test_city_hint_matches_name_arabic_name_or_code_and_limits_the_search(self):
		# Dokki is a Giza district: searching only Cairo finds nothing.
		self.assertIsNone(district_match.match_district("Dokki", "Cairo"))
		self.assertEqual(district_match.match_district("Dokki", "eg-02")["district_id"], "d-dokki")
		self.assertEqual(district_match.match_district("Dokki", ["Dokki", "Giza"])["district_id"], "d-dokki")

	def test_an_unknown_city_hint_searches_every_city(self):
		self.assertEqual(district_match.match_district("Dokki", "Atlantis")["district_id"], "d-dokki")

	def test_no_match_and_empty_text_return_none(self):
		self.assertIsNone(district_match.match_district("Somewhere else entirely", None))
		self.assertIsNone(district_match.match_district("", None))
		self.assertIsNone(district_match.match_district(None, None))

	def test_districts_without_drop_off_are_never_matched(self):
		raw = [
			{
				"cityId": "c1",
				"cityName": "Cairo",
				"cityOtherName": "القاهرة",
				"cityCode": "EG-01",
				"districts": [
					{"districtId": "ok", "districtName": "Maadi", "dropOffAvailability": True},
					{
						"districtId": "no",
						"districtName": "Zamalek",
						"districtOtherName": "الزمالك",
						"dropOffAvailability": False,
					},
				],
			}
		]
		from marketplace_integrations.integrations.bosta import locations

		directory = locations._parse_directory(raw)
		with patch.object(district_match.locations, "get_directory", return_value=directory):
			self.assertIsNone(district_match.match_district("Zamalek", None))
			self.assertIsNone(district_match.match_district("الزمالك", None))
			self.assertEqual(district_match.match_district("Maadi", None)["district_id"], "ok")
