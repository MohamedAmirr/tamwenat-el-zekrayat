# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""ERPNext Warehouse-partitioned Noon stock updates.

Stock is converted into each mapping's listing UOM (``listing_uom()``, shared
with ``integrations/noon/pricing.py`` - #150 review fix) before being sent:
sending the Item's raw stock-UOM quantity while pricing at a different
``sales_uom`` would overstate how many listing units are actually sellable
(e.g. a "Box" listing of 12 units would report 120 raw units in stock instead
of 10 boxes), letting Noon oversell. ``get_sellable_qty()`` performs the same
UOM conversion Talabat's catalog sync already relies on
(``integrations/talabat/catalog_sync.py``)."""

import frappe
from frappe import _

from marketplace_integrations.integrations.noon.catalog import (
	get_account,
	validate_connection_enabled,
	validate_warehouse_code,
)
from marketplace_integrations.integrations.noon.client import NoonClient
from marketplace_integrations.integrations.noon.constants import STOCK_CHUNK_SIZE
from marketplace_integrations.integrations.noon.sync import (
	authorize_selection,
	chunks,
	enqueue_push,
	is_push_paused,
	item_status_error,
	persist_outcomes,
	require_items_response,
	response_by_key,
	selected_mappings,
)
from marketplace_integrations.services.account_warehouse import validate_account_warehouse
from marketplace_integrations.services.listing_price import CatalogBuildError, listing_uom
from marketplace_integrations.services.listing_quantity import get_sellable_qty

STOCK_PATH = "/stock/v1/stock-update"


def queue_stock_push(account_name, mapping_names=None):
	"""Manual Desk entry point: runs once regardless of the account push switch."""
	mapping_names = authorize_selection(account_name, mapping_names)
	get_account(account_name)
	return enqueue_push(push_stock, "stock", account_name, mapping_names, manual=True)


def push_stock(account_name, mapping_names=None, manual=False):
	"""Push current stock for enabled exact mappings of one Noon account. A manual run
	(queued from a Desk click) ignores the account push switch, which governs only
	automatic triggers; ``manual`` is set by the enqueuer, never read from the request."""
	if not manual and is_push_paused(account_name, "stock"):
		# Switch turned off after this job was queued: no Noon call, no mapping touched.
		return {"synced": 0, "failed": 0, "paused": True}
	account = get_account(account_name)
	selected = selected_mappings(account.name, mapping_names, stock_only=True)
	if not selected:
		return {"synced": 0, "failed": 0, "skipped": True}
	try:
		validate_account_warehouse(account, "Noon stock synchronization")
		validate_connection_enabled(account)
		if not account.noon_warehouse_code:
			frappe.throw(_("Marketplace Account {0} requires a Noon Warehouse Code.").format(account.name))
		validate_warehouse_code(account.connection, account.noon_warehouse_code)
	except Exception as error:
		message = (
			str(error)
			if isinstance(error, frappe.ValidationError)
			else _("Noon stock setup failed; see Error Log.")
		)
		persist_outcomes({row.name: message for row, _, _ in selected}, inventory=True)
		if not isinstance(error, frappe.ValidationError):
			frappe.log_error(title="Noon stock setup", message=frappe.get_traceback())
		return {"synced": 0, "failed": len(selected)}

	prepared = []
	local_errors = {}
	for row, resolved, item in selected:
		try:
			listing_uom_value = listing_uom(frappe._dict(resolved), item)
			qty = get_sellable_qty(item.name, account.warehouse, listing_uom_value)
		except CatalogBuildError as error:
			local_errors[row.name] = str(error)
			continue
		prepared.append(
			(
				row,
				{
					"warehouse_code": account.noon_warehouse_code,
					"partner_sku": row.external_reference,
					"qty": qty,
				},
			)
		)
	if local_errors:
		persist_outcomes(local_errors, inventory=True)

	client = NoonClient(account.connection)
	synced = 0
	failed = len(local_errors)
	for batch in chunks(prepared, STOCK_CHUNK_SIZE):
		# Fresh DB read before every chunk (the previous chunk's commit ended the
		# transaction snapshot): a running job makes no further Noon write once the
		# switch is off. Mappings of unpushed chunks keep their status untouched.
		if not manual and is_push_paused(account.name, "stock"):
			return {"synced": synced, "failed": failed, "paused": True}
		try:
			response = client.request(
				"POST", STOCK_PATH, json={"items": [payload for _, payload in batch]}
			)
			results = response_by_key(
				require_items_response(response, client), ("warehouse_code", "partner_sku")
			)
			outcomes = {
				row.name: item_status_error(
					results.get((account.noon_warehouse_code, row.external_reference))
				)
				for row, _ in batch
			}
		except Exception as error:
			message = (
				str(error)
				if isinstance(error, frappe.ValidationError)
				else _("Noon stock update failed; see Error Log.")
			)
			outcomes = {row.name: message for row, _ in batch}
			if not isinstance(error, frappe.ValidationError):
				frappe.log_error(title="Noon stock update", message=frappe.get_traceback())
		persist_outcomes(outcomes, inventory=True)
		synced += sum(not error for error in outcomes.values())
		failed += sum(bool(error) for error in outcomes.values())
	return {"synced": synced, "failed": failed}
