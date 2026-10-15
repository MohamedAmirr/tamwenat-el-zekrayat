# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon FBPI return reference evidence (#153).

Standalone doctype, deliberately NOT a Marketplace Order child table - a
return with no exact Order Nr match must still persist as a real,
queryable record (an unlinked return), not be dropped for lack of a parent.
"""

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, flt, now_datetime

from marketplace_integrations.services.mappings import resolve_mapping_and_item

INSPECTION_OUTCOMES = ("Sellable", "Damaged")


def compute_return_identity_key(marketplace_account, purchase_item_nr):
	"""Return the collision-safe unique key for one Noon return item,
	mirroring ``Marketplace Order``'s ``compute_order_identity_key()`` -
	same normalize-inside-the-key-function rationale (docs/frappe-
	conventions.md): both inputs are stripped here, not left to field-level
	normalization that runs later than ``before_save()``."""
	marketplace_account = cstr(marketplace_account).strip()
	purchase_item_nr = cstr(purchase_item_nr).strip()
	if not marketplace_account or not purchase_item_nr:
		frappe.throw(
			_("Marketplace Account and Purchase Item Nr are required to compute the return identity key.")
		)
	return hashlib.sha256(f"{marketplace_account}\x00{purchase_item_nr}".encode()).hexdigest()


class NoonReturn(Document):
	@frappe.whitelist()
	def acknowledge_receipt(self, receipt_note=None):
		"""Record one internal receipt acknowledgement without calling Noon."""
		self.check_permission("write")
		if self.is_new():
			frappe.throw(_("Save the Noon Return before acknowledging its receipt."))

		locked = frappe.get_doc("Noon Return", self.name, for_update=True)
		if locked.receipt_acknowledged_on:
			return {
				"acknowledged": True,
				"already_acknowledged": True,
				"acknowledged_by": locked.receipt_acknowledged_by,
				"acknowledged_on": locked.receipt_acknowledged_on,
			}

		locked.receipt_acknowledged_on = now_datetime()
		locked.receipt_acknowledged_by = frappe.session.user
		locked.receipt_acknowledgement_note = cstr(receipt_note).strip()
		locked.save(ignore_permissions=True)
		return {
			"acknowledged": True,
			"already_acknowledged": False,
			"acknowledged_by": locked.receipt_acknowledged_by,
			"acknowledged_on": locked.receipt_acknowledged_on,
		}

	@frappe.whitelist()
	def record_inspection(self, outcome):
		"""Record the merchant's inspection decision; Sellable drafts a Sales Return.

		No Noon call is made and nothing is submitted. Damaged items are only
		recorded (Noon is responsible for them), so no stock moves.
		"""
		self.check_permission("write")
		if self.is_new():
			frappe.throw(_("Save the Noon Return before recording its inspection."))
		outcome = cstr(outcome).strip()
		if outcome not in INSPECTION_OUTCOMES:
			frappe.throw(_("Inspection outcome must be Sellable or Damaged."))

		locked = frappe.get_doc("Noon Return", self.name, for_update=True)
		if not locked.receipt_acknowledged_on:
			frappe.throw(_("Acknowledge the return as received before recording its inspection."))
		if locked.inspection_outcome and locked.inspection_outcome != outcome:
			frappe.throw(
				_("The inspection outcome was already recorded as {0} and cannot be changed.").format(
					locked.inspection_outcome
				)
			)

		if outcome == "Damaged":
			locked._require_submitted_sales_order()
			locked.inspection_outcome = outcome
			locked.save(ignore_permissions=True)
			return {"outcome": outcome, "sales_return": None}

		existing = locked._existing_sales_return()
		if existing:
			return {"outcome": outcome, "sales_return": existing, "already_created": True}

		sales_return = locked._make_draft_sales_return()
		locked.inspection_outcome = outcome
		locked.sales_return = sales_return
		locked.save(ignore_permissions=True)
		return {"outcome": outcome, "sales_return": sales_return, "already_created": False}

	@frappe.whitelist()
	def create_credit_note(self):
		"""Draft a return Sales Invoice (Credit Note) for one unit; never submitted, no Noon call."""
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

		self.check_permission("write")
		if self.is_new():
			frappe.throw(_("Save the Noon Return before creating its Credit Note."))
		frappe.has_permission("Sales Invoice", "create", throw=True)

		locked = frappe.get_doc("Noon Return", self.name, for_update=True)
		if not locked.inspection_outcome:
			frappe.throw(_("Record the inspection outcome before creating the Credit Note."))
		existing = locked._existing_linked("credit_note")
		if existing:
			return {"credit_note": existing, "already_created": True}

		sales_order = locked._require_submitted_sales_order()
		candidate = locked._find_source_row("Sales Invoice", "Sales Invoice Item", "sales_order", sales_order)
		if frappe.db.get_value("Sales Invoice", candidate.parent, "update_stock"):
			frappe.throw(
				_(
					"Sales Invoice {0} updates stock, so a Credit Note from it would return the stock a second time."
				).format(candidate.parent)
			)

		doc = make_sales_return(candidate.parent)
		kept = [row for row in doc.items if row.sales_invoice_item == candidate.name]
		if len(kept) != 1:
			frappe.throw(_("Could not match the invoice row on {0}.").format(candidate.parent))
		row = kept[0]
		row.qty = -1
		row.stock_qty = flt(-1 * flt(row.conversion_factor or 1))
		doc.set("items", [row])
		doc.run_method("calculate_taxes_and_totals")
		doc.insert()

		locked.credit_note = doc.name
		locked.save(ignore_permissions=True)
		return {"credit_note": doc.name, "already_created": False}

	def _existing_linked(self, fieldname):
		"""A linked return document while it is draft or submitted; a cancelled or deleted one is replaced."""
		doctype = {"sales_return": "Delivery Note", "credit_note": "Sales Invoice"}[fieldname]
		name = self.get(fieldname)
		if not name:
			return None
		return name if frappe.db.get_value(doctype, name, "docstatus") in (0, 1) else None

	def _existing_sales_return(self):
		return self._existing_linked("sales_return")

	def _require_submitted_sales_order(self):
		if not self.marketplace_order:
			frappe.throw(_("This return is not linked to a Marketplace Order."))
		sales_order = frappe.db.get_value("Marketplace Order", self.marketplace_order, "erpnext_sales_order")
		if not sales_order or frappe.db.get_value("Sales Order", sales_order, "docstatus") != 1:
			frappe.throw(_("The linked Marketplace Order has no submitted Sales Order."))
		return sales_order

	def _find_source_row(self, parent_doctype, child_doctype, sales_order_field, sales_order):
		"""The one submitted, non-return source row for this return's mapped Item on the Sales Order."""
		if not cstr(self.partner_sku).strip():
			frappe.throw(_("This return has no Partner SKU, so its Item cannot be resolved."))
		mapping = resolve_mapping_and_item(self.marketplace_account, cstr(self.partner_sku).strip())
		if not mapping:
			frappe.throw(_("No enabled mapping exists for Partner SKU {0}.").format(self.partner_sku))

		rows = frappe.get_all(
			child_doctype,
			filters={sales_order_field: sales_order, "item_code": mapping["item"], "docstatus": 1},
			fields=["name", "parent"],
		)
		parents = set(
			frappe.get_all(
				parent_doctype,
				filters={
					"name": ["in", [row.parent for row in rows] or [""]],
					"is_return": 0,
					"docstatus": 1,
				},
				pluck="name",
			)
		)
		candidates = [row for row in rows if row.parent in parents]
		if len(candidates) != 1:
			names = ", ".join(sorted({row.parent for row in candidates})) or _("none")
			frappe.throw(
				_(
					"Expected exactly one submitted {0} row for Item {1} on Sales Order {2}; found: {3}."
				).format(parent_doctype, mapping["item"], sales_order, names)
			)
		return candidates[0]

	def _make_draft_sales_return(self):
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

		frappe.has_permission("Delivery Note", "create", throw=True)
		sales_order = self._require_submitted_sales_order()
		warehouse = frappe.db.get_value("Marketplace Account", self.marketplace_account, "warehouse")
		if not warehouse:
			frappe.throw(_("The Marketplace Account has no warehouse; returned stock needs one."))
		candidate = self._find_source_row(
			"Delivery Note", "Delivery Note Item", "against_sales_order", sales_order
		)

		doc = make_sales_return(candidate.parent)
		kept = [row for row in doc.items if row.dn_detail == candidate.name]
		if len(kept) != 1:
			frappe.throw(_("Could not match the delivery row on {0}.").format(candidate.parent))
		row = kept[0]
		row.qty = -1
		row.stock_qty = flt(-1 * flt(row.conversion_factor or 1))
		row.warehouse = warehouse
		doc.set("items", [row])
		doc.set_warehouse = warehouse
		doc.run_method("calculate_taxes_and_totals")
		doc.insert()
		return doc.name

	def validate(self):
		# Guard against missing required fields - let Frappe's mandatory field
		# validation throw the appropriate MandatoryError after this method completes.
		if not self.marketplace_account or not self.purchase_item_nr:
			return

		candidate_key = compute_return_identity_key(self.marketplace_account, self.purchase_item_nr)
		duplicate = frappe.db.exists(
			"Noon Return",
			{"return_identity_key": candidate_key, "name": ["!=", self.name or ""]},
		)
		if duplicate:
			frappe.throw(
				_(
					"A Noon Return for Marketplace Account {0} and Purchase Item Nr {1} already exists ({2})."
				).format(
					cstr(self.marketplace_account).strip(), cstr(self.purchase_item_nr).strip(), duplicate
				)
			)

	def before_save(self):
		self.purchase_item_nr = cstr(self.purchase_item_nr).strip()
		# Guard against missing required fields - let Frappe's mandatory field
		# validation throw the appropriate MandatoryError. Only compute the identity
		# key if both required fields are present.
		if self.marketplace_account and self.purchase_item_nr:
			self.return_identity_key = compute_return_identity_key(
				self.marketplace_account, self.purchase_item_nr
			)
