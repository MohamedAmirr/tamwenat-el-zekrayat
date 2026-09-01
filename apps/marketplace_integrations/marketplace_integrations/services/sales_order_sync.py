# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Explicit Marketplace Order to ERPNext Sales Order synchronization.

Marketplace Order remains Talabat's operational record.  This module creates
or updates only a Draft Sales Order after an authorized user explicitly asks
for it.  ERPNext's configured price list and pricing rules calculate the
commercial values; Talabat payment values never enter the Sales Order.
"""

import hashlib

import frappe
from frappe import _
from frappe.utils import cstr, flt, getdate, now_datetime

from marketplace_integrations.marketplace_integrations.doctype.marketplace_product_mapping.marketplace_product_mapping import (
	compute_mapping_identity_key,
)

FINAL_ITEM_STATUSES = {"IN_CART", "ADDITION"}


class SyncBlocker(frappe.ValidationError):
	"""A user-actionable prerequisite that prevents Sales Order synchronization."""


def sync_sales_order(marketplace_order):
	"""Create or update one Marketplace Order's linked Draft Sales Order.

	The Marketplace Order row is locked before it is reloaded, so concurrent
	button clicks observe a successful link rather than creating a second Sales
	Order.  Expected blockers are persisted as sanitized sync state and returned
	to the API without raising, so that status write is not rolled back.
	"""
	frappe.db.get_value("Marketplace Order", marketplace_order, "name", for_update=True)
	order = frappe.get_doc("Marketplace Order", marketplace_order)

	try:
		account = _get_account(order)
		_validate_sync_permissions(order, account)
		_validate_account_configuration(account)
		lines = _build_sales_order_lines(order, account)
		customer = _get_or_create_customer(account)
		snapshot = _make_snapshot(account, customer, lines, _delivery_date(order))
		current_hash = _snapshot_hash(snapshot)

		if order.erpnext_sales_order:
			sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
			if order.last_synced_hash == current_hash:
				return _save_success(order, current_hash, sales_order.name, updated=False)
			if sales_order.docstatus != 0:
				return _save_submitted_blocker(order)
			_apply_snapshot(sales_order, snapshot)
			sales_order.save()
			return _save_success(order, current_hash, sales_order.name, updated=True)

		sales_order = frappe.new_doc("Sales Order")
		_apply_snapshot(sales_order, snapshot)
		sales_order.insert()
		return _save_success(order, current_hash, sales_order.name, updated=False)
	except SyncBlocker as exc:
		return _save_failure(order, str(exc))
	except frappe.PermissionError:
		raise
	except Exception:
		frappe.log_error(
			title=_("Marketplace Sales Order synchronization"),
			message=_("Marketplace Sales Order synchronization failed."),
		)
		return _save_failure(order, _("ERPNext Sales Order synchronization failed. Please review the ERPNext configuration and try again."))


def build_sales_order_snapshot(order, account=None, customer=None):
	"""Return the deterministic ERPNext-commercial input for an order.

	No Talabat money, customer PII, status, address, or raw payload belongs in
	the snapshot.  The ERPNext Sales Order uses its normal transaction-date,
	price-list, and pricing-rule defaults when the explicit sync happens.
	"""
	account = account or _get_account(order)
	_validate_account_configuration(account)
	lines = _build_sales_order_lines(order, account)
	customer = customer or _require_customer(account)
	return _make_snapshot(account, customer, lines, _delivery_date(order))


def _build_sales_order_lines(order, account):
	"""Validate and return the final UNIT lines that affect a Sales Order."""

	lines = []
	for row in order.items or []:
		if row.external_status not in FINAL_ITEM_STATUSES:
			continue
		if row.pricing_type == "KG":
			raise SyncBlocker(
				_("Talabat KG line {0} ({1}) cannot be synchronized until its ERPNext UOM setup is configured.").format(
					row.external_item_id, _line_identifier(row)
				)
			)
		if row.pricing_type != "UNIT":
			raise SyncBlocker(
				_("Talabat line {0} ({1}) has an unsupported pricing type.").format(
					row.external_item_id, _line_identifier(row)
				)
			)
		if flt(row.fulfilled_quantity) <= 0:
			raise SyncBlocker(
				_("Talabat line {0} ({1}) needs a positive fulfilled quantity before it can be synchronized.").format(
					row.external_item_id, _line_identifier(row)
				)
			)

		mapping = _get_enabled_mapping(account.name, row.external_sku)
		if not mapping:
			raise SyncBlocker(
				_("No enabled Marketplace Product Mapping exists for Talabat SKU {0} on line {1}.").format(
					_line_identifier(row), row.external_item_id
				)
			)
		item_uom = mapping.sales_uom or frappe.get_cached_value("Item", mapping.item, "stock_uom")
		lines.append(
			{
				"external_item_id": cstr(row.external_item_id).strip(),
				"item_code": mapping.item,
				"qty": flt(row.fulfilled_quantity),
				"uom": item_uom,
			}
		)

	if not lines:
		raise SyncBlocker(_("No final Talabat order lines are available to synchronize."))
	return sorted(lines, key=lambda line: line["external_item_id"])


def _make_snapshot(account, customer, lines, delivery_date):
	return {
		"company": account.erpnext_company,
		"customer": customer,
		"selling_price_list": account.erpnext_selling_price_list,
		"delivery_date": delivery_date,
		"items": lines,
	}


def set_sync_status_from_current_snapshot(order, preserve_sync_error=False):
	"""Set in-memory status while Marketplace Order is being saved.

	A source-order save must never be blocked merely because a mapping or later
	ERPNext configuration has changed.  A linked order with no longer-valid
	commercial input is therefore visibly modified, not silently considered in
	sync.
	"""
	if preserve_sync_error and order.erpnext_sync_status == "Sync Error":
		return
	if not order.erpnext_sales_order or not order.last_synced_hash:
		return
	try:
		current_hash = _snapshot_hash(build_sales_order_snapshot(order))
	except SyncBlocker:
		order.erpnext_sync_status = "Modified After Sync"
		return
	order.erpnext_sync_status = "Synced" if current_hash == order.last_synced_hash else "Modified After Sync"


def get_current_sync_status(marketplace_order):
	"""Calculate the persisted order's current status for the form load."""
	order = frappe.get_doc("Marketplace Order", marketplace_order)
	if not order.erpnext_sales_order or not order.last_synced_hash:
		return order.erpnext_sync_status

	previous_status = order.erpnext_sync_status
	set_sync_status_from_current_snapshot(order, preserve_sync_error=True)
	if order.erpnext_sync_status != previous_status:
		order.flags.skip_sync_status_update = True
		order.save()
	return order.erpnext_sync_status


def _get_account(order):
	account = frappe.get_doc("Marketplace Account", order.marketplace_account)
	if not account.enabled:
		raise SyncBlocker(_("Marketplace Account {0} is disabled.").format(account.name))
	return account


def _validate_sync_permissions(order, account):
	order.check_permission("write")
	account.check_permission("write")
	sales_order_permission = "write" if order.erpnext_sales_order else "create"
	if not frappe.has_permission("Sales Order", ptype=sales_order_permission):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if not account.erpnext_customer and not frappe.has_permission("Customer", ptype="create"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def _validate_account_configuration(account):
	if not account.erpnext_company:
		raise SyncBlocker(_("Marketplace Account {0} needs an ERPNext Company before synchronizing orders.").format(account.name))
	if not account.erpnext_selling_price_list:
		raise SyncBlocker(
			_("Marketplace Account {0} needs an ERPNext Selling Price List before synchronizing orders.").format(
				account.name
			)
		)
	price_list = frappe.get_doc("Price List", account.erpnext_selling_price_list)
	if not price_list.enabled or not price_list.selling:
		raise SyncBlocker(
			_("ERPNext Selling Price List {0} must be enabled and marked for selling.").format(price_list.name)
		)


def _get_or_create_customer(account):
	if account.erpnext_customer:
		return _require_customer(account)

	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": _generic_customer_name(account),
			"customer_type": "Company",
		}
	).insert()
	account.erpnext_customer = customer.name
	account.save()
	return customer.name


def _require_customer(account):
	if not account.erpnext_customer:
		raise SyncBlocker(_("Marketplace Account {0} has no ERPNext Customer.").format(account.name))
	if not frappe.db.exists("Customer", account.erpnext_customer):
		raise SyncBlocker(
			_("ERPNext Customer {0} configured on Marketplace Account {1} does not exist.").format(
				account.erpnext_customer, account.name
			)
		)
	return account.erpnext_customer


def _generic_customer_name(account):
	return _("Marketplace Account {0}").format(account.name)


def _get_enabled_mapping(marketplace_account, external_sku):
	external_sku = cstr(external_sku).strip()
	if not external_sku:
		return None
	key = compute_mapping_identity_key(marketplace_account, external_sku)
	values = frappe.db.get_value(
		"Marketplace Product Mapping",
		{"mapping_identity_key": key, "enabled": 1},
		["item", "sales_uom"],
		as_dict=True,
	)
	return frappe._dict(values) if values else None


def _apply_snapshot(sales_order, snapshot):
	"""Populate a Sales Order and let ERPNext calculate all commercial data."""
	sales_order.company = snapshot["company"]
	sales_order.customer = snapshot["customer"]
	sales_order.selling_price_list = snapshot["selling_price_list"]
	sales_order.delivery_date = snapshot["delivery_date"]
	sales_order.items = []
	for line in snapshot["items"]:
		sales_order.append(
			"items",
			{"item_code": line["item_code"], "qty": line["qty"], "uom": line["uom"]},
		)
	# Sales Order's standard controller resolves the Price List, active Pricing
	# Rules, currency, and item details.  It intentionally receives no Talabat
	# rate, tax, discount, fee, or transaction timestamp.
	sales_order.set_missing_values()


def _snapshot_hash(snapshot):
	return hashlib.sha256(frappe.as_json(snapshot).encode()).hexdigest()


def _save_success(order, snapshot_hash, sales_order, updated):
	order.erpnext_sales_order = sales_order
	order.last_synced_hash = snapshot_hash
	order.last_erpnext_sync_on = now_datetime()
	order.erpnext_sync_status = "Synced"
	order.last_sync_error = None
	order.flags.skip_sync_status_update = True
	order.save()
	return {"ok": True, "sales_order": sales_order, "updated": updated}


def _save_submitted_blocker(order):
	message = _("ERPNext Sales Order {0} is submitted and cannot be updated from Marketplace Order.").format(
		order.erpnext_sales_order
	)
	order.erpnext_sync_status = "Modified After Sync"
	order.last_sync_error = message
	order.flags.skip_sync_status_update = True
	order.save()
	return {"ok": False, "status": order.erpnext_sync_status, "error": message}


def _save_failure(order, message):
	order.erpnext_sync_status = "Sync Error"
	order.last_sync_error = cstr(message).strip()[:500] or _("ERPNext Sales Order synchronization failed.")
	order.flags.skip_sync_status_update = True
	order.save()
	return {"ok": False, "status": order.erpnext_sync_status, "error": order.last_sync_error}


def _line_identifier(row):
	return cstr(row.external_sku).strip() or _("no SKU")


def _delivery_date(order):
	if not order.promised_for:
		raise SyncBlocker(_("Talabat Promised For is required to set the ERPNext Sales Order Delivery Date."))
	return getdate(order.promised_for)
