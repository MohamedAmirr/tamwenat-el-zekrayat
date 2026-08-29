# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import hashlib

import frappe
from frappe.model.document import Document

# Separator used to build this doctype's document name (vendor + talabat_sku).
# A plain "-" collides for real: item codes in this app's own seed data
# contain hyphens, and Talabat vendor/chain IDs are UUID-shaped (all
# hyphens), so `vendor="X-Y", sku="Z"` and `vendor="X", sku="Y-Z"` would
# otherwise both produce "X-Y-Z". "::" is vanishingly unlikely to appear in
# either value; validate() rejects it outright so the invariant this naming
# scheme depends on is enforced, not just assumed.
NAME_SEPARATOR = "::"


def compute_item_vendor_key(item, vendor):
	"""Unambiguous, fixed-length encoding for the (item, vendor)
	secondary-uniqueness key.

	A NUL-separated hash rather than a "-"-joined string (real collision risk
	- see NAME_SEPARATOR's docstring) or a raw JSON encoding (that had its own
	problem: frappe.as_json's default ensure_ascii=True blows up non-ASCII
	content - e.g. Arabic item codes - into \\uXXXX escapes, and even the
	pure-ASCII worst case of two 140-char fields left almost no margin against
	the field's length limit). A hash is always a fixed 64 hex chars
	regardless of input content or length, and the NUL byte can't appear in
	either value undetected the way a printable separator could.
	"""
	digest_input = f"{item}\x00{vendor}".encode()
	return hashlib.sha256(digest_input).hexdigest()


class TalabatProductMapping(Document):
	def autoname(self):
		self._validate_no_separator_in_name_fields()
		self.name = f"{self.vendor}{NAME_SEPARATOR}{self.talabat_sku}"

	def validate(self):
		self._validate_no_separator_in_name_fields()
		self.validate_unique_item_vendor()

	def before_save(self):
		self.item_vendor_key = compute_item_vendor_key(self.item, self.vendor)

	def _validate_no_separator_in_name_fields(self):
		if NAME_SEPARATOR in (self.vendor or "") or NAME_SEPARATOR in (self.talabat_sku or ""):
			frappe.throw(frappe._("Vendor and Talabat SKU may not contain '{0}'.").format(NAME_SEPARATOR))

	def validate_unique_item_vendor(self):
		# item_vendor_key isn't set yet at this point (before_save runs after
		# validate) - compute the candidate directly for this friendly pre-check.
		# The field's own DB unique index is the atomic backstop for the
		# concurrent case; this is just a cleaner error for the common case.
		candidate_key = compute_item_vendor_key(self.item, self.vendor)
		duplicate = frappe.db.exists(
			"Talabat Product Mapping",
			{
				"item_vendor_key": candidate_key,
				"name": ["!=", self.name or ""],
			},
		)
		if duplicate:
			frappe.throw(
				frappe._("A mapping for item {0} and vendor {1} already exists ({2}).").format(
					self.item, self.vendor, duplicate
				)
			)
