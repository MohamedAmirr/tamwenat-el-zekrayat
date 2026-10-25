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
		# #324: Links and the list show platform_order_reference (title_field); Frappe's
		# own fallback for an empty title is the docname, so keep it filled. Mappers that
		# have no merchant-facing number leave it empty - fall back to external_order_id.
		if not cstr(self.platform_order_reference).strip():
			self.platform_order_reference = self.external_order_id
		self.order_identity_key = compute_order_identity_key(self.marketplace_account, self.external_order_id)
		# The echo-guard marker is one-way (#181): it is written with update_modified=False,
		# so a copy loaded before it was set can still be saved and would write NULL back,
		# making the planner reopen and cancel the Closed Sales Order.  Clearing it goes
		# through services.marketplace_outbound.clear_outbound_cancelled_marker().
		before = self.get_doc_before_save()
		if before and before.get("outbound_cancelled_on") and not self.outbound_cancelled_on:
			self.outbound_cancelled_on = before.outbound_cancelled_on
		# Same one-way rule for the Shopify item-edit marker (#356): a stale copy must not
		# blank it.  Only the re-baseline (sales_order_sync.release_outbound_edit_marker) clears
		# it on a document, by setting flags.clear_outbound_edit_marker.
		if (
			before
			and before.get("outbound_edit_fingerprint")
			and not self.outbound_edit_fingerprint
			and not self.flags.clear_outbound_edit_marker
		):
			self.outbound_edit_fingerprint = before.outbound_edit_fingerprint
			self.outbound_edit_state = before.outbound_edit_state
		if not self.flags.skip_sync_status_update:
			from marketplace_integrations.services.sales_order_sync import (
				set_sync_status_from_current_snapshot,
			)

			set_sync_status_from_current_snapshot(self)
