# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon FBPI order polling and persistence (#145).

Triggered by the hourly poll, a manual Desk action, and the
``FBPI::ORDER_SYNC`` webhook (``api/noon_webhooks.py``, #163), which queues
this same deduplicated import without reading the event payload.
``poll_all_accounts()`` is the hourly ``hooks.py``
scheduler entry; ``queue_order_import()``/``run_order_import()`` are the
manual Desk-triggered equivalent, both funnelled through the same
``import_orders_created_between()`` so neither path reimplements the
"list, then import" loop independently (mirrors
``integrations/shopify/orders.py``'s ``import_orders_updated_between()``).

#145's own list-based polling deliberately never calls Noon's
``GetFbpiOrder`` (single-order GET) endpoint for a bulk run:
``ListFbpiOrders``' ``orders[]`` elements already document the identical
shape GetFbpiOrder would return (docs/noon-integration/noon_api_full.md), so
re-fetching each listed order individually would only add an unnecessary
HTTP call per order on every poll, with no additional data. A later,
separate manual per-order action (``get_order()``/``refresh_order()``/
``queue_order_refresh()``/``run_order_refresh()`` below) does call
``GetFbpiOrder`` directly - for troubleshooting one specific order on
demand, not as part of any bulk run. ``GetFbpiOrderCustomerData`` is the one
other order-scoped GET this module calls, and only for an order whose
stored customer name is still blank (see ``_maybe_fetch_customer_data()``
below).
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cstr, flt

from marketplace_integrations.services.sync_status import CANCELLED
from marketplace_integrations.integrations.noon.catalog import get_account, validate_connection_enabled
from marketplace_integrations.integrations.noon.client import NoonClient
from marketplace_integrations.integrations.noon.constants import (
	ORDER_IMPORT_LOOKBACK_DAYS,
	ORDERS_PAGE_SIZE,
)
from marketplace_integrations.integrations.noon.order_mapper import normalize_order
from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)
from marketplace_integrations.services.marketplace_order_reconciliation import (
	schedule_first_syncs,
	write_catch_up_summary,
)
from marketplace_integrations.services.marketplace_orders import (
	ITEM_FIELDS,
	JSON_ITEM_FIELDS,
	upsert_marketplace_order,
)

_CUSTOMER_FIELDS = (
	"customer_first_name",
	"customer_last_name",
	"customer_city",
	"customer_formatted_address",
)

LIST_ORDERS_PATH = "/fbpi/v1/fbpi-orders/list"
GET_ORDER_PATH = "/fbpi/v1/fbpi-order/{fbpi_order_nr}/get"
CUSTOMER_DATA_PATH = "/fbpi/v1/fbpi-order/{fbpi_order_nr}/customer-details/get"
_ORDER_IMPORT_RQ_JOB_PREFIX = "marketplace-noon-order-import"
_ORDER_REFRESH_RQ_JOB_PREFIX = "marketplace-noon-order-refresh"


def _validate_order_import_setup(account):
	"""Require the two account fields every order-import call depends on:
	the warehouse Noon orders are listed for (decision 9's required
	``warehouse_code``) and the country used for decision 20's scope
	filter."""
	if not account.noon_warehouse_code:
		frappe.throw(_("Marketplace Account {0} requires a Noon Warehouse Code.").format(account.name))
	if not account.noon_country_code:
		frappe.throw(_("Marketplace Account {0} requires a Noon Country Code.").format(account.name))


def list_orders(account, created_after=None, created_before=None):
	"""Yield raw ``ListFbpiOrders`` order dicts across every page for one
	account's Noon Warehouse.

	The request body's filters (``warehouse_code``/``created_after``/
	``created_before``) are fixed once and never change across pages -
	Noon's own pagination contract requires it (decision 11): only
	``next_token`` changes between requests.
	"""
	client = NoonClient(account.connection)
	body = {"warehouse_code": account.noon_warehouse_code}
	if created_after:
		body["created_after"] = created_after
	if created_before:
		body["created_before"] = created_before

	next_token = None
	seen_tokens = set()
	while True:
		params = {"next_token": next_token} if next_token else None
		response = client.request("POST", LIST_ORDERS_PATH, json=body, params=params)
		if response.status_code != 200:
			frappe.throw(client.error_message(response))
		try:
			data = response.json()
		except ValueError:
			frappe.throw(_("Noon returned an invalid order list response."))
		if not isinstance(data, dict) or not isinstance(data.get("orders"), list):
			frappe.throw(_("Noon returned an invalid order list response."))
		if len(data["orders"]) > ORDERS_PAGE_SIZE:
			frappe.throw(_("Noon returned more orders than its documented page size."))
		yield from data["orders"]
		next_token = data.get("next_token")
		if not next_token:
			return
		if not isinstance(next_token, str) or next_token in seen_tokens:
			frappe.throw(_("Noon returned an invalid order pagination cursor."))
		seen_tokens.add(next_token)


def get_order(account, fbpi_order_nr):
	"""Fetch one Noon FBPI order's current full detail via ``GetFbpiOrder``.

	``GET /fbpi/v1/fbpi-order/{fbpi_order_nr}/get`` - a manual, on-demand,
	single-order troubleshooting call (unlike ``list_orders()`` above, this
	is never used by a bulk import/poll run). Per docs/noon-integration/
	noon_api_full.md, the response body is the same documented flat order
	shape as one ``ListFbpiOrders`` element (no request body, only the path
	parameter) - so ``order_mapper.normalize_order()`` accepts it unchanged,
	same as any ``ListFbpiOrders`` element.
	"""
	fbpi_order_nr = cstr(fbpi_order_nr).strip()
	if not fbpi_order_nr:
		frappe.throw(_("A Noon FBPI order number is required."))
	client = NoonClient(account.connection)
	path = GET_ORDER_PATH.format(fbpi_order_nr=quote(fbpi_order_nr, safe=""))
	response = client.request("GET", path)
	if response.status_code == 404:
		frappe.throw(_("Noon order {0} was not found.").format(fbpi_order_nr))
	if response.status_code != 200:
		frappe.throw(client.error_message(response))
	try:
		data = response.json()
	except ValueError:
		frappe.throw(_("Noon returned an invalid order response."))
	if not isinstance(data, dict):
		frappe.throw(_("Noon returned an invalid order response."))
	return data


def import_order(account, raw_order, bump_trigger_source="Noon Order Import"):
	"""Normalize and upsert one raw Noon FBPI order object.

	``bump_trigger_source`` is the sync-debounce trigger source recorded if this import
	bumps (#178): the poller keeps "Noon Order Import"; a self-initiated re-import (our
	own create-shipment / mark-out-of-stock actions, ``refresh_order()``) passes "Manual"
	so its Sales Order sync is not mislabelled as a poll. Still bumps either way - the
	planner must see the change those actions made.

	Returns ``(marketplace_order_doc, unmapped_skus)``. Raises on any
	validation failure (decision 4) - the caller
	(``_import_order_isolated()``) is what isolates one order's failure
	from the rest of a run.

	#146 decision 10 (concurrency hardening): ``Document.save()`` ->
	``update_children()`` re-syncs EVERY ``Table`` field on a Marketplace
	Order on every save, deleting any DB row of that child doctype absent
	from the in-memory copy (confirmed directly against the installed
	``frappe/model/document.py``: ``update_child_table()`` deletes every row
	whose ``name`` is not in the parent's current in-memory list for that
	exact ``parentfield``). A poll run that loaded this order's row under
	ordinary REPEATABLE READ semantics could therefore silently delete a
	``Marketplace Order Shipment``/``...Unit``/``...AWB`` row committed by a
	concurrent fulfillment action (``integrations/noon/fulfillment.py``)
	between this poll's own read and its own save - that concurrent write is
	invisible to a plain snapshot read, but ``update_children()`` does not
	know or care that it only ever touches ``items`` here; it iterates every
	Table field on the doctype.

	The fix is a **locking** read, not a plain one: ``frappe.db.sql(...,
	FOR UPDATE)`` is an InnoDB locking read, which (unlike an ordinary
	consistent/snapshot read under REPEATABLE READ) always reads the latest
	COMMITTED row version and takes an exclusive row lock, blocking until any
	other transaction holding that same lock (e.g. a concurrent fulfillment
	write, which acquires the identical lock before it modifies a shipment
	table - see ``integrations/noon/fulfillment.py``) releases it.

	Round-3 review (#146 review finding #1) found the ordering half of this
	fix was still wrong: this function used to call ``normalize_order()``
	(which itself issues plain consistent reads against ``Marketplace
	Product Mapping`` via ``resolve_mapping_and_item()``) BEFORE taking the
	``FOR UPDATE`` lock below. Under MariaDB's ``innodb_snapshot_isolation=ON``,
	ANY earlier consistent read in the same transaction - on any table, not
	just this order's own row - establishes that transaction's read view;
	a later locking read that then hits a row committed after that read view
	was created raises error 1020 (``frappe.QueryDeadlockError``), which
	MariaDB handles by rolling back the WHOLE transaction (same as a real
	deadlock) - destroying ``_import_order_isolated()``/``refresh_order()``'s
	open savepoint out from under them. The fix is ordering, not a retry: the
	``FOR UPDATE`` lock below is now the very FIRST database statement this
	function issues - before ``normalize_order()``, before the customer-
	data/stale-status-group reads, and before ``upsert_marketplace_order()``'s
	own ``frappe.get_doc()`` - so nothing in THIS FUNCTION can establish a
	stale read view ahead of it. ``identity_key`` is therefore computed
	directly from ``raw_order["fbpi_order_nr"]`` (plain dict access, not a DB
	read) rather than from ``normalize_order()``'s output. A previous version
	of this function retried once on 1020 instead of reordering - that
	swallowed the error from this function's own perspective, but the
	caller's savepoint was still gone, so its later (try/except-unprotected)
	``release_savepoint()`` call raised "SAVEPOINT does not exist" and
	aborted the entire per-account run. Locking first removes the race
	instead of papering over its symptom.

	Round-4 fix (post-round-3 review + ``test_concurrent_fulfillment_write_
	survives_concurrent_poll_save``): being first-in-FUNCTION was still not
	enough. MariaDB's snapshot-isolation read view is scoped to the whole
	transaction, not to a function or savepoint - a savepoint does not open
	a new read view, only a ``commit()``/``rollback()`` does. Round-4 tried
	to close this by having each caller commit once, right after its own
	setup reads and before entering the per-order savepoint/lock flow.

	Round-5 fix (review finding #1): that was still not enough, because
	every real caller does MORE than a fixed set of setup reads before
	reaching this function's lock - ``list_orders()`` (called in a loop by
	``import_orders_created_between()``, which both ``_run_import_for_account()``
	and ``refresh_order()`` ultimately funnel through for each order) makes
	its own plain consistent read on every paginated HTTP call it issues via
	``NoonClient.request()`` (every Noon API call reads the ``Noon
	Connection``/``__Auth`` row), and ``get_order()`` (``refresh_order()``'s
	own single-order ``GetFbpiOrder`` call) does the same. A commit placed
	once, before any of that, cannot stay valid once any later read happens
	in the same still-open transaction - which is exactly what pagination/
	per-order HTTP calls do. The lock below is therefore only actually
	first-in-TRANSACTION because each caller that can reach this point now
	commits as the LAST thing it does before the savepoint that leads here -
	``_import_order_isolated()`` commits immediately before its own
	savepoint (closing the gap for every order, including ones after
	in-between pagination calls, not just the first one in a run), and
	``refresh_order()`` commits immediately after ``get_order()`` returns and
	before its own savepoint - never earlier than the last read, in either
	case.

	``_import_order_isolated()``/``refresh_order()`` additionally commit
	immediately after each order (decision 10's "one commit per order", not
	one commit for the whole run) so this lock is held only for the duration
	of one order's import, not for an entire multi-order run.
	"""
	if not isinstance(raw_order, dict):
		frappe.throw(_("Noon FBPI order payload must be an object."))
	fbpi_order_nr = cstr(raw_order.get("fbpi_order_nr")).strip()
	identity_key = compute_order_identity_key(account.name, fbpi_order_nr)

	# Lock FIRST (#146 review finding #1) - no other DB read happens before
	# this in this function. That alone is not sufficient (round-4 fix,
	# see this function's docstring): this is only actually first-in-
	# TRANSACTION because every caller that can reach this point
	# (``_run_import_for_account()``, ``refresh_order()``) commits
	# immediately after its own setup reads and before the per-order
	# savepoint that leads here.
	result = frappe.db.sql(
		"""
		SELECT `name` FROM `tabMarketplace Order`
		WHERE `order_identity_key` = %s
		FOR UPDATE
		""",
		(identity_key,),
	)
	existing_name = result[0][0] if result else None

	normalized = normalize_order(account, raw_order)
	unmapped_skus = normalized.pop("_unmapped_skus", [])

	from marketplace_integrations.services.sales_order_sync_debounce import (
		bump_if_linked_order_changed,
		bump_sync_debounce,
		linked_order_fingerprint,
	)

	had_sales_order_before = False
	had_eligible_before = False
	fingerprint_before = None
	if existing_name:
		had_sales_order_before = bool(
			frappe.db.get_value("Marketplace Order", existing_name, "erpnext_sales_order")
		)
		had_eligible_before = _has_eligible_units(existing_name)
		fingerprint_before = linked_order_fingerprint(account.name, fbpi_order_nr)

	_maybe_fetch_customer_data(account, normalized)
	_zero_out_stale_status_groups(account, normalized)
	order = upsert_marketplace_order(account.name, normalized)

	# #146 decision 7 bumped ONLY on the first transition into eligibility and never
	# once a Sales Order was linked.  #178 part 3 reverses the linked half: a poll
	# that changes a LINKED order (units cancelled/out of stock/shipped, shipment
	# membership, ...) now bumps too, so the reconciliation planner sees it
	# (``bump_if_linked_order_changed()``: only when the evidence fingerprint
	# changed, so a poll that merely re-confirms an order never re-bumps).  The
	# first-eligibility bump for a not-yet-linked order is unchanged.
	if not had_sales_order_before and not had_eligible_before and _has_eligible_units(order.name):
		bump_sync_debounce(order.name, bump_trigger_source)
	else:
		bump_if_linked_order_changed(order.name, fingerprint_before, bump_trigger_source)

	return order, unmapped_skus


def _has_eligible_units(order_name):
	"""Return whether ``order_name`` currently has at least one Sales-Order-
	eligible unit, per #146 decision 2 (``external_status ==
	NOON_ELIGIBLE_EXTERNAL_STATUS``, ``integration_status !=
	NOON_INELIGIBLE_INTEGRATION_STATUS``, ``current_quantity > 0``).

	This reuses the exact same constants ``services/sales_order_sync.py``'s
	Noon branch defines for the identical rule (#146 review finding #10 - a
	previous version of this function duplicated the rule as two inline
	string literals instead) as a cheap existence check (not a full line
	build), imported lazily inside this function rather than at module load
	time to avoid a circular import with ``services/sales_order_sync_debounce.py``,
	which does not itself import this module - the same lazy-import pattern
	``import_order()`` uses for the debounce helpers."""
	from marketplace_integrations.services.sales_order_sync import (
		NOON_ELIGIBLE_EXTERNAL_STATUS,
		NOON_INELIGIBLE_INTEGRATION_STATUS,
	)

	rows = frappe.get_all(
		"Marketplace Order Item",
		filters={"parenttype": "Marketplace Order", "parent": order_name},
		fields=["external_status", "integration_status", "current_quantity"],
	)
	return any(
		row.external_status == NOON_ELIGIBLE_EXTERNAL_STATUS
		and row.integration_status != NOON_INELIGIBLE_INTEGRATION_STATUS
		and flt(row.current_quantity) > 0
		for row in rows
	)


def _zero_out_stale_status_groups(account, normalized):
	"""Zero out (never delete) any previously-imported ``sku:``-keyed
	``Marketplace Order Item`` row whose exact status-group key is absent
	from this poll's freshly computed ``normalized["items"]`` (post-#145-
	review BLOCKER fix).

	``order_mapper._aggregate_items()`` keys each row by the exact triple
	``sku:<partner_sku>:<mp_status>:<integration_status>``, and
	``upsert_marketplace_order()``'s generic policy is "a row absent from a
	later payload is retained at its last-known quantity" - correct for
	Talabat/Shopify, where a later payload can legitimately be a narrower
	slice of an order's line items (e.g. a partial/recovery import). Noon's
	FBPI ``ListFbpiOrders``/``GetFbpiOrder`` responses are not a delta feed,
	though: every poll/GET always returns the order's COMPLETE, current
	``items[]`` (docs/noon-integration/noon_api_full.md) - so once a unit's
	status moves on (e.g. CONFIRMED/ACKNOWLEDGED -> CONFIRMED/SHIPPED), its
	old group key never reappears in any later payload again, and the
	generic "retain" policy left that stale ACKNOWLEDGED-keyed row's
	quantity/amount untouched forever - double-counting those same physical
	units against the order's real total once the new SHIPPED-keyed row was
	also added.

	Because every payload is always the complete item list (never a delta),
	the correct comparison is a plain literal key diff, not a "does this
	SKU still appear anywhere in the order" check: ANY existing ``sku:``-keyed
	row whose exact key is not one of THIS poll's freshly computed group
	keys represents units that, as of this complete read, no longer have
	that exact status combination at all - whether they transitioned to a
	different status (the expected case) or the SKU is otherwise absent from
	this read. It is therefore unconditionally zeroed, regardless of whether
	other rows for the same ``partner_sku`` are still present under a
	different status. Only ``current_quantity``/``fulfilled_quantity``/
	``transaction_amount`` are zeroed - every other field (including
	``original_quantity``, ``external_status``, ``integration_status``) is
	left exactly as last recorded, so the zeroed row still reads as a
	legible historical record ("N units were once ACKNOWLEDGED, now 0
	remain at that status") instead of being wiped or deleted; keeping it
	also lets ``upsert_marketplace_order()``'s own identity-matching find
	and update this exact row again (rather than appending a duplicate) if
	Noon ever reports the very same status combination again later.
	"""
	identity_key = compute_order_identity_key(account.name, normalized["external_order_id"])
	order_name = frappe.db.get_value("Marketplace Order", {"order_identity_key": identity_key}, "name")
	if not order_name:
		return  # First import for this order - there is no prior state to go stale.

	current_keys = {cstr(row.get("external_item_id")).strip() for row in normalized.get("items") or []}
	existing_rows = frappe.get_all(
		"Marketplace Order Item",
		filters={
			"parenttype": "Marketplace Order",
			"parent": order_name,
			"external_item_id": ["like", "sku:%"],
		},
		fields=["external_item_id", *ITEM_FIELDS, *JSON_ITEM_FIELDS],
	)
	for existing in existing_rows:
		external_item_id = cstr(existing.get("external_item_id")).strip()
		if not external_item_id or external_item_id in current_keys:
			continue
		stale_row = {fieldname: existing.get(fieldname) for fieldname in (*ITEM_FIELDS, *JSON_ITEM_FIELDS)}
		stale_row["external_item_id"] = external_item_id
		stale_row["current_quantity"] = 0
		stale_row["fulfilled_quantity"] = 0
		stale_row["transaction_amount"] = 0.0
		normalized["items"].append(stale_row)


def _maybe_fetch_customer_data(account, normalized):
	"""Fetch ``GetFbpiOrderCustomerData`` once per order (decision 17).

	Judgment call not covered by the 21 locked decisions: "called once, on
	first import only" is implemented here as "called whenever the
	currently stored ``customer_first_name`` is still blank" - checked
	against the DB, not against whether a Marketplace Order document
	already exists for this identity. A brand-new order has no stored
	value yet, so this still fires on genuine first import; a previously
	failed fetch also has no stored value, so the next poll retries it
	(decision 17's "retried on failure"); a successful fetch is never
	retried again, since the field is then non-blank. No new field is
	added to track this separately, matching the Files section (no
	``marketplace_order.json`` change listed for #145).

	Any fetch failure is logged and otherwise ignored - it must never block
	the order import itself (decision 17).

	``upsert_marketplace_order()`` has no "retain if absent from this
	payload" concept for order-level scalar fields the way it does for child
	item rows - it overwrites every one of ``ORDER_FIELDS`` from whatever
	``normalized_order.get(fieldname)`` returns, including ``None``, on
	every save. So on a poll where the fetch is skipped (already fetched),
	the previously stored customer fields must be read back and re-included
	in ``normalized`` here, or the upsert that follows would blank them out.
	"""
	identity_key = compute_order_identity_key(account.name, normalized["external_order_id"])
	existing = frappe.db.get_value(
		"Marketplace Order", {"order_identity_key": identity_key}, _CUSTOMER_FIELDS, as_dict=True
	)
	if existing and existing.get("customer_first_name"):
		for fieldname in _CUSTOMER_FIELDS:
			normalized[fieldname] = existing.get(fieldname)
		return
	try:
		client = NoonClient(account.connection)
		path = CUSTOMER_DATA_PATH.format(fbpi_order_nr=quote(normalized["external_order_id"], safe=""))
		response = client.request("GET", path)
		if response.status_code != 200:
			frappe.log_error(
				title="Noon order: customer data fetch failed", message=client.error_message(response)
			)
			return
		data = response.json()
		if not isinstance(data, dict):
			frappe.log_error(
				title="Noon order: customer data fetch failed",
				message="Noon returned a non-object customer data response.",
			)
			return
	except Exception:
		frappe.log_error(title="Noon order: customer data fetch failed", message=frappe.get_traceback())
		return

	normalized["customer_first_name"] = data.get("first_name")
	normalized["customer_last_name"] = data.get("last_name")
	normalized["customer_city"] = data.get("city")
	# administrative_division has no dedicated field (decision 17) - folded
	# into the existing customer_formatted_address field.
	normalized["customer_formatted_address"] = data.get("administrative_division")


def _import_order_isolated(account, raw_order, trigger_source):
	"""Import one order under its own DB savepoint (decision 6), isolating
	its failure from every other order in the same run - same per-order
	isolation pattern as Shopify's ``_import_order_isolated()``/
	``_log_order_import_failure()``.

	Round-5 fix (review finding #1): commits HERE, immediately before the
	savepoint/lock below, rather than relying on ``_run_import_for_account()``
	to have committed at the right spot before the per-order loop started.
	Everything between that caller's own commit and this one - the setup
	reads it did before the loop, and ``list_orders()``'s own paginated HTTP
	calls (each one a plain consistent read against the Noon Connection row,
	inside ``NoonClient.request()``) made between every order it yields - is
	a plain read in the same still-open transaction, any one of which could
	otherwise establish a stale read view ahead of ``import_order()``'s
	raw-SQL lock. Committing right here, with nothing between this commit and
	the savepoint/lock but the savepoint call itself, is what actually
	guarantees the lock is first-in-transaction for every order, not just the
	first one in a run. See docs/frappe-conventions.md and
	``import_order()``'s own docstring.
	"""
	fbpi_order_nr = cstr((raw_order or {}).get("fbpi_order_nr")).strip()
	frappe.db.commit()
	savepoint = "noon_order_import_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(savepoint)
	try:
		order, unmapped_skus = import_order(account, raw_order)
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint)
		_log_order_import_failure(account, fbpi_order_nr, str(exc), trigger_source)
		return None, fbpi_order_nr, str(exc)
	else:
		frappe.db.release_savepoint(savepoint)
		# #146 decision 10: commit after each order's own savepoint, not once
		# at the end of the whole run - releases import_order()'s row lock
		# promptly and starts every order's import in a fresh transaction, so
		# one order's lock is never held across the rest of the run.
		frappe.db.commit()
		if unmapped_skus:
			_log_unmapped_skus(account, order, unmapped_skus, trigger_source)
		return order, None, None


def import_orders_created_between(
	account, created_after, created_before, on_progress=None, trigger_source="Manual"
):
	"""List orders created in ``[created_after, created_before]`` and
	import each one, with per-order savepoint isolation (decision 6) - the
	one shared "list, then import" path both ``run_order_import()`` (manual)
	and ``poll_all_accounts()`` (scheduled) funnel through.

	``on_progress(imported, errors)`` is called after every order.

	Returns ``(imported_names, errors)`` where ``errors`` is a list of
	``{"fbpi_order_nr": ..., "error": ...}`` dicts - one order's failure
	never stops the rest of the list from being processed.

	An order whose ``mp_code`` isn't ``"noon"`` is skipped entirely, with no
	Marketplace Sync Log row at all (decision 20) - it belongs to a
	different marketplace (Namshi), not a rejected Noon order.
	"""
	imported = []
	errors = []
	for raw_order in list_orders(account, created_after=created_after, created_before=created_before):
		if not isinstance(raw_order, dict) or cstr(raw_order.get("mp_code")).strip() != "noon":
			continue
		_order, fbpi_order_nr, error = _import_order_isolated(account, raw_order, trigger_source)
		if error:
			errors.append({"fbpi_order_nr": fbpi_order_nr, "error": error})
		elif _order:
			imported.append(_order.name)
		if on_progress:
			on_progress(imported, errors)
	return imported, errors


def _log_order_import_failure(account, fbpi_order_nr, error, trigger_source):
	"""Write one committed ``Marketplace Sync Log`` row for a single failed
	order import (decision 6), independent of the run's own outer
	transaction - same rationale/shape as Shopify's
	``_log_order_import_failure()``: an insert under its own savepoint,
	committed immediately so a later order's failure/rollback in the same
	run can never discard it, with any failure while writing the log itself
	caught and reported to the Error Log rather than propagating.
	"""
	savepoint = "noon_order_import_failure_log_" + frappe.generate_hash(length=10)
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
				"request_data": frappe.as_json({"account": account.name, "fbpi_order_nr": fbpi_order_nr}),
				"traceback": error,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(
			title="Noon order import: failed to write failure log", message=frappe.get_traceback()
		)
	else:
		frappe.db.release_savepoint(savepoint)
		frappe.db.commit()


def _log_unmapped_skus(account, order, unmapped_skus, trigger_source):
	"""Write one committed ``Marketplace Sync Log`` row (``status="Invalid"``)
	listing this order's unmapped ``partner_sku``s (decision 19). The order
	itself has already imported successfully by the time this is called -
	this is an operational visibility signal, not a failure; committed
	independently for the same reason ``_log_order_import_failure()`` is.
	"""
	savepoint = "noon_order_import_unmapped_log_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(savepoint)
	try:
		now = frappe.utils.now_datetime()
		frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"marketplace_account": account.name,
				"marketplace_order": order.name,
				"status": "Invalid",
				"trigger_source": trigger_source,
				"attempted_at": now,
				"completed_at": now,
				"request_data": frappe.as_json(
					{"account": account.name, "fbpi_order_nr": order.external_order_id}
				),
				"response_data": frappe.as_json({"unmapped_partner_skus": unmapped_skus}),
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(
			title="Noon order import: failed to write unmapped-SKU log", message=frappe.get_traceback()
		)
	else:
		frappe.db.release_savepoint(savepoint)
		frappe.db.commit()


def _as_noon_timestamp(value):
	"""Format a UTC ``datetime`` as Noon's documented
	``YYYY-MM-DDTHH:MM:SSZ`` (decision 11) - no fractional seconds."""
	return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_import_for_account(account_name, trigger_source, catch_up_unlinked=False):
	"""Shared by ``run_order_import()`` (manual) and ``poll_all_accounts()``
	(scheduled): resolve and validate the account, then re-list and import
	the fixed lookback window (decision 18) - ``created_before`` is fixed at
	this call's own start, per decision 11.
	"""
	account = get_account(account_name)
	validate_connection_enabled(account)
	_validate_order_import_setup(account)

	# Round-5 fix (review finding #1): a commit was previously issued here,
	# right after the setup reads above and before the per-order loop. That
	# was still not sufficient - ``import_orders_created_between()``'s
	# ``list_orders()`` makes its own plain consistent read (via
	# ``NoonClient.request()``) on every page it fetches while iterating, in
	# the SAME transaction this commit opened, ahead of every order's own
	# lock except the very first one. Committing here can therefore never be
	# enough on its own; ``_import_order_isolated()`` now owns committing
	# immediately before its own savepoint/lock instead, which is the only
	# place that is actually guaranteed to have nothing else run between the
	# commit and the lock. See ``docs/frappe-conventions.md`` and
	# ``_import_order_isolated()``'s/``import_order()``'s own docstrings.
	run_started = datetime.now(UTC)
	created_after = _as_noon_timestamp(run_started - timedelta(days=ORDER_IMPORT_LOOKBACK_DAYS))
	created_before = _as_noon_timestamp(run_started)

	imported, errors = import_orders_created_between(
		account, created_after, created_before, trigger_source=trigger_source
	)
	scheduled = 0
	if catch_up_unlinked:
		scheduled = schedule_first_syncs(
			{
				"marketplace_account": account.name,
				"platform": "Noon",
				"erpnext_sales_order": ["is", "not set"],
				"erpnext_sync_status": ["!=", CANCELLED],
			}
		)
	if errors:
		frappe.log_error(title="Noon order import: per-order failures", message=frappe.as_json(errors))
	if catch_up_unlinked:
		write_catch_up_summary(
			account.name, imported=len(imported), scheduled=scheduled, failures=len(errors)
		)
	return {"imported": len(imported), "errors": len(errors), "scheduled": scheduled}


def enqueue_order_import(account_name, trigger_source="Manual", catch_up_unlinked=False):
	"""Shared enqueue helper for a per-account, deduplicated
	``run_order_import`` job on the ``long`` queue (decision 12's job_id
	scheme) - the one place ``queue_order_import()``, ``poll_all_accounts()``,
	and ``api/noon_webhooks.py``'s webhook receiver all funnel through, so the
	queue/timeout/job_id/deduplicate args are defined once instead of being
	copied at each call site.

	Returns the ``frappe.enqueue()`` result directly: a queued job, or
	``None`` if an identical job_id is already queued/running (RQ's own
	``deduplicate=True`` behavior). Raises outward on an enqueue failure
	(e.g. Redis down) - callers that need per-call isolation from that
	(``poll_all_accounts()``) catch it themselves.
	"""
	# The nightly catch-up has its own job id so it is not dropped as a duplicate of
	# a running hourly/manual import (and vice versa); both are still one per account.
	job_prefix = f"{_ORDER_IMPORT_RQ_JOB_PREFIX}-catch-up" if catch_up_unlinked else _ORDER_IMPORT_RQ_JOB_PREFIX
	return frappe.enqueue(
		run_order_import,
		queue="long",
		timeout=3600,
		job_id=f"{job_prefix}-{account_name}",
		deduplicate=True,
		account_name=account_name,
		trigger_source=trigger_source,
		catch_up_unlinked=catch_up_unlinked,
	)


def queue_order_import(account_name):
	"""Queue a manual order-import run for one Noon account on the ``long``
	queue, deduplicated per account (decision 12) - never makes a Noon HTTP
	call in the calling request itself.
	"""
	account = get_account(account_name)
	account.check_permission("write")
	validate_connection_enabled(account)
	_validate_order_import_setup(account)
	job = enqueue_order_import(account.name, trigger_source="Manual")
	return {"queued": job is not None}


def run_order_import(account_name, trigger_source="Manual", catch_up_unlinked=False):
	"""The enqueued worker target for a manual order-import run.

	Switches to Administrator (decision 12, matching Talabat's/Shopify's
	webhook/background workers) since this runs detached from whatever
	Desk session originally queued it.
	"""
	frappe.set_user("Administrator")
	if not catch_up_unlinked:
		return _run_import_for_account(account_name, trigger_source)
	try:
		return _run_import_for_account(account_name, trigger_source, catch_up_unlinked=True)
	except Exception:
		error = frappe.get_traceback()
		frappe.db.rollback()
		write_catch_up_summary(account_name, error=error)
		raise


def refresh_order(marketplace_order_name):
	"""Re-fetch one existing Marketplace Order's current Noon FBPI state via
	``GetFbpiOrder`` and reconcile it against the stored record.

	Manual troubleshooting/recovery action for one specific order - not the
	normal polling path (mirrors Talabat's/Shopify's own ``refresh_order()``
	shape), routed through the same ``import_order()``
	(``normalize_order()`` + ``upsert_marketplace_order()``) path #145's
	poller already uses, under the same per-order DB-savepoint isolation and
	``Marketplace Sync Log`` failure-logging pattern
	(``_log_order_import_failure()``/``_log_unmapped_skus()``) as the rest of
	this module.

	Unlike Talabat's/Shopify's own ``refresh_order()``, this one is never
	called synchronously in the Desk request - it always runs enqueued
	(``queue_order_refresh()``/``run_order_refresh()`` below), matching
	#144's manual single-mapping action convention already established for
	this Noon module (``queue_pricing_push()``/``queue_stock_push()``/
	``queue_account_sync()`` all enqueue on the ``long`` queue rather than
	call Noon inline, even for one mapping) - not a new convention invented
	for this action.

	A failure is logged the same way a bulk-run order failure is, then
	re-raised (unlike a bulk run's per-order isolation, which swallows the
	failure into an ``errors`` list) so the enqueued worker's own failure is
	visible via the normal RQ job-failure/Error Log path.
	"""
	doc = frappe.get_doc("Marketplace Order", marketplace_order_name)
	account = get_account(doc.marketplace_account)
	validate_connection_enabled(account)
	fbpi_order_nr = cstr(doc.external_order_id).strip()
	if not fbpi_order_nr:
		frappe.throw(_("Marketplace Order {0} has no Noon FBPI order number to refresh.").format(doc.name))

	# Round-5 fix (review finding #1): ``get_order()`` (the ``GetFbpiOrder``
	# HTTP call) and the validation that depends on its response now run
	# BEFORE the savepoint/lock below, not inside it, and the commit that
	# used to sit here (right after the plain reads above) has moved to
	# immediately after this call returns instead. That HTTP call makes its
	# own plain consistent read (via ``NoonClient.request()``) against the
	# Noon Connection row - committing before issuing it would not stop that
	# read from re-establishing a stale-ahead-of-the-lock read view in the
	# transaction the commit just opened; only a commit placed AFTER it,
	# with nothing but the savepoint/lock between that commit and
	# ``import_order()``'s raw-SQL lock, actually guarantees the lock is
	# first-in-transaction. See docs/frappe-conventions.md and
	# ``import_order()``'s own docstring. Nothing has been written yet at
	# this point, so a failure here needs no savepoint of its own to roll
	# back - it still logs and re-raises the same way a failure inside the
	# savepoint below does.
	try:
		raw_order = get_order(account, fbpi_order_nr)
		if not isinstance(raw_order, dict) or cstr(raw_order.get("mp_code")).strip() != "noon":
			frappe.throw(_("Noon order {0} is not a Noon-marketplace order.").format(fbpi_order_nr))
		returned_order_nr = cstr(raw_order.get("fbpi_order_nr")).strip()
		if returned_order_nr and returned_order_nr != fbpi_order_nr:
			frappe.throw(
				_(
					"Noon returned order {0}, which does not match Marketplace Order {1}'s "
					"External Order ID {2}."
				).format(returned_order_nr, doc.name, fbpi_order_nr)
			)
	except Exception as exc:
		_log_order_import_failure(account, fbpi_order_nr, str(exc), "Manual")
		raise

	frappe.db.commit()

	savepoint = "noon_order_refresh_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(savepoint)
	try:
		order, unmapped_skus = import_order(account, raw_order, bump_trigger_source="Manual")
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint)
		_log_order_import_failure(account, fbpi_order_nr, str(exc), "Manual")
		raise
	else:
		frappe.db.release_savepoint(savepoint)
		frappe.db.commit()  # #146 decision 10: same per-order commit as above
		if unmapped_skus:
			_log_unmapped_skus(account, order, unmapped_skus, "Manual")
		return {"name": order.name}


def queue_order_refresh(marketplace_order_name):
	"""Queue a manual ``GetFbpiOrder`` refresh for one Marketplace Order on
	the ``long`` queue, deduplicated per order - never makes a Noon HTTP call
	in the calling request itself (same shape as ``queue_order_import()``
	above, scoped to one order instead of one account).
	"""
	doc = frappe.get_doc("Marketplace Order", marketplace_order_name)
	doc.check_permission("write")
	job = frappe.enqueue(
		run_order_refresh,
		queue="long",
		timeout=3600,
		job_id=f"{_ORDER_REFRESH_RQ_JOB_PREFIX}-{doc.name}",
		deduplicate=True,
		marketplace_order_name=doc.name,
	)
	return {"queued": job is not None}


def run_order_refresh(marketplace_order_name):
	"""The enqueued worker target for a manual single-order refresh.

	Switches to Administrator (same rationale as ``run_order_import()``
	above) since this runs detached from whatever Desk session originally
	queued it.
	"""
	frappe.set_user("Administrator")
	return refresh_order(marketplace_order_name)


def poll_all_accounts():
	"""Hourly scheduler entry (decision 18, ``hooks.py``'s
	``scheduler_events``) - deliberately lightweight: lists enabled Noon
	accounts and enqueues one deduplicated background job per account on the
	``long`` queue, then returns immediately. It never lists or imports
	orders itself.

	Previously this function ran the full "list, then import" loop for every
	enabled account inline, as one job on the scheduler's own ``default``
	queue - which has a short timeout. At any real order volume that risks a
	silent RQ timeout mid-sweep, which would then skip every account after
	the one that timed out, every single hour, with no visible error beyond
	an RQ timeout log entry (post-#145-review finding). Moving the actual
	per-account work into its own enqueued ``long``-queue job, one job per
	account, means one account's work can run arbitrarily long without ever
	risking another account's turn, and a per-account failure now surfaces
	as a normal RQ job failure instead of silently truncating the sweep.

	Reuses ``run_order_import()`` (the same worker ``queue_order_import()``
	already enqueues for a manual Desk-triggered import) under the exact
	same ``job_id`` scheme - ``f"{_ORDER_IMPORT_RQ_JOB_PREFIX}-{account_name}"``
	with ``deduplicate=True`` - so a manual import and this hourly poll for
	the same account properly dedupe against each other: RQ refuses to
	enqueue a second job under a job_id that is already queued/running, so
	neither path can ever run concurrently against the same account. Only
	``trigger_source`` differs ("Scheduled Reconciliation" here vs. "Manual"
	for the Desk-triggered path) - both still re-list the same fixed 7-day
	lookback window (decision 18) via ``_run_import_for_account()``, which
	every enabled Noon account gets fresh on every run, not just orders
	created since the previous run, because ``ListFbpiOrders`` only filters
	on order creation time and would otherwise never surface a later
	``mp_status``/``integration_status`` change on an already-seen order.

	Enqueuing itself is wrapped per account so that a failure to enqueue one
	account's job (e.g. a transient Redis error) cannot stop the remaining
	accounts in the same sweep from being enqueued - same per-account
	isolation rationale this function always had, just moved from "isolating
	one account's full import run" to "isolating one account's enqueue call",
	since the import run itself is now isolated by construction (it's its
	own RQ job).
	"""
	accounts = frappe.get_all(
		"Marketplace Account",
		filters={"platform": "Noon", "connection_doctype": "Noon Connection", "enabled": 1},
		pluck="name",
		order_by="name asc",
	)
	queued = 0
	for account_name in accounts:
		try:
			job = enqueue_order_import(account_name, trigger_source="Scheduled Reconciliation")
		except Exception:
			frappe.log_error(
				title="Noon order poll: failed to enqueue account", message=frappe.get_traceback()
			)
			continue
		if job is not None:
			queued += 1
	return {"queued": queued}
