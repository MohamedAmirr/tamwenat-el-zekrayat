# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#364: retire the "Modified After Sync" and "Not Synced" Marketplace Order statuses.

- Every "Modified After Sync" row is re-planned WITHOUT executing anything
  (``set_sync_status_from_current_snapshot`` only sets values in memory) and given the
  status its real condition calls for: Needs Action or Sync Error with its reason code, or
  Syncing when the planner says the sync can apply the change itself (or ERPNext already
  matches, which the next sync confirms as Synced).  A row that cannot be classified is
  conservatively Needs Action for review.
- A "Not Synced" row that is recent (created within ``LEGACY_NOT_SYNCED_RECENT_DAYS`` days, by
  ``creation``) AND eligible for an automatic sync becomes Syncing and is bumped through the
  debounce, so the minute sweep syncs it.  Eligible means: Talabat always; Noon when the order
  has at least one eligible unit (``noon.orders.has_eligible_units``); Shopify when it is not
  cancelled and its financial status is PAID or PARTIALLY_PAID, or a manual payment is pending
  (the rule ``sales_order_sync`` applies).  Every other "Not Synced" row becomes Needs Action
  with ``LEGACY_NOT_SYNCED_REVIEW`` instead of mass-syncing months-old orders.
- "Cancelled" rows are not touched.

The status and reason fields are written with ``frappe.db.set_value`` (and the debounce
bump): a bulk status rewrite that must not run the ``before_save`` status recompute, and
none of these writes touches stock or accounting.

The new status values come from ``services/sync_status.py`` (this patch is new and imports the
module); only the two retired statuses, which no longer exist there, are literals.  Idempotent: once a row has left the two old
statuses nothing selects it again.
"""

import frappe
from frappe.utils import add_days, get_datetime, now_datetime

from marketplace_integrations.integrations.noon.orders import has_eligible_units
from marketplace_integrations.services import sync_reason_codes as reasons
from marketplace_integrations.services import sync_status
from marketplace_integrations.services.sales_order_sync import (
	is_manual_payment_pending,
	set_sync_status_from_current_snapshot,
)
from marketplace_integrations.services.sales_order_sync_debounce import bump_sync_debounce

OLD_NEEDS_REVIEW = "Modified After Sync"
OLD_NEW = "Not Synced"
# A legacy Not Synced row older than this (days since ``creation``) is not auto-synced.
LEGACY_NOT_SYNCED_RECENT_DAYS = 30
TRIGGER_SOURCE = "Scheduled Sweep"

_STATE_FIELDS = (
	"erpnext_sync_status",
	"last_sync_error",
	"sync_reason_code",
	"sync_action_type",
	"sync_remediation_mode",
	"sync_action_state",
	"sync_blocking_doctype",
	"sync_blocking_document",
)


def execute():
	if not frappe.db.table_exists("Marketplace Order"):
		return
	migrate_modified_after_sync_rows()
	migrate_not_synced_rows()


def migrate_modified_after_sync_rows():
	for name in frappe.get_all(
		"Marketplace Order", filters={"erpnext_sync_status": OLD_NEEDS_REVIEW}, pluck="name"
	):
		try:
			classify_row(name)
		except Exception:
			frappe.log_error(
				title="Migrate Modified After Sync status",
				message=frappe.get_traceback(),
				reference_doctype="Marketplace Order",
				reference_name=name,
			)
			_write_state(
				name,
				{
					"erpnext_sync_status": sync_status.NEEDS_ACTION,
					"last_sync_error": reasons.instruction(reasons.SALES_ORDER_CHANGED_IN_ERP),
					"sync_reason_code": reasons.SALES_ORDER_CHANGED_IN_ERP,
					"sync_action_type": reasons.REASONS[reasons.SALES_ORDER_CHANGED_IN_ERP]["action_type"],
					"sync_remediation_mode": reasons.MANUAL,
					"sync_action_state": sync_status.WAITING_FOR_USER,
				},
			)


def classify_row(name):
	"""Give one "Modified After Sync" row the status its real condition calls for."""
	order = frappe.get_doc("Marketplace Order", name)
	# In memory only: nothing below saves ``order``.
	order.erpnext_sync_status = sync_status.SYNCED
	set_sync_status_from_current_snapshot(order)
	if order.erpnext_sync_status in (sync_status.SYNCED, sync_status.SYNCING):
		# Executable, already matching, or no Sales Order/baseline to judge: the debounced
		# sync decides; nothing is executed here.
		bump_sync_debounce(name, TRIGGER_SOURCE)
		return
	_write_state(name, {field: order.get(field) for field in _STATE_FIELDS})


def migrate_not_synced_rows():
	cutoff = add_days(now_datetime(), -LEGACY_NOT_SYNCED_RECENT_DAYS)
	for row in frappe.get_all(
		"Marketplace Order", filters={"erpnext_sync_status": OLD_NEW}, fields=["name", "creation"]
	):
		if get_datetime(row.creation) >= cutoff and is_eligible_for_sync(row.name):
			bump_sync_debounce(row.name, TRIGGER_SOURCE)
		else:
			_write_state(
				row.name,
				{
					"erpnext_sync_status": sync_status.NEEDS_ACTION,
					"last_sync_error": reasons.instruction(reasons.LEGACY_NOT_SYNCED_REVIEW),
					"sync_reason_code": reasons.LEGACY_NOT_SYNCED_REVIEW,
					"sync_action_type": reasons.REASONS[reasons.LEGACY_NOT_SYNCED_REVIEW]["action_type"],
					"sync_remediation_mode": reasons.MANUAL,
					"sync_action_state": sync_status.WAITING_FOR_USER,
				},
			)


def is_eligible_for_sync(name):
	"""Whether a never-synced order can be synced automatically (see the module docstring)."""
	order = frappe.get_doc("Marketplace Order", name)
	if order.platform == "Noon":
		return has_eligible_units(name)
	if order.platform == "Shopify":
		return not order.cancelled_at and (
			order.external_financial_status in ("PAID", "PARTIALLY_PAID") or is_manual_payment_pending(order)
		)
	return True


def _write_state(name, values):
	frappe.db.set_value("Marketplace Order", name, values, update_modified=False)
