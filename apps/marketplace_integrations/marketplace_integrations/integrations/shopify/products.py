# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Shopify catalog discovery and mapping (#105).

Uses the existing ``Marketplace Product Mapping`` model unchanged - a
Shopify product variant's GID is the sole mapping identity, stored in both
``external_reference`` (the shared, generic opaque lookup value every
platform adapter writes) and ``external_variant_id`` (Shopify-specific,
required to be equal to ``external_reference`` by THIS module's own
``upsert_mapping()`` - see its docstring). ``external_sku``/
``external_barcode``/``external_product_id`` remain descriptive-only and are
refreshed on every sweep; core mapping resolution
(``services/mappings.py``) never uses them.

This module never opens or imports ``integrations/talabat/catalog.py`` (or
any other Talabat module) - the new Shopify catalog-mapping Desk page
(``marketplace_shopify_catalog_mapping``) is a sibling page, not a change to
Talabat's, and this file intentionally duplicates the small permission-check
shape rather than sharing code with Talabat's module, so "Talabat mapping
behavior remains unaffected" holds by construction. See
docs/marketplace-integrations/architecture.md's #105 entry.

GraphQL query shape: a flat ``productVariants`` root query (single cursor),
not nested ``products { variants }`` - simpler single-cursor pagination,
matching ``ShopifyClient.execute()``'s existing one-call-site pattern. The
``productVariants``/``ProductVariant``/generic ``nodes`` field names and
argument shapes below were verified against
https://shopify.dev/docs/api/admin-graphql/latest/objects/ProductVariant,
https://shopify.dev/docs/api/admin-graphql/latest/queries/productVariants on
2026-09-19 (external source, approved for reliance by the user - not
documented in docs/shopify/README.md before this issue; see that doc's new
"Products and variants (for #105)" section for the same citation).

Two independent passes make up one sweep (``run_catalog_sweep()``):

1. A full paginated walk of ``productVariants`` (the whole store's variant
   catalog, not filtered to this account) - every live variant GID not
   already covered by an ENABLED Marketplace Product Mapping on this account
   becomes an "Unmapped" exception. No document writes happen in this pass.
2. A walk of this account's SAVED Marketplace Product Mapping rows (enabled
   or not - deletion/malformation can affect a disabled row too). A row
   whose ``external_reference`` isn't even shaped like a product variant GID
   is a "Variant-less" exception (a malformed value, most likely written by
   the generic Excel mapping importer rather than this feature - never "a
   Shopify product with zero variants", which the flat query used above
   cannot structurally detect). Every other row is looked up via a batched
   ``nodes(ids: [...])`` query (chunked, see SHOPIFY_NODES_LOOKUP_CHUNK_SIZE
   - the first such batched lookup in this codebase, gap-check #2): a `null`
   result is a "Deleted" exception; a live result refreshes that mapping's
   descriptive fields (external_sku/external_barcode/external_product_id) if
   they changed, under its own DB savepoint so one bad row can't roll back
   rows already written earlier in the same sweep, releasing the savepoint
   AND committing per row on success (borrowed from #104's
   ``retry_errored_webhook_deliveries()`` exactly, not just its savepoint
   half - see docs/frappe-conventions.md's savepoint-per-row guidance).
   Without the per-row commit, a mid-run failure later in the SAME sweep
   would roll back every descriptive-field refresh already written earlier
   in that run too, since none of it was ever durably committed.

Exceptions are presentation-only, computed fresh at sweep time and never
persisted as their own doctype rows - Marketplace Product Mapping.item is
``reqd: 1``, so an "Unmapped" Shopify variant structurally cannot be stored
as a mapping row until a user picks an Item for it via ``upsert_mapping()``.

Progress is reported as "N processed so far" (an indeterminate running
count), not a percentage - Shopify's ``productVariants(first, after)``
cursor connection never exposes a total count up front, unlike
``services/item_import_jobs.py``'s XLSX-row-count-based percentage (gap-check
#1). The job/status/realtime-progress shape below is adapted from that
module (queue/status/cache/realtime), not reused directly, precisely because
of that percentage-vs-count difference.
"""

import re

import frappe
from frappe.utils import cstr

from marketplace_integrations.integrations.shopify.client import ShopifyClient
from marketplace_integrations.integrations.shopify.constants import (
	SHOPIFY_NODES_LOOKUP_CHUNK_SIZE,
	SHOPIFY_VARIANTS_PAGE_SIZE,
)

MAPPING_DOCTYPE = "Marketplace Product Mapping"

_VARIANT_GID_RE = re.compile(r"^gid://shopify/ProductVariant/\d+$")

_VARIANTS_QUERY = """
query ShopifyProductVariants($first: Int!, $after: String) {
  productVariants(first: $first, after: $after) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      sku
      barcode
      product {
        id
        title
      }
    }
  }
}
"""

# Generic `nodes(ids: [ID!]!)` lookup - returns one entry per input id, with
# `null` for any id that no longer resolves to a live object (a deleted
# variant). Shopify doesn't guarantee response order matches the requested
# id order, so callers must match results back to requests by each node's
# own `id` field, never by position - see the id-keyed lookup below.
# `... on ProductVariant` is required because `nodes` is typed to the union
# `[Node]!`, not `[ProductVariant]!`.
_NODES_QUERY = """
query ShopifyProductVariantNodes($ids: [ID!]!) {
  nodes(ids: $ids) {
    id
    ... on ProductVariant {
      sku
      barcode
      product {
        id
      }
    }
  }
}
"""

# How often (in rows processed) to push a realtime/cache progress update
# during the two passes - frequent enough to feel live, infrequent enough
# not to flood frappe.publish_realtime() on a large catalog.
PROGRESS_UPDATE_INTERVAL = 50

RESULT_TTL = 60 * 60
SWEEP_PROGRESS_EVENT = "marketplace_shopify_catalog_sweep_progress"

# Gap-check (review finding #4): a first sweep of a real store can produce
# tens of thousands of exception rows (every variant is unmapped by
# definition before any mapping exists), and the full list would otherwise be
# stored in the Redis status cache, published via frappe.publish_realtime(),
# and re-sent on every ~1.5s status poll from the page JS - unbounded payload
# size. The underlying sweep still walks/processes the whole catalog; only
# what's returned/cached/published/rendered is capped.
MAX_EXCEPTIONS_RETURNED = 500

# Namespaces the RQ-level job id used for deduplication (review finding #5)
# by account, not by this run's own random job id - a per-run random suffix
# would make every enqueue's dedup key unique and defeat deduplication
# entirely. Distinct from job_id_for_result, which stays per-run/random so
# each caller's own status poll is scoped to their own request.
_SWEEP_RQ_JOB_ID_PREFIX = "marketplace-shopify-catalog-sweep"


def _is_variant_gid(value):
	return bool(_VARIANT_GID_RE.match(cstr(value or "")))


def _require_mapping_permission(ptype):
	if not frappe.has_permission(MAPPING_DOCTYPE, ptype=ptype):
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)


def get_account(account_name):
	"""Return an authorized Shopify Marketplace Account and its connection.

	Gap-check #5: no such function existed for Shopify before #105 - written
	fresh here by direct analogy to
	``integrations/talabat/catalog.py::get_account()`` (per-document ``read``
	check on the loaded Marketplace Account, platform/connection_doctype
	assertions, loading and permission-checking the linked connection too).
	"""
	account = frappe.get_doc("Marketplace Account", account_name)
	account.check_permission("read")
	if account.platform != "Shopify":
		frappe.throw(frappe._("Marketplace Account {0} is not a Shopify account.").format(account.name))
	if account.connection_doctype != "Shopify Connection":
		frappe.throw(
			frappe._("Marketplace Account {0} does not use a Shopify Connection.").format(account.name)
		)
	connection = frappe.get_doc("Shopify Connection", account.connection)
	connection.check_permission("read")
	return account, connection


def upsert_mapping(
	account_name,
	external_reference,
	item,
	external_product_id=None,
	external_sku=None,
	external_barcode=None,
):
	"""Explicitly create or update one Shopify variant GID -> ERPNext Item
	mapping, e.g. from the catalog sweep results grid.

	Decision 7 (locked plan): the shared Marketplace Product Mapping
	controller is also used by Talabat and the generic Excel mapping
	importer, so it must not gain a Shopify-specific
	``external_variant_id == external_reference`` rule. This feature's OWN
	upsert path enforces that consistency instead, every time - the variant
	GID is this feature's sole identity value, so the two fields must never
	diverge for a mapping created/edited through this page.
	"""
	account, _connection = get_account(account_name)
	external_reference = cstr(external_reference).strip()
	item = cstr(item).strip()
	if not external_reference:
		frappe.throw(frappe._("Shopify product variant GID is required."))
	if not _is_variant_gid(external_reference):
		frappe.throw(
			frappe._("{0} is not a valid Shopify product variant GID.").format(external_reference)
		)
	if not item:
		frappe.throw(frappe._("Item is required."))

	item_doc = frappe.get_doc("Item", item)
	item_doc.check_permission("read")

	existing = frappe.db.get_value(
		MAPPING_DOCTYPE,
		{"marketplace_account": account.name, "external_reference": external_reference},
		"name",
	)
	if existing:
		mapping = frappe.get_doc(MAPPING_DOCTYPE, existing)
		mapping.check_permission("write")
	else:
		_require_mapping_permission("create")
		mapping = frappe.get_doc(
			{
				"doctype": MAPPING_DOCTYPE,
				"marketplace_account": account.name,
				"external_reference": external_reference,
			}
		)

	mapping.item = item_doc.name
	mapping.enabled = 1
	mapping.external_variant_id = external_reference
	if external_sku is not None:
		mapping.external_sku = cstr(external_sku).strip()
	if external_barcode is not None:
		mapping.external_barcode = cstr(external_barcode).strip()
	if external_product_id is not None:
		mapping.external_product_id = cstr(external_product_id).strip()

	if existing:
		mapping.save()
	else:
		mapping.insert()
	return {"name": mapping.name}


def queue_catalog_sweep(account_name):
	"""Authorize and enqueue a Shopify catalog sweep for one Marketplace
	Account. No Shopify HTTP call happens in this request - the sweep itself
	runs in the background, mirroring services/item_import_jobs.py's queue
	shape.

	Permission shape (gap-check #4): a per-document ``read`` check on the
	Marketplace Account (via get_account(), same as
	integrations/talabat/catalog.py's get_account()) plus a doctype-level
	``write`` check on Marketplace Product Mapping - the sweep both reads
	mappings (to build the exception lists) and writes to them (descriptive
	field refresh on every sweep, see decision 5), so ``write`` is the
	correct check to require up front rather than ``read``.

	Review finding #5: two overlapping sweeps of the SAME account are likely
	to throttle each other's ``ShopifyClient`` calls into failure (see
	``MAX_THROTTLE_ATTEMPTS``), and per finding #2 a failed run now discards
	its own progress. ``deduplicate=True`` (supported by this repo's Frappe
	version - confirmed against ``frappe.utils.background_jobs.enqueue()``)
	rejects a second enqueue for the same account while a prior run is still
	queued or actively running, keyed on an account-scoped RQ job id (NOT
	``job_id_for_result``, which stays per-run/random for status polling).
	``frappe.enqueue()`` returns ``None`` instead of a ``Job`` when it
	silently skips a deduplicated enqueue, per its own source - so that's
	the caller-visible signal used to turn it into a clear error rather than
	a silently-dropped request.
	"""
	account, _connection = get_account(account_name)
	_require_mapping_permission("write")

	job_id = frappe.generate_hash(length=16)
	owner = frappe.session.user
	# Review finding #3: seed the "queued" cache entry BEFORE enqueuing, not
	# after. frappe.enqueue() hands the job straight to an RQ worker, which on
	# a fast/idle worker can start running (and publish "running"/progress
	# status via _update_sweep_status()) before this function got a chance to
	# write the initial "queued" entry - _update_sweep_status() would then
	# create its own state dict from scratch, and this later write would
	# clobber that real progress back down to "queued"/processed=0.
	_set_sweep_status(job_id, {"owner": owner, "status": "queued", "processed": 0})
	job = frappe.enqueue(
		run_catalog_sweep,
		queue="long",
		timeout=3600,
		job_id=f"{_SWEEP_RQ_JOB_ID_PREFIX}-{account.name}",
		deduplicate=True,
		job_id_for_result=job_id,
		owner=owner,
		account_name=account.name,
	)
	if job is None:
		# Don't leave a "queued" entry behind for a sweep that was rejected
		# and will never run - a caller polling this job_id would otherwise
		# see a permanently stuck "queued" status.
		frappe.cache().delete_value(_sweep_cache_key(job_id))
		frappe.throw(
			frappe._("A catalog sweep is already running for Marketplace Account {0}.").format(
				account.name
			)
		)
	return {"job_id": job_id, "status": "queued"}


def get_sweep_status(job_id):
	state = frappe.cache().get_value(_sweep_cache_key(job_id))
	if not state or state.get("owner") != frappe.session.user:
		frappe.throw(frappe._("Catalog sweep job not found."), frappe.PermissionError)
	return {key: value for key, value in state.items() if key != "owner"}


def run_catalog_sweep(job_id_for_result, owner, account_name):
	"""Background job body: walk the full live Shopify catalog plus this
	account's saved mappings, and report unmapped/deleted/variant-less
	exceptions. See module docstring for the two-pass shape."""
	try:
		_update_sweep_status(job_id_for_result, owner, status="running", processed=0)
		account, connection = get_account(account_name)
		client = ShopifyClient(connection.name)

		unmapped, processed = _find_unmapped_variants(account.name, client, job_id_for_result, owner)
		deleted, variant_less, processed, refresh_failures = _check_saved_mappings(
			account.name, client, job_id_for_result, owner, processed
		)

		exceptions = (
			[{"category": "Unmapped", **row} for row in unmapped]
			+ [{"category": "Deleted", **row} for row in deleted]
			+ [{"category": "Variant-less", **row} for row in variant_less]
		)
		total_exceptions = len(exceptions)
		truncated = total_exceptions > MAX_EXCEPTIONS_RETURNED
		if truncated:
			exceptions = exceptions[:MAX_EXCEPTIONS_RETURNED]
		result = {
			"exceptions": exceptions,
			"total_exceptions": total_exceptions,
			"truncated": truncated,
			# Review finding #4: a descriptive-field refresh failure is
			# caught, rolled back to its own savepoint, and logged to the
			# Error Log (_refresh_mapping_descriptive_fields()) - without
			# this count, a sweep with failed rows still reports plain
			# "completed" with no indication anything needs attention.
			# Deliberately just a count plus a static message on the page,
			# not a new exception category or per-row detail.
			"refresh_failures": refresh_failures,
		}
		_update_sweep_status(job_id_for_result, owner, status="completed", processed=processed, result=result)
	except Exception as exc:
		_update_sweep_status(job_id_for_result, owner, status="failed", error=str(exc))
		raise


def _find_unmapped_variants(account_name, client, job_id, owner):
	"""Pass 1: page through the full live productVariants catalog, returning
	every variant GID with no ENABLED Marketplace Product Mapping on this
	account. No document writes happen here.

	Review finding #6: a variant with an existing but DISABLED mapping row is
	still classified "Unmapped" (no ENABLED mapping row) - that part is
	unchanged. But its exception row's ``mapping`` field is populated with
	that disabled row's name (rather than left ``None``) so the page's "Open
	mapping" link renders - without it, an operator has no visibility that
	picking an Item here will re-enable an existing row (upsert_mapping()'s
	documented behavior, unchanged by this fix) rather than create a new one.
	"""
	mapping_rows = frappe.get_all(
		MAPPING_DOCTYPE,
		filters={"marketplace_account": account_name},
		fields=["name", "external_reference", "enabled"],
	)
	mapped_references = {row.external_reference for row in mapping_rows if row.enabled}
	disabled_mapping_by_reference = {
		row.external_reference: row.name for row in mapping_rows if not row.enabled
	}

	unmapped = []
	processed = 0
	cursor = None
	while True:
		data = client.execute(_VARIANTS_QUERY, {"first": SHOPIFY_VARIANTS_PAGE_SIZE, "after": cursor})
		connection_data = data.get("productVariants") or {}
		nodes = connection_data.get("nodes") or []

		for node in nodes:
			processed += 1
			# Review finding #3: this progress tick must run for EVERY variant
			# processed, mapped or not - it previously sat inside the
			# unmapped-only branch below, so a catalog that's mostly already
			# mapped (the common steady-state case) reported "0 processed" for
			# the whole pass instead of a live running count.
			if processed % PROGRESS_UPDATE_INTERVAL == 0:
				_update_sweep_status(job_id, owner, status="running", processed=processed)

			gid = node.get("id") if isinstance(node, dict) else None
			if not gid or gid in mapped_references:
				continue
			product = (node.get("product") or {}) if isinstance(node, dict) else {}
			unmapped.append(
				{
					"external_reference": gid,
					"external_sku": node.get("sku"),
					"external_barcode": node.get("barcode"),
					"external_product_id": product.get("id"),
					"product_title": product.get("title"),
					"item": None,
					"mapping": disabled_mapping_by_reference.get(gid),
				}
			)

		page_info = connection_data.get("pageInfo") or {}
		if not page_info.get("hasNextPage"):
			break
		cursor = page_info.get("endCursor")
		if not cursor:
			break

	_update_sweep_status(job_id, owner, status="running", processed=processed)
	return unmapped, processed


def _check_saved_mappings(account_name, client, job_id, owner, processed_so_far):
	"""Pass 2: walk this account's saved mappings (any enabled state),
	classify malformed identities as "Variant-less", and batch-check every
	remaining variant GID via `nodes(ids: [...])`; a null result is
	"Deleted", a live result refreshes descriptive fields (decision 5).

	Returns ``(deleted, variant_less, processed, refresh_failures)`` - the
	last is a plain count of rows whose descriptive-field refresh failed and
	was rolled back (review finding #4), for the caller to surface in the
	sweep result."""
	rows = frappe.get_all(
		MAPPING_DOCTYPE,
		filters={"marketplace_account": account_name},
		fields=[
			"name",
			"item",
			"external_reference",
			"external_sku",
			"external_barcode",
			"external_product_id",
		],
	)

	variant_less = []
	gid_rows = []
	for row in rows:
		if _is_variant_gid(row.external_reference):
			gid_rows.append(row)
		else:
			variant_less.append(
				{
					"external_reference": row.external_reference,
					"external_sku": row.external_sku,
					"external_barcode": row.external_barcode,
					"external_product_id": row.external_product_id,
					"product_title": None,
					"item": row.item,
					"mapping": row.name,
				}
			)

	deleted = []
	refresh_failures = 0
	processed = processed_so_far
	for chunk in _chunked(gid_rows, SHOPIFY_NODES_LOOKUP_CHUNK_SIZE):
		ids = [row.external_reference for row in chunk]
		data = client.execute(_NODES_QUERY, {"ids": ids})
		nodes = data.get("nodes") or []
		# Review finding #1: look up each row's node by `id`, never by list
		# position - `_NODES_QUERY` already selects `id` on every node for
		# exactly this purpose. Pairing by position (e.g. zip()) silently
		# writes one variant's sku/barcode/product_id onto a DIFFERENT
		# mapping row, or misclassifies an unrelated row as "Deleted", the
		# moment Shopify's response omits an id or returns a different order
		# than requested.
		nodes_by_id = {node["id"]: node for node in nodes if node and node.get("id")}
		for row in chunk:
			processed += 1
			# A row missing from the response entirely (a shorter list than
			# requested) is treated the same as an explicit null node -
			# "Deleted", the existing null-node handling below - not a third
			# behavior: either way Shopify no longer returned a live object
			# for that id.
			node = nodes_by_id.get(row.external_reference)
			if not node:
				deleted.append(
					{
						"external_reference": row.external_reference,
						"external_sku": row.external_sku,
						"external_barcode": row.external_barcode,
						"external_product_id": row.external_product_id,
						"product_title": None,
						"item": row.item,
						"mapping": row.name,
					}
				)
			else:
				if not _refresh_mapping_descriptive_fields(row, node):
					refresh_failures += 1
		_update_sweep_status(job_id, owner, status="running", processed=processed)

	return deleted, variant_less, processed, refresh_failures


def _refresh_mapping_descriptive_fields(row, node):
	"""Re-write a saved mapping's descriptive-only fields
	(external_sku/external_barcode/external_product_id) if Shopify's current
	values differ from what's stored (decision 5). Never touches identity
	(external_reference/external_variant_id) or `item`.

	Runs under its own DB savepoint (gap-check #6, borrowed from #104's
	retry_errored_webhook_deliveries() exactly - savepoint release AND a
	per-row commit) so one bad row's write failure can't roll back rows
	already refreshed earlier in the same sweep pass. Review finding #2:
	without the commit, a savepoint release alone leaves every prior row's
	write only pending in the same open transaction - a LATER row's
	exception in the same run (e.g. ShopifyClient exhausting its
	MAX_THROTTLE_ATTEMPTS mid-pass) still propagates out of
	run_catalog_sweep() and rolls back the whole transaction, discarding
	every descriptive-field refresh already "committed" earlier in that run.

	Returns False only when a write was attempted and failed (caller counts
	this into the sweep result's ``refresh_failures``, review finding #4);
	True for both "nothing needed writing" and "wrote successfully".
	"""
	product = node.get("product") or {}
	new_sku = node.get("sku")
	new_barcode = node.get("barcode")
	new_product_id = product.get("id")

	if (
		(row.external_sku or None) == (new_sku or None)
		and (row.external_barcode or None) == (new_barcode or None)
		and (row.external_product_id or None) == (new_product_id or None)
	):
		return True

	savepoint = "shopify_catalog_sweep_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(savepoint)
	try:
		# Review finding #1: unlike Talabat's sync-bookkeeping fields
		# (integrations/talabat/catalog_sync.py's _mark_queued()/_mark_error(),
		# which legitimately use update_modified=False), these three fields are
		# services/mapping_import.py's MANAGED_COLUMNS - that importer's ONLY
		# concurrency guard is comparing an exported __modified timestamp
		# against the row's current `modified` (_check_not_stale()). Passing
		# update_modified=False here would let a sweep silently rewrite a
		# MANAGED_COLUMNS value without moving `modified`, so a stale,
		# unedited re-upload of a since-refreshed workbook would pass the
		# staleness check and revert this refresh. Let `modified` update
		# normally (the default) so that guard actually catches this case.
		frappe.db.set_value(
			MAPPING_DOCTYPE,
			row.name,
			{
				"external_sku": new_sku,
				"external_barcode": new_barcode,
				"external_product_id": new_product_id,
			},
		)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(
			title="Shopify catalog sweep: descriptive field refresh failed",
			message=frappe.get_traceback(),
		)
		return False
	else:
		frappe.db.release_savepoint(savepoint)
		frappe.db.commit()
		return True


def _chunked(items, size):
	items = list(items)
	for start in range(0, len(items), size):
		yield items[start : start + size]


def _set_sweep_status(job_id, state):
	frappe.cache().set_value(_sweep_cache_key(job_id), state, expires_in_sec=RESULT_TTL)


def _update_sweep_status(job_id, owner, **changes):
	state = frappe.cache().get_value(_sweep_cache_key(job_id)) or {"owner": owner}
	state.update(changes)
	_set_sweep_status(job_id, state)
	frappe.publish_realtime(
		SWEEP_PROGRESS_EVENT,
		{"job_id": job_id, **{key: value for key, value in state.items() if key != "owner"}},
		user=owner,
	)


def _sweep_cache_key(job_id):
	return f"marketplace_integrations:shopify:catalog_sweep_job:{job_id}"
