# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Marketplace "Needs Attention" (#169) - the ONE definition of which
marketplace records need a human, shared by the "Marketplace Needs
Attention" Script Report and the per-account counts in
services/marketplace_channels.py (#167).

``get_attention_items(filters)`` returns one dict per problem. Native List
View filters cannot express this (it ORs across Marketplace Order, Marketplace
Sync Log, Shopify Webhook Delivery and Noon shipment child rows), hence a
service plus a Script Report.

Every read is ``frappe.get_list`` (never ``get_all``) so the result is scoped
to what the calling user may read, and every collector runs a fixed number of
bulk queries - never one query per order.

Problem sources (``problem_type``):

- Sync Error / Needs Action: Marketplace Order.erpnext_sync_status (#364). A Needs Action row also
  carries the sync action state and a link to the blocking or prepared document.
- Unmapped Items: computed live from the order's items against enabled
  Marketplace Product Mappings, with the same eligibility and lookup key as
  services/sales_order_sync.py (Talabat/Noon ``external_sku``, Shopify
  ``external_variant_id``). Only orders that are not yet Synced and not
  cancelled; accounts that are disabled are skipped (their mappings can never
  resolve, so every item would be flagged). The "not cancelled" filter is
  Shopify-only in practice: ``cancelled_at`` is only ever set by
  integrations/shopify/order_mapper.py, so Talabat/Noon orders are never
  excluded by it (their cancelled lines are excluded per item instead).
- Order Import Error: Marketplace Sync Log (Order Import, Error), hidden only
  once a Marketplace Order with the same external id exists for that account
  AND was last updated (last_platform_update_on, else modified) after the
  failed attempt - a failed update of an already-imported order stays visible.
- Fulfillment Error: Marketplace Sync Log (Fulfillment, Error), keyed by
  (order, action); hidden once a later Success row exists for the same key.
  Rows of the retired shipment actions (RETIRED_FULFILLMENT_ACTIONS) are never listed.
- Webhook Retries Exhausted: Shopify Webhook Delivery, same condition as
  shopify_reconciliation.get_webhook_delivery_exhausted_retry_count(). Listed
  in the report only: an exhausted delivery can never clear (the retry cap
  blocks any further attempt), so it is NOT counted by get_attention_counts()
  and never drives the account level.
- Noon Fulfillment: ONE row per Noon order with units pending Out of Stock (no age threshold).
  Shipment problems are Delivery Note rows (the carrier items below).
- Noon Return Unlinked (#164): one row per Noon Return with no Marketplace
  Order (no exact order_nr match, see integrations/noon/returns.py).
- Carrier Return Review (#411, #415): one row per draft Sales Return a returned carrier shipment created,
  with ``role`` "Stock User" (informational); it clears when the return is submitted or cancelled.
- Carrier Creation Problem / Cancelled / Exception / Return / Status Stale: one row per carrier Delivery
  Note (epic #413) whose create failed or has an unknown result, that the carrier cancelled, that is in an
  exception or returned state (the adapter's ``problem_states``), or that the carrier has not updated for
  CARRIER_STALE_DAYS. The message names the carrier (the adapter's ``SERVICE_NAME``). A carrier is not a
  Marketplace Account, so these rows have the carrier name as platform and no account; they appear when no
  account filter is set and the platform filter is blank or a carrier in ``CARRIERS``.
- Payment Error: Shopify Payment Sync Log Error/Invalid rows keyed by order and
  transaction, hidden by a later Success for the same pair.
- Payment Excess: submitted mirrored Payment Entries with a positive
  unallocated amount.
- Payment Entry Cancelled: cancelled mirrored Payment Entries without a
  submitted amendment.
"""

import json
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import add_to_date, cstr, flt, get_datetime, now_datetime

from marketplace_integrations.integrations.shopify.webhooks import MAX_WEBHOOK_DELIVERY_ATTEMPTS
from marketplace_integrations.marketplace_integrations.doctype.marketplace_product_mapping.marketplace_product_mapping import (
	compute_mapping_identity_key,
)
from marketplace_integrations.services.carrier_shipments import CARRIERS
from marketplace_integrations.services.sales_order_sync import (
	FINAL_ITEM_STATUSES,
	NOON_ELIGIBLE_EXTERNAL_STATUS,
	NOON_INELIGIBLE_INTEGRATION_STATUS,
)
from marketplace_integrations.services.sync_status import CANCELLED, NEEDS_ACTION, SYNC_ERROR, SYNCED

UNMAPPED_ITEMS = "Unmapped Items"
ORDER_IMPORT_ERROR = "Order Import Error"
FULFILLMENT_ERROR = "Fulfillment Error"
WEBHOOK_RETRIES_EXHAUSTED = "Webhook Retries Exhausted"
NOON_FULFILLMENT = "Noon Fulfillment"
NOON_RETURN_UNLINKED = "Noon Return Unlinked"
CARRIER_CREATION_PROBLEM = "Carrier Creation Problem"
CARRIER_EXCEPTION = "Carrier Exception"
CARRIER_RETURN = "Carrier Return"
CARRIER_STATUS_STALE = "Carrier Status Stale"
CARRIER_CANCELLED = "Carrier Cancelled"
CARRIER_RETURN_REVIEW = "Carrier Return Review"
#: The role that checks the goods of a draft return a returned shipment created (informational: the
#: list itself is scoped by Delivery Note read permission).
CARRIER_RETURN_REVIEW_ROLE = "Stock User"
PAYMENT_ERROR = "Payment Error"
PAYMENT_EXCESS = "Payment Excess"
PAYMENT_ENTRY_CANCELLED = "Payment Entry Cancelled"
CARRIER_STALE_DAYS = 5
CARRIER_MAX_AGE_DAYS = 60
#: A "Carrier Cancelled" item clears after this many days (or at once when the Delivery Note is
#: cancelled in ERPNext: the list reads docstatus < 2 only).
CARRIER_CANCELLED_DAYS = 14

PROBLEM_TYPES = [
	SYNC_ERROR,
	NEEDS_ACTION,
	UNMAPPED_ITEMS,
	ORDER_IMPORT_ERROR,
	FULFILLMENT_ERROR,
	WEBHOOK_RETRIES_EXHAUSTED,
	NOON_FULFILLMENT,
	NOON_RETURN_UNLINKED,
	CARRIER_CREATION_PROBLEM,
	CARRIER_EXCEPTION,
	CARRIER_RETURN,
	CARRIER_STATUS_STALE,
	CARRIER_CANCELLED,
	CARRIER_RETURN_REVIEW,
	PAYMENT_ERROR,
	PAYMENT_EXCESS,
	PAYMENT_ENTRY_CANCELLED,
]

NEXT_OPEN_SHIPMENT = "Open Shipment"
NEXT_MAP_ITEM = "Map Item"
NEXT_RETRY = "Retry"
NEXT_OPEN_ORDER = "Open Order"
NEXT_OPEN_PAYMENT = "Open Payment Entry"
NEXT_OPEN_RETURN = "Open Return"
NEXT_VIEW_ERROR = "View Error"

# Keys inside Marketplace Sync Log.request_data that carry the platform's
# external order id: Shopify writes order_gid, Noon fbpi_order_nr, Talabat
# order_id (see integrations/*/orders.py and talabat/order_events.py).
IMPORT_ERROR_ORDER_ID_KEYS = ("order_gid", "fbpi_order_nr", "order_id")

EXPLANATION_MAX_LENGTH = 200
# A Needs Action explanation is the stored sync message, which is capped at this length.
SYNC_MESSAGE_MAX_LENGTH = 500
CHUNK_SIZE = 500
MAX_LISTED_SKUS = 5


def get_attention_items(filters=None):
	"""One dict per problem, newest first. ``filters`` may carry ``platform``,
	``account`` and ``problem_type``. Each item has: problem_type, platform,
	account, reference_doctype, reference_name, explanation, detected_at,
	next_action and next_action_reference (the SKU to prefill for Map Item,
	otherwise None) and next_action_mapping (the name of an existing but
	disabled Marketplace Product Mapping for that SKU, otherwise None).
	"""
	filters = frappe._dict(filters or {})
	items = []
	carrier_types = {
		CARRIER_CREATION_PROBLEM,
		CARRIER_EXCEPTION,
		CARRIER_RETURN,
		CARRIER_STATUS_STALE,
		CARRIER_CANCELLED,
		CARRIER_RETURN_REVIEW,
	}
	if (
		not filters.account
		and filters.platform in (None, "", *CARRIERS)
		and filters.problem_type in (None, "", *carrier_types)
	):
		items.extend(_collect_carrier_items(filters.problem_type))
	accounts = {} if filters.platform in CARRIERS else _get_accounts(filters)
	if not accounts and not items:
		return []

	collectors = [
		({SYNC_ERROR, NEEDS_ACTION}, _collect_order_status_items),
		({UNMAPPED_ITEMS}, _collect_unmapped_items),
		({ORDER_IMPORT_ERROR}, _collect_import_errors),
		({FULFILLMENT_ERROR}, _collect_fulfillment_errors),
		({WEBHOOK_RETRIES_EXHAUSTED}, _collect_exhausted_webhooks),
		({NOON_FULFILLMENT}, _collect_noon_rollups),
		({NOON_RETURN_UNLINKED}, _collect_unlinked_noon_returns),
	]
	for types, collector in collectors:
		if not accounts or (filters.problem_type and filters.problem_type not in types):
			continue
		items.extend(collector(accounts))
	shopify_accounts = {name: account for name, account in accounts.items() if account.platform == "Shopify"}
	if shopify_accounts:
		# Read once per attention load; every payment collector filters by these orders.
		order_accounts = _shopify_order_accounts(shopify_accounts)
		payment_collectors = [
			({PAYMENT_ERROR}, _collect_payment_errors),
			({PAYMENT_EXCESS}, _collect_payment_excess),
			({PAYMENT_ENTRY_CANCELLED}, _collect_cancelled_payment_entries),
		]
		for types, collector in payment_collectors:
			if not filters.problem_type or filters.problem_type in types:
				items.extend(collector(shopify_accounts, order_accounts))

	if filters.problem_type:
		items = [item for item in items if item["problem_type"] == filters.problem_type]
	items.sort(
		key=lambda item: get_datetime(item["detected_at"]) if item["detected_at"] else datetime.min,
		reverse=True,
	)
	return items


def get_attention_counts(account_names):
	"""Per-account counts derived from ``get_attention_items()``:
	``orders_needing_attention`` is the number of distinct Marketplace Orders
	with at least one problem, ``import_errors`` the number of Order Import
	Error rows, ``fulfillment_errors`` the number of Fulfillment Error rows,
	and ``payment_errors`` the number of Shopify payment issues across the
	Payment Error, Payment Excess and Payment Entry Cancelled types.
	Webhook Retries Exhausted rows are deliberately not counted (they can never
	clear, so they would pin the account level forever).
	"""
	counts = {
		name: {
			"orders_needing_attention": 0,
			"import_errors": 0,
			"fulfillment_errors": 0,
			"payment_errors": 0,
		}
		for name in account_names
	}
	if not account_names:
		return counts

	order_refs = {name: set() for name in account_names}
	for item in get_attention_items():
		account = item["account"]
		if account not in counts:
			continue
		if item["problem_type"] in {PAYMENT_ERROR, PAYMENT_EXCESS, PAYMENT_ENTRY_CANCELLED}:
			counts[account]["payment_errors"] += 1
		if item["reference_doctype"] == "Marketplace Order":
			order_refs[account].add(item["reference_name"])
		elif item["problem_type"] == ORDER_IMPORT_ERROR:
			counts[account]["import_errors"] += 1
		elif item["problem_type"] == FULFILLMENT_ERROR:
			counts[account]["fulfillment_errors"] += 1
	for account, refs in order_refs.items():
		counts[account]["orders_needing_attention"] = len(refs)
	return counts


def _item(
	problem_type, account, reference_doctype, reference_name, explanation, detected_at, next_action, **extra
):
	return {
		"problem_type": problem_type,
		"platform": account.platform,
		"account": account.name,
		"reference_doctype": reference_doctype,
		"reference_name": reference_name,
		"explanation": cstr(explanation)[: extra.get("explanation_max_length", EXPLANATION_MAX_LENGTH)],
		"detected_at": detected_at,
		"next_action": next_action,
		"next_action_reference": extra.get("next_action_reference"),
		"next_action_mapping": extra.get("next_action_mapping"),
		"action_state": extra.get("action_state"),
		"linked_doctype": extra.get("linked_doctype"),
		"linked_document": extra.get("linked_document"),
		"role": extra.get("role"),
	}


def _get_accounts(filters):
	account_filters = {}
	if filters.platform:
		account_filters["platform"] = filters.platform
	if filters.account:
		account_filters["name"] = filters.account
	rows = frappe.get_list(
		"Marketplace Account",
		filters=account_filters,
		fields=["name", "platform", "enabled", "connection_doctype", "connection"],
		limit_page_length=0,
	)
	return {row.name: row for row in rows}


def _chunks(values, size=CHUNK_SIZE):
	values = list(values)
	for start in range(0, len(values), size):
		yield values[start : start + size]


def _get_orders(accounts, filters, fields, or_filters=None):
	return frappe.get_list(
		"Marketplace Order",
		filters={"marketplace_account": ["in", list(accounts)], **filters},
		or_filters=or_filters,
		fields=fields,
		limit_page_length=0,
	)


def _collect_order_status_items(accounts):
	orders = _get_orders(
		accounts,
		{"erpnext_sync_status": ["in", [SYNC_ERROR, NEEDS_ACTION]]},
		[
			"name",
			"marketplace_account",
			"erpnext_sync_status",
			"last_sync_error",
			"last_erpnext_sync_on",
			"modified",
			"sync_action_state",
			"sync_blocking_doctype",
			"sync_blocking_document",
			"sync_prepared_doctype",
			"sync_prepared_document",
		],
	)
	items = []
	for order in orders:
		account = accounts[order.marketplace_account]
		# The prepared draft (when there is one) is the document to act on; else the blocker.
		linked_doctype = order.sync_prepared_doctype or order.sync_blocking_doctype
		linked_document = order.sync_prepared_document or order.sync_blocking_document
		extra = {
			"action_state": order.sync_action_state,
			"linked_doctype": linked_doctype if linked_document else None,
			"linked_document": linked_document,
		}
		if order.erpnext_sync_status == SYNC_ERROR:
			items.append(
				_item(
					SYNC_ERROR,
					account,
					"Marketplace Order",
					order.name,
					order.last_sync_error or _("ERPNext Sales Order sync failed."),
					order.last_erpnext_sync_on or order.modified,
					NEXT_RETRY,
					**extra,
				)
			)
		else:
			items.append(
				_item(
					NEEDS_ACTION,
					account,
					"Marketplace Order",
					order.name,
					order.last_sync_error or _("The marketplace order needs your action."),
					order.modified,
					NEXT_OPEN_ORDER,
					explanation_max_length=SYNC_MESSAGE_MAX_LENGTH,
					**extra,
				)
			)
	return items


def _unmapped_reference_for(platform, row):
	"""The lookup key an item would be resolved by, or ``None`` if this item
	would not be turned into a Sales Order line at all (same eligibility as
	services/sales_order_sync.py)."""
	if platform == "Talabat":
		if row.external_status not in FINAL_ITEM_STATUSES:
			return None
		return cstr(row.external_sku).strip()
	if platform == "Noon":
		if row.external_status != NOON_ELIGIBLE_EXTERNAL_STATUS:
			return None
		if row.integration_status == NOON_INELIGIBLE_INTEGRATION_STATUS or flt(row.current_quantity) <= 0:
			return None
		return cstr(row.external_sku).strip()
	if platform == "Shopify":
		if flt(row.current_quantity) <= 0:
			return None
		return cstr(row.external_variant_id).strip()
	return None


def _collect_unmapped_items(accounts):
	enabled_accounts = {name: account for name, account in accounts.items() if account.enabled}
	if not enabled_accounts:
		return []

	orders = _get_orders(
		enabled_accounts,
		{
			"erpnext_sync_status": ["not in", [SYNCED, CANCELLED]],
			"cancelled_at": ["is", "not set"],
		},
		["name", "marketplace_account", "received_on", "creation"],
	)
	if not orders:
		return []
	orders_by_name = {order.name: order for order in orders}

	order_refs = {}
	all_keys = set()
	for chunk in _chunks(orders_by_name):
		rows = frappe.get_list(
			"Marketplace Order Item",
			parent_doctype="Marketplace Order",
			filters={"parent": ["in", chunk]},
			fields=[
				"parent",
				"external_sku",
				"external_variant_id",
				"external_status",
				"integration_status",
				"current_quantity",
			],
			order_by="parent asc, idx asc",
			limit_page_length=0,
		)
		for row in rows:
			account = enabled_accounts[orders_by_name[row.parent].marketplace_account]
			reference = _unmapped_reference_for(account.platform, row)
			if reference is None:
				continue
			order_refs.setdefault(row.parent, [])
			if reference not in order_refs[row.parent]:
				order_refs[row.parent].append(reference)
			if reference:
				all_keys.add(compute_mapping_identity_key(account.name, reference))

	mapped_keys = set()
	disabled_mappings = {}
	for chunk in _chunks(all_keys):
		for row in frappe.get_list(
			"Marketplace Product Mapping",
			filters={"mapping_identity_key": ["in", chunk]},
			fields=["name", "mapping_identity_key", "enabled"],
			limit_page_length=0,
		):
			if row.enabled:
				mapped_keys.add(row.mapping_identity_key)
			else:
				disabled_mappings[row.mapping_identity_key] = row.name

	items = []
	for order_name, references in order_refs.items():
		order = orders_by_name[order_name]
		account = enabled_accounts[order.marketplace_account]
		unmapped = [
			reference
			for reference in references
			if not reference or compute_mapping_identity_key(account.name, reference) not in mapped_keys
		]
		if not unmapped:
			continue
		labels = [reference or _("(blank)") for reference in unmapped]
		listed = ", ".join(labels[:MAX_LISTED_SKUS])
		if len(labels) > MAX_LISTED_SKUS:
			listed += ", ..."
		items.append(
			_item(
				UNMAPPED_ITEMS,
				account,
				"Marketplace Order",
				order_name,
				_("{0} item(s) have no enabled product mapping: {1}").format(len(unmapped), listed),
				order.received_on or order.creation,
				NEXT_MAP_ITEM,
				next_action_reference=unmapped[0] or None,
				next_action_mapping=disabled_mappings.get(
					compute_mapping_identity_key(account.name, unmapped[0])
				)
				if unmapped[0]
				else None,
			)
		)
	return items


def _collect_carrier_items(problem_type=None):
	"""Carrier problems read from the Delivery Notes of every carrier in ``CARRIERS`` (epic #413, #415): a
	create that failed or has an unknown result, a delivery the carrier cancelled, an exception state, a
	return nobody recorded and a status the carrier has not updated. The carrier of a note is the adapter
	whose ``applies_to`` accepts its transporter; the adapter's ``problem_states`` names its exception and
	return statuses. Read with the caller's permissions (``get_list``)."""
	if not frappe.has_permission("Delivery Note", "read"):
		return []
	fields = [
		"name",
		"docstatus",
		"transporter",
		"is_return",
		"custom_integration_status",
		"custom_shipping_status",
		"custom_last_integration_error",
		"custom_last_sync_at",
		"lr_no",
		"creation",
		"modified",
	]
	states = {name: adapter.problem_states() for name, adapter in CARRIERS.items()}
	stale_cutoff = add_to_date(now_datetime(), days=-CARRIER_STALE_DAYS)
	recent = add_to_date(now_datetime(), days=-CARRIER_MAX_AGE_DAYS)
	cancelled_cutoff = add_to_date(now_datetime(), days=-CARRIER_CANCELLED_DAYS)
	by_name = {}

	def collect(filters, **kw):
		for row in frappe.get_list(
			"Delivery Note", filters={"docstatus": ["<", 2], **filters}, fields=fields, limit_page_length=0, **kw
		):
			by_name.setdefault(row.name, row)

	def statuses_of(kind):
		return sorted({status for found in states.values() for status in found.get(kind, [])})

	# A started shipment, whatever the carrier calls its id (Noon has none): judged by the Integration Status.
	shipped = {
		"custom_integration_status": ["in", ["Created", "Cancellation Pending", "Cancellation Unknown", "Cancelled"]]
	}
	if problem_type in (None, CARRIER_CREATION_PROBLEM):
		collect({"custom_integration_status": ["in", ["Creation Failed", "Creation Unknown"]], "docstatus": 0})
		# A draft that was never started but carries an error: the carrier's nightly check found a shipment
		# the draft does not know about (the adapter wrote the error).
		collect(
			{
				"custom_integration_status": ["in", ["", "Not Created"]],
				"custom_last_integration_error": ["is", "set"],
				"docstatus": 0,
			}
		)
	if problem_type in (None, CARRIER_CANCELLED):
		collect({**shipped, "custom_integration_status": "Cancelled"})
	for kind in (CARRIER_EXCEPTION, CARRIER_RETURN):
		if problem_type in (None, kind) and statuses_of(kind):
			collect({**shipped, "custom_shipping_status": ["in", statuses_of(kind)]})
	if problem_type in (None, CARRIER_STATUS_STALE):
		collect(
			{**shipped, "custom_integration_status": "Created", "creation": [">=", recent]},
			or_filters=[["custom_last_sync_at", "<=", stale_cutoff], ["custom_last_sync_at", "is", "not set"]],
		)
	rows = list(by_name.values())
	items = _collect_carrier_return_reviews() if problem_type in (None, CARRIER_RETURN_REVIEW) else []
	adapters = {}

	def adapter_of(row):
		if row.transporter not in adapters:
			adapters[row.transporter] = next(
				(
					(name, adapter)
					for name, adapter in CARRIERS.items()
					if adapter.applies_to({"is_return": 0, "transporter": row.transporter})
				),
				None,
			)
		return None if row.is_return else adapters[row.transporter]

	returned = set()
	if rows:
		# The Delivery Note read check above authorizes this derived set only.
		returned = {
			row.return_against
			for row in frappe.get_all(
				"Delivery Note",
				filters={"return_against": ["in", [r.name for r in rows]], "docstatus": 1},
				fields=["return_against"],
				limit_page_length=0,
			)
		}
	# A cancel this app sent to the carrier and the carrier accepted (#410) was started by a person: not "the
	# carrier cancelled this delivery". Read from the Cancel log rows (a derived set, after the Delivery Note
	# read check above).
	cancelled_here = set()
	cancelled_names = [row.name for row in rows if row.custom_integration_status == "Cancelled"]
	if cancelled_names:
		cancelled_here = {
			row.reference_docname
			for row in frappe.get_all(
				"Integration Request",
				filters={
					"integration_request_service": ["in", [a.SERVICE_NAME for a in CARRIERS.values()]],
					"custom_operation_type": "Cancel",
					"custom_result": "Success",
					"reference_doctype": "Delivery Note",
					"reference_docname": ["in", cancelled_names],
				},
				fields=["reference_docname"],
			)
		}

	def add(kind, carrier, row, explanation, when, **extra):
		items.append(
			_item(
				kind,
				frappe._dict(name="", platform=carrier),
				"Delivery Note",
				row.name,
				explanation,
				when,
				NEXT_OPEN_SHIPMENT,
				**extra,
			)
		)

	for row in rows:
		found = adapter_of(row)
		if not found:
			continue
		carrier, adapter = found
		name = adapter.SERVICE_NAME
		status = row.custom_integration_status
		shipping_status = row.custom_shipping_status
		seen = row.custom_last_sync_at or row.modified
		error = row.custom_last_integration_error
		if (
			(status in ("Creation Failed", "Creation Unknown") or (status in ("", "Not Created", None) and error))
			and row.docstatus == 0
			and problem_type in (None, CARRIER_CREATION_PROBLEM)
		):
			add(CARRIER_CREATION_PROBLEM, carrier, row, f"{name}: {error}" if error else _(status), row.modified)
		if status == "Cancelled" and row.docstatus == 1 and problem_type in (None, CARRIER_CANCELLED):
			# Cancelled at the carrier but still submitted here (the local cancel failed): stays until it is
			# cancelled in ERPNext, however the carrier was told.
			add(
				CARRIER_CANCELLED,
				carrier,
				row,
				_("{0} shipment is cancelled but this Delivery Note is still submitted. Press Cancel.").format(name),
				seen,
			)
		elif (
			status == "Cancelled"
			and row.name not in cancelled_here
			and seen >= cancelled_cutoff
			and problem_type in (None, CARRIER_CANCELLED)
		):
			add(CARRIER_CANCELLED, carrier, row, _("{0} cancelled this delivery.").format(name), seen)
		if (
			shipping_status in states[carrier].get(CARRIER_EXCEPTION, [])
			and status != "Cancelled"
			and problem_type in (None, CARRIER_EXCEPTION)
		):
			error = row.custom_last_integration_error or shipping_status
			add(CARRIER_EXCEPTION, carrier, row, f"{name}: {error}", seen)
		if (
			shipping_status in states[carrier].get(CARRIER_RETURN, [])
			and row.name not in returned
			and problem_type in (None, CARRIER_RETURN)
		):
			add(
				CARRIER_RETURN,
				carrier,
				row,
				_("{0} reports this shipment was returned. Submit a return Delivery Note against it.").format(name),
				seen,
			)
		if (
			status == "Created"
			and shipping_status not in adapter.terminal_states()
			and row.creation >= recent
			and seen <= stale_cutoff
			and problem_type in (None, CARRIER_STATUS_STALE)
		):
			add(
				CARRIER_STATUS_STALE,
				carrier,
				row,
				_("{0} has not updated this shipment for 5 days.").format(name),
				seen,
			)
	return items


def _collect_carrier_return_reviews():
	"""One row per draft Sales Return that a returned shipment created (#411), for the Stock User who checks
	the goods and submits it. It clears once the return is submitted or cancelled. The carrier is the one of
	the delivery the return is against. Read with the caller's permissions (``get_list``)."""
	rows = frappe.get_list(
		"Delivery Note",
		filters={"is_return": 1, "docstatus": 0, "custom_return_source": ["like", "Returned by %"]},
		fields=["name", "return_against", "creation"],
		limit_page_length=0,
	)
	# The Delivery Note read check of the caller authorizes this derived lookup of the original notes.
	transporters = {
		row.name: row.transporter
		for row in frappe.get_all(
			"Delivery Note",
			filters={"name": ["in", [r.return_against for r in rows]]},
			fields=["name", "transporter"],
			limit_page_length=0,
		)
	}
	items = []
	for row in rows:
		found = next(
			(
				(name, adapter)
				for name, adapter in CARRIERS.items()
				if adapter.applies_to({"is_return": 0, "transporter": transporters.get(row.return_against)})
			),
			None,
		)
		if not found:
			continue
		items.append(
			_item(
				CARRIER_RETURN_REVIEW,
				frappe._dict(name="", platform=found[0]),
				"Delivery Note",
				row.name,
				_("{0} returned shipment {1}. Check the returned goods and submit this draft return.").format(
					found[1].SERVICE_NAME, row.return_against
				),
				row.creation,
				NEXT_OPEN_RETURN,
				role=CARRIER_RETURN_REVIEW_ROLE,
			)
		)
	return items


def _parse_request_data(value):
	try:
		data = json.loads(value) if value else {}
	except (TypeError, ValueError):
		return {}
	return data if isinstance(data, dict) else {}


def _import_failure_order_id(log):
	request_data = _parse_request_data(log.request_data)
	return next(
		(cstr(request_data[key]).strip() for key in IMPORT_ERROR_ORDER_ID_KEYS if request_data.get(key)),
		"",
	)


def get_resolved_import_failures(logs):
	"""Names of the Order Import failure ``logs`` that a later import resolved.

	``logs`` are ``Marketplace Sync Log`` rows carrying ``name``,
	``marketplace_account``, ``request_data``, ``attempted_at`` and
	``creation``. A failure is resolved when a Marketplace Order with its
	external id was created after the failed attempt (a first import that
	succeeded later), or was updated after it (platform time, falling back to
	``modified``). Creation is checked separately because some platforms,
	Shopify among them, stamp ``last_platform_update_on`` with the platform's
	own update time, which can predate the failed attempt even though the
	order was only imported afterwards. An order that existed before the
	failure and was not updated since (a failed update) stays unresolved.
	Shared by the Needs Attention report and the Shopify Order Import
	Failures report so both agree on what counts as recovered."""
	ids_by_account = {}
	for log in logs:
		order_id = _import_failure_order_id(log)
		if order_id:
			ids_by_account.setdefault(log.marketplace_account, set()).add(order_id)
	# (account, external id) -> (when the Marketplace Order was created,
	# when it was last updated).
	imported = {}
	for account_name, order_ids in ids_by_account.items():
		for chunk in _chunks(order_ids):
			for row in _get_orders(
				[account_name],
				{"external_order_id": ["in", chunk]},
				["external_order_id", "creation", "last_platform_update_on", "modified"],
			):
				imported[(account_name, cstr(row.external_order_id).strip())] = (
					get_datetime(row.creation),
					get_datetime(row.last_platform_update_on or row.modified),
				)

	resolved = set()
	for log in logs:
		order_id = _import_failure_order_id(log)
		order = imported.get((log.marketplace_account, order_id)) if order_id else None
		if order and max(order) > get_datetime(log.attempted_at or log.creation):
			resolved.add(log.name)
	return resolved


def _collect_import_errors(accounts):
	logs = frappe.get_list(
		"Marketplace Sync Log",
		filters={
			"sync_type": "Order Import",
			"status": "Error",
			"marketplace_account": ["in", list(accounts)],
		},
		fields=["name", "marketplace_account", "request_data", "traceback", "attempted_at", "creation"],
		order_by="attempted_at desc, creation desc",
		limit_page_length=0,
	)

	# Latest failure per (account, external order id); a log with no
	# recognizable id can't be matched to an order, so it stays visible.
	candidates = []
	seen = set()
	for log in logs:
		order_id = _import_failure_order_id(log)
		if order_id:
			key = (log.marketplace_account, order_id)
			if key in seen:
				continue
			seen.add(key)
		candidates.append((log, order_id))

	resolved = get_resolved_import_failures([log for log, _order_id in candidates])

	items = []
	for log, order_id in candidates:
		if log.name in resolved:
			continue
		items.append(
			_item(
				ORDER_IMPORT_ERROR,
				accounts[log.marketplace_account],
				"Marketplace Sync Log",
				log.name,
				_("Order {0} could not be imported: {1}").format(
					order_id or _("(unknown)"), log.traceback or ""
				),
				log.attempted_at or log.creation,
				NEXT_VIEW_ERROR,
			)
		)
	return items


def _collect_payment_errors(accounts, order_accounts):
	"""Latest unresolved payment mirroring failure per order and transaction."""
	if not order_accounts:
		return []
	fields = [
		"name",
		"marketplace_account",
		"marketplace_order",
		"request_data",
		"response_data",
		"attempted_at",
		"creation",
	]
	failed = {"sync_type": "Payment", "status": ["in", ["Error", "Invalid"]]}
	# Rows carry their account; rows written before that was stored are found by order instead.
	errors = {
		log.name: log
		for log in frappe.get_list(
			"Marketplace Sync Log",
			filters={**failed, "marketplace_account": ["in", list(accounts)]},
			fields=fields,
			limit_page_length=0,
		)
	}
	for chunk in _chunks(order_accounts):
		for log in frappe.get_list(
			"Marketplace Sync Log",
			filters={**failed, "marketplace_account": ["is", "not set"], "marketplace_order": ["in", chunk]},
			fields=fields,
			limit_page_length=0,
		):
			errors.setdefault(log.name, log)
	errors = sorted(
		(log for log in errors.values() if log.marketplace_order in order_accounts),
		key=lambda log: (get_datetime(log.attempted_at or log.creation), log.name),
		reverse=True,
	)
	if not errors:
		return []
	order_names = {log.marketplace_order for log in errors if log.marketplace_order}
	resolving = {}
	for chunk in _chunks(order_names):
		for log in frappe.get_list(
			"Marketplace Sync Log",
			filters={
				"sync_type": "Payment",
				"status": "Success",
				"marketplace_order": ["in", chunk],
			},
			fields=fields,
			limit_page_length=0,
		):
			data = _parse_request_data(log.request_data)
			transaction_id = cstr(data.get("transaction_id")).strip()
			key = (log.marketplace_order, transaction_id)
			stamp = get_datetime(log.attempted_at or log.creation)
			if transaction_id and (key not in resolving or stamp > resolving[key]):
				resolving[key] = stamp
	items = []
	seen = set()
	for log in errors:
		data = _parse_request_data(log.request_data)
		transaction_id = cstr(data.get("transaction_id")).strip()
		key = (log.marketplace_order, transaction_id)
		if not transaction_id or key in seen:
			continue
		seen.add(key)
		stamp = get_datetime(log.attempted_at or log.creation)
		if resolving.get(key) and resolving[key] > stamp:
			continue
		response = _parse_request_data(log.response_data)
		message = cstr(response.get("message")).strip()[:EXPLANATION_MAX_LENGTH]
		items.append(
			_item(
				PAYMENT_ERROR,
				accounts[order_accounts[log.marketplace_order]],
				"Marketplace Order",
				log.marketplace_order,
				_("Shopify payment {0} could not be recorded: {1}").format(
					transaction_id, message or log.status
				),
				log.attempted_at or log.creation,
				NEXT_OPEN_ORDER,
			)
		)
	return items


def _shopify_order_accounts(accounts):
	orders = _get_orders(accounts, {"platform": "Shopify"}, ["name", "marketplace_account"])
	return {row.name: row.marketplace_account for row in orders}


def _payment_entries(order_accounts, filters, fields):
	"""Payment Entries of the given marketplace orders, the order list sent in chunks."""
	entries = []
	if not frappe.has_permission("Payment Entry", "read"):
		return entries
	for chunk in _chunks(order_accounts):
		entries.extend(
			frappe.get_list(
				"Payment Entry",
				filters={**filters, "custom_marketplace_order": ["in", chunk]},
				fields=fields,
				limit_page_length=0,
			)
		)
	return sorted(entries, key=lambda entry: entry.modified, reverse=True)


def _collect_payment_excess(accounts, order_accounts):
	if not order_accounts:
		return []
	entries = _payment_entries(
		order_accounts,
		{"docstatus": 1, "unallocated_amount": [">", 0]},
		["name", "custom_marketplace_order", "unallocated_amount", "modified"],
	)
	return [
		_item(
			PAYMENT_EXCESS,
			accounts[order_accounts[entry.custom_marketplace_order]],
			"Payment Entry",
			entry.name,
			_("Payment exceeds the order by {0}.").format(flt(entry.unallocated_amount)),
			entry.modified,
			NEXT_OPEN_PAYMENT,
		)
		for entry in entries
		if entry.custom_marketplace_order in order_accounts
	]


def _collect_cancelled_payment_entries(accounts, order_accounts):
	if not order_accounts:
		return []
	entries = _payment_entries(
		order_accounts,
		{"docstatus": 2, "custom_marketplace_transaction_id": ["is", "set"]},
		["name", "custom_marketplace_order", "custom_marketplace_transaction_id", "modified"],
	)
	if not entries:
		return []
	# A submitted amendment of a cancelled entry resolves it (#231).
	amended = set()
	for chunk in _chunks(entry.name for entry in entries):
		amended.update(
			row.amended_from
			for row in frappe.get_list(
				"Payment Entry",
				filters={"docstatus": 1, "amended_from": ["in", chunk]},
				fields=["amended_from"],
				limit_page_length=0,
			)
		)
	return [
		_item(
			PAYMENT_ENTRY_CANCELLED,
			accounts[order_accounts[entry.custom_marketplace_order]],
			"Payment Entry",
			entry.name,
			_(
				"Mirrored payment transaction {0} was cancelled. Review the entry and record the payment "
				"manually if needed."
			).format(
				entry.custom_marketplace_transaction_id
			),
			entry.modified,
			NEXT_OPEN_PAYMENT,
		)
		for entry in entries
		if entry.custom_marketplace_order in order_accounts and entry.name not in amended
	]


#: Sync Log actions of the old Marketplace Order shipment flow (removed in #415): old Error rows of these are
#: stale, there is nothing left to retry or discard, so they are never listed.
RETIRED_FULFILLMENT_ACTIONS = (
	"CreateShipment",
	"GetShipment",
	"CancelShipment",
	"DiscardShipment",
	"AddShipmentCourierAwbs",
	"UploadShipmentInvoice",
)


def _fulfillment_message(log):
	"""The human-readable error text integrations/noon/fulfillment.py stores
	(as a JSON string) in response_data - never the raw traceback."""
	try:
		message = json.loads(log.response_data) if log.response_data else ""
	except (TypeError, ValueError):
		message = ""
	message = cstr(message).strip() if isinstance(message, str) else ""
	if not message or "Traceback" in message:
		return ""
	return message[:EXPLANATION_MAX_LENGTH]


def _fulfillment_action_label(action):
	"""Plain verb for a Noon fulfillment action code; an unknown code is shown as stored."""
	labels = {
		"UpdateOrder": _("Mark units Out of Stock"),
		"UploadOrderInvoice": _("Upload order invoice"),
		"ReleaseStuckOosReservation": _("Release Out of Stock reservation"),
	}
	return labels.get(action) or action or _("Fulfillment action")


def _collect_fulfillment_errors(accounts):
	filters = {"sync_type": "Fulfillment", "marketplace_account": ["in", list(accounts)]}
	fields = [
		"name",
		"marketplace_account",
		"marketplace_order",
		"request_data",
		"response_data",
		"attempted_at",
		"creation",
	]
	errors = frappe.get_list(
		"Marketplace Sync Log",
		filters={**filters, "status": "Error"},
		fields=fields,
		order_by="attempted_at desc, creation desc",
		limit_page_length=0,
	)
	if not errors:
		return []

	def action_of(log):
		return cstr(_parse_request_data(log.request_data).get("action"))

	def stamp_of(log):
		return get_datetime(log.attempted_at or log.creation)

	errors = [log for log in errors if action_of(log) not in RETIRED_FULFILLMENT_ACTIONS]
	if not errors:
		return []
	order_names = {log.marketplace_order for log in errors if log.marketplace_order}
	# Latest resolving stamp per (order, action).
	last_success = {}
	for chunk in _chunks(order_names):
		for log in frappe.get_list(
			"Marketplace Sync Log",
			filters={**filters, "status": "Success", "marketplace_order": ["in", chunk]},
			fields=fields,
			limit_page_length=0,
		):
			key = (log.marketplace_order, action_of(log))
			stamp = stamp_of(log)
			if key not in last_success or stamp > last_success[key]:
				last_success[key] = stamp

	# One lookup for the customer-facing order numbers shown instead of the MKTORD names.
	references = {}
	for chunk in _chunks(order_names):
		for order in frappe.get_list(
			"Marketplace Order",
			filters={"name": ["in", chunk]},
			fields=["name", "platform_order_reference"],
			limit_page_length=0,
		):
			references[order.name] = order.platform_order_reference

	items = []
	seen = set()
	for log in errors:
		action = action_of(log)
		key = (log.marketplace_order, action)
		if key in seen:
			continue
		seen.add(key)
		later = last_success.get(key)
		if later and later > stamp_of(log):
			continue
		label = _fulfillment_action_label(action)
		message = _fulfillment_message(log) or _("{0} failed - open the order.").format(label)
		order_reference = references.get(log.marketplace_order) or log.marketplace_order or ""
		explanation = _("{0} failed on order {1}: {2}").format(label, order_reference, message)
		items.append(
			_item(
				FULFILLMENT_ERROR,
				accounts[log.marketplace_account],
				"Marketplace Sync Log",
				log.name,
				explanation,
				log.attempted_at or log.creation,
				NEXT_VIEW_ERROR,
			)
		)
	return items


def _collect_exhausted_webhooks(accounts):
	account_by_connection = {
		account.connection: account
		for account in accounts.values()
		if account.platform == "Shopify"
		and account.connection_doctype == "Shopify Connection"
		and account.connection
	}
	if not account_by_connection:
		return []

	deliveries = frappe.get_list(
		"Shopify Webhook Delivery",
		filters={
			"connection": ["in", list(account_by_connection)],
			"status": "Error",
			"attempt_count": [">=", MAX_WEBHOOK_DELIVERY_ATTEMPTS],
		},
		fields=[
			"name",
			"connection",
			"topic",
			"order_gid",
			"attempt_count",
			"error",
			"received_at",
			"creation",
		],
		limit_page_length=0,
	)
	return [
		_item(
			WEBHOOK_RETRIES_EXHAUSTED,
			account_by_connection[delivery.connection],
			"Shopify Webhook Delivery",
			delivery.name,
			_("Webhook {0} failed after {1} attempts and will not be retried: {2}").format(
				delivery.topic or "", delivery.attempt_count, delivery.error or ""
			),
			delivery.received_at or delivery.creation,
			NEXT_VIEW_ERROR,
		)
		for delivery in deliveries
	]


def _pending_oos_count(value):
	try:
		units = json.loads(value) if isinstance(value, str) else value
	except (TypeError, ValueError):
		return 0
	return len(units) if isinstance(units, list) else 0


def _collect_noon_rollups(accounts):
	noon_accounts = {name: account for name, account in accounts.items() if account.platform == "Noon"}
	if not noon_accounts:
		return []

	# Shipment problems are Delivery Note rows now (the carrier collector); one rollup row per order
	# remains for units reserved for a queued Out of Stock call.
	pending = _get_orders(
		noon_accounts,
		{"platform": "Noon", "noon_pending_oos_units": ["not in", ["", "[]"]]},
		["name", "marketplace_account", "platform", "modified", "noon_pending_oos_units"],
	)
	items = []
	for order in pending:
		count = _pending_oos_count(order.noon_pending_oos_units)
		if not count:
			continue
		items.append(
			_item(
				NOON_FULFILLMENT,
				noon_accounts[order.marketplace_account],
				"Marketplace Order",
				order.name,
				_("{0} unit(s) pending Out of Stock").format(count),
				order.modified,
				NEXT_OPEN_ORDER,
			)
		)
	return items + _collect_outdated_noon_invoices(noon_accounts)


def _collect_outdated_noon_invoices(noon_accounts):
	"""One row per submitted Noon Delivery Note whose items changed after its shipment invoice was uploaded
	(#416): the invoice may need re-upload. Nothing is re-uploaded automatically. Notes are read with the
	caller's permissions (``get_list``, uploads of the last ``CARRIER_MAX_AGE_DAYS`` days); the item
	comparison is done by the Noon adapter."""
	if not frappe.has_permission("Delivery Note", "read"):
		return []
	from marketplace_integrations.integrations.noon.delivery_note_shipment import outdated_invoices

	rows = frappe.get_list(
		"Delivery Note",
		filters={
			"docstatus": 1,
			"custom_noon_invoice_uploaded_on": [">=", add_to_date(now_datetime(), days=-CARRIER_MAX_AGE_DAYS)],
		},
		fields=["name", "custom_noon_invoice_items_hash", "custom_noon_invoice_uploaded_on"],
		limit_page_length=0,
	)
	outdated = outdated_invoices(rows)
	if not outdated:
		return []
	# a derived lookup of orders, after the Delivery Note read check above
	accounts_of = {
		order.name: order.marketplace_account
		for order in frappe.get_all(
			"Marketplace Order",
			filters={"name": ["in", list(set(outdated.values()))], "marketplace_account": ["in", list(noon_accounts)]},
			fields=["name", "marketplace_account"],
			limit_page_length=0,
		)
	}
	uploaded_on = {row.name: row.custom_noon_invoice_uploaded_on for row in rows}
	return [
		_item(
			NOON_FULFILLMENT,
			noon_accounts[accounts_of[order]],
			"Delivery Note",
			note,
			_("The items changed after the invoice was uploaded. The invoice may need re-upload."),
			uploaded_on[note],
			NEXT_OPEN_SHIPMENT,
		)
		for note, order in outdated.items()
		if order in accounts_of
	]


def _collect_unlinked_noon_returns(accounts):
	noon_accounts = {name: account for name, account in accounts.items() if account.platform == "Noon"}
	if not noon_accounts:
		return []
	returns = frappe.get_list(
		"Noon Return",
		filters={"marketplace_account": ["in", list(noon_accounts)], "marketplace_order": ["is", "not set"]},
		fields=["name", "marketplace_account", "purchase_item_nr", "order_nr", "first_seen_on", "creation"],
		limit_page_length=0,
	)
	return [
		_item(
			NOON_RETURN_UNLINKED,
			noon_accounts[row.marketplace_account],
			"Noon Return",
			row.name,
			_("Noon return {0} (order {1}) matches no Marketplace Order.").format(
				row.purchase_item_nr, row.order_nr or _("unknown")
			),
			row.first_seen_on or row.creation,
			NEXT_OPEN_RETURN,
		)
		for row in returns
	]
