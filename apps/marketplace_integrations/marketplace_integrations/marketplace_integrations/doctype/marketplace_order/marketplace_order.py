# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr


def compute_order_identity_key(marketplace_account, external_order_id):
	"""Return the collision-safe unique key for one marketplace order."""
	marketplace_account = cstr(marketplace_account).strip()
	external_order_id = cstr(external_order_id).strip()
	if not marketplace_account or not external_order_id:
		frappe.throw(
			_("Marketplace Account and External Order ID are required to compute the order identity key.")
		)
	return hashlib.sha256(f"{marketplace_account}\x00{external_order_id}".encode()).hexdigest()


class MarketplaceOrder(Document):
	def validate(self):
		candidate_key = compute_order_identity_key(self.marketplace_account, self.external_order_id)
		duplicate = frappe.db.exists(
			"Marketplace Order",
			{"order_identity_key": candidate_key, "name": ["!=", self.name or ""]},
		)
		if duplicate:
			frappe.throw(
				_(
					"A Marketplace Order for Marketplace Account {0} and External Order ID {1} already exists ({2})."
				).format(
					cstr(self.marketplace_account).strip(), cstr(self.external_order_id).strip(), duplicate
				)
			)

	def before_save(self):
		self.external_order_id = cstr(self.external_order_id).strip()
		self.order_identity_key = compute_order_identity_key(self.marketplace_account, self.external_order_id)
		if not self.flags.skip_sync_status_update:
			from marketplace_integrations.services.sales_order_sync import set_sync_status_from_current_snapshot

			set_sync_status_from_current_snapshot(self)
