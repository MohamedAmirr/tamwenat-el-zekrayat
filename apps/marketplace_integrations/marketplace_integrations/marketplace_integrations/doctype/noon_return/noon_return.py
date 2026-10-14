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
from frappe.utils import cstr, now_datetime


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
