# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""ERPNext Selling Price List to existing Noon partner SKU pricing."""

import frappe
from frappe import _

from marketplace_integrations.integrations.noon.catalog import get_account, validate_connection_enabled
from marketplace_integrations.integrations.noon.client import NoonClient
from marketplace_integrations.integrations.noon.constants import COUNTRY_CURRENCY, PRICING_CHUNK_SIZE
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
from marketplace_integrations.services.listing_price import (
	CatalogBuildError,
	listing_uom,
	resolve_listing_price,
)

PRICING_PATH = "/pricing/v1/pricing/upsert"


def queue_pricing_push(account_name, mapping_names=None):
	"""Manual Desk entry point: runs once regardless of the account push switch."""
	mapping_names = authorize_selection(account_name, mapping_names)
	get_account(account_name)
	return enqueue_push(push_pricing, "pricing", account_name, mapping_names, manual=True)


def validate_price_setup(account):
	"""Require an enabled selling Price List in the Noon country's currency."""
	validate_connection_enabled(account)
	if not account.noon_country_code or account.noon_country_code not in COUNTRY_CURRENCY:
		frappe.throw(_("Marketplace Account {0} requires a valid Noon Country Code.").format(account.name))
	if not account.erpnext_selling_price_list:
		frappe.throw(
			_("Marketplace Account {0} requires an ERPNext Selling Price List.").format(account.name)
		)
	price_list = frappe.get_doc("Price List", account.erpnext_selling_price_list)
	if not price_list.enabled or not price_list.selling:
		frappe.throw(_("Price List {0} must be an enabled selling price list.").format(price_list.name))
	expected = COUNTRY_CURRENCY[account.noon_country_code]
	if price_list.currency != expected:
		frappe.throw(
			_("Price List {0} currency {1} does not match Noon country {2} currency {3}.").format(
				price_list.name, price_list.currency, account.noon_country_code, expected
			)
		)
	return price_list


def push_pricing(account_name, mapping_names=None, manual=False):
	"""Set current prices for existing Noon partner SKUs, item by item. A manual run
	(queued from a Desk click) ignores the account push switch, which governs only
	automatic triggers; ``manual`` is set by the enqueuer, never read from the request."""
	if not manual and is_push_paused(account_name, "pricing"):
		# Switch turned off after this job was queued: no Noon call, no mapping touched.
		return {"synced": 0, "failed": 0, "paused": True}
	account = get_account(account_name)
	selected = selected_mappings(account.name, mapping_names)
	if not selected:
		return {"synced": 0, "failed": 0, "skipped": True}
	try:
		price_list = validate_price_setup(account)
	except Exception as error:
		message = (
			str(error)
			if isinstance(error, frappe.ValidationError)
			else _("Noon pricing setup failed; see Error Log.")
		)
		persist_outcomes({row.name: message for row, _, _ in selected})
		if not isinstance(error, frappe.ValidationError):
			frappe.log_error(title="Noon pricing setup", message=frappe.get_traceback())
		return {"synced": 0, "failed": len(selected)}

	prepared = []
	local_errors = {}
	for row, resolved, item in selected:
		try:
			price = resolve_listing_price(price_list, item, listing_uom(frappe._dict(resolved), item))
		except CatalogBuildError as error:
			local_errors[row.name] = str(error)
			continue
		prepared.append(
			(
				row,
				{
					"partner_sku": row.external_reference,
					"country_code": account.noon_country_code,
					"price": float(price),
				},
			)
		)
	if local_errors:
		persist_outcomes(local_errors)
	client = NoonClient(account.connection)
	synced = 0
	failed = len(local_errors)
	for batch in chunks(prepared, PRICING_CHUNK_SIZE):
		# Fresh DB read before every chunk: stop before the next Noon write once off.
		if not manual and is_push_paused(account.name, "pricing"):
			return {"synced": synced, "failed": failed, "paused": True}
		try:
			response = client.request("POST", PRICING_PATH, json={"items": [payload for _, payload in batch]})
			results = response_by_key(
				require_items_response(response, client), ("partner_sku", "country_code")
			)
			outcomes = {
				row.name: item_status_error(results.get((row.external_reference, account.noon_country_code)))
				for row, _ in batch
			}
		except Exception as error:
			message = (
				str(error)
				if isinstance(error, frappe.ValidationError)
				else _("Noon pricing update failed; see Error Log.")
			)
			outcomes = {row.name: message for row, _ in batch}
			if not isinstance(error, frappe.ValidationError):
				frappe.log_error(title="Noon pricing update", message=frappe.get_traceback())
		persist_outcomes(outcomes)
		synced += sum(not error for error in outcomes.values())
		failed += sum(bool(error) for error in outcomes.values())
	return {"synced": synced, "failed": failed}
