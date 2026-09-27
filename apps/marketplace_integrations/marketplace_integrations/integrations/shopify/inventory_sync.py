# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""ERPNext-authoritative Shopify inventory synchronization (#107, PR1 + PR2).

PR1 = the sync service plus MANUAL actions (per-mapping / bulk "Sync Shopify
Inventory" and per-account "Reconcile Shopify Inventory").

PR2 (this module's ``sweep_changed_bins()`` / ``reconcile_all_accounts()``,
registered in ``hooks.py``'s ``scheduler_events``) adds the only two automatic
triggers: a changed-Bin sweep (every
``constants.SHOPIFY_INVENTORY_SWEEP_CRON``) and a daily full reconciliation.
Both are scheduler-callback-cheap - they only read Bins/mappings/the per-
account watermark and call ``enqueue_account_sync()`` below (the same helper
the manual actions use); every actual Shopify read/mutation still happens
only in the worker, ``sync_account_inventory()``. Still **no `doc_events`
anywhere** - both triggers are polling/cron, never a document hook.

Locked decisions this implements (see the issue's "Implementation plan"):

- **Quantity** (3/5): writes ``on_hand`` (Shopify derives ``available``) from
  ``floor(max(Bin.actual_qty, 0))`` in the Item's **stock UOM** - ``sales_uom``
  is ignored. Source: ``services/listing_quantity.py::get_stock_quantities()``
  (one bulk ``Bin`` query per chunk).
- **Skips** (6): a disabled Account, Connection, mapping, or ERPNext Item makes
  no mutation. Disabled mappings/Items are reported as ``skipped`` (never
  ``Error``). So is a mapping whose Item is not a stock Item
  (``is_stock_item = 0``: a service / Product Bundle parent / template Item -
  its Shopify variant is deliberately untracked, and a missing Bin would
  otherwise read as 0 and turn tracking on / sell the variant out). This holds
  for explicit selections and full reconciliation alike. Bundle parents wrongly
  flagged ``is_stock_item = 1`` are NOT detected - no bundle logic here. A variant not stocked at the location gets ``inventoryActivate``;
  an untracked inventory item gets ``inventoryItemUpdate(tracked: true)`` first
  (both deliberate, user-approved changes to Shopify configuration).
- **Ids at sync time** (7): per chunk, one ``nodes(ids:)`` read returns each
  variant's inventory item id, ``tracked`` flag, and the current ``on_hand`` at
  the bound location - the compare value. Nothing is cached on the mapping.
- **Compare-and-set** (8): ``inventorySetQuantities`` with
  ``changeFromQuantity`` (``compareQuantity`` is deprecated), never
  ``ignoreCompareQuantity``. An item whose Shopify value already equals the
  target is skipped (no mutation, counts as synced). ``COMPARE_FAILED`` re-reads
  and resends the whole chunk, at most ``SHOPIFY_INVENTORY_MAX_STALE_RETRIES``
  times, then marks the chunk Error. Any other userError falls back to sending
  the chunk's items one at a time so the bad item is isolated (this module -
  like #106 - never parses ``userErrors[].field`` to attribute an error).
- **Idempotency** (9): every mutation carries ``@idempotent(key: $idempotencyKey)``
  with a FRESH ``uuid4`` per logical mutation - never derived from
  (item, location, target, compare), so a quantity that returns to an earlier
  compare value inside Shopify's idempotency window can't replay a cached
  no-op. The key is reused only across ``ShopifyClient.execute()``'s own
  throttle/401 resends (its variables are unchanged there).
  ``inventoryItemUpdate`` carries no ``@idempotent`` (unverified, see below).
- **Location** (10): writes to ``Marketplace Account.shopify_location_gid``. The
  Location is only validated when the Account is saved, so the worker re-checks
  it against the shop's active Locations once per job.
- **Status** (4): results go to the mapping's own ``inventory_sync_status`` /
  ``inventory_last_synced_on`` / ``inventory_last_error`` via
  ``frappe.db.set_value(..., update_modified=False)``. The shared
  ``sync_status`` / ``last_synced_on`` / ``last_error`` (written by #106's price
  push) are never touched. A **whole-job setup failure** (before the
  per-mapping/per-chunk loop starts - e.g. the Location re-check throwing
  because the bound Location was deactivated) also writes ``Error`` +
  ``inventory_last_error`` to every mapping this job would ACTUALLY have
  touched, via ``_record_whole_job_failure()`` - otherwise those rows would
  silently keep showing a stale prior status forever, visible only in the
  Error Log. "Would actually have touched" is computed by re-running
  ``_mappings_for_sync()`` + ``_fetch_items()`` + ``_split_active_mappings()``,
  the exact same classification the worker itself uses, so a skip-class
  mapping (disabled, disabled Item, non-stock Item, template Item) is left
  untouched exactly as a normal run would leave it, and a mapping since moved
  to another account is never touched under the wrong account's job. The
  persisted message is the exception text only for a ``frappe.ValidationError``
  (already user-facing); any other exception gets a generic "see Error Log"
  message so an internal/DB-error string, or a raw Shopify response fragment,
  is never shown on a mapping row - full detail always still goes to
  ``frappe.log_error()``. A failure inside ``_record_whole_job_failure()``
  itself is caught and logged separately so it can never mask/replace the
  original exception being re-raised.
- **No inbound path**: this module never creates a Stock Entry / Stock
  Reconciliation / Stock Ledger Entry, and ``SHOPIFY_WEBHOOK_TOPICS`` has no
  inventory topic.

Where work happens (no ambiguity about the Desk request path):

- ``queue_inventory_sync()`` / ``queue_account_reconciliation()`` (Desk
  request): permission checks, ``_validate_inventory_account_local()`` (platform,
  connection, enabled flags, Location set, Warehouse - all local DB reads), and
  the enqueue. **No Shopify HTTP call is made in the request.**
- ``sync_account_inventory()`` (background worker): the Location use-time
  re-check (a live read), then all reads/mutations against Shopify.

Persistence: each chunk's outcomes are written under their own savepoint and
committed (``frappe.db.commit()``) before the next chunk, so a later throttle
exhaustion or exception can't roll back finished chunks. Pending writes are
committed before the HTTP calls, so no write lock / transaction with pending
writes is held across HTTP. (Some non-locking reads still happen around HTTP,
e.g. ``ShopifyClient.execute()`` loads the Shopify Connection before each
request and ``validate_inventory_account()`` reads the Account / Connection /
Warehouse before the Location call - the token comes from the Redis cache.)

Unverified Shopify assumptions (could not be checked without a live store; each
is also recorded in docs/shopify/README.md's "Inventory (#107)" section -
verify on ONE test variant before the first real run):

VERIFIED on shopify.dev 2026-09-24 (``mutations/inventorySetQuantities``,
``objects/InventoryQuantity``): ``@idempotent(key: $idempotencyKey)`` with
``$idempotencyKey: String!``; ``reason: "correction"`` (a documented example -
the full value list is not stated); the ``referenceDocumentUri`` example style
``logistics://some.warehouse/take/<timestamp>`` (no format rules stated; the
URI used matches that style); ``InventoryQuantity`` fields ``id, name,
quantity, updatedAt``.

STILL UNVERIFIED:

1. That ``InventoryLevel.quantities(names: ["on_hand"])`` returns an ``on_hand``
   entry for a stocked level.
2. The maximum ``quantities`` per ``inventorySetQuantities`` call (chunk size
   50 is a guess).
3. That ``changeFromQuantity`` has the semantics ``compareQuantity`` had (the
   page gives no explanation).
4. That ``inventoryActivate`` works for an untracked item (this module enables
   tracking first, so it never relies on it), the initial level it creates
   (assumed ``on_hand`` 0, used as the compare value; a wrong guess surfaces as
   ``COMPARE_FAILED`` and is recovered by the re-read), and its
   ``inventoryLevel { id }`` payload shape.
5. That ``inventorySetQuantities`` is atomic when it returns userErrors (if
   not, the skip-if-equal re-read on retry/fallback is what converges; a test
   with a partially-applying fake covers that).
6. Whether ``inventoryItemUpdate`` requires/accepts ``@idempotent`` in the
   pinned API version, and its effect on other locations' quantity records when
   ``tracked`` flips to true.
7. Cost/throttling of a full reconciliation (``ShopifyClient.execute()`` caps at
   3 throttle attempts x 10s; progress is committed per chunk to keep finished
   work).
8. That the stale-compare userError code is ``COMPARE_FAILED`` (seen in one doc
   fetch; a later fetch said the code enum is not listed).

Operating requirement (decision 3): because ``on_hand`` is pushed, orders must
be marked fulfilled in Shopify (manually is fine) - v1 has no fulfillment sync,
so ``committed`` would otherwise never clear and units are subtracted twice once
ERPNext ships. The reverse ordering also matters: if an order is fulfilled in
Shopify BEFORE ERPNext posts the stock-out (Delivery Note), the next sync pushes
the higher ERPNext ``on_hand`` back until ERPNext catches up, so stock is
briefly overstated.
"""

import uuid

import frappe
from frappe import _
from frappe.utils import add_to_date, cstr, now_datetime

from marketplace_integrations.integrations.shopify.client import ShopifyClient
from marketplace_integrations.integrations.shopify.constants import (
	SHOPIFY_INVENTORY_MAX_STALE_RETRIES,
	SHOPIFY_INVENTORY_QUANTITY_NAME,
	SHOPIFY_INVENTORY_REASON,
	SHOPIFY_INVENTORY_REFERENCE_DOCUMENT_URI,
	SHOPIFY_INVENTORY_STALE_COMPARE_CODE,
	SHOPIFY_INVENTORY_SWEEP_OVERLAP_MINUTES,
	SHOPIFY_INVENTORY_SYNC_CHUNK_SIZE,
)
from marketplace_integrations.integrations.shopify.locations import fetch_shop_and_active_locations
from marketplace_integrations.integrations.shopify.products import _is_variant_gid
from marketplace_integrations.services.account_warehouse import validate_account_warehouse
from marketplace_integrations.services.listing_quantity import get_stock_quantities
from marketplace_integrations.services.mapping_push import group_mappings_by_account, parse_mapping_names

MAPPING_DOCTYPE = "Marketplace Product Mapping"

REMOTE_ERROR = _("Shopify did not accept the inventory update.")

# Bounds a userErrors message written to a mapping / job result - same
# rationale as catalog_push.py's ERROR_MESSAGE_MAX_LENGTH.
ERROR_MESSAGE_MAX_LENGTH = 500

# Detailed Error Log entries written per job; further failures are counted and
# summarized in ONE final entry (per-mapping errors on the rows are unaffected).
MAX_ERROR_LOG_ENTRIES_PER_JOB = 20

RESULT_TTL = 60 * 60
INVENTORY_PROGRESS_EVENT = "marketplace_shopify_inventory_sync_progress"

# Bounds what is cached/published/rendered, not what the worker processes.
MAX_INVENTORY_RESULTS_RETURNED = 500

# How often (in chunks processed) to write an intermediate progress update -
# same rationale as catalog_push.py's PUSH_PROGRESS_UPDATE_INTERVAL. The final
# "completed" update always runs unconditionally. Only the polling dialog reads
# this status; INVENTORY_PROGRESS_EVENT is published for parity with #105/#106
# but nothing in this codebase listens for it.
INVENTORY_PROGRESS_UPDATE_INTERVAL = 10

# Per-account RQ dedup job id (same shape as #105/#106), shared by manual sync,
# reconciliation and (PR2) the automatic sweep, so two jobs for one account can
# never race each other's compare-and-set writes.
_SYNC_RQ_JOB_ID_PREFIX = "marketplace-shopify-inventory-sync"

_INVENTORY_READ_QUERY = """
query MarketplaceInventoryRead($ids: [ID!]!, $locationId: ID!, $names: [String!]!) {
  nodes(ids: $ids) {
    id
    ... on ProductVariant {
      inventoryItem {
        id
        tracked
        inventoryLevel(locationId: $locationId) {
          quantities(names: $names) {
            name
            quantity
          }
        }
      }
    }
  }
}
"""

_INVENTORY_SET_MUTATION = """
mutation MarketplaceInventorySet($input: InventorySetQuantitiesInput!, $idempotencyKey: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $idempotencyKey) {
    inventoryAdjustmentGroup {
      id
    }
    userErrors {
      code
      field
      message
    }
  }
}
"""

_INVENTORY_ACTIVATE_MUTATION = """
mutation MarketplaceInventoryActivate(
  $inventoryItemId: ID!
  $locationId: ID!
  $idempotencyKey: String!
) {
  inventoryActivate(inventoryItemId: $inventoryItemId, locationId: $locationId)
    @idempotent(key: $idempotencyKey) {
    inventoryLevel {
      id
    }
    userErrors {
      field
      message
    }
  }
}
"""

_INVENTORY_ITEM_UPDATE_MUTATION = """
mutation MarketplaceInventoryItemTrack($id: ID!, $input: InventoryItemInput!) {
  inventoryItemUpdate(id: $id, input: $input) {
    inventoryItem {
      id
      tracked
    }
    userErrors {
      field
      message
    }
  }
}
"""


APPLIED_TRACKING = "tracking"
APPLIED_ACTIVATION = "activation"
_APPLIED_NOTES = {
	APPLIED_TRACKING: "Inventory tracking was already enabled on Shopify for this item before this error.",
	APPLIED_ACTIVATION: "The item was already activated at the location on Shopify before this error.",
}


class _EntryFailed(Exception):
	"""One mapping could not be prepared; carries the message to persist."""


def _as_account(value):
	if getattr(value, "doctype", None) == "Marketplace Account":
		return value
	return frappe.get_doc("Marketplace Account", value)


def validate_inventory_account(account):
	"""Return ``(account_doc, connection)`` fully validated for inventory sync.

	Runs ``_validate_inventory_account_local()``'s fast, non-network checks and
	THEN the Location use-time re-check (a live read-only Shopify call). Only
	the background worker calls this full version; the Desk request path stops
	at the local half - see the module docstring.
	"""
	account_doc, connection = _validate_inventory_account_local(account)
	_validate_location_in_use(account_doc, connection)
	return account_doc, connection


def _validate_inventory_account_local(account):
	"""Return ``(account_doc, connection)`` checking only local platform /
	connection / enabled / Location-set / Warehouse configuration - no
	network call."""
	account_doc = _as_account(account)
	if account_doc.platform != "Shopify":
		frappe.throw(_("Marketplace Account {0} is not a Shopify account.").format(account_doc.name))
	if account_doc.connection_doctype != "Shopify Connection" or not account_doc.connection:
		frappe.throw(_("Marketplace Account {0} does not use a Shopify Connection.").format(account_doc.name))
	if not account_doc.enabled:
		frappe.throw(_("Marketplace Account {0} is disabled.").format(account_doc.name))
	if not cstr(account_doc.shopify_location_gid).strip():
		frappe.throw(
			_("Marketplace Account {0} requires a Shopify Location for inventory sync.").format(
				account_doc.name
			)
		)
	validate_account_warehouse(account_doc, context=_("Shopify inventory sync"))

	connection = frappe.get_doc("Shopify Connection", account_doc.connection)
	if not connection.enabled:
		frappe.throw(_("Shopify Connection {0} is disabled.").format(connection.name))
	return account_doc, connection


def _validate_location_in_use(account_doc, connection):
	"""Use-time re-check (decision 10): the Location is only validated when
	the Account is saved, so confirm it is still an active Location of the shop
	before writing to it. One live read-only call per job (worker only)."""
	info = fetch_shop_and_active_locations(connection.name)
	active_ids = {location.get("id") for location in info["active_locations"]}
	if account_doc.shopify_location_gid not in active_ids:
		frappe.throw(
			_("Shopify Location {0} is no longer an active Location on Connection {1}.").format(
				account_doc.shopify_location_gid, connection.name
			)
		)


# --- Desk entry points (no Shopify HTTP call in any of these) ---------------


def queue_inventory_sync(mapping_names):
	"""Authorize and enqueue a Shopify inventory sync for the given Marketplace
	Product Mapping names, one background job per affected Marketplace Account.

	Request path: write-permission checks on every mapping and its Account
	(``services/mapping_push.py``'s ``group_mappings_by_account()``, unchanged
	from #106) and ``_validate_inventory_account_local()`` only. **No Shopify
	HTTP call happens here** - the Location re-check and every read/mutation run
	in the worker (``sync_account_inventory()``).

	Per-account tolerant: a misconfigured account (or one whose job is already
	running) goes into ``skipped_accounts`` with its real reason while every
	valid account in the same request still queues. Returns
	``{"jobs": [...], "skipped_accounts": [...]}``.
	"""
	mapping_names = parse_mapping_names(mapping_names)
	accounts = group_mappings_by_account(mapping_names)

	skipped_accounts = []
	valid_accounts = {}
	for account_name, selected in accounts.items():
		try:
			_validate_inventory_account_local(selected["account"])
		except frappe.ValidationError as error:
			frappe.clear_last_message()
			skipped_accounts.append(
				{"account": account_name, "reason": _config_error_message(error, account_name)}
			)
			continue
		valid_accounts[account_name] = selected["mapping_names"]

	return _enqueue_accounts(valid_accounts, skipped_accounts)


def queue_account_reconciliation(account_name):
	"""Authorize and enqueue a full Shopify inventory reconciliation for one
	Marketplace Account (every enabled mapping, no selection). Same return
	shape and same no-HTTP-in-the-request guarantee as ``queue_inventory_sync()``.
	"""
	account_doc = frappe.get_doc("Marketplace Account", account_name)
	account_doc.check_permission("write")
	# The worker writes the mappings' inventory status fields on the user's
	# behalf, so require write on that doctype too.
	frappe.has_permission(MAPPING_DOCTYPE, "write", throw=True)

	skipped_accounts = []
	valid_accounts = {}
	try:
		_validate_inventory_account_local(account_doc)
	except frappe.ValidationError as error:
		frappe.clear_last_message()
		skipped_accounts.append(
			{"account": account_doc.name, "reason": _config_error_message(error, account_doc.name)}
		)
	else:
		valid_accounts[account_doc.name] = None
	return _enqueue_accounts(valid_accounts, skipped_accounts)


def _enqueue_accounts(valid_accounts, skipped_accounts):
	"""Enqueue every valid account, never discarding jobs already enqueued
	because a later account is rejected. ``valid_accounts`` maps account name to
	its mapping names (``None`` = the whole account)."""
	owner = frappe.session.user
	jobs = []
	for account_name, mapping_names in valid_accounts.items():
		enqueued, job_id = enqueue_account_sync(account_name, mapping_names, owner)
		if not enqueued:
			skipped_accounts.append(
				{
					"account": account_name,
					"reason": _("An inventory sync is already running for this Marketplace Account."),
				}
			)
			continue
		jobs.append(
			{
				"account": account_name,
				"job_id": job_id,
				"mapping_count": len(mapping_names) if mapping_names is not None else None,
			}
		)
	return {"jobs": jobs, "skipped_accounts": skipped_accounts}


def enqueue_account_sync(account_name, mapping_names=None, owner=None):
	"""Enqueue one deduplicated inventory-sync job for an account and return
	``(enqueued, job_id)``: ``enqueued`` is False if a job for that account is
	already queued/running (RQ dedup); ``job_id`` is the status job id (``None``
	when there is no ``owner``).

	The only place a sync job is enqueued - the manual actions call it, and
	PR2's ``sweep_changed_bins()`` / ``reconcile_all_accounts()`` call it too
	(they only enqueue). ``owner`` is the Desk user for owner-scoped status;
	without one no status entry/realtime event is created.
	"""
	job_id = frappe.generate_hash(length=16) if owner else None
	if job_id:
		# Seed the "queued" entry BEFORE enqueueing so a fast worker can't
		# publish "running" first (same reasoning as #105/#106).
		_set_status(job_id, {"owner": owner, "status": "queued", "processed": 0})
	job = frappe.enqueue(
		sync_account_inventory,
		queue="long",
		timeout=3600,
		job_id=f"{_SYNC_RQ_JOB_ID_PREFIX}-{account_name}",
		deduplicate=True,
		job_id_for_result=job_id,
		owner=owner,
		account_name=account_name,
		mapping_names=mapping_names,
	)
	if job is None:
		if job_id:
			frappe.cache().delete_value(_status_cache_key(job_id))
		return False, None
	return True, job_id


def get_sync_status(job_id):
	state = frappe.cache().get_value(_status_cache_key(job_id))
	if not state or state.get("owner") != frappe.session.user:
		frappe.throw(_("Inventory sync job not found."), frappe.PermissionError)
	return {key: value for key, value in state.items() if key != "owner"}


# --- Worker -----------------------------------------------------------------


def sync_account_inventory(account_name, mapping_names=None, job_id_for_result=None, owner=None):
	"""Background job body (the worker): push ERPNext stock to Shopify ``on_hand``
	for every given mapping (``None`` = every enabled mapping of the account).

	Returns the result payload. Status/progress is written only when a
	``job_id_for_result`` is given. See the module docstring for the shape.
	"""
	frappe.flags.shopify_inventory_logged_failures = 0
	frappe.flags.shopify_inventory_suppressed_failures = 0
	# Flips to True right before the first per-mapping/per-chunk outcome could
	# ever be persisted. An exception raised while this is still False (e.g.
	# validate_inventory_account()'s Location re-check hitting a deactivated
	# Location or a connection/network error) means no mapping has its own
	# outcome recorded anywhere - without the except-block fallback below,
	# every mapping this job would have touched would silently keep showing
	# its previous (possibly stale) inventory_sync_status forever.
	mapping_processing_started = False
	try:
		_report(job_id_for_result, owner, status="running", processed=0, result=_result_payload([]))
		account_doc, connection = validate_inventory_account(account_name)
		client = ShopifyClient(connection.name)
		location_id = account_doc.shopify_location_gid

		mappings = _mappings_for_sync(account_doc.name, mapping_names)
		found_names = {mapping.name for mapping in mappings}
		item_by_name = _fetch_items({mapping.item for mapping in mappings if mapping.item})
		mappings, results = _split_active_mappings(mappings, item_by_name)
		# Selected mappings deleted / moved to another account after queueing:
		# report each so the job's totals reconcile with the queued count.
		for missing_name in dict.fromkeys(mapping_names or []):
			if missing_name not in found_names:
				results.append(
					{
						"mapping_name": missing_name,
						"ok": False,
						"error": cstr(_("Mapping no longer exists or was moved to another account.")),
					}
				)
		# Commit before any HTTP call so no write lock is held across it.
		frappe.db.commit()

		mapping_processing_started = True
		# Local (non-network) problems first, persisted as one batch.
		syncable = []
		local_outcomes = {}
		local_entries = []
		for mapping in mappings:
			problem = _local_problem(mapping, item_by_name)
			if problem:
				local_entries.append(frappe._dict(name=mapping.name))
				local_outcomes[mapping.name] = (False, problem)
			else:
				syncable.append(mapping)
		results.extend(_persist_chunk(local_entries, local_outcomes))
		_report(
			job_id_for_result,
			owner,
			status="running",
			processed=len(results),
			result=_result_payload(results),
		)

		chunk_size = SHOPIFY_INVENTORY_SYNC_CHUNK_SIZE
		for chunk_index, chunk_start in enumerate(range(0, len(syncable), chunk_size), start=1):
			chunk = syncable[chunk_start : chunk_start + chunk_size]
			results.extend(_sync_chunk(client, account_doc, location_id, chunk, item_by_name))
			if chunk_index % INVENTORY_PROGRESS_UPDATE_INTERVAL == 0:
				_report(
					job_id_for_result,
					owner,
					status="running",
					processed=len(results),
					result=_result_payload(results),
				)

		payload = _result_payload(results)
		_report(job_id_for_result, owner, status="completed", processed=len(results), result=payload)
		return payload
	except Exception as exc:
		_report(job_id_for_result, owner, status="failed", error=str(exc))
		if not mapping_processing_started:
			try:
				_record_whole_job_failure(account_name, mapping_names, exc)
			except Exception:
				# Never let a failure in the fallback itself mask/replace the
				# real exception being re-raised below (see this module's
				# docstring's "Status" bullet).
				frappe.log_error(
					title="Failed to record whole-job sync failure", message=frappe.get_traceback()
				)
		raise
	finally:
		_log_suppressed_failures()


def _record_whole_job_failure(account_name, mapping_names, exc):
	"""Bulk-write a job-level error onto every mapping this job would ACTUALLY
	have touched, for the one failure mode nothing else covers: a whole-job
	setup step (``validate_inventory_account()``, the Location re-check, ...)
	raising BEFORE the per-mapping/per-chunk loop starts - so no mapping
	already got its own outcome recorded via ``_mark_error()``/``_persist_chunk()``
	(see ``sync_account_inventory()``'s ``mapping_processing_started`` guard,
	which ensures this is never called once real per-mapping processing has
	begun). Without this, those mappings would keep showing whatever
	``inventory_sync_status`` they already had - often ``Synced``, with an
	aging ``inventory_last_synced_on`` - drifting silently except for the
	Error Log entry the caller's own ``except`` block already produces.

	"Would actually have touched" is computed by re-running the exact same
	classification the worker itself uses (``_mappings_for_sync()`` ->
	``_fetch_items()`` -> ``_split_active_mappings()``), scoped to this
	account. A skip-class mapping (disabled mapping, disabled Item, non-stock
	Item, template Item) never gets a status write from a normal run, so this
	fallback must not write one either - or a stale ``Error`` would persist
	forever, since a later successful run silently skips it with no write to
	clear it. Scoping through ``_mappings_for_sync()`` (rather than a bare
	``name in names`` filter) also means a mapping moved to another account
	between enqueue and a setup failure is naturally excluded, the same as the
	worker's own "no longer exists or was moved" handling.

	One bulk ``frappe.db.set_value()`` UPDATE (matched by name), never a
	``frappe.get_doc()`` per mapping - same style as
	``_mark_error()``/``_mark_synced()``. Explicitly committed here since this
	runs inside the worker's own ``except`` block, right before re-raising -
	``frappe.utils.background_jobs.execute_job()`` rolls back the transaction
	on an unhandled exception, which would otherwise silently discard this
	write too."""
	mappings = _mappings_for_sync(account_name, mapping_names)
	item_by_name = _fetch_items({mapping.item for mapping in mappings if mapping.item})
	active_mappings, _skipped = _split_active_mappings(mappings, item_by_name)
	names = [mapping.name for mapping in active_mappings]
	if not names:
		return
	frappe.db.set_value(
		MAPPING_DOCTYPE,
		{"name": ["in", names]},
		{
			"inventory_sync_status": "Error",
			"inventory_last_error": _whole_job_failure_message(exc),
		},
		update_modified=False,
	)
	frappe.db.commit()


def _whole_job_failure_message(exc):
	"""User-facing text for a whole-job setup failure (decision 3): the
	exception's own text only for ``frappe.ValidationError`` - a validation
	message is already meant to be shown to a mapping viewer (e.g. "Location
	is no longer active") - otherwise a generic message, since an unexpected
	exception's raw text can carry a Shopify response fragment, an internal DB
	error, or anything else not meant for that audience. Full detail always
	still goes to the Error Log via the caller's own ``except`` block
	regardless of which message is chosen here."""
	if isinstance(exc, frappe.ValidationError):
		return cstr(exc).strip()[:ERROR_MESSAGE_MAX_LENGTH]
	return cstr(_("Sync failed, see Error Log for details."))


def _sync_chunk(client, account_doc, location_id, mappings, item_by_name):
	"""Sync one chunk of mappings and persist every outcome together under one
	savepoint/commit."""
	item_codes = [item_by_name[mapping.item].name for mapping in mappings if mapping.item in item_by_name]
	quantities = get_stock_quantities(account_doc.warehouse, item_codes)
	entries = [
		frappe._dict(
			name=mapping.name,
			variant_gid=mapping.external_reference,
			target=quantities[mapping.item],
			# Store-side changes already made for this entry (see _prepare_entry).
			applied=[],
		)
		for mapping in mappings
	]
	# Commit before HTTP so no write lock is held across it.
	frappe.db.commit()

	outcomes = {}
	_sync_entries(client, location_id, entries, outcomes)
	for entry in entries:
		# Safety net: every entry must end with an outcome.
		outcomes.setdefault(entry.name, (False, REMOTE_ERROR))
		ok, error = outcomes[entry.name]
		if not ok and entry.applied:
			outcomes[entry.name] = (False, _with_applied_note(error, entry.applied))
	return _persist_chunk(entries, outcomes)


def _sync_entries(client, location_id, entries, outcomes, allow_fallback=True):
	"""Read -> prepare -> set for ``entries`` (each ``name, variant_gid,
	target``), recording ``outcomes[name] = (ok, error)``. Never raises for a
	remote problem."""
	pending = list(entries)
	stale_retries = 0
	while pending:
		try:
			state = _read_inventory_state(client, location_id, [entry.variant_gid for entry in pending])
		except Exception:
			_log_failure("read")
			for entry in pending:
				outcomes[entry.name] = (False, REMOTE_ERROR)
			return

		to_set = []
		for entry in pending:
			node = state.get(entry.variant_gid)
			if node is None:
				outcomes[entry.name] = (False, _("The Shopify variant or its inventory item was not found."))
				continue
			try:
				compare = _prepare_entry(client, location_id, entry, node)
			except _EntryFailed as failure:
				outcomes[entry.name] = (False, cstr(failure))
				continue
			except Exception:
				_log_failure("prepare")
				outcomes[entry.name] = (False, REMOTE_ERROR)
				continue
			if compare is None:
				outcomes[entry.name] = (True, None)  # already equal: no mutation
				continue
			to_set.append((entry, node["inventory_item_id"], compare))
		if not to_set:
			return

		try:
			user_errors = _set_quantities(client, location_id, to_set)
		except Exception:
			_log_failure("set")
			for entry, _item_id, _compare in to_set:
				outcomes[entry.name] = (False, REMOTE_ERROR)
			return

		if not user_errors:
			for entry, _item_id, _compare in to_set:
				outcomes[entry.name] = (True, None)
			return

		if any(
			isinstance(error, dict) and error.get("code") == SHOPIFY_INVENTORY_STALE_COMPARE_CODE
			for error in user_errors
		):
			if stale_retries >= SHOPIFY_INVENTORY_MAX_STALE_RETRIES:
				message = _("Shopify inventory kept changing; gave up after {0} retries.").format(
					SHOPIFY_INVENTORY_MAX_STALE_RETRIES
				)
				for entry, _item_id, _compare in to_set:
					outcomes[entry.name] = (False, message)
				return
			stale_retries += 1
			pending = [entry for entry, _item_id, _compare in to_set]
			continue

		if allow_fallback and len(to_set) > 1:
			# Isolate the bad item: resend one at a time (no nested fallback).
			for entry, _item_id, _compare in to_set:
				_sync_entries(client, location_id, [entry], outcomes, allow_fallback=False)
			return

		message = _format_user_errors(user_errors)
		for entry, _item_id, _compare in to_set:
			outcomes[entry.name] = (False, message)
		return


def _read_inventory_state(client, location_id, variant_gids):
	"""Return ``{variant_gid: {inventory_item_id, tracked, has_level,
	on_hand}}`` for every variant that resolved with an inventory item. Matched
	by each node's own ``id`` (response order isn't guaranteed)."""
	data = client.execute(
		_INVENTORY_READ_QUERY,
		{"ids": list(variant_gids), "locationId": location_id, "names": [SHOPIFY_INVENTORY_QUANTITY_NAME]},
	)
	state = {}
	for node in (data or {}).get("nodes") or []:
		if not isinstance(node, dict) or not node.get("id"):
			continue
		inventory_item = node.get("inventoryItem")
		if not isinstance(inventory_item, dict) or not inventory_item.get("id"):
			continue
		level = inventory_item.get("inventoryLevel")
		on_hand = None
		if isinstance(level, dict):
			for quantity in level.get("quantities") or []:
				if (
					isinstance(quantity, dict)
					and quantity.get("name") == SHOPIFY_INVENTORY_QUANTITY_NAME
					and isinstance(quantity.get("quantity"), int)
					and not isinstance(quantity.get("quantity"), bool)
				):
					on_hand = quantity["quantity"]
		state[node["id"]] = {
			"inventory_item_id": inventory_item["id"],
			"tracked": bool(inventory_item.get("tracked")),
			"has_level": isinstance(level, dict),
			"on_hand": on_hand,
		}
	return state


def _prepare_entry(client, location_id, entry, node):
	"""Make the inventory item settable and return the compare value to send,
	or ``None`` if Shopify already equals the target (nothing to do).

	Untracked -> ``inventoryItemUpdate(tracked: true)``; no level at the
	location -> ``inventoryActivate`` (compare value 0). Raises ``_EntryFailed``
	with a message for a userError or an unreadable current quantity.
	"""
	flipped = False
	if not node["tracked"]:
		_run_mutation(
			client,
			_INVENTORY_ITEM_UPDATE_MUTATION,
			{"id": node["inventory_item_id"], "input": {"tracked": True}},
			"inventoryItemUpdate",
			idempotent=False,
		)
		entry.applied.append(APPLIED_TRACKING)
		flipped = True

	if not node["has_level"]:
		_run_mutation(
			client,
			_INVENTORY_ACTIVATE_MUTATION,
			{"inventoryItemId": node["inventory_item_id"], "locationId": location_id},
			"inventoryActivate",
		)
		entry.applied.append(APPLIED_ACTIVATION)
		return 0

	on_hand = node["on_hand"]
	if on_hand is None:
		if flipped:
			# Tracking was just enabled; the pre-flip read may not carry a
			# quantity. Guess 0 - a wrong guess is COMPARE_FAILED, which the
			# whole-chunk re-read recovers.
			return 0
		raise _EntryFailed(_("Could not read the current Shopify quantity at the bound Location."))
	if not flipped and on_hand == entry.target:
		return None
	return on_hand


def _set_quantities(client, location_id, to_set):
	"""Send one ``inventorySetQuantities`` for ``[(entry, inventory_item_id,
	compare), ...]`` and return the payload's ``userErrors`` list (empty on
	success). A fresh idempotency key per call."""
	quantities = [
		{
			"inventoryItemId": inventory_item_id,
			"locationId": location_id,
			"quantity": entry.target,
			"changeFromQuantity": compare,
		}
		for entry, inventory_item_id, compare in to_set
	]
	variables = {
		"input": {
			"name": SHOPIFY_INVENTORY_QUANTITY_NAME,
			"reason": SHOPIFY_INVENTORY_REASON,
			"referenceDocumentUri": SHOPIFY_INVENTORY_REFERENCE_DOCUMENT_URI,
			"quantities": quantities,
		},
		"idempotencyKey": _new_idempotency_key(),
	}
	response = client.execute(_INVENTORY_SET_MUTATION, variables)
	payload = (response or {}).get("inventorySetQuantities")
	if not isinstance(payload, dict):
		return [{"message": cstr(REMOTE_ERROR)}]
	return payload.get("userErrors") or []


def _run_mutation(client, query, variables, field, idempotent=True):
	"""Run a prep mutation; raise ``_EntryFailed`` on a userError. ``idempotent``
	adds a fresh idempotency key variable."""
	if idempotent:
		variables = {**variables, "idempotencyKey": _new_idempotency_key()}
	response = client.execute(query, variables)
	payload = (response or {}).get(field)
	if not isinstance(payload, dict):
		raise _EntryFailed(REMOTE_ERROR)
	user_errors = payload.get("userErrors") or []
	if user_errors:
		raise _EntryFailed(_format_user_errors(user_errors))


def _with_applied_note(error, applied):
	"""Append a fixed clause saying which store-side changes had already been
	made, keeping the whole message within ERROR_MESSAGE_MAX_LENGTH (the error
	text is truncated first so the clause is never cut)."""
	note = " ".join(_(_APPLIED_NOTES[change]) for change in applied)
	room = max(ERROR_MESSAGE_MAX_LENGTH - len(note) - 1, 0)
	return f"{cstr(error).strip()[:room]} {note}".strip()


def _new_idempotency_key():
	return str(uuid.uuid4())


# --- Resolution / persistence -----------------------------------------------


def _mappings_for_sync(account_name, mapping_names):
	"""Every needed mapping field in ONE query. ``mapping_names=None`` means the
	whole account's enabled mappings (reconciliation)."""
	filters = {"marketplace_account": account_name}
	if mapping_names is None:
		filters["enabled"] = 1
	else:
		filters["name"] = ["in", list(mapping_names)]
	return frappe.get_all(
		MAPPING_DOCTYPE,
		filters=filters,
		fields=["name", "item", "enabled", "external_reference"],
		order_by="name asc",
	)


def _fetch_items(item_names):
	"""``{item_name: row(name, disabled, is_stock_item, has_variants)}`` for
	every Item in ONE query."""
	if not item_names:
		return {}
	rows = frappe.get_all(
		"Item",
		filters={"name": ["in", list(item_names)]},
		fields=["name", "disabled", "is_stock_item", "has_variants"],
	)
	return {row.name: row for row in rows}


def _split_active_mappings(mappings, item_by_name):
	"""Return ``(active_mappings, skipped_results)``. A disabled mapping, one
	whose Item is disabled, one whose Item is not a stock Item
	(``is_stock_item = 0`` - a missing Bin would read 0 and enable tracking /
	sell out a deliberately untracked variant), or one whose Item is a template
	(``has_variants = 1`` - ERPNext defaults ``is_stock_item`` to 1 even for a
	template and refuses to post stock against it directly, so it has no Bin
	either) gets no mutation and no status write, and is reported as skipped
	(never Error) - in explicit selections and full reconciliation alike. A
	mapping whose Item no longer exists is kept - it surfaces as a local error."""
	active = []
	skipped_results = []
	for mapping in mappings:
		item = item_by_name.get(mapping.item)
		reason = None
		if not mapping.enabled:
			reason = _("Mapping is disabled.")
		elif item and item.disabled:
			reason = _("Item is disabled.")
		elif item and not item.is_stock_item:
			reason = _("Item is not a stock Item.")
		elif item and item.has_variants:
			reason = _("Item is a template (has variants).")
		if reason:
			skipped_results.append(
				{"mapping_name": mapping.name, "ok": False, "skipped": True, "error": cstr(reason)}
			)
		else:
			active.append(mapping)
	return active, skipped_results


def _local_problem(mapping, item_by_name):
	"""Return an error message for a mapping that can never reach Shopify, else
	``None``."""
	if mapping.item not in item_by_name:
		return _("Item {0} does not exist.").format(mapping.item)
	if not _is_variant_gid(mapping.external_reference):
		return _("Marketplace Product Mapping {0} does not have a valid Shopify product variant GID.").format(
			mapping.name
		)
	return None


def _persist_chunk(entries, outcomes):
	"""Write every entry's outcome under one savepoint, then commit - a later
	chunk's failure can't roll this chunk back. Returns the result rows."""
	if not entries:
		return []
	savepoint = "shopify_inventory_sync_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(savepoint)
	try:
		for entry in entries:
			ok, error = outcomes[entry.name]
			if ok:
				_mark_synced(entry.name)
			else:
				_mark_error(entry.name, error)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		_log_failure("write")
		# Best effort: never leave a stale status while the job reports Error.
		for entry in entries:
			try:
				_mark_error(entry.name, REMOTE_ERROR)
			except Exception:
				pass
		frappe.db.commit()
		return [{"mapping_name": entry.name, "ok": False, "error": cstr(REMOTE_ERROR)} for entry in entries]
	else:
		frappe.db.release_savepoint(savepoint)
		frappe.db.commit()

	rows = []
	for entry in entries:
		ok, error = outcomes[entry.name]
		rows.append({"mapping_name": entry.name, "ok": ok, **({} if ok else {"error": cstr(error)})})
	return rows


def _mark_synced(mapping_name):
	frappe.db.set_value(
		MAPPING_DOCTYPE,
		mapping_name,
		{
			"inventory_sync_status": "Synced",
			"inventory_last_synced_on": now_datetime(),
			"inventory_last_error": None,
		},
		update_modified=False,
	)


def _mark_error(mapping_name, error):
	frappe.db.set_value(
		MAPPING_DOCTYPE,
		mapping_name,
		{"inventory_sync_status": "Error", "inventory_last_error": cstr(error).strip()},
		update_modified=False,
	)


def _format_user_errors(user_errors):
	messages = []
	for error in user_errors:
		if isinstance(error, dict):
			message = cstr(error.get("message")).strip()
			if message:
				messages.append(message)
	combined = "; ".join(messages)
	if not combined:
		return cstr(REMOTE_ERROR)
	return combined[:ERROR_MESSAGE_MAX_LENGTH]


def _config_error_message(error, account_name):
	message = cstr(error).strip()
	return message or _("Marketplace Account {0} is not configured for Shopify inventory sync.").format(
		account_name
	)


def _log_failure(stage):
	# Per-mapping errors never echo a raw remote body (same rule as #106);
	# the detail goes to the Error Log instead - capped per job so a throttled
	# full reconciliation can't flood it (see _log_suppressed_failures()).
	logged = frappe.flags.get("shopify_inventory_logged_failures") or 0
	if logged >= MAX_ERROR_LOG_ENTRIES_PER_JOB:
		frappe.flags.shopify_inventory_suppressed_failures = (
			frappe.flags.get("shopify_inventory_suppressed_failures") or 0
		) + 1
		return
	frappe.flags.shopify_inventory_logged_failures = logged + 1
	frappe.log_error(title=f"Shopify inventory sync: {stage} failed", message=frappe.get_traceback())


def _log_suppressed_failures():
	suppressed = frappe.flags.get("shopify_inventory_suppressed_failures") or 0
	if not suppressed:
		return
	frappe.flags.shopify_inventory_suppressed_failures = 0
	frappe.log_error(
		title="Shopify inventory sync: more failures suppressed",
		message=f"...and {suppressed} more failures suppressed (only the first "
		f"{MAX_ERROR_LOG_ENTRIES_PER_JOB} are logged in detail per job).",
	)


# --- PR2: automatic triggers (scheduled sweep + full reconciliation) --------
#
# Both entry points below are the ONLY things hooks.py's scheduler_events
# calls for #107. Neither makes a Shopify HTTP call - they only read Bins /
# Marketplace Product Mappings / the per-account watermark (frappe.cache())
# and call enqueue_account_sync() (this module's single enqueue choke point).
# All actual Shopify reads/mutations happen only in sync_account_inventory(),
# exactly as for the manual actions.
#
# Watermark: one frappe.cache() key per account, holding the wall-clock time
# (a python datetime - RedisWrapper pickles values, same as the job-status
# dicts cached above) up to which sweep_changed_bins() has already looked for
# Bin changes on that account. Deliberately no TTL - an account's watermark
# is meant to persist indefinitely, not expire like the short-lived
# per-job-run status cache entries above; a lost/evicted entry (e.g. a
# redis-cache restart/flush) is handled by the missing-watermark fallback
# below, not by TTL tuning.
#
# Missing-watermark fallback (design decision the issue leaves open - see the
# PR2 report): treat a missing watermark (first-ever sweep tick for that
# account, or a lost/evicted cache entry) as "look back
# SHOPIFY_INVENTORY_SWEEP_OVERLAP_MINUTES from now", NOT as "run one full
# account sync". A full sync triggered by every cache eviction would make the
# sweep's own Shopify write volume depend on Redis eviction behavior rather
# than on real ERPNext stock changes, and reconcile_all_accounts() (decision
# 2) already exists as the "watermark lost/drifted" recovery path - a second,
# sweep-triggered full sync would duplicate that safety net for no benefit.


_EXCLUDED_ACCOUNTS_LOG_CACHE_KEY = "marketplace_integrations:shopify:inventory_reconcile_excluded_accounts_logged_date"


def _log_excluded_accounts_once_per_day(excluded):
	"""Write ONE Error Log entry (one line per excluded account) the first
	time ``reconcile_all_accounts()`` runs with a non-empty ``excluded`` list
	on a given calendar day - a ``frappe.cache()`` guard (no TTL, keyed by
	date string) rather than per-tick logging, since the daily reconciliation
	could in principle be triggered more than once on the same day and this
	must not turn into per-run noise (decision 6)."""
	if not excluded:
		return
	today = now_datetime().date().isoformat()
	if frappe.cache().get_value(_EXCLUDED_ACCOUNTS_LOG_CACHE_KEY) == today:
		return
	frappe.cache().set_value(_EXCLUDED_ACCOUNTS_LOG_CACHE_KEY, today)
	lines = "\n".join(f"{name}: {reason}" for name, reason in excluded)
	frappe.log_error(
		title="Shopify inventory reconciliation: accounts excluded from automatic sync",
		message=(
			"The following enabled Shopify Marketplace Accounts are excluded from "
			f"automatic inventory sync (misconfigured):\n{lines}"
		),
	)


def _watermark_cache_key(account_name):
	return f"marketplace_integrations:shopify:inventory_sweep_watermark:{account_name}"


def _sweep_since(account_name, tick_time):
	"""Return the ``Bin.modified`` lower bound to sweep from for this
	account: the stored watermark, or (missing watermark) ``tick_time``
	itself - either way minus the overlap window (decision 1's "start at
	10" minutes)."""
	watermark = frappe.cache().get_value(_watermark_cache_key(account_name))
	base = watermark or tick_time
	return add_to_date(base, minutes=-SHOPIFY_INVENTORY_SWEEP_OVERLAP_MINUTES)


def _advance_watermark(account_name, tick_time):
	frappe.cache().set_value(_watermark_cache_key(account_name), tick_time)


def _enabled_shopify_accounts():
	"""Bulk-filtered Marketplace Accounts a scheduled job may enqueue for -
	see ``_enabled_shopify_accounts_with_exclusions()`` for the filter
	criteria. Used by ``sweep_changed_bins()``, which stays silent about a
	misconfigured account (no user to show a skip reason to on a tick this
	frequent) - ``reconcile_all_accounts()`` uses the exclusions-reporting
	variant instead so a misconfiguration gets a once-daily Error Log entry
	(decision 6)."""
	accounts, _excluded = _enabled_shopify_accounts_with_exclusions()
	return accounts


def _enabled_shopify_accounts_with_exclusions():
	"""Return ``(usable_accounts, excluded)`` where ``excluded`` is
	``[(account_name, reason), ...]`` for every enabled Shopify Marketplace
	Account that exists but fails a config check: Shopify platform, enabled,
	using an enabled Shopify Connection, with both a Warehouse and a Shopify
	Location configured, AND a Warehouse that would actually pass
	``validate_account_warehouse()`` (not a group / disabled Warehouse, and
	not one belonging to a different Company than the account's own
	``erpnext_company``). This last check mirrors the worker's own validation
	(``sync_account_inventory()`` -> ``validate_inventory_account()`` ->
	``validate_account_warehouse()``) so a misconfigured account is excluded
	HERE, before any enqueue, rather than getting enqueued by every sweep tick
	and failing in the worker every time (previously a real gap: this filter
	only checked "Warehouse is set", not "Warehouse is usable"). Deliberately
	no result-count limit - see constants.py's note next to
	SHOPIFY_INVENTORY_SWEEP_OVERLAP_MINUTES on why an ``ORDER BY ... LIMIT``
	here would starve accounts rather than bound a backlog."""
	accounts = frappe.get_all(
		"Marketplace Account",
		filters={"platform": "Shopify", "enabled": 1, "connection_doctype": "Shopify Connection"},
		fields=["name", "warehouse", "shopify_location_gid", "connection"],
	)
	if not accounts:
		return [], []

	enabled_connections = set(
		frappe.get_all(
			"Shopify Connection",
			filters={
				"name": ["in", [account.connection for account in accounts if account.connection]],
				"enabled": 1,
			},
			pluck="name",
		)
	)

	usable = []
	excluded = []
	for account in accounts:
		if not account.warehouse:
			excluded.append((account.name, _("No Warehouse configured.")))
		elif not cstr(account.shopify_location_gid).strip():
			excluded.append((account.name, _("No Shopify Location configured.")))
		elif not account.connection or account.connection not in enabled_connections:
			excluded.append((account.name, _("Shopify Connection is disabled.")))
		elif not _has_usable_warehouse(account):
			excluded.append(
				(account.name, _("Warehouse is disabled, a group, or belongs to a different Company."))
			)
		else:
			usable.append(account)
	return usable, excluded


def _has_usable_warehouse(account):
	"""Wrap the shared ``validate_account_warehouse()`` check (never
	duplicated) as a bool for a scheduler callback, which has no user to
	``frappe.throw()`` an error message at. Does one extra
	``frappe.get_doc()`` per candidate account - acceptable since this
	project's Shopify accounts are expected to stay a handful (see
	constants.py's note on why the sweep/reconcile account list has no
	result-count limit either)."""
	try:
		validate_account_warehouse(_as_account(account.name), context=_("Shopify inventory sync"))
	except frappe.ValidationError:
		return False
	return True


def sweep_changed_bins():
	"""Scheduled sweep (``constants.SHOPIFY_INVENTORY_SWEEP_CRON``, see
	hooks.py's ``scheduler_events["cron"]``) that coalesces relevant ERPNext
	stock changes by account and variant (decision 1): for every enabled
	Shopify Marketplace Account, find Bins in its Warehouse whose
	``modified`` is newer than that account's watermark (minus the overlap
	window) AND whose Item is an enabled mapping on that account, and
	enqueue ONE deduplicated inventory-sync job per account scoped to just
	those mappings' names.

	Cheap by construction: only Bin / Marketplace Product Mapping /
	watermark reads plus ``enqueue_account_sync()`` (itself just an RQ
	enqueue) - no Shopify HTTP call happens anywhere in this function or
	anything it calls. The per-account RQ dedup job id
	(``_SYNC_RQ_JOB_ID_PREFIX-{account}``) means a tick for an account whose
	previous job is still running is a no-op, not a pile-up - confirmed by
	``enqueue_account_sync()``'s own ``deduplicate=True`` call.

	The watermark is advanced to this tick's own start time for every
	account whose Bin query succeeded AND whose enqueue (if one was
	attempted) was accepted - i.e. whenever nothing changed, or a change was
	found and ``enqueue_account_sync()`` returned ``enqueued=True``. It is
	deliberately NOT advanced when ``enqueue_account_sync()`` rejects the
	enqueue (RQ dedup: a sync for this account is already queued/running) -
	advancing it anyway would let a change that occurred while the account
	was busy fall outside the next tick's overlap window and go stale until
	the next scheduled full reconciliation. Held back this way, the next
	tick naturally re-reads from the old watermark and re-discovers the same
	change once the busy job finishes.

	One account's failure (a bad Warehouse row, a DB error) is isolated
	(logged, not raised) so it can't stop every other account's tick.
	"""
	tick_time = now_datetime()
	for account in _enabled_shopify_accounts():
		try:
			_sweep_account(account, tick_time)
		except Exception:
			frappe.log_error(
				title="Shopify inventory sweep: account failed", message=frappe.get_traceback()
			)


def _sweep_account(account, tick_time):
	since = _sweep_since(account.name, tick_time)
	mappings = frappe.get_all(
		"Marketplace Product Mapping",
		filters={"marketplace_account": account.name, "enabled": 1},
		fields=["name", "item"],
	)
	should_advance = True
	if mappings:
		mapping_names_by_item = {}
		for mapping in mappings:
			mapping_names_by_item.setdefault(mapping.item, []).append(mapping.name)

		changed_items = frappe.get_all(
			"Bin",
			filters={
				"warehouse": account.warehouse,
				"item_code": ["in", list(mapping_names_by_item.keys())],
				"modified": [">", since],
			},
			pluck="item_code",
		)
		affected_mapping_names = sorted(
			{name for item_code in set(changed_items) for name in mapping_names_by_item.get(item_code, [])}
		)
		if affected_mapping_names:
			enqueued, _job_id = enqueue_account_sync(account.name, affected_mapping_names)
			# Dedup rejection (a sync for this account is already
			# queued/running): hold the watermark back so the next tick
			# re-covers this same span instead of losing the change - see
			# this function's own docstring.
			should_advance = enqueued

	# Advance whenever nothing changed, or a change was found and its
	# enqueue was accepted - never on a dedup-rejected enqueue. See the
	# module-level note on why this is safe.
	if should_advance:
		_advance_watermark(account.name, tick_time)


def reconcile_all_accounts():
	"""Scheduled daily full reconciliation (decision 2): enqueue one full (no
	``mapping_names`` filter) inventory sync for every enabled Shopify
	Marketplace Account, via the same ``enqueue_account_sync()`` the sweep
	and the manual "Reconcile Shopify Inventory" button use. Same
	no-Shopify-HTTP-in-this-function guarantee as ``sweep_changed_bins()`` -
	only the bulk account read plus an enqueue.

	Recovers from anything the sweep could miss: a lost/evicted watermark, a
	change whose Bin write never showed up within the overlap window, or a
	mapping created/enabled after its Item's Bin last changed (the sweep
	only reacts to a NEW ``Bin.modified``, so a freshly-mapped Item whose
	stock hasn't moved since would otherwise never be pushed).

	A dedup-rejected enqueue (a sync for that account is already
	queued/running) is not retried or tracked for a later run - this is the
	one daily "catch everything" pass, so a skip here is written to the
	Error Log (never silently dropped) rather than given its own
	retry/pending-flag mechanism, which is out of scope.

	**Excluded-account visibility (decision 6):** unlike the sweep, this is
	the one automatic entry point that runs infrequently enough (once a day)
	to log which enabled Shopify Accounts exist but are excluded from
	automatic sync entirely (a disabled/wrong-Company Warehouse, a disabled
	Connection, a missing Warehouse/Location) - see
	``_enabled_shopify_accounts_with_exclusions()`` and
	``_log_excluded_accounts_once_per_day()``. Without this, such a
	misconfiguration was previously invisible until someone manually clicked
	"Reconcile".
	"""
	accounts, excluded = _enabled_shopify_accounts_with_exclusions()
	_log_excluded_accounts_once_per_day(excluded)
	for account in accounts:
		try:
			enqueued, _job_id = enqueue_account_sync(account.name)
			if not enqueued:
				frappe.log_error(
					title="Shopify daily inventory reconciliation skipped",
					message=(
						f"Marketplace Account {account.name}: enqueue rejected because a Shopify "
						"inventory sync job for this account was already queued/running "
						"(RQ dedup). Not retried by this reconciliation run."
					),
				)
		except Exception:
			frappe.log_error(
				title="Shopify inventory reconciliation: account failed",
				message=frappe.get_traceback(),
			)


# --- Job status -------------------------------------------------------------


def _result_payload(results):
	total = len(results)
	return {
		"results": results[:MAX_INVENTORY_RESULTS_RETURNED],
		"total": total,
		"succeeded": sum(1 for row in results if row["ok"]),
		"failed": sum(1 for row in results if not row["ok"] and not row.get("skipped")),
		"skipped_mappings": sum(1 for row in results if row.get("skipped")),
		"truncated": total > MAX_INVENTORY_RESULTS_RETURNED,
	}


def _report(job_id, owner, **changes):
	if not job_id:
		return
	state = frappe.cache().get_value(_status_cache_key(job_id)) or {"owner": owner}
	state.update(changes)
	_set_status(job_id, state)
	frappe.publish_realtime(
		INVENTORY_PROGRESS_EVENT,
		{"job_id": job_id, **{key: value for key, value in state.items() if key != "owner"}},
		user=owner,
	)


def _set_status(job_id, state):
	frappe.cache().set_value(_status_cache_key(job_id), state, expires_in_sec=RESULT_TTL)


def _status_cache_key(job_id):
	return f"marketplace_integrations:shopify:inventory_sync_job:{job_id}"
