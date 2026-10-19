# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Resolve a delivery address to a Bosta city and, when it can be told, one district (#305).

1. City: Shopify's province code (Egypt only) through a fixed map to a Bosta city code, else
   the Address city/state text against Bosta's city name, Arabic name or code.
2. District by text, inside that city: a district is a candidate only when ALL of its
   significant words appear in the address. The area Bosta puts in parentheses
   ("ElBurouj (ElShorouk)") may be left out when the rest of the name is unique in the city.
   Latin words are spelling-folded ("Al Buroj" == "ElBurouj"; al/el, ou/oo/o, ee). A candidate whose words are a
   strict subset of another's is dropped. Exactly one left is the district.
3. Otherwise Bosta's ``pinLocation`` for the Shopify coordinates (Egypt only).
4. Otherwise the operator picks; when steps 2 or 3 left several candidates they are returned
   so the dialog can list only those.
"""

import re

from frappe.utils import cstr

from marketplace_integrations.integrations.bosta import locations

# Shopify Egypt province code -> Bosta city code (Bosta cities: /cities `code`).
# Only "SU" (Shopify's "6th of October" province, which Bosta files under Giza) is confirmed
# from a real production order. The other codes follow Shopify's ISO 3166-2:EG-style codes and
# MUST be checked against production orders before they are trusted.
SHOPIFY_EG_PROVINCE_TO_BOSTA_CITY_CODE = {
	"ALX": "EG-02",
	"ASN": "EG-21",
	"AST": "EG-17",
	"BA": "EG-23",
	"BH": "EG-04",
	"BNS": "EG-16",
	"C": "EG-01",
	"DK": "EG-05",
	"DT": "EG-14",
	"FYM": "EG-15",
	"GH": "EG-07",
	"GZ": "EG-25",
	"HU": "EG-01",
	"IS": "EG-11",
	"JS": "EG-26",
	"KB": "EG-06",
	"KFS": "EG-08",
	"KN": "EG-20",
	"LX": "EG-22",
	"MN": "EG-19",
	"MNF": "EG-09",
	"MT": "EG-28",
	"PTS": "EG-13",
	"SHG": "EG-18",
	"SHR": "EG-10",
	"SIN": "EG-27",
	"SU": "EG-25",
	"SUZ": "EG-12",
	"WAD": "EG-24",
}

_ARABIC_LETTERS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})  # noqa: RUF001
# Arabic-Indic and Persian digits to Latin.
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
# Arabic diacritics (harakat, superscript alef) and tatweel.
_STRIPPED = re.compile("[ً-ٰٟـ]")
_NON_WORD = re.compile(r"[^\w]+")
# Bosta's area suffix in a district name: "ElBurouj (ElShorouk)".
_PARENTHESIZED = re.compile(r"\([^)]*\)")
# A leading Latin article, glued or not: "elburouj" -> "burouj".
_LATIN_ARTICLE = re.compile(r"^(?:al|el)(?=[a-z]{3,}$)")
_LATIN_ARTICLE_WORDS = {"al", "el"}
# The gap between a digit and a letter ("6اكتوبر" -> "6 اكتوبر").  # noqa: RUF003
_DIGIT_LETTER_GAP = re.compile(r"(?<=\d)(?=[^\W\d_])|(?<=[^\W\d_])(?=\d)")


def normalize(text):
	"""Lowercase; the alef variants become a plain alef, teh marbuta becomes heh, alef maksura
	becomes yeh; no diacritics or tatweel; Arabic digits -> Latin;
	punctuation collapsed to single spaces."""
	text = _STRIPPED.sub("", cstr(text).lower()).translate(_ARABIC_LETTERS).translate(_DIGITS)
	return _NON_WORD.sub(" ", text).strip()


def _normalized_words(raw):
	return {word for entry in raw.split() for word in normalize(entry).split()}


# Words that say nothing about the area (building, floor, street, "near"...).
FILLER_WORDS = _normalized_words(
	"عمارة عماره فيلا شقة شقه دور الدور برج building bldg villa flat apartment floor street st "
	"شارع ش near بجوار امام خلف"
)
# District-name words that are not specific enough to match on their own.
GENERIC_WORDS = _normalized_words("الحي الحى district مجاورة مجاوره neighbourhood neighborhood zone area")


def tokens(text):
	"""The address or name as words: normalized, digits split from letters, filler dropped,
	Latin words spelling-folded."""
	spaced = _DIGIT_LETTER_GAP.sub(" ", normalize(text))
	return [
		_fold_latin(word)
		for word in spaced.split()
		if word not in FILLER_WORDS and word not in _LATIN_ARTICLE_WORDS
	]


def _fold_latin(word):
	"""Transliteration variants of one Latin word: no leading al/el, ou/oo/o -> u, ee -> i
	("Buroj", "Burouj" and "Burooj" all become "buruj")."""
	if not word.isascii():
		return word
	word = _LATIN_ARTICLE.sub("", word)
	return word.replace("ou", "u").replace("oo", "u").replace("o", "u").replace("ee", "i")


def _is_specific(word):
	return not word.isdigit() and word not in GENERIC_WORDS


def resolve_location(address):
	"""``{city_id, city_name, district_id?, district_candidates?}`` for an address, or ``{}``.

	``address`` keys (all optional): ``text``, ``hints`` (city/state strings), ``country_code``,
	``province_code``, ``lat``, ``lng``. ``district_id`` is set only when exactly one district
	is found; ``district_candidates`` (district IDs, all in the returned city) when two or more
	are left.
	"""
	if not any(address.get(key) for key in ("text", "hints", "province_code", "lat")):
		return {}
	directory = locations.get_directory()
	city = _resolve_city(directory, address)
	words = set(tokens(address.get("text")))

	hits = _text_hits(directory, [city] if city else directory, words)
	if len(hits) == 1:
		return _result(hits[0][0], hits[0][1])

	pin_hits = _pin_hits(directory, city, address, words)
	if len(pin_hits) == 1:
		return _result(pin_hits[0][0], pin_hits[0][1])

	candidates = pin_hits if len(pin_hits) > 1 else hits
	candidate_cities = {candidate_city["id"] for candidate_city, _district in candidates}
	if len(candidates) > 1 and len(candidate_cities) == 1:
		return _result(candidates[0][0], None, [district["id"] for _city, district in candidates])
	return _result(city, None) if city else {}


def _result(city, district, candidates=None):
	result = {"city_id": city["id"], "city_name": city["name"]}
	if district:
		result["district_id"] = district["id"]
	if candidates:
		result["district_candidates"] = candidates
	return result


def _resolve_city(directory, address):
	by_code = {city["code"]: city for city in directory if city.get("code")}
	if cstr(address.get("country_code")).upper() == "EG":
		code = SHOPIFY_EG_PROVINCE_TO_BOSTA_CITY_CODE.get(cstr(address.get("province_code")).upper())
		if code in by_code:
			return by_code[code]
	wanted = {normalize(hint) for hint in address.get("hints") or []} - {""}
	if not wanted:
		return None
	for city in directory:
		if wanted & {normalize(city["name"]), normalize(city["name_ar"]), normalize(city.get("code"))}:
			return city
	return None


def _text_hits(directory, cities, address_words):
	"""``[(city, district)]`` whose every significant word is in the address, minus any whose
	word set is a strict subset of another hit's."""
	hits = []  # (city, district, words)
	for city in cities:
		main_name_counts = _main_name_counts(city)
		for district in city["districts"]:
			matched = None
			for name in (district["name"], district["name_ar"]):
				options = [set(tokens(name))]
				main_words = frozenset(tokens(_PARENTHESIZED.sub(" ", name)))
				# The parenthesized area is optional only when the rest is unique in the city.
				if main_words != options[0] and main_name_counts.get(main_words) == 1:
					options.append(set(main_words))
				for name_words in options:
					if (
						name_words
						and any(_is_specific(word) for word in name_words)
						and name_words <= address_words
						and (matched is None or len(name_words) > len(matched))
					):
						matched = name_words
			if matched:
				hits.append((city, district, matched))
	kept = [hit for hit in hits if not any(hit[2] < other[2] for other in hits)]
	return [(city, district) for city, district, _words in kept]


def _main_name_counts(city):
	"""How many districts of the city share each name with its parenthesized area removed."""
	counts = {}
	for district in city["districts"]:
		for main_words in {
			frozenset(tokens(_PARENTHESIZED.sub(" ", name))) for name in (district["name"], district["name_ar"])
		}:
			counts[main_words] = counts.get(main_words, 0) + 1
	return counts


def _pin_hits(directory, city, address, address_words):
	"""``[(city, district)]`` from Bosta's pinLocation for the order's coordinates (Egypt only):
	every result in the city, narrowed to those sharing a specific word with the address when
	there are several and any share one."""
	lat, lng = address.get("lat"), address.get("lng")
	if cstr(address.get("country_code")).upper() != "EG" or lat in (None, "") or lng in (None, ""):
		return []
	known = {
		(known_city["id"], district["id"]): (known_city, district)
		for known_city in directory
		for district in known_city["districts"]
	}
	rows = []
	for row in locations.get_pin_districts(lat, lng):
		found = known.get((row["city_id"], row["district_id"]))
		if found and (not city or found[0]["id"] == city["id"]) and found not in rows:
			rows.append(found)
	if len(rows) > 1:
		narrowed = [
			(row_city, district)
			for row_city, district in rows
			if {
				word
				for name in (district["name"], district["name_ar"])
				for word in tokens(name)
				if _is_specific(word)
			}
			& address_words
		]
		if narrowed:
			return narrowed
	return rows
