# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Pure phone-number cleaning for marketplace buyers (no frappe, never raises)."""

import re
import unicodedata

MIN_DIGITS = 7
MAX_DIGITS = 15
# Longer input is junk (or an attack); the first numbers are all that is ever used.
MAX_INPUT_LENGTH = 200

ASCII_DIGITS = "0123456789"

# Several numbers in one field: newline, "/", ",", ";", "|", the Arabic comma/semicolon, the
# Arabic word "و" (also without spaces), " - " (spaced dash only: a dash inside a number is a
# plain separator) and the words "or"/"and".
_CANDIDATE_SPLIT = re.compile(r"[\n\r/,;|،؛و]|\s-\s|\s(?:or|and)\s", re.IGNORECASE)
_NUMBER_START = re.compile(r"(\+)?\s*[0-9]")


def _to_ascii_digits(text):
	"""Map every Unicode decimal digit (Arabic-Indic, full-width, Devanagari, ...) to ASCII.

	Characters that are digits but not decimals (superscripts such as ``²``) are not mapped
	and are later dropped like any other symbol.
	"""
	return "".join(str(digit) if (digit := unicodedata.decimal(ch, None)) is not None else ch for ch in text)


def clean_phone_number(value):
	"""Return the first usable number of ``value`` in one storage form, else ``None``.

	The result always matches ``^\\+?[0-9]{7,15}$`` (ASCII digits only), so it passes
	Frappe's phone pattern. Unicode decimal digits become ASCII. When the field holds
	several numbers (see ``_CANDIDATE_SPLIT``) the first valid one wins. Within a
	candidate only the FIRST contiguous number group is read: it starts at the first
	digit (with a leading ``+``) and continues over digits, spaces, ``-``, ``.`` and
	parentheses, so words and the digits that follow them ("ext 12", "x12",
	"call after 5") are ignored. Egyptian forms ``+20...``, ``0020...``, ``20...``,
	``0...`` and a bare 10-digit mobile all become ``+20`` plus the 10 digits
	(``+201069452847``); any other number keeps a plus and its digits. Unusable:
	masked (``*`` in the candidate), fewer than 7 or more than 15 digits, one repeated
	digit, or not text.
	"""
	if isinstance(value, bool):
		return None
	if isinstance(value, int):
		value = str(value)
	if not isinstance(value, str):
		return None
	text = _to_ascii_digits(value[:MAX_INPUT_LENGTH]).replace("＋", "+")  # noqa: RUF001 - intentional full-width plus
	for candidate in _CANDIDATE_SPLIT.split(text):
		cleaned = _clean_candidate(candidate)
		if cleaned:
			return cleaned
	return None


def _first_number_group(candidate):
	"""Return ``(plus, digits)`` of the first contiguous number group, or ``None``."""
	match = _NUMBER_START.search(candidate)
	if not match:
		return None
	plus = bool(match.group(1))
	digits = []
	for ch in candidate[match.start() :]:
		if ch in ASCII_DIGITS:
			digits.append(ch)
		elif not (ch.isspace() or ch in "+-.()"):
			break
		elif ch == "+" and digits:
			break
	return plus, "".join(digits)


def _clean_candidate(candidate):
	if "*" in candidate:
		return None
	group = _first_number_group(candidate)
	if not group:
		return None
	plus, digits = group
	if not digits or len(set(digits)) == 1:
		return None  # one repeated digit (0000000, 1111111111): placeholder junk
	if digits.startswith("00"):
		digits, plus = digits[2:], True
	if digits.startswith("200") and len(digits) == 13:
		digits, plus = "20" + digits[3:], True  # "+20 0106..." typed with the trunk zero kept
	if plus:
		if digits.startswith("20") and len(digits) == 12:
			return "+" + digits
	elif digits.startswith("0") and len(digits) == 11:
		return "+20" + digits[1:]
	elif digits.startswith("1") and len(digits) == 10:
		return "+20" + digits
	elif digits.startswith("20") and len(digits) == 12:
		return "+" + digits
	if not MIN_DIGITS <= len(digits) <= MAX_DIGITS:
		return None
	return "+" + digits


def phone_match_key(value):
	"""Return the digits of the cleaned number (full E.164 without ``+``), or ``None``.

	Both sides of a comparison are cleaned the same way, so ``01069452847`` and
	``+201069452847`` give ``201069452847`` while ``+971501234567`` never equals
	``01501234567`` (``201501234567``).
	"""
	cleaned = clean_phone_number(value)
	if not cleaned:
		return None
	return cleaned[1:]
