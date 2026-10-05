# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Asynchronous Talabat order webhook processing."""

import frappe
from frappe.utils import cstr

from marketplace_integrations.integrations.talabat.order_mapper import normalize_order
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.services.sales_order_sync_debounce import bump_sync_debounce


def process_order_event(marketplace_account, payload):
	"""Normalize and idempotently persist one authenticated order event."""
	# The public webhook request is intentionally handled as Guest. Its
	# signature has already been validated before this job is enqueued, so the
	# trusted integration worker must not inherit Guest's document permissions.
	frappe.set_user("Administrator")
	savepoint = "talabat_order_event_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(savepoint)
	try:
		order = upsert_marketplace_order(marketplace_account, normalize_order(payload))
	except Exception as exc:
		# Persist the failure for the Needs Attention report (#169), then
		# re-raise so the RQ job still fails exactly as before (the worker
		# rolls its transaction back on failure, hence the commit). The failed
		# upsert is rolled back first so that commit cannot persist its
		# partial writes.
		_rollback_to_savepoint(savepoint)
		order_id = cstr((payload or {}).get("order_id") if isinstance(payload, dict) else "").strip()
		log_order_import_failure(marketplace_account, order_id, str(exc), "Talabat Webhook", commit=True)
		raise
	# Schedule the eventual Sales Order sync via the shared per-order debounce
	# (#132) rather than enqueueing a sync job directly - a burst of webhooks
	# for the same order only ever results in one eventual sync.
	# bump_sync_debounce() records trigger_source="Talabat Webhook" on the
	# order itself (#135); _run_debounced_sync() reads it back and threads it
	# into sync_sales_order() so the Marketplace Sync Log row this eventually
	# produces records the real origin, not "Manual".
	bump_sync_debounce(order.name, trigger_source="Talabat Webhook")
	return order


def log_order_import_failure(marketplace_account, order_id, error, trigger_source, commit=False):
	"""Write one ``Marketplace Sync Log`` row (``sync_type="Order Import"``,
	``status="Error"``) for a Talabat order that could not be imported (#169),
	shaped like the Shopify/Noon ones: ``request_data`` carries
	``{"account", "order_id"}``, which the Needs Attention report matches
	against ``Marketplace Order.external_order_id``. A failure while writing
	the log is reported to the Error Log, never raised, so it cannot mask the
	import error being recorded. ``commit`` is for callers (the webhook
	worker) whose own transaction is about to be discarded.
	"""
	savepoint = "talabat_order_import_failure_log_" + frappe.generate_hash(length=10)
	try:
		frappe.db.savepoint(savepoint)
		now = frappe.utils.now_datetime()
		frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"marketplace_account": marketplace_account,
				"status": "Error",
				"trigger_source": trigger_source,
				"attempted_at": now,
				"completed_at": now,
				"request_data": frappe.as_json({"account": marketplace_account, "order_id": order_id}),
				"traceback": error,
			}
		).insert(ignore_permissions=True)
		frappe.db.release_savepoint(savepoint)
		if commit:
			frappe.db.commit()
	except Exception:
		_rollback_to_savepoint(savepoint)
		frappe.log_error(
			title="Talabat order import: failed to write failure log", message=frappe.get_traceback()
		)


def _rollback_to_savepoint(savepoint):
	"""Roll back to ``savepoint``; if that itself fails (a deadlock or lock
	wait timeout already rolled the whole transaction back, so the savepoint
	no longer exists) fall back to a full rollback. Never raises, so it can't
	mask the exception being handled."""
	try:
		frappe.db.rollback(save_point=savepoint)
	except Exception:
		try:
			frappe.db.rollback()
		except Exception:
			frappe.log_error(title="Talabat order import: rollback failed", message=frappe.get_traceback())
