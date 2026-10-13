# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr


class BostaShipment(Document):
	"""One carrier delivery request for one Sales Order.

	This is deliberately independent of Marketplace Order shipment rows: Bosta
	fulfills ERPNext Sales Orders, regardless of where the order originated.
	"""

	def validate(self):
		self._validate_required_delivery_details()

	def _validate_required_delivery_details(self):
		for fieldname, label in (
			("receiver_name", _("Receiver Name")),
			("receiver_phone", _("Receiver Phone")),
			("city", _("City")),
			("address_first_line", _("Address First Line")),
		):
			if not cstr(self.get(fieldname)).strip():
				frappe.throw(_("{0} is required.").format(label))

		if len(cstr(self.address_first_line).strip()) <= 5:
			frappe.throw(_("Address First Line must be more than 5 characters."))

		has_district_id = bool(cstr(self.district_id).strip())
		has_city_and_district_name = bool(cstr(self.city_id).strip()) and bool(
			cstr(self.district_name).strip()
		)
		if has_district_id == has_city_and_district_name:
			frappe.throw(_("Enter District ID, or both City ID and District Name."))
