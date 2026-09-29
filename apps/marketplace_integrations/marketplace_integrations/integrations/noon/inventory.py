# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""ERPNext Warehouse-partitioned Noon stock updates."""

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
	item_status_error,
	persist_outcomes,
	require_items_response,
	response_by_key,
	selected_mappings,
)
from marketplace_integrations.services.account_warehouse import validate_account_warehouse
from marketplace_integrations.services.listing_quantity import get_stock_quantities

STOCK_PATH = "/stock/v1/stock-update"


def queue_stock_push(account_name, mapping_names=None):
	mapping_names = authorize_selection(account_name, mapping_names)
	get_account(account_name)
	return enqueue_push(push_stock, "stock", account_name, mapping_names)


def push_stock(account_name, mapping_names=None):
	"""Push current stock for enabled exact mappings of one Noon account."""
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

	client = NoonClient(account.connection)
	synced = failed = 0
	for batch in chunks(selected, STOCK_CHUNK_SIZE):
		try:
			quantities = get_stock_quantities(account.warehouse, [item.name for _, _, item in batch])
			payload = [
				{
					"warehouse_code": account.noon_warehouse_code,
					"partner_sku": row.external_reference,
					"qty": quantities[item.name],
				}
				for row, _, item in batch
			]
			response = client.request("POST", STOCK_PATH, json={"items": payload})
			results = response_by_key(
				require_items_response(response, client), ("warehouse_code", "partner_sku")
			)
			outcomes = {
				row.name: item_status_error(
					results.get((account.noon_warehouse_code, row.external_reference))
				)
				for row, _, _ in batch
			}
		except Exception as error:
			message = (
				str(error)
				if isinstance(error, frappe.ValidationError)
				else _("Noon stock update failed; see Error Log.")
			)
			outcomes = {row.name: message for row, _, _ in batch}
			if not isinstance(error, frappe.ValidationError):
				frappe.log_error(title="Noon stock update", message=frappe.get_traceback())
		persist_outcomes(outcomes, inventory=True)
		synced += sum(not error for error in outcomes.values())
		failed += sum(bool(error) for error in outcomes.values())
	return {"synced": synced, "failed": failed}
