# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Resolve a delivery address to a Bosta city and, when it can be told, one district (#305).

1. City: Shopify's province code (Egypt only) through a fixed map to a Bosta city code, else
   the Address city/state text against Bosta's city name, Arabic name or code.
2. District by text, within the resolved city (or every city when none resolved, in which case
   a hit is only ever offered as a candidate, never used as the district): a district is a
   candidate only when ALL of its significant words appear in the address, and a number in its
   name sits next to a generic word or another word of that name there. A number that follows
   a filler word ("فيلا 3") is a house number and is ignored. The area Bosta puts in parentheses
   ("ElBurouj (ElShorouk)") may be left out when the rest of the name is unique in the city, but
   such a match is only ever a candidate: "elandalos" alone is not proof of "ElAndalous (Badr City)".
   Latin words are spelling-folded ("Al Buroj" == "ElBurouj"; al/el, ou/oo/o, ee). A candidate whose words are a
   strict subset of another's is dropped. Exactly one left is the district.
3. Otherwise Bosta's ``pinLocation`` for the Shopify coordinates (Egypt only).
4. Otherwise the operator picks; when steps 2 or 3 left several candidates they are returned
   so the dialog can list only those.
"""

import re

import frappe
from frappe.utils import cstr

from marketplace_integrations.integrations.bosta import locations

# Shopify Egypt province code -> Bosta city code (Bosta cities: /cities `code`).
# Verified against Shopify's Egypt checkout province list on 2026-10-19. "SU" ("6th of October")
# and "HU" (Helwan) are Shopify-only provinces; Bosta has them under Giza and Cairo.
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
	Latin words spelling-folded, leading zeros of numbers dropped ("09" == "9"). A number that
	directly follows a filler word is a house number ("فيلا 3", "building 12") and is dropped."""
	spaced = _DIGIT_LETTER_GAP.sub(" ", normalize(text))
	words = []
	after_filler = False
	for word in spaced.split():
		if word in FILLER_WORDS:
			after_filler = True
			continue
		if word in _LATIN_ARTICLE_WORDS:
			after_filler = False
			continue
		if word.isdigit():
			if not after_filler:
				words.append(word.lstrip("0") or "0")
		else:
			words.append(_fold_latin(word))
		after_filler = False
	return words


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
	address_seq = tokens(address.get("text"))
	words = set(address_seq)

	hits = _text_hits([city] if city else directory, address_seq)
	# Without a resolved city a text hit is never trusted on its own: a word like "Maadi" can
	# name a district in several cities, so it can only be offered as a candidate. Nor is one
	# that matched without its parenthesized area.
	if city and len(hits) == 1 and not hits[0][2]:
		return _result(hits[0][0], hits[0][1])

	pin_hits = _pin_hits(directory, city, address, words)
	if len(pin_hits) == 1:
		return _result(pin_hits[0][0], pin_hits[0][1])

	candidates = pin_hits if len(pin_hits) > 1 else [(hit[0], hit[1]) for hit in hits]
	if candidates and len({candidate_city["id"] for candidate_city, _district in candidates}) == 1:
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


def _text_hits(cities, address_seq):
	"""``[(city, district, partial)]`` whose every significant word is in the address, minus
	any whose word set is a strict subset of another hit's. ``partial``: matched without the
	parenthesized area. A number in a district name must sit next to a generic word
	("الحي 3", "District 3") or another word of that name in the address."""
	address_words = set(address_seq)
	hits = []  # (city, district, words, partial)
	for city in cities:
		for district, options in _city_index(city):
			matched = None
			for name_words, partial in options:
				if (
					name_words <= address_words
					and (matched is None or len(name_words) > len(matched[0]))
					and _numbers_adjacent(name_words, address_seq)
				):
					matched = (name_words, partial)
			if matched:
				hits.append((city, district, *matched))
	kept = [hit for hit in hits if not any(hit[2] < other[2] for other in hits)]
	return [(city, district, partial) for city, district, _words, partial in kept]


def _numbers_adjacent(name_words, address_seq):
	neighbours = GENERIC_WORDS | {word for word in name_words if not word.isdigit()}
	for number in (word for word in name_words if word.isdigit()):
		if not any(
			word == number and _next_to(address_seq, index, neighbours)
			for index, word in enumerate(address_seq)
		):
			return False
	return True


def _next_to(sequence, index, words):
	before = sequence[index - 1] if index > 0 else None
	after = sequence[index + 1] if index + 1 < len(sequence) else None
	return before in words or after in words


def _city_index(city):
	"""``[(district, [(word set that may match, partial)])]`` for a city, tokenized once per request
	(the directory itself is kept per request by ``locations.get_directory``)."""
	index = getattr(frappe.local, "bosta_name_index", None)
	if index is None:
		index = frappe.local.bosta_name_index = {}
	entry = index.get(id(city))
	if entry and entry[0] is city:
		return entry[1]
	main_name_counts = _main_name_counts(city)
	rows = []
	for district in city["districts"]:
		options = []
		for name in (district["name"], district["name_ar"]):
			full = frozenset(tokens(name))
			candidates = [(full, False)]
			main_words = frozenset(tokens(_PARENTHESIZED.sub(" ", name)))
			# The parenthesized area is optional only when the rest is unique in the city.
			if main_words != full and main_name_counts.get(main_words) == 1:
				candidates.append((main_words, True))
			options.extend(
				(words, partial) for words, partial in candidates if words and any(_is_specific(w) for w in words)
			)
		rows.append((district, options))
	index[id(city)] = (city, rows)
	return rows


def _main_name_counts(city):
	"""How many districts of the city share each name with its parenthesized area removed."""
	counts = {}
	for district in city["districts"]:
		for main_words in {
			frozenset(tokens(_PARENTHESIZED.sub(" ", name)))
			for name in (district["name"], district["name_ar"])
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
