# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Small shared selection and persistence helpers for Noon price and stock."""

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from marketplace_integrations.services.mappings import resolve_mapping_and_item

MAPPING_DOCTYPE = "Marketplace Product Mapping"

# Per-account push switches on Marketplace Account, keyed by enqueue "kind".
PUSH_SWITCH_FIELDS = {"stock": "noon_inventory_push_enabled", "pricing": "noon_price_push_enabled"}


class PushPaused(frappe.ValidationError):
	"""The account's push switch is off. A ValidationError so manual callers
	still see the message; scheduled callers catch it as a quiet skip."""


def push_paused_message(kind):
	if kind == "pricing":
		return _("Price push is paused for this account.")
	return _("Inventory push is paused for this account.")


def is_push_paused(account_name, kind):
	"""True when the account's "Push Inventory/Prices to Noon" switch is off.
	A single fresh DB read, safe to call from the worker before anything else."""
	return not frappe.db.get_value("Marketplace Account", account_name, PUSH_SWITCH_FIELDS[kind])


def selected_mappings(account_name, mapping_names=None, stock_only=False):
	"""Yield only exact, enabled account mappings to active ERPNext Items."""
	filters = {"marketplace_account": account_name}
	if mapping_names is not None:
		if not mapping_names:
			return []
		filters["name"] = ["in", list(mapping_names)]
	# Trusted read: authorize_selection() authorized the account and every named mapping before this runs.
	rows = frappe.get_all(
		MAPPING_DOCTYPE,
		filters=filters,
		fields=["name", "external_reference", "enabled"],
		order_by="name asc",
	)
	resolved_rows = []
	for row in rows:
		if not row.enabled:
			continue
		resolved = resolve_mapping_and_item(account_name, row.external_reference)
		if not resolved or resolved["mapping_name"] != row.name:
			continue
		resolved_rows.append((row, resolved))
	if not resolved_rows:
		return []
	# Trusted read: authorize_selection() (or the worker's own context) authorized this account and these mappings.
	items = frappe.get_all(
		"Item",
		filters={"name": ["in", list({resolved["item"] for _, resolved in resolved_rows})]},
		fields=["name", "stock_uom", "disabled", "is_stock_item", "has_variants"],
	)
	item_by_name = {item.name: item for item in items}
	selected = []
	for row, resolved in resolved_rows:
		item = item_by_name.get(resolved["item"])
		if not item:
			continue
		if item.disabled or (stock_only and (not item.is_stock_item or item.has_variants)):
			continue
		selected.append((row, resolved, item))
	return selected


def authorize_selection(account_name, mapping_names=None):
	"""Check Desk write access without allowing another account's mapping."""
	account = frappe.get_doc("Marketplace Account", account_name)
	account.check_permission("write")
	if not frappe.has_permission(MAPPING_DOCTYPE, "write"):
		raise frappe.PermissionError
	if mapping_names is None:
		return None
	from marketplace_integrations.services.mapping_push import parse_mapping_names

	names = parse_mapping_names(mapping_names)
	for name in names:
		mapping = frappe.get_doc(MAPPING_DOCTYPE, name)
		mapping.check_permission("write")
		if mapping.marketplace_account != account_name:
			frappe.throw(
				_("Mapping {0} does not belong to Marketplace Account {1}.").format(name, account_name)
			)
	return names


def enqueue_push(worker, kind, account_name, mapping_names=None):
	if is_push_paused(account_name, kind):
		# The single enqueue choke point: nothing can queue a push for a paused account.
		raise PushPaused(push_paused_message(kind))
	job = frappe.enqueue(
		worker,
		queue="long",
		timeout=3600,
		job_id=f"marketplace-noon-{kind}-{account_name}",
		deduplicate=True,
		account_name=account_name,
		mapping_names=mapping_names,
	)
	return {"queued": job is not None}


def chunks(rows, size):
	for start in range(0, len(rows), size):
		yield rows[start : start + size]


def persist_outcomes(outcomes, *, inventory=False):
	"""Commit one batch's individual mapping outcomes under a savepoint."""
	if not outcomes:
		return
	status_field = "inventory_sync_status" if inventory else "sync_status"
	error_field = "inventory_last_error" if inventory else "last_error"
	time_field = "inventory_last_synced_on" if inventory else "last_synced_on"
	savepoint = "noon_sync_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(savepoint)
	try:
		for mapping_name, error in outcomes.items():
			values = (
				{status_field: "Error", error_field: cstr(error)[:500]}
				if error
				else {
					status_field: "Synced",
					error_field: None,
					time_field: now_datetime(),
				}
			)
			frappe.db.set_value(MAPPING_DOCTYPE, mapping_name, values, update_modified=False)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	else:
		frappe.db.release_savepoint(savepoint)
		frappe.db.commit()


def require_items_response(response, client):
	if response.status_code != 200:
		frappe.throw(client.error_message(response))
	try:
		body = response.json()
	except ValueError:
		frappe.throw(_("Noon returned an invalid sync response."))
	if not isinstance(body, dict) or not isinstance(body.get("items"), list):
		frappe.throw(_("Noon returned an invalid sync response."))
	return body["items"]


def response_by_key(items, key_fields):
	"""Match results by documented identity fields, never array position."""
	result = {}
	for item in items:
		if not isinstance(item, dict):
			continue
		key = tuple(item.get(field) for field in key_fields)
		if all(key):
			# An ambiguous duplicate response must never be treated as success.
			result[key] = item if key not in result else None
	return result


def item_status_error(item):
	"""Return None only for Noon's documented per-item success status.

	The checked-in endpoint snapshot lists the status fields but omits their
	values. Noon's official pricing and stock guides define success as
	status_id=0 with status_code=OK (approved for #144 on 2026-09-29).
	"""
	if not isinstance(item, dict) or not isinstance(item.get("status"), dict):
		return _("Noon did not return a status for this item.")
	status = item["status"]
	if status.get("status_id") == 0 and status.get("status_code") == "OK":
		return None
	code = status.get("status_code")
	message = status.get("message")
	parts = [value for value in (code, message) if isinstance(value, str) and value]
	return cstr(": ".join(parts) or _("Noon rejected this item."))[:500]
