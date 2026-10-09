# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Backfill #177 reconciliation baselines without guessing historical intent."""

import frappe

from marketplace_integrations.services.sales_order_reconciliation import (
	SalesOrderLinesMismatch,
	snapshot_with_sales_order_item_names,
)
from marketplace_integrations.services.sales_order_sync import (
	SyncBlocker,
	_refund_ids,
	build_sales_order_snapshot,
	snapshot_hash,
)

NO_BASELINE_MESSAGE = "No reconciled snapshot baseline is available for this Sales Order."


def execute():
	if not frappe.db.table_exists("Marketplace Order") or not frappe.db.has_column(
		"Marketplace Order", "last_synced_snapshot"
	):
		return
	backfill_orders(_candidate_orders())


def _candidate_orders(names=None):
	filters = {"erpnext_sales_order": ["is", "set"]}
	if names:
		filters["name"] = ["in", names]
	return frappe.get_all(
		"Marketplace Order",
		filters=filters,
		fields=[
			"name",
			"erpnext_sales_order",
			"last_synced_hash",
			"last_synced_snapshot",
			"erpnext_sync_status",
		],
	)


def backfill_orders(orders):
	for order in orders:
		if order.last_synced_snapshot:
			continue
		try:
			matched = _backfill(order)
		except SyncBlocker:
			# Expected for e.g. a Shopify order cancelled since it was synced: the
			# baseline cannot be rebuilt, which is not an error worth logging.
			matched = False
		except Exception:
			matched = False
			if _is_relabellable(order):
				_mark_no_baseline(order.name)
			frappe.log_error(
				title="Backfill last synced snapshot",
				message=frappe.get_traceback(),
				reference_doctype="Marketplace Order",
				reference_name=order.name,
			)
			continue
		if not matched and _is_relabellable(order):
			_mark_no_baseline(order.name)


def _is_relabellable(order):
	"""Only a submitted Sales Order whose sync did not fail uses the baseline status."""
	if order.erpnext_sync_status == "Sync Error":
		return False
	return frappe.db.get_value("Sales Order", order.erpnext_sales_order, "docstatus") == 1


def _backfill(order):
	"""Persist the baseline when the rebuilt hash matches; return whether it did.

	A matching hash means the rebuilt snapshot (whose quantities already net out
	refunds) equals what was synced, so the order's current refund ids were
	already reflected and are stored as the baseline's ``refund_ids``.  A hash
	mismatch stores nothing (conservative).  Sales Order rows that cannot be
	paired with the snapshot lines also yield no baseline.
	"""
	marketplace_order = frappe.get_doc("Marketplace Order", order.name)
	sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
	snapshot = build_sales_order_snapshot(marketplace_order)
	if not order.last_synced_hash or snapshot_hash(snapshot) != order.last_synced_hash:
		return False
	try:
		baseline = snapshot_with_sales_order_item_names(
			snapshot, sales_order, refund_ids=_refund_ids(marketplace_order)
		)
	except SalesOrderLinesMismatch:
		return False
	frappe.db.set_value(
		"Marketplace Order",
		order.name,
		"last_synced_snapshot",
		frappe.as_json(baseline),
		update_modified=False,
	)
	return True


def _mark_no_baseline(order_name):
	frappe.db.set_value(
		"Marketplace Order",
		order_name,
		{"erpnext_sync_status": "Modified After Sync", "last_sync_error": NO_BASELINE_MESSAGE},
		update_modified=False,
	)
