# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Operator-visible attempts to synchronize Marketplace Orders to ERPNext."""

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, now_datetime

UNEXPECTED_SYNC_ERROR = _(
	"ERPNext Sales Order synchronization failed. Please review the ERPNext configuration and try again."
)


class MarketplaceSyncLog(Document):
	@frappe.whitelist()
	def resync(self):
		"""Retry this log's Marketplace Order and append a separate attempt row."""
		frappe.only_for("System Manager")
		self.check_permission("read")

		attempt = frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"marketplace_order": self.marketplace_order,
				"status": "Queued",
				"trigger_source": "Manual",
				"attempted_at": now_datetime(),
				"request_data": json.dumps(
					{"marketplace_order": self.marketplace_order, "retry_of": self.name},
					ensure_ascii=False,
				),
			}
		)
		attempt.insert()

		try:
			from marketplace_integrations.services.sales_order_sync import sync_sales_order

			result = sync_sales_order(self.marketplace_order)
			attempt.status = _status_for_result(result)
			attempt.response_data = json.dumps(result, ensure_ascii=False, default=str)
		except Exception as exc:
			frappe.log_error(title=_("Marketplace Order resynchronization"))
			attempt.status = "Error"
			attempt.response_data = json.dumps(
				{"ok": False, "error": str(exc)}, ensure_ascii=False
			)
			attempt.traceback = frappe.get_traceback()
		finally:
			attempt.completed_at = now_datetime()
			attempt.save()

		return {"name": attempt.name, "status": attempt.status}


def _status_for_result(result):
	if result.get("ok"):
		return "Success"
	if result.get("error") == UNEXPECTED_SYNC_ERROR:
		return "Error"
	return "Invalid"


def clear_old_logs(days=90):
	"""Delete Marketplace Sync Log rows whose creation is older than ``days``."""
	frappe.db.delete(
		"Marketplace Sync Log",
		{"creation": ["<", add_days(now_datetime(), -int(days))]},
	)
