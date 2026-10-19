# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Pre-generated Noon Logistics AWB pool (see
``integrations/noon/awb_pool.py`` and docs/noon-integration/fulfillment-release.md).

One row per AWB number Noon returned from ``GetNoonLogisticsAWBs``. ``Available``
rows are claimed by shipment creation and flip to ``Used`` for good - a claimed
AWB never returns to the pool.
"""

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr


def compute_awb_identity_key(marketplace_account, awb_nr):
	"""Collision-safe unique key for (account, AWB Nr), same shape and
	normalize-inside-the-key-function rationale as ``Noon Return``'s
	``compute_return_identity_key()`` (docs/frappe-conventions.md)."""
	marketplace_account = cstr(marketplace_account).strip()
	awb_nr = cstr(awb_nr).strip()
	if not marketplace_account or not awb_nr:
		frappe.throw(_("Marketplace Account and AWB Nr are required to compute the AWB identity key."))
	return hashlib.sha256(f"{marketplace_account}\x00{awb_nr}".encode()).hexdigest()


class NoonAWB(Document):
	def before_save(self):
		self.awb_nr = cstr(self.awb_nr).strip()
		self.awb_identity_key = compute_awb_identity_key(self.marketplace_account, self.awb_nr)
