# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Operator-visible attempts to synchronize Marketplace Orders to ERPNext."""

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, now_datetime


class MarketplaceSyncLog(Document):
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
