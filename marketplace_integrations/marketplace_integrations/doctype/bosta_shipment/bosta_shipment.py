# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, flt, now_datetime

CANCELED = "Canceled"
PENDING = "Pending Bosta"
# Fields of the Bosta delivery that the Bosta Edit action owns: locked for everyone once the
# shipment has a tracking number (except the system write flag).
DELIVERY_DETAIL_FIELDS = (
	"receiver_name",
	"receiver_phone",
	"city",
	"city_id",
	"city_code",
	"address_first_line",
)
SYSTEM_FIELDS = (
	"sales_order",
	"company",
	"delivery_note",
	"fulfillment_key",
	"reservation_status",
	"handover_status",
	"handover_confirmed_by",
	"handover_confirmed_on",
	"stock_posting_status",
	"shipment_items",
	"external_delivery_identity_key",
	"active_sales_order",
	"status",
	"bosta_delivery_id",
	"tracking_number",
	"unique_business_reference",
	"bosta_state_code",
	"bosta_state_value",
	"bosta_delivery_type",
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
	"shipment_fee",
	"bosta_terminated_on",
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
		self._validate_company()
		self._lock_delivery_details_after_tracking()
		self._validate_required_delivery_details()
		self._validate_identity_keys()
		self._validate_shipment_items()
		self._lock_shipment_items()
		if self.has_value_changed("delivery_note_error"):
			# "Detected" in Needs Attention must not move on every later save.
			self.delivery_note_error_at = now_datetime() if self.delivery_note_error else None

	def _validate_company(self):
		if not self.sales_order:
			return
		order_company = frappe.db.get_value("Sales Order", self.sales_order, "company")
		if self.company and self.company != order_company:
			frappe.throw(_("Company {0} does not match Sales Order {1}.").format(self.company, self.sales_order))
		self.company = order_company

	def before_insert(self):
		if not self.fulfillment_key:
			self.fulfillment_key = frappe.generate_hash(length=40)

	def _validate_identity_keys(self):
		before = self.get_doc_before_save()
		if before and before.fulfillment_key and self.fulfillment_key != before.fulfillment_key:
			frappe.throw(_("Fulfillment Key cannot be changed after creation."))
		if self.fulfillment_key and frappe.db.exists(
			"Bosta Shipment", {"fulfillment_key": self.fulfillment_key, "name": ["!=", self.name or ""]}
		):
			frappe.throw(_("Fulfillment Key must be unique."))

		delivery_id = cstr(self.bosta_delivery_id).strip()
		if not delivery_id:
			self.external_delivery_identity_key = None
			return

		identity_key = hashlib.sha256(frappe.as_json(["Bosta", delivery_id]).encode()).hexdigest()
		if before and before.bosta_delivery_id and before.bosta_delivery_id != self.bosta_delivery_id:
			frappe.throw(_("Bosta Delivery ID cannot be changed after it is recorded."))
		other_delivery_ids = frappe.db.sql(
			"SELECT bosta_delivery_id FROM `tabBosta Shipment` WHERE name != %s",
			(self.name or "",),
			as_dict=True,
		)
		duplicate = any(cstr(row.bosta_delivery_id).strip() == delivery_id for row in other_delivery_ids)
		if duplicate:
			# Existing duplicate identifiers are left untouched by the migration. They
			# remain visible for manual review and cannot claim a canonical unique key.
			if before and cstr(before.bosta_delivery_id).strip() == delivery_id:
				self.external_delivery_identity_key = None
				return
			frappe.throw(_("Bosta Delivery ID {0} is already linked to another shipment.").format(delivery_id))
		self.external_delivery_identity_key = identity_key

	def _validate_shipment_items(self):
		for row in self.get("shipment_items") or []:
			so_item = frappe.db.get_value(
				"Sales Order Item", row.so_detail, ["parent", "item_code"], as_dict=True
			)
			if not so_item or so_item.parent != self.sales_order or so_item.item_code != row.item_code:
				frappe.throw(
					_("Shipment item {0} does not belong to Sales Order {1}.").format(
						row.so_detail, self.sales_order
					)
				)
			if row.warehouse and frappe.db.get_value("Warehouse", row.warehouse, "company") != self.company:
				frappe.throw(_("Warehouse {0} does not belong to Company {1}.").format(row.warehouse, self.company))
			stock_precision = frappe.get_precision("Sales Order Item", "stock_qty")
			qty_precision = frappe.get_precision("Sales Order Item", "qty")
			if flt(row.stock_qty, stock_precision) <= 0 or flt(row.qty, qty_precision) <= 0:
				frappe.throw(_("Shipment item quantities must be greater than zero."))

	def _lock_shipment_items(self):
		before = self.get_doc_before_save()
		if before and not self.flags.bosta_system_write:
			old_items = [row.as_dict() for row in before.get("shipment_items") or []]
			new_items = [row.as_dict() for row in self.get("shipment_items") or []]
			if old_items != new_items:
				frappe.throw(_("Shipment items are managed by the system and cannot be edited."))

	def after_insert(self):
		# The name only exists after insert; db_set also updates this instance.
		self.db_set("unique_business_reference", f"BOSTA-{self.name}", update_modified=False)
		self._sync_sales_order_bosta_state()

	def on_update(self):
		self._sync_sales_order_bosta_state()

	def _sync_sales_order_bosta_state(self):
		"""Mirror the same active-first shipment selection used by the Sales Order summary."""
		if not self.sales_order or not frappe.db.exists(
			"Custom Field", "Sales Order-custom_bosta_delivery_status"
		):
			return
		rows = frappe.get_all(
			"Bosta Shipment",
			filters={"sales_order": self.sales_order},
			fields=["name", "status", "bosta_state_code"],
			order_by="creation desc, name desc",
			limit_page_length=0,
		)
		current = next((row for row in rows if row.status != CANCELED), rows[0] if rows else None)
		if current:
			# Import lazily: statuses imports this controller for its CANCELED/save helpers.
			from marketplace_integrations.integrations.bosta.statuses import BOSTA_STATE_NAMES

			state = BOSTA_STATE_NAMES.get(current.bosta_state_code) or current.status or ""
		else:
			state = ""
		if frappe.db.get_value("Sales Order", self.sales_order, "custom_bosta_delivery_status") != state:
			# This is a derived display field; don't run Sales Order accounting or update modified.
			frappe.db.set_value(
				"Sales Order", self.sales_order, "custom_bosta_delivery_status", state, update_modified=False
			)

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

	def _lock_delivery_details_after_tracking(self):
		"""Once Bosta has the delivery, the receiver and address here must keep matching it, so
		they change only through Bosta Edit (which sets ``flags.bosta_system_write``), for
		every user including System Manager."""
		before = self.get_doc_before_save()
		if self.flags.bosta_system_write or not before or not before.tracking_number:
			return
		for fieldname in DELIVERY_DETAIL_FIELDS:
			if cstr(self.get(fieldname) or "") != cstr(before.get(fieldname) or ""):
				frappe.throw(
					_(
						"{0} cannot be edited once Bosta has the delivery; use Edit Bosta Shipment on the Sales Order."
					).format(_(self.meta.get_label(fieldname)))
				)

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
