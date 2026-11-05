# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

# ruff: noqa: RUF001  (the inputs are deliberately full-width and other-script characters)

import re

from frappe.tests import UnitTestCase

from marketplace_integrations.services.phone_numbers import clean_phone_number, phone_match_key

EGYPT = "+201069452847"
OUTPUT_SHAPE = re.compile(r"^\+?[0-9]{7,15}$")


class UnitTestPhoneNumbers(UnitTestCase):
	def test_egyptian_forms_share_one_storage_form_and_key(self):
		for value in (
			"+201069452847",
			"201069452847",
			"00201069452847",
			"0020 106 945 2847",
			"01069452847",
			"1069452847",
			"+20 106-945 2847",
			"(+20) 106.945.2847",
			"+2001069452847",
			"tel:+201069452847",
			"＋２０１０６９４５２８４７",
		):
			self.assertEqual(clean_phone_number(value), EGYPT, value)
			self.assertEqual(phone_match_key(value), "201069452847", value)

	def test_key_is_the_full_number_so_other_countries_do_not_collide(self):
		self.assertNotEqual(phone_match_key("+971501234567"), phone_match_key("01501234567"))
		self.assertEqual(phone_match_key("+971501234567"), "971501234567")
		self.assertEqual(phone_match_key("01501234567"), "201501234567")

	def test_unicode_decimal_digits_become_ascii(self):
		for value in (
			"٠١٠٦٩٤٥٢٨٤٧",  # Arabic-Indic
			"۰۱۰۶۹۴۵۲۸۴۷",  # Eastern Arabic-Indic
			"０１０６９４５２８４７",  # full-width
			"०१०६९४५२८४७",  # Devanagari
		):
			self.assertEqual(clean_phone_number(value), EGYPT, value)

	def test_superscripts_are_not_digits(self):
		self.assertIsNone(clean_phone_number("0106²9452847"))
		self.assertEqual(clean_phone_number("01069452847²"), EGYPT)

	def test_first_valid_number_of_several(self):
		for value in (
			"01069452847 / 01223456789",
			"01069452847, 01223456789",
			"01069452847; 01223456789",
			"01069452847\n01223456789",
			"01069452847 - 01223456789",
			"01069452847 و 01223456789",
			"1069452847و01223456789",
			"01069452847 or 01223456789",
			"01069452847|01223456789",
			"abc, 01069452847 / 01223456789",
		):
			self.assertEqual(clean_phone_number(value), EGYPT, value)

	def test_only_the_first_number_group_is_read(self):
		for value in (
			"whatsapp 01069452847",
			"Tel: 01069452847 ext 12",
			"01069452847 x12",
			"01069452847 call after 5",
			"رقم: 01069452847 .",
		):
			self.assertEqual(clean_phone_number(value), EGYPT, value)
		self.assertIsNone(clean_phone_number("call after 5"))

	def test_non_egyptian_numbers_keep_plus_and_digits(self):
		self.assertEqual(clean_phone_number("+971 50 123 4567"), "+971501234567")
		self.assertEqual(clean_phone_number("00971501234567"), "+971501234567")

	def test_unusable_values_return_none(self):
		for value in (
			None,
			"",
			"   ",
			"masked",
			"***",
			"+2010*****47",
			"123456",
			"+2" + "1" * 16,
			"0" * 11,
			"1111111",
			"ext 12",
			12.5,
			["01069452847"],
			{"a": 1},
			object(),
			True,
		):
			self.assertIsNone(clean_phone_number(value), repr(value))
			self.assertIsNone(phone_match_key(value), repr(value))

	def test_integer_and_very_long_input_do_not_raise(self):
		self.assertEqual(clean_phone_number(1069452847), EGYPT)
		self.assertEqual(clean_phone_number("01069452847" + " " * 5000 + "9" * 5000), EGYPT)
		self.assertIsNone(clean_phone_number("9" * 100000))

	def test_output_is_always_plus_and_ascii_digits_only(self):
		for value in (
			"+971501234567",
			"1069452847",
			"+123456789012345",
			"٠١٠٦٩٤٥٢٨٤٧",
			"０１０６９４５２８４７",
			"+٩٧١٥٠١٢٣٤٥٦٧",
			"tel: (+20) 1x0.6",
			"²³¹ 0106²9452847",
			"00" + "12345678" * 3,
		):
			cleaned = clean_phone_number(value)
			if cleaned:
				self.assertTrue(OUTPUT_SHAPE.fullmatch(cleaned), cleaned)
				self.assertLessEqual(len(cleaned), 16)
