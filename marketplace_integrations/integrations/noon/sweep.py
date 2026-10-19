# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Scheduled Noon pushes: the hourly full re-push and the 2-minute changed-stock sweep.

Neither entry point makes a Noon HTTP call; they only read accounts / Bins /
mappings / the per-account watermark and enqueue deduplicated jobs through
``sync.enqueue_push()``. An account whose push switch is off is skipped quietly.
"""

import frappe
from frappe.utils import add_to_date, now_datetime

from marketplace_integrations.integrations.noon.constants import NOON_INVENTORY_SWEEP_OVERLAP_MINUTES
from marketplace_integrations.integrations.noon.inventory import push_stock
from marketplace_integrations.integrations.noon.sync import PushPaused, enqueue_push


def sweep_all_accounts():
	from marketplace_integrations.integrations.noon.pricing import push_pricing

	accounts = frappe.get_all(
		"Marketplace Account",
		filters={"platform": "Noon", "enabled": 1},
		pluck="name",
		order_by="name asc",
	)
	queued = 0
	for account_name in accounts:
		for worker, kind in ((push_pricing, "pricing"), (push_stock, "stock")):
			try:
				queued += bool(enqueue_push(worker, kind, account_name)["queued"])
			except PushPaused:
				continue  # that push is switched off for this account: quiet skip
	return queued


# --- Changed-stock sweep ----------------------------------------------------
#
# Watermark: one frappe.cache() key per account holding the wall-clock time up
# to which the sweep has already looked for Bin changes (same mechanism as
# Shopify's sweep; no TTL). A missing watermark means "look back one overlap
# window from now"; the hourly full push is the recovery path for anything lost.


def _watermark_cache_key(account_name):
	return f"marketplace_integrations:noon:inventory_sweep_watermark:{account_name}"


def _sweep_since(account_name, tick_time):
	watermark = frappe.cache().get_value(_watermark_cache_key(account_name))
	return add_to_date(watermark or tick_time, minutes=-NOON_INVENTORY_SWEEP_OVERLAP_MINUTES)


def _advance_watermark(account_name, tick_time):
	frappe.cache().set_value(_watermark_cache_key(account_name), tick_time)


def sweep_changed_bins():
	"""For every enabled Noon account with the inventory switch on, find Bins in
	its Warehouse modified since its watermark (minus the overlap window) whose
	Item has an enabled mapping on that account, and enqueue ONE deduplicated
	stock push scoped to just those mappings.

	The watermark advances to this tick's start when nothing changed or the
	enqueue was accepted, and is held back when the enqueue was deduplicated away
	(a stock push for the account is already queued/running), so the next tick
	re-discovers the change. One account's failure is logged and isolated."""
	tick_time = now_datetime()
	accounts = frappe.get_all(
		"Marketplace Account",
		filters={"platform": "Noon", "enabled": 1, "noon_inventory_push_enabled": 1},
		fields=["name", "warehouse", "noon_warehouse_code"],
		order_by="name asc",
	)
	for account in accounts:
		if not account.warehouse or not account.noon_warehouse_code:
			continue
		try:
			_sweep_account(account, tick_time)
		except PushPaused:
			continue  # switched off between the account query and the enqueue
		except Exception:
			frappe.log_error(title="Noon inventory sweep: account failed", message=frappe.get_traceback())


def _sweep_account(account, tick_time):
	since = _sweep_since(account.name, tick_time)
	mappings = frappe.get_all(
		"Marketplace Product Mapping",
		filters={"marketplace_account": account.name, "enabled": 1},
		fields=["name", "item"],
	)
	should_advance = True
	if mappings:
		names_by_item = {}
		for mapping in mappings:
			names_by_item.setdefault(mapping.item, []).append(mapping.name)
		changed_items = frappe.get_all(
			"Bin",
			filters={
				"warehouse": account.warehouse,
				"item_code": ["in", list(names_by_item)],
				"modified": [">", since],
			},
			pluck="item_code",
		)
		affected = sorted({name for item in set(changed_items) for name in names_by_item.get(item, [])})
		if affected:
			should_advance = bool(enqueue_push(push_stock, "stock", account.name, affected)["queued"])
	if should_advance:
		_advance_watermark(account.name, tick_time)
