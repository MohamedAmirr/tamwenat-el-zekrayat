# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, now_datetime

CANCELED = "Canceled"
PENDING = "Pending Bosta"
SYSTEM_FIELDS = (
	"sales_order",
	"delivery_note",
	"status",
	"bosta_delivery_id",
	"tracking_number",
	"unique_business_reference",
	"bosta_state_code",
	"bosta_state_value",
	"last_bosta_event_timestamp",
	"bosta_exception_reason",
	"bosta_exception_code",
	"bosta_status_snapshot",
	"last_bosta_status_on",
	"last_error",
	"delivery_note_error",
	"delivery_note_error_at",
	"bosta_requested_on",
	"cod_amount",
)


def save_as_system(shipment, insert=False):
	"""Persist a Bosta Shipment from this app's own services (roles already checked)."""
	shipment.flags.bosta_system_write = True
	if insert:
		shipment.insert(ignore_permissions=True)
	else:
		shipment.save(ignore_permissions=True)
	return shipment


class BostaShipment(Document):
	"""One carrier delivery request for one Sales Order.

	This is deliberately independent of Marketplace Order shipment rows: Bosta
	fulfills ERPNext Sales Orders, regardless of where the order originated.
	"""

	def validate(self):
		self._block_system_field_writes()
		self._validate_required_delivery_details()
		self._validate_single_active_shipment()
		if self.has_value_changed("delivery_note_error"):
			# "Detected" in Needs Attention must not move on every later save.
			self.delivery_note_error_at = now_datetime() if self.delivery_note_error else None
		# Only an ACTIVE shipment holds its Sales Order; the unique index on this
		# field is the database-level "one active shipment per Sales Order" guard.
		# NULL (not "") for a Canceled shipment so unique does not collide.
		self.active_sales_order = None if self.status == CANCELED else self.sales_order

	def _validate_single_active_shipment(self):
		if self.status != CANCELED and frappe.db.exists(
			"Bosta Shipment",
			{"sales_order": self.sales_order, "status": ["!=", CANCELED], "name": ["!=", self.name or ""]},
		):
			frappe.throw(_("Sales Order {0} already has an active Bosta Shipment.").format(self.sales_order))

	def after_insert(self):
		# The name only exists after insert; db_set also updates this instance.
		self.db_set("unique_business_reference", f"BOSTA-{self.name}", update_modified=False)

	def _block_system_field_writes(self):
		"""System-set fields are writable only by server code (``flags.bosta_system_write``,
		set by this app's services after their own role check) or an administrator.
		``read_only`` in the form is not a server-side guarantee."""
		if (
			self.flags.bosta_system_write
			or frappe.session.user == "Administrator"
			or "System Manager" in frappe.get_roles()
		):
			return
		before = self.get_doc_before_save()
		for fieldname in SYSTEM_FIELDS:
			old = before.get(fieldname) if before else (PENDING if fieldname == "status" else None)
			if cstr(self.get(fieldname) or "") != cstr(old or ""):
				frappe.throw(_("{0} is set by the system and cannot be edited.").format(_(fieldname)))

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
