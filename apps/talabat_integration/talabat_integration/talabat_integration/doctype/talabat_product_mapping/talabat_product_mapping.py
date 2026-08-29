# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class TalabatProductMapping(Document):
	def validate(self):
		self.validate_unique_item_vendor()

	def validate_unique_item_vendor(self):
		duplicate = frappe.db.exists(
			"Talabat Product Mapping",
			{
				"item": self.item,
				"vendor": self.vendor,
				"name": ["!=", self.name],
			},
		)
		if duplicate:
			frappe.throw(
				frappe._("A mapping for item {0} and vendor {1} already exists ({2}).").format(
					self.item, self.vendor, duplicate
				)
			)
