# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Operator-visible attempts to synchronize Marketplace Orders to ERPNext."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, now_datetime


class MarketplaceSyncLog(Document):
	def validate(self):
		self._validate_marketplace_order_required_for_sales_order_sync()

	def _validate_marketplace_order_required_for_sales_order_sync(self):
		"""#139's schema-conflict resolution relaxed ``marketplace_order`` from
		a static ``reqd=1`` to ``mandatory_depends_on: eval:doc.sync_type==
		"Sales Order Sync"`` (an Order Import failure has no Marketplace Order
		to link yet). ``mandatory_depends_on`` only drives the Desk form's
		mandatory-field indicator, though - Frappe's own server-side check
		(``Document._get_missing_mandatory_fields()``) only ever looks at the
		static ``reqd`` flag, so without this explicit check a ``Sales Order
		Sync`` row could be inserted with no ``marketplace_order`` at all,
		silently losing the guarantee ``sync_type`` was introduced to
		preserve for every existing writer (``sales_order_sync.py::
		_create_sync_log()``)."""
		if self.sync_type == "Sales Order Sync" and not self.marketplace_order:
			frappe.throw(
				_("Marketplace Order is mandatory when Sync Type is Sales Order Sync"),
				frappe.MandatoryError,
			)

	@frappe.whitelist()
	def resync(self):
		"""Retry this log's Marketplace Order.

		``sync_sales_order()`` (see ``services/sales_order_sync.py``) already
		writes its own single ``Marketplace Sync Log`` row for every attempt -
		success, expected blocker, or unexpected exception - via its internal
		``_create_sync_log()``. This method used to ALSO insert/update its own
		row around that call, so every resync produced two log rows for one
		attempt. It now does nothing but delegate: ``sync_sales_order()``'s own
		logging, tagged "Manual Resync" so it's distinguishable in the log from
		an original "Manual" sync, is the single source of truth for what
		happened.
		"""
		frappe.only_for("System Manager")
		self.check_permission("read")

		from marketplace_integrations.services.sales_order_sync import sync_sales_order

		return sync_sales_order(self.marketplace_order, trigger_source="Manual Resync")


def clear_old_logs(days=90):
	"""Delete Marketplace Sync Log rows whose creation is older than ``days``."""
	frappe.db.delete(
		"Marketplace Sync Log",
		{"creation": ["<", add_days(now_datetime(), -int(days))]},
	)
