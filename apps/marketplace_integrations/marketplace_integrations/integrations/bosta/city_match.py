# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Resolve a delivery address to a Bosta city (#305, #318). Bosta resolves the district itself.

1. For an Egypt Shopify order, Shopify's province code through a fixed map to a Bosta city
   code. Only this city is trusted for a bulk create without review (``from_shopify``).
2. Else the Address city/state text against Bosta's city name, Arabic name or code.
"""

import re

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


def normalize(text):
	"""Lowercase; the alef variants become a plain alef, teh marbuta becomes heh, alef maksura
	becomes yeh; no diacritics or tatweel; Arabic digits -> Latin;
	punctuation collapsed to single spaces."""
	text = _STRIPPED.sub("", cstr(text).lower()).translate(_ARABIC_LETTERS).translate(_DIGITS)
	return _NON_WORD.sub(" ", text).strip()


def resolve_city(address):
	"""``{city_id, city_name, from_shopify}`` for an address, or ``{}``.

	``address`` keys (all optional): ``hints`` (city/state strings), ``country_code``,
	``province_code``. ``city_name`` is the directory's name.
	"""
	if not any(address.get(key) for key in ("hints", "province_code")):
		return {}
	directory = locations.get_directory()
	by_code = {city["code"]: city for city in directory if city.get("code")}
	if cstr(address.get("country_code")).upper() == "EG":
		code = SHOPIFY_EG_PROVINCE_TO_BOSTA_CITY_CODE.get(cstr(address.get("province_code")).upper())
		if code in by_code:
			return _result(by_code[code], from_shopify=True)
	wanted = {normalize(hint) for hint in address.get("hints") or []} - {""}
	for city in directory if wanted else ():
		if wanted & {normalize(city["name"]), normalize(city["name_ar"]), normalize(city.get("code"))}:
			return _result(city, from_shopify=False)
	return {}


def _result(city, from_shopify):
	return {"city_id": city["id"], "city_name": city["name"], "from_shopify": from_shopify}
