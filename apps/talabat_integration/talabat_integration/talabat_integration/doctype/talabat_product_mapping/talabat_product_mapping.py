# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class TalabatProductMapping(Document):
	def validate(self):
		self.validate_unique_item_vendor()

	def before_save(self):
		self.item_vendor_key = f"{self.item}-{self.vendor}"

	def validate_unique_item_vendor(self):
		# item_vendor_key isn't set yet at this point (before_save runs after
		# validate) - compute the candidate directly for this friendly pre-check.
		# The field's own DB unique index is the atomic backstop for the
		# concurrent case; this is just a cleaner error for the common case.
		candidate_key = f"{self.item}-{self.vendor}"
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
