# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.bosta import district_match


def _d(district_id, name, name_ar=""):
	return {"id": district_id, "name": name, "name_ar": name_ar}


DIRECTORY = [
	{
		"id": "city-cairo",
		"name": "Cairo",
		"name_ar": "القاهرة",
		"code": "EG-01",
		"districts": [
			_d("d-5th-1", "5th Settlement - District 1", "التجمع الخامس - الحي ١"),  # noqa: RUF001
			_d("d-5th-3", "5th Settlement - District 3", "التجمع الخامس - الحي ٣"),
			_d("d-maadi", "Maadi", "المعادي"),
			_d("d-maadi-sar", "Maadi Sarayat", "معادي السرايات"),
			_d("d-nasr", "Nasr City", "مدينة نصر"),
			_d("d-zamalek", "Zamalek", "الزمالك"),
			_d("d-generic", "District 5", "الحي ٥"),  # noqa: RUF001
			_d("d-burouj", "ElBurouj (ElShorouk)", "البروج (الشروق)"),
			_d("d-darmasr-shorouk", "Dar Masr (ElShorouk)", "دار مصر (الشروق)"),
			_d("d-darmasr-andalos", "Dar Masr (ElAndalos)", "دار مصر (الاندلس)"),
		],
	},
	{
		"id": "city-giza",
		"name": "Giza",
		"name_ar": "الجيزة",
		"code": "EG-25",
		"districts": [
			_d("d-maadi-giza", "Maadi", "المعادي"),
			_d("d-oct-9", "District 09 (06 October)", "الحي 09 (6 اكتوبر)"),
			_d("d-oct-8", "District 08 (06 October)", "الحي 08 (6 اكتوبر)"),
			_d("d-narges-1", "ElNarges 01", "النرجس 01"),
			_d("d-narges-2", "ElNarges 02", "النرجس 02"),
			_d("d-dokki", "Dokki", "الدقي"),
		],
	},
	{"id": "city-alex", "name": "Alexandria", "name_ar": "الإسكندرية", "code": "EG-02", "districts": []},
]


def _resolve(text="", **extra):
	return district_match.resolve_location({"text": text, **extra})


class UnitTestBostaDistrictMatch(UnitTestCase):
	def setUp(self):
		self.pin = []
		for target, kwargs in (
			(
				"marketplace_integrations.integrations.bosta.district_match.locations.get_directory",
				{"return_value": DIRECTORY},
			),
			(
				"marketplace_integrations.integrations.bosta.district_match.locations.get_pin_districts",
				{"side_effect": lambda lat, lng: self.pin},
			),
		):
			patcher = patch(target, **kwargs)
			patcher.start()
			self.addCleanup(patcher.stop)

	# City

	def test_shopify_province_code_picks_the_bosta_city_for_egypt_only(self):
		self.assertEqual(_resolve("x", country_code="EG", province_code="SU")["city_id"], "city-giza")
		self.assertEqual(_resolve("x", country_code="EG", province_code="C")["city_id"], "city-cairo")
		self.assertEqual(_resolve("x", country_code="EG", province_code="ALX")["city_id"], "city-alex")
		self.assertEqual(_resolve("x", country_code="EG", province_code="HU")["city_id"], "city-cairo")
		self.assertEqual(_resolve("x", country_code="SA", province_code="SU"), {})
		self.assertEqual(_resolve("x", province_code="SU"), {})

	def test_city_falls_back_to_the_address_city_or_state_text(self):
		self.assertEqual(_resolve("x", hints=["", "الجيزة"])["city_id"], "city-giza")
		self.assertEqual(_resolve("x", hints=["eg-01"])["city_id"], "city-cairo")
		self.assertEqual(_resolve("x", hints=["Atlantis"]), {})

	def test_province_code_wins_over_the_address_text(self):
		result = _resolve("x", country_code="EG", province_code="SU", hints=["Cairo"])
		self.assertEqual(result["city_id"], "city-giza")

	# Text

	def test_all_significant_words_match_arabic_with_filler_and_digits(self):
		result = _resolve("التجمع الخامس - الحي 3 فيلا 160", hints=["Cairo"])
		self.assertEqual(result, {"city_id": "city-cairo", "city_name": "Cairo", "district_id": "d-5th-3"})

	def test_english_words_in_any_order_match(self):
		result = _resolve("Villa 160, District 3, Fifth... 5th Settlement", hints=["Cairo"])
		self.assertEqual(result["district_id"], "d-5th-3")

	def test_a_missing_significant_word_means_no_district(self):
		result = _resolve("5th Settlement elandalos-building 170", hints=["Cairo"])
		self.assertNotIn("district_id", result)
		self.assertNotIn("district_candidates", result)

	def test_digits_glued_to_letters_are_split(self):
		result = _resolve("6اكتوبر الحي 09", country_code="EG", province_code="SU")  # noqa: RUF001
		self.assertEqual(result["district_id"], "d-oct-9")

	def test_arabic_letter_variants_and_diacritics_are_normalized(self):
		self.assertEqual(district_match.normalize("المَعـادى"), district_match.normalize("المعادي"))
		self.assertEqual(district_match.normalize("الحي ٣ - Floor ۴"), "الحي 3 floor 4")
		self.assertEqual(_resolve("الزَّمالك", hints=["Cairo"])["district_id"], "d-zamalek")

	def test_a_district_whose_words_are_a_subset_of_another_hit_is_dropped(self):
		self.assertEqual(_resolve("Maadi Sarayat", hints=["Cairo"])["district_id"], "d-maadi-sar")
		self.assertEqual(_resolve("Maadi", hints=["Cairo"])["district_id"], "d-maadi")

	def test_names_made_only_of_generic_words_never_match(self):
		result = _resolve("District 5, الحي 5", hints=["Cairo"])
		self.assertNotIn("district_id", result)

	def test_same_name_in_two_cities_needs_a_city(self):
		result = _resolve("Maadi")
		self.assertNotIn("district_id", result)
		self.assertEqual(_resolve("Maadi", hints=["Giza"])["district_id"], "d-maadi-giza")

	def test_two_unrelated_hits_are_candidates_not_a_match(self):
		result = _resolve("Nasr City near Zamalek", hints=["Cairo"])
		self.assertNotIn("district_id", result)
		self.assertEqual(sorted(result["district_candidates"]), ["d-nasr", "d-zamalek"])

	def test_hits_in_different_cities_give_no_candidates_and_no_city(self):
		self.assertEqual(_resolve("Maadi Dokki"), {})

	# pinLocation

	def test_a_single_pin_result_is_used_and_supplies_the_city(self):
		self.pin = [{"city_id": "city-giza", "district_id": "d-oct-9", "name": "District 09"}]
		result = _resolve("somewhere vague", country_code="EG", lat=29.97, lng=30.94)
		self.assertEqual(result, {"city_id": "city-giza", "city_name": "Giza", "district_id": "d-oct-9"})

	def test_pin_results_outside_the_resolved_city_are_ignored(self):
		self.pin = [{"city_id": "city-giza", "district_id": "d-oct-9", "name": "x"}]
		result = _resolve("vague", country_code="EG", province_code="C", lat=1, lng=2)
		self.assertEqual(result, {"city_id": "city-cairo", "city_name": "Cairo"})

	def test_several_pin_results_are_narrowed_by_words_in_the_address(self):
		self.pin = [
			{"city_id": "city-giza", "district_id": "d-narges-1", "name": ""},
			{"city_id": "city-giza", "district_id": "d-narges-2", "name": ""},
			{"city_id": "city-giza", "district_id": "d-oct-9", "name": ""},
		]
		result = _resolve("حي النرجس فيلا 4", country_code="EG", lat=1, lng=2)
		# Both Narges districts share the word with the address: they stay candidates.
		self.assertNotIn("district_id", result)
		self.assertEqual(sorted(result["district_candidates"]), ["d-narges-1", "d-narges-2"])

	def test_several_pin_results_narrowed_to_one_by_words_use_it(self):
		self.pin = [
			{"city_id": "city-giza", "district_id": "d-narges-1", "name": ""},
			{"city_id": "city-giza", "district_id": "d-oct-9", "name": ""},
		]
		result = _resolve("النرجس", country_code="EG", lat=1, lng=2)
		self.assertEqual(result["district_id"], "d-narges-1")

	def test_several_pin_results_sharing_nothing_with_the_address_are_all_candidates(self):
		self.pin = [
			{"city_id": "city-giza", "district_id": "d-narges-1", "name": ""},
			{"city_id": "city-giza", "district_id": "d-dokki", "name": ""},
		]
		result = _resolve("unrelated words", country_code="EG", lat=1, lng=2)
		self.assertEqual(sorted(result["district_candidates"]), ["d-dokki", "d-narges-1"])

	def test_several_pin_results_with_one_word_match_use_that_one(self):
		self.pin = [
			{"city_id": "city-giza", "district_id": "d-narges-1", "name": ""},
			{"city_id": "city-giza", "district_id": "d-dokki", "name": ""},
		]
		result = _resolve("الدقي شارع 5", country_code="EG", lat=1, lng=2)
		self.assertEqual(result["district_id"], "d-dokki")

	def test_pin_is_not_used_outside_egypt_or_without_both_coordinates(self):
		self.pin = [{"city_id": "city-giza", "district_id": "d-oct-9", "name": ""}]
		self.assertEqual(_resolve("vague", country_code="SA", lat=1, lng=2), {})
		self.assertEqual(_resolve("vague", lat=1, lng=2), {})
		self.assertEqual(_resolve("vague", country_code="EG", lat=1), {})
		self.assertEqual(_resolve("vague", country_code="EG", lat=1, lng=""), {})

	def test_a_text_match_needs_no_pin_lookup(self):
		with patch.object(district_match.locations, "get_pin_districts") as pin:
			_resolve("Dokki", country_code="EG", province_code="GZ", lat=1, lng=2)
		pin.assert_not_called()

	# Spelling fold and the optional parenthesized area

	def test_latin_spelling_variants_match_the_bosta_name(self):
		for text in ("Al Buroj, Cairo, Cairo", "El Burouj, Cairo", "elburooj Cairo"):
			with self.subTest(text=text):
				self.assertEqual(_resolve(text, hints=["Cairo"]).get("district_id"), "d-burouj")

	def test_parenthesized_area_is_optional_when_the_rest_is_unique_in_the_city(self):
		self.assertEqual(_resolve("البروج، عمارة 5", hints=["Cairo"]).get("district_id"), "d-burouj")
		self.assertEqual(_resolve("Al Buroj (ElShorouk)", hints=["Cairo"]).get("district_id"), "d-burouj")

	def test_parenthesized_area_is_required_when_the_rest_is_shared(self):
		self.assertNotIn("district_id", _resolve("Dar Masr building 3", hints=["Cairo"]))
		self.assertEqual(
			_resolve("Dar Masr ElShorouk building 3", hints=["Cairo"]).get("district_id"), "d-darmasr-shorouk"
		)
