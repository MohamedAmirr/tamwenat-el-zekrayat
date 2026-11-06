# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Nightly order catch-up enqueue coordinator (#354)."""

import frappe

TRIGGER_SOURCE = "Scheduled Reconciliation"

# Shopify financial statuses of a fully cancelled order with no Sales Order (#353).
CANCELLED_UNPAID_STATUSES = frozenset({"PENDING", "VOIDED", "EXPIRED", "AUTHORIZED", "REFUNDED"})
CANCELLED_PAID_STATUSES = frozenset({"PAID", "PARTIALLY_PAID", "PARTIALLY_REFUNDED"})
# Source string: translate with ``_()`` where it is shown; stored untranslated by the patch.
CANCELLED_PAID_ERROR = "Cancelled in Shopify but the payment was not fully refunded; refund it in Shopify"


def schedule_first_syncs(filters, exclude_names=None):
	"""Schedule the first sync (through the debounce) for every order matching ``filters``.
	Never creates a Sales Order inline. Returns the number of orders that now have a sync
	pending, including ones a recent run already scheduled.  ``exclude_names`` leaves out
	orders a caller already scheduled and counted, so no order is counted twice."""
	from marketplace_integrations.services.sales_order_sync_debounce import bump_sync_debounce_unless_pending

	if exclude_names:
		filters = {**filters, "name": ["not in", list(exclude_names)]}
	rows = frappe.get_all(
		"Marketplace Order",
		filters=filters,
		fields=["name", "sync_debounce_until", "pending_sync_trigger_source"],
	)
	for row in rows:
		bump_sync_debounce_unless_pending(row, TRIGGER_SOURCE)
	return len(rows)


def write_catch_up_summary(account, imported=0, scheduled=0, failures=0, error=None, skipped=0):
	"""Record one account's nightly catch-up run in ``Marketplace Sync Log`` (#354):
	orders imported, first syncs scheduled, failures, and (Shopify) orders skipped because they
	were created before the account's "Sync Orders From" cutoff. Committed on its own."""
	now = frappe.utils.now_datetime()
	try:
		frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"marketplace_account": account,
				"status": "Error" if error or failures else "Success",
				"trigger_source": TRIGGER_SOURCE,
				"attempted_at": now,
				"completed_at": now,
				"response_data": frappe.as_json(
					{
						"imported": imported,
						"first_syncs_scheduled": scheduled,
						"failures": failures,
						**({"skipped_before_sync_start": skipped} if skipped else {}),
					}
				),
				"traceback": error,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="Nightly order catch-up: failed to write summary log")


def enqueue_nightly_order_reconciliation():
	"""Enqueue one deduplicated long job per enabled Shopify/Noon account."""
	frappe.set_user("Administrator")
	queued = 0
	errors = []
	for account in frappe.get_all(
		"Marketplace Account",
		filters={"platform": ["in", ["Shopify", "Noon"]], "enabled": 1},
		fields=["name", "platform"],
	):
		try:
			if account.platform == "Shopify":
				job = frappe.enqueue(
					"marketplace_integrations.integrations.shopify.orders.reconcile_all_accounts_orders",
					queue="long",
					timeout=3600,
					job_id=f"marketplace-shopify-order-reconciliation-{account.name}",
					deduplicate=True,
					account_name=account.name,
					catch_up_unlinked=True,
				)
			else:
				from marketplace_integrations.integrations.noon.orders import enqueue_order_import

				job = enqueue_order_import(account.name, trigger_source=TRIGGER_SOURCE, catch_up_unlinked=True)
			queued += bool(job)
		except Exception as exc:
			errors.append({"account": account.name, "error": str(exc)[:300]})
	if errors:
		frappe.log_error(
			title="Nightly marketplace order reconciliation enqueue failures",
			message=frappe.as_json({"queued": queued, "errors": errors}),
		)
	return {"queued": queued, "errors": len(errors)}
