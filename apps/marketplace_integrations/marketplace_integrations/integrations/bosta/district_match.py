# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Match a free-text delivery address to exactly one Bosta district (#305).

A district matches when its full English or Arabic name appears in the address as whole
words (after normalization). There is no fuzzy or partial matching. Anything other than
exactly one remaining match returns ``None``, so the operator picks the district.
"""

import re

from frappe.utils import cstr

from marketplace_integrations.integrations.bosta import locations

_ARABIC_LETTERS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})  # noqa: RUF001
# Arabic-Indic and Persian digits to Latin.
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
# Arabic diacritics (harakat, superscript alef) and tatweel.
_STRIPPED = re.compile("[ً-ٰٟـ]")
_NON_WORD = re.compile(r"[^\w]+")


def normalize(text):
	"""Lowercase; the alef variants become a plain alef, teh marbuta becomes heh, alef maksura
	becomes yeh; no diacritics or tatweel; Arabic digits -> Latin;
	punctuation collapsed to single spaces."""
	text = _STRIPPED.sub("", cstr(text).lower()).translate(_ARABIC_LETTERS).translate(_DIGITS)
	return _NON_WORD.sub(" ", text).strip()


def match_district(text, city_hint=None):
	"""``{city_id, city_name, district_id}`` when exactly one district matches, else ``None``.

	``city_hint`` is one string or a list of strings (the address city and state). When one
	equals a Bosta city's name, Arabic name or code, only that city's districts are searched.
	When a district name is contained in another matched district's name (``5th Settlement``
	in ``5th Settlement - District 3``), the longer one wins.
	"""
	haystack = f" {normalize(text)} "
	if not haystack.strip():
		return None
	directory = locations.get_directory()
	cities = _cities_for_hint(directory, city_hint)

	matches = []  # (city, district, matched normalized name)
	for city in cities:
		for district in city["districts"]:
			names = {normalize(district["name"]), normalize(district["name_ar"])} - {""}
			found = [name for name in names if f" {name} " in haystack]
			if found:
				matches.append((city, district, max(found, key=len)))

	# A match inside a longer matched name is the same place, not a second candidate.
	remaining = [
		match
		for match in matches
		if not any(
			other is not match and f" {match[2]} " in f" {other[2]} " and match[2] != other[2]
			for other in matches
		)
	]
	if len(remaining) != 1:
		return None
	city, district, _name = remaining[0]
	return {"city_id": city["id"], "city_name": city["name"], "district_id": district["id"]}


def _cities_for_hint(directory, city_hint):
	hints = [city_hint] if isinstance(city_hint, str) else list(city_hint or [])
	wanted = {normalize(hint) for hint in hints} - {""}
	if not wanted:
		return directory
	narrowed = [
		city
		for city in directory
		if wanted & {normalize(city["name"]), normalize(city["name_ar"]), normalize(city.get("code"))}
	]
	return narrowed or directory
