# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Canonical Shopify order retrieval and persistence (#109)."""

import time
from datetime import UTC

import frappe
from frappe import _
from frappe.utils import add_to_date, cstr, escape_html, get_datetime, now_datetime

from marketplace_integrations.integrations.shopify.client import ShopifyClient
from marketplace_integrations.integrations.shopify.constants import (
	SHOPIFY_ORDER_LINE_ITEMS_PAGE_SIZE,
	SHOPIFY_ORDER_RECONCILE_OVERLAP_MINUTES,
	SHOPIFY_ORDER_REFUNDS_PAGE_SIZE,
	SHOPIFY_ORDER_SHIPPING_LINES_PAGE_SIZE,
	SHOPIFY_ORDER_TRANSACTIONS_PAGE_SIZE,
	SHOPIFY_ORDERS_PAGE_SIZE,
)
from marketplace_integrations.integrations.shopify.order_mapper import normalize_order
from marketplace_integrations.services.marketplace_order_reconciliation import (
	schedule_first_syncs,
	write_catch_up_summary,
)
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.services.sales_order_sync_debounce import (
	bump_if_linked_order_changed,
	bump_sync_debounce,
	linked_order_fingerprint,
)
from marketplace_integrations.services.sync_status import CANCELLED, NEEDS_ACTION

ORDER_IMPORT_RESULT_TTL = 60 * 60
MAX_ORDER_IMPORT_DAYS = 60
_ORDER_IMPORT_RQ_JOB_PREFIX = "marketplace-shopify-order-import"

_ORDER_QUERY = """
query ShopifyOrder($id: ID!, $lineItemsFirst: Int!, $transactionsFirst: Int!, $refundsFirst: Int!, $refundLineItemsFirst: Int!, $shippingLinesFirst: Int!) {
  order(id: $id) {
    id
    name
    createdAt
    processedAt
    updatedAt
    cancelledAt
    cancelReason
    closed
    closedAt
    requiresShipping
    retailLocation { id name }
    shippingLines(first: $shippingLinesFirst) {
      pageInfo { hasNextPage }
      nodes {
        title code source carrierIdentifier deliveryCategory phone
        originalPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
      }
    }
    displayFinancialStatus
    displayFulfillmentStatus
    sourceName
    app { name }
    paymentTerms { paymentTermsType paymentTermsName dueInDays }
    paymentGatewayNames
    currencyCode
    presentmentCurrencyCode
    customer {
      id
      firstName
      lastName
      defaultEmailAddress { emailAddress }
      defaultPhoneNumber { phoneNumber }
    }
    shippingAddress {
      address1
      address2
      city
      province
      provinceCode
      countryCodeV2
      phone
      country
      zip
      latitude
      longitude
      formatted
    }
    billingAddress {
      address1
      address2
      city
      province
      provinceCode
      countryCodeV2
      phone
      country
      zip
      latitude
      longitude
      formatted
    }
    currentSubtotalPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
    currentTotalDiscountsSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
    currentTotalTaxSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
    currentShippingPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
    currentTotalDutiesSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
    currentTotalPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
    transactions(first: $transactionsFirst) {
      id kind status gateway formattedGateway manualPaymentGateway amountSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } } createdAt processedAt
    }
    refunds(first: $refundsFirst) {
      id createdAt note totalRefundedSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
      refundLineItems(first: $refundLineItemsFirst) {
        pageInfo { hasNextPage }
        nodes { id quantity restockType lineItem { id }
          subtotalSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
          totalTaxSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
        }
      }
    }
    lineItems(first: $lineItemsFirst) {
      pageInfo { hasNextPage }
      nodes {
        id name sku quantity currentQuantity unfulfilledQuantity nonFulfillableQuantity variant { id }
        originalUnitPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
        priceAfterAllDiscountsBeforeTaxesSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
        discountAllocations { allocatedAmountSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } } }
        taxLines { title rate priceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } } }
      }
    }
  }
}
"""

_ORDER_IDS_QUERY = """
query ShopifyOrderIds($first: Int!, $after: String, $query: String!) {
  orders(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes { id updatedAt }
  }
}
"""


# Admin GraphQL OrderCancelReason values (Shopify, fetched 2026-10-08; see
# docs/shopify/README.md and issue #182).
SHOPIFY_ORDER_CANCEL_REASONS = (
	"CUSTOMER",
	"DECLINED",
	"FRAUD",
	"INVENTORY",
	"OTHER",
	"STAFF",
)

_ORDER_CANCEL_MUTATION = """
mutation ShopifyOrderCancel($orderId: ID!, $reason: OrderCancelReason!, $restock: Boolean!, $notifyCustomer: Boolean!) {
  orderCancel(orderId: $orderId, reason: $reason, restock: $restock, notifyCustomer: $notifyCustomer) {
    jobResult { id }
    orderCancelUserErrors { field message }
  }
}
"""

_ORDER_CANCEL_JOB_QUERY = """
query ShopifyOrderCancelJob($id: ID!) {
  node(id: $id) {
    ... on OrderCancelJobResult {
      id
      done
      status
      errors { field message code }
    }
  }
}
"""
ORDER_CANCEL_JOB_POLL_ATTEMPTS = 3
ORDER_CANCEL_JOB_POLL_INTERVAL_SECONDS = 2


def account_for_connection(connection_name):
	accounts = frappe.get_all(
		"Marketplace Account",
		filters={
			"connection_doctype": "Shopify Connection",
			"connection": connection_name,
			"platform": "Shopify",
			"enabled": 1,
		},
		fields=["name"],
	)
	if len(accounts) != 1:
		return None
	return frappe.get_doc("Marketplace Account", accounts[0].name)


def import_order(account, order_gid):
	"""Fetch one canonical Shopify order and upsert it by Shopify GID."""
	if isinstance(account, str):
		account = frappe.get_doc("Marketplace Account", account)
	if account.platform != "Shopify" or account.connection_doctype != "Shopify Connection":
		frappe.throw(_("Marketplace Account {0} is not a Shopify account.").format(account.name))
	order_gid = cstr(order_gid).strip()
	if not order_gid:
		frappe.throw(_("Shopify order GID is required."))

	client = ShopifyClient(account.connection)
	data = client.execute(
		_ORDER_QUERY,
		{
			"id": order_gid,
			"lineItemsFirst": SHOPIFY_ORDER_LINE_ITEMS_PAGE_SIZE,
			"transactionsFirst": SHOPIFY_ORDER_TRANSACTIONS_PAGE_SIZE,
			"refundsFirst": SHOPIFY_ORDER_REFUNDS_PAGE_SIZE,
			"refundLineItemsFirst": SHOPIFY_ORDER_LINE_ITEMS_PAGE_SIZE,
			"shippingLinesFirst": SHOPIFY_ORDER_SHIPPING_LINES_PAGE_SIZE,
		},
	)
	order = data.get("order")
	if not order:
		return None
	normalized = normalize_order(order)

	def is_stale(existing):
		stored_payload = existing.latest_payload
		if isinstance(stored_payload, str):
			try:
				stored_payload = frappe.parse_json(stored_payload)
			except (TypeError, ValueError):
				return False
		stored_updated_at = (
			(stored_payload or {}).get("updatedAt") if isinstance(stored_payload, dict) else None
		)
		incoming_updated_at = order.get("updatedAt")
		if not stored_updated_at or not incoming_updated_at:
			return False
		try:
			return get_datetime(incoming_updated_at) < get_datetime(stored_updated_at)
		except (TypeError, ValueError):
			return False

	return upsert_marketplace_order(account.name, normalized, is_stale=is_stale)


def cancel_order(account, order_gid, reason):
	"""Ask Shopify to cancel an order, without restocking or notifying the customer.

	Shopify's ``orderCancel`` returns an asynchronous OrderCancelJobResult rather
	than an Order. The caller owns the durable outbound marker and post-accept refresh.
	"""
	if isinstance(account, str):
		account = frappe.get_doc("Marketplace Account", account)
	if account.platform != "Shopify" or account.connection_doctype != "Shopify Connection":
		frappe.throw(_("Marketplace Account {0} is not a Shopify account.").format(account.name))
	order_gid = cstr(order_gid).strip()
	if not order_gid:
		frappe.throw(_("Shopify order GID is required."))
	if reason not in SHOPIFY_ORDER_CANCEL_REASONS:
		frappe.throw(_("A valid Shopify cancellation reason is required."))

	data = ShopifyClient(account.connection).execute(
		_ORDER_CANCEL_MUTATION,
		{
			"orderId": order_gid,
			"reason": reason,
			"restock": False,
			"notifyCustomer": False,
		},
	)
	result = (data or {}).get("orderCancel")
	if not isinstance(result, dict):
		frappe.throw(_("Shopify did not return an order cancellation result."))
	user_errors = result.get("orderCancelUserErrors") or result.get("userErrors") or []
	if user_errors:
		messages = [cstr(error.get("message")).strip() for error in user_errors if isinstance(error, dict)]
		# Desk renders a thrown message as HTML and the text is Shopify's, so escape it.
		frappe.throw(
			escape_html(" ".join(message for message in messages if message))
			or _("Shopify rejected the cancellation.")
		)
	job_result = result.get("jobResult")
	if not isinstance(job_result, dict) or not cstr(job_result.get("id")).strip():
		frappe.throw(_("Shopify accepted no cancellation job."))
	return {"job_id": cstr(job_result.get("id")).strip()}


def poll_order_cancel_job(account, job_id):
	"""Poll an accepted Shopify cancellation job a bounded number of times.

	Returns its terminal result, or ``None`` when still running/unknown after the
	bounded wait. GraphQL top-level errors propagate to the caller; after mutation
	acceptance callers must retain the marker because the outcome is uncertain.
	"""
	if isinstance(account, str):
		account = frappe.get_doc("Marketplace Account", account)
	job_id = cstr(job_id).strip()
	if not job_id:
		frappe.throw(_("Shopify cancellation job ID is required."))
	client = ShopifyClient(account.connection)
	for attempt in range(ORDER_CANCEL_JOB_POLL_ATTEMPTS):
		data = client.execute(_ORDER_CANCEL_JOB_QUERY, {"id": job_id})
		node = (data or {}).get("node")
		if isinstance(node, dict):
			status = cstr(node.get("status")).strip().upper()
			if status in {"FAILED", "SUCCEEDED"}:
				return node
		if attempt + 1 < ORDER_CANCEL_JOB_POLL_ATTEMPTS:
			time.sleep(ORDER_CANCEL_JOB_POLL_INTERVAL_SECONDS)
	return None


def list_order_ids_updated_since(client, since, until=None):
	"""List order ids with ``updatedAt >= since`` (and, when ``until`` is
	given, ``<= until`` too), oldest-updated first (``sortKey: UPDATED_AT``).

	Bounding ``until`` in the query string itself (rather than only filtering
	client-side) keeps the listing query's own cost down - a range import
	with a narrow end date no longer has to page all the way up to "now"
	server-side only to discard everything past ``until`` in Python.
	"""
	after = None
	query = f"updated_at:>={since}" if until is None else f"updated_at:>={since} AND updated_at:<={until}"
	while True:
		data = client.execute(
			_ORDER_IDS_QUERY,
			{"first": SHOPIFY_ORDERS_PAGE_SIZE, "after": after, "query": query},
		)
		connection = data.get("orders") or {}
		yield from connection.get("nodes") or []
		page_info = connection.get("pageInfo") or {}
		if not page_info.get("hasNextPage"):
			return
		after = page_info.get("endCursor")
		if not after:
			return


def _import_order_isolated(
	account, order_gid, bump_linked_changes=False, trigger_source="Manual", bump_unlinked=False
):
	"""Import one order under its own DB savepoint, isolating its failure
	from every other order in the same range-import/reconciliation run (#109
	decision 6/review finding #2) - the same per-order isolation
	run_order_import() already used, now shared by both callers rather than
	duplicated. Returns (order_or_None, error_message_or_None).

	#178: when ``bump_linked_changes`` is true, the "before" evidence fingerprint
	read and the debounce bump also run INSIDE this savepoint, so a failure in
	either is reported as this order's error (and rolled back with its import)
	instead of escaping and ending the whole run before the watermark advances.
	"""
	savepoint = "shopify_order_import_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(savepoint)
	try:
		fingerprint_before = (
			linked_order_fingerprint(account.name, order_gid) if bump_linked_changes else None
		)
		order = import_order(account, order_gid)
		if bump_linked_changes and order:
			bump_if_linked_order_changed(order.name, fingerprint_before, trigger_source)
		# A manual import has no webhook behind it, so a new order would otherwise
		# never be synced to ERPNext automatically.
		if (
			bump_unlinked
			and order
			and not order.erpnext_sales_order
			and not order.cancelled_at
			and order.erpnext_sync_status != CANCELLED
		):
			bump_sync_debounce(order.name, trigger_source)
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint)
		return None, str(exc)
	else:
		frappe.db.release_savepoint(savepoint)
		return order, None


def import_orders_updated_between(
	account,
	client,
	start,
	end=None,
	on_progress=None,
	trigger_source="Manual",
	bump_linked_changes=False,
	bump_unlinked=False,
):
	"""List order ids updated in ``[start, end]`` (``end`` defaults to no
	upper bound) via ``list_order_ids_updated_since()`` and ``import_order()``
	each one, with per-order savepoint isolation - the one shared "list then
	import" path #109 decision 6 requires both the manual range job
	(run_order_import()) and the scheduled reconciliation
	(reconcile_all_accounts_orders()) to funnel through, so neither
	reimplements this loop independently.

	``on_progress(imported, errors)`` is called after every order (used by
	run_order_import() to report live job status); reconciliation passes
	nothing and only inspects the final totals.

	Returns (imported_names, errors) where errors is a list of
	{"order_gid": ..., "error": ...} dicts - one order's failure never stops
	the rest of the list from being processed.

	#139 section 4: every per-order failure ALSO writes a ``Marketplace Sync
	Log`` row (``sync_type="Order Import"``, ``status="Error"``) in addition
	to (not instead of) the ``frappe.log_error()`` calls its two callers
	already make on the aggregated error list - so the reconciliation
	dashboard has a per-order, queryable record of Order Import failures, the
	same way Sales Order Sync failures already do via
	services/sales_order_sync.py::_create_sync_log(). ``trigger_source``
	distinguishes a manual range import (run_order_import(), the default
	"Manual") from the scheduled sweep (reconcile_all_accounts_orders() passes
	"Scheduled Reconciliation").

	#178: ``bump_linked_changes`` (default False) is passed True ONLY by the scheduled
	sweep, reconcile_all_accounts_orders(): it then bumps the sales-order sync
	debounce for an already-linked order whose evidence fingerprint changed. A manual
	range import (run_order_import()) never bumps linked orders, regardless of ``trigger_source``.
	``bump_unlinked`` (manual import only) schedules the first Sales Order sync for each
	imported order that has no Sales Order yet and is not cancelled.
	"""
	imported = []
	errors = []
	for node in list_order_ids_updated_since(client, start, until=end):
		if end and node.get("updatedAt") and node["updatedAt"] > end:
			break
		order_gid = node.get("id")
		# #178: the fingerprint read and bump happen inside _import_order_isolated()
		# (per-order isolation), and only when ``bump_linked_changes`` (scheduled sweep).
		# The webhook path imports through import_order() directly and already bumps
		# unconditionally, so it is deliberately not touched here (no double bump).
		order, error = _import_order_isolated(
			account,
			order_gid,
			bump_linked_changes=bump_linked_changes,
			trigger_source=trigger_source,
			bump_unlinked=bump_unlinked,
		)
		if error:
			errors.append({"order_gid": order_gid, "error": error})
			_log_order_import_failure(account, order_gid, error, trigger_source)
		elif order:
			imported.append(order.name)
		if on_progress:
			on_progress(imported, errors)
	return imported, errors


def _log_order_import_failure(account, order_gid, error, trigger_source):
	"""Write one ``Marketplace Sync Log`` row for a single failed order
	import (#139 section 4). There is no Marketplace Order to link yet (the
	import itself is what failed), so ``marketplace_order`` is left blank -
	the doctype's ``mandatory_depends_on`` only requires it for
	``sync_type="Sales Order Sync"`` rows. ``ignore_permissions=True`` mirrors
	sales_order_sync.py::_create_sync_log() - this runs from a background job
	or the scheduler, not an interactive System Manager session.

	The insert is wrapped in its own savepoint and committed independently
	(review finding #2a) - the callers' loop (import_orders_updated_between())
	runs inside a caller-managed transaction that a LATER order's failure, or
	a later re-raise (run_order_import()), can roll back wholesale
	(reconcile_all_accounts_orders()'s ``frappe.db.rollback(save_point=...)``,
	or the exception propagating out of run_order_import()) - without this
	commit, an earlier order's already-logged failure row would be silently
	discarded by that later rollback, same rationale as
	products.py's per-row commit inside its own sweep loop.

	Any exception raised while writing the log itself is caught and reported
	to the Error Log rather than propagating (review finding #2b) - one bad
	log write must not stop the rest of the orders in the same batch, matching
	import_orders_updated_between()'s own docstring promise that "one order's
	failure never stops the rest"."""
	savepoint = "shopify_order_import_failure_log_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(savepoint)
	try:
		now = frappe.utils.now_datetime()
		frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"marketplace_account": account.name,
				"status": "Error",
				"trigger_source": trigger_source,
				"attempted_at": now,
				"completed_at": now,
				"request_data": frappe.as_json({"account": account.name, "order_gid": order_gid}),
				"traceback": error,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(
			title="Shopify order import: failed to write failure log",
			message=frappe.get_traceback(),
		)
	else:
		frappe.db.release_savepoint(savepoint)
		frappe.db.commit()


def reconcile_all_accounts_orders(account_name=None, catch_up_unlinked=False):
	"""Refresh each enabled Shopify account from its persisted watermark.

	Each account's whole run is also isolated from every other account's: an
	unresolvable connection, an exhausted Shopify GraphQL error, or the
	pagination-overflow throw for one account must not stop reconciliation
	for the accounts after it in this same scheduled run (review finding
	#2). Per-order isolation within an account is provided by
	import_orders_updated_between()/_import_order_isolated() above - the same
	path run_order_import() uses, per #109 decision 6.

	No savepoint wraps the per-account try/except: MariaDB's COMMIT releases
	every savepoint on the connection, not just the caller's own, and
	_log_order_import_failure() commits independently for each failed order
	(review finding #2a) - a savepoint taken before that point would already
	be invalid by the time an account-level exception tried to roll back to
	it (`SAVEPOINT ... does not exist`), which previously meant one account's
	late failure could abort every account after it. Any Marketplace Orders
	already imported for an account that later raises are therefore left
	committed rather than rolled back - harmless, since re-import is
	idempotent (same rationale as the docstring above)."""
	filters = {"platform": "Shopify", "connection_doctype": "Shopify Connection", "enabled": 1}
	if account_name:
		filters["name"] = account_name
	for row in frappe.get_all(
		"Marketplace Account",
		filters=filters,
		fields=["name", "shopify_orders_reconciled_through"],
	):
		try:
			account = frappe.get_doc("Marketplace Account", row.name)
			start = row.shopify_orders_reconciled_through or add_to_date(now_datetime(), days=-60)
			run_started = now_datetime()
			_imported, errors = import_orders_updated_between(
				account,
				ShopifyClient(account.connection),
				_as_shopify_timestamp(start),
				trigger_source="Scheduled Reconciliation",
				bump_linked_changes=True,
				bump_unlinked=catch_up_unlinked,
			)
			scheduled = 0
			if catch_up_unlinked:
				scheduled += schedule_first_syncs(
					{
						"marketplace_account": account.name,
						"platform": "Shopify",
						"cancelled_at": ["is", "not set"],
						"erpnext_sales_order": ["is", "not set"],
						"erpnext_sync_status": ["!=", CANCELLED],
					}
				)
				# Canceled-but-paid orders are intentionally outside the normal
				# unlinked import bump (their cancelled_at excludes that path).
				scheduled += schedule_first_syncs(
					{
						"marketplace_account": account.name,
						"platform": "Shopify",
						"cancelled_at": ["is", "set"],
						"erpnext_sales_order": ["is", "not set"],
						"erpnext_sync_status": NEEDS_ACTION,
					}
				)
			# Advance the watermark only after a successful run (decision 8) -
			# per-order failures are already isolated above and don't count as
			# a failed run on their own, but to the run-start time MINUS the
			# overlap window, not the exact run-start time, so a narrow race
			# at this run's own boundary is still covered by the next run.
			watermark = add_to_date(run_started, minutes=-SHOPIFY_ORDER_RECONCILE_OVERLAP_MINUTES)
			account.db_set("shopify_orders_reconciled_through", watermark, update_modified=False)
			if errors:
				frappe.log_error(
					title="Shopify order reconciliation: per-order failures",
					message=frappe.as_json(errors),
				)
			if catch_up_unlinked:
				write_catch_up_summary(
					account.name, imported=len(_imported), scheduled=scheduled, failures=len(errors)
				)
		except Exception:
			error = frappe.get_traceback()
			if catch_up_unlinked:
				frappe.db.rollback()
			frappe.log_error(title="Shopify order reconciliation failed for account", message=error)
			if catch_up_unlinked:
				write_catch_up_summary(row.name, error=error)
		else:
			# Committed per-account, mirroring webhooks.py's
			# retry_errored_webhook_deliveries() sweep: this account's imported
			# orders and advanced watermark must survive a later account's
			# failure in this same scheduled run, rather than relying solely on
			# the RQ worker's end-of-job commit (which a later account's
			# exception could otherwise roll back before it ever happens).
			frappe.db.commit()


def queue_order_import(account_name, start, end):
	account = frappe.get_doc("Marketplace Account", account_name)
	account.check_permission("read")
	if account.platform != "Shopify" or account.connection_doctype != "Shopify Connection":
		frappe.throw(_("Marketplace Account {0} is not a Shopify account.").format(account.name))
	# get_datetime(None) returns the current time, so a missing bound must be
	# caught before parsing.
	start_dt = get_datetime(start) if start else None
	end_dt = get_datetime(end) if end else None
	if not start_dt or not end_dt or start_dt > end_dt:
		frappe.throw(_("A valid Shopify order import date range is required."))
	if (end_dt - start_dt).days > MAX_ORDER_IMPORT_DAYS:
		frappe.throw(_("Shopify order imports are limited to 60 days."))
	job_id = frappe.generate_hash(length=16)
	owner = frappe.session.user
	_set_import_status(job_id, {"owner": owner, "status": "queued", "processed": 0})
	job = frappe.enqueue(
		run_order_import,
		queue="long",
		timeout=3600,
		job_id=f"{_ORDER_IMPORT_RQ_JOB_PREFIX}-{account.name}",
		deduplicate=True,
		job_id_for_result=job_id,
		owner=owner,
		account_name=account.name,
		start=_as_shopify_timestamp(start_dt),
		end=_as_shopify_timestamp(end_dt),
	)
	if job is None:
		frappe.cache().delete_value(_import_cache_key(job_id))
		frappe.throw(
			_("A Shopify order import is already running for Marketplace Account {0}.").format(account.name)
		)
	return {"job_id": job_id, "status": "queued"}


def get_order_import_status(job_id):
	state = frappe.cache().get_value(_import_cache_key(job_id))
	if not state or state.get("owner") != frappe.session.user:
		frappe.throw(_("Shopify order import job not found."), frappe.PermissionError)
	return {key: value for key, value in state.items() if key != "owner"}


def run_order_import(job_id_for_result, owner, account_name, start, end):
	try:
		_set_import_status(job_id_for_result, {"owner": owner, "status": "running", "processed": 0})
		account = frappe.get_doc("Marketplace Account", account_name)
		client = ShopifyClient(account.connection)

		def report(imported, errors):
			_set_import_status(
				job_id_for_result, {"owner": owner, "status": "running", "processed": len(imported)}
			)

		# Shares import_orders_updated_between() with reconcile_all_accounts_orders()
		# (#109 decision 6) - the manual range job and the scheduled
		# reconciliation must never reimplement the "list then import" loop
		# independently.
		imported, errors = import_orders_updated_between(
			account, client, start, end=end, on_progress=report, bump_unlinked=True
		)
		_set_import_status(
			job_id_for_result,
			{
				"owner": owner,
				"status": "completed",
				"processed": len(imported),
				"result": _order_import_result_payload(imported, errors),
			},
		)
	except Exception as exc:
		_set_import_status(
			job_id_for_result, {"owner": owner, "status": "failed", "processed": 0, "error": str(exc)}
		)
		raise


def _order_import_result_payload(imported, errors):
	"""Shape the completed job's result to match the same
	{results, total, succeeded, failed, skipped_mappings, truncated} contract
	the shared job-status dialog (public/js/shopify_catalog_push_status.js,
	#106/#107's precedent) already renders for the Shopify catalog push and
	inventory sync jobs - so #109's manual/reconciliation import (review
	finding #7) can reuse that same widget rather than a new one. Each row's
	``mapping_name`` key is a generic "this run's document name" - here a
	Marketplace Order name (success) or the order GID that failed - not
	actually a Marketplace Product Mapping; the widget's row link target is
	overridden per-caller (see ORDER_IMPORT_OPTIONS in that JS file).
	"""
	results = [{"mapping_name": name, "ok": True} for name in imported]
	results.extend({"mapping_name": e["order_gid"], "ok": False, "error": e["error"]} for e in errors)
	return {
		"results": results,
		"total": len(results),
		"succeeded": len(imported),
		"failed": len(errors),
		"skipped_mappings": 0,
		"truncated": False,
	}


def _import_cache_key(job_id):
	return f"marketplace_integrations:shopify:order_import:{job_id}"


def _set_import_status(job_id, state):
	frappe.cache().set_value(_import_cache_key(job_id), state, expires_in_sec=ORDER_IMPORT_RESULT_TTL)


def _as_shopify_timestamp(value):
	return get_datetime(value).astimezone(UTC).isoformat().replace("+00:00", "Z")
