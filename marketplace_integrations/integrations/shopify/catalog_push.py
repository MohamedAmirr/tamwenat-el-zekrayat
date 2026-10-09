# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Explicit, retry-safe Shopify price publishing (#106).

Price-only, update-only, per the issue's locked implementation plan:

- **Update-only** (decision 1): every mapping pushed here already carries a
  Shopify variant/product GID pair, written by #105's discovery
  (``integrations/shopify/products.py::upsert_mapping()``). This module never
  creates a Shopify product/variant and never calls ``productCreate``/
  ``productUpdate``/``productSet`` - the only mutation used is
  ``productVariantsBulkUpdate``, keyed on an existing variant ``id``. An
  update keyed on an existing GID is inherently replay-safe: retrying a
  partial failure can only converge to the same state, never create a
  duplicate variant. No idempotency-key mechanism is needed because there is
  no create path to guard.
- **Price-only** (decisions 2/3): title, SKU, and barcode are all explicitly
  out of scope - title is product-level while mappings are variant-level, and
  SKU/barcode are treated as Shopify-owned (never overwritten from ERPNext).
  Only ``price`` is ever sent.
- **UOM** (decision 4): always prices at the Item's own stock UOM, ignoring
  ``Marketplace Product Mapping.sales_uom`` - Shopify variants have no UOM
  concept. This is a deliberate divergence from Talabat's own catalog sync,
  which does convert via ``sales_uom``.
- **Currency guard** (decision 4): ``validate_push_account()`` requires the
  account's Price List currency to match the Shopify shop's own currency
  (``shop { currencyCode }``, a read-only query, same read-vs-write rule as
  ``integrations/shopify/locations.py``'s ``shop { id }`` read) - this is
  intentionally a SEPARATE, minimal query rather than an extension of
  ``locations.py``'s combined shop/locations query, since Marketplace
  Account save-time Location validation and this catalog-push validation are
  unrelated concerns that happen to both read ``shop``. This network call is
  deliberately kept OUT of the synchronous Desk request
  (``queue_catalog_push()``, which calls only
  ``_validate_push_account_local()``'s local checks) - a throttled shop could
  otherwise block that request for several seconds per account. It runs only
  inside the background worker (``push_catalog()``, via the full
  ``validate_push_account()``), which already re-validates independently per
  account regardless.
- **No retry-state field** (decision 5): ``sync_status``/``last_error`` plus
  the already-stored GIDs are enough - retry is just re-selecting ``Error``
  rows and pushing again.
- **Job-status + results grid** (decision 7): reuses
  ``integrations/shopify/products.py``'s catalog-sweep job/status/cache/
  realtime-progress shape (adapted, not copy-pasted) rather than Talabat's
  simpler fire-and-forget push, so a push reports "N pushed, M failed, here's
  why" instead of writing status silently onto each mapping row. One
  background job is queued per affected Marketplace Account (mirroring
  decision 8's shared grouping shape, and Talabat's own one-job-per-account
  pattern) - a bulk push spanning more than one Shopify account therefore
  returns more than one job id, each independently pollable.

Worker shape per Marketplace Account job (``push_catalog()``): mappings are
grouped by ``external_product_id`` (one or more ``productVariantsBulkUpdate``
calls per Shopify product, covering every mapped variant of that product,
``allowPartialUpdates: true``) - under its own ``frappe.db.savepoint()`` PLUS
a per-group ``frappe.db.commit()`` (borrowing
``products.py``'s ``_refresh_mapping_descriptive_fields()`` pattern exactly -
see ``docs/frappe-conventions.md``'s savepoint-per-row/per-group guidance),
so a later throttle-exhaustion in the same run can't roll back groups already
finished. A group's variants are sent in chunks of
``constants.py``'s ``SHOPIFY_CATALOG_PUSH_VARIANT_CHUNK_SIZE`` - one mutation
call per chunk, still inside the same savepoint/commit - so a product with
more mapped variants than Shopify's per-call limit doesn't fail as a single
oversized call. A mapping whose identity isn't actually shaped like a Shopify
variant/product GID pair (e.g. written by the generic Excel mapping importer)
never reaches the network call - marked Error locally, same as Talabat's
``CatalogBuildError`` handling. A mapping that is disabled, or whose linked
Item is disabled, is excluded before grouping even begins (matching
Talabat's own ``active`` semantics in ``build_catalog_product()``) - nothing
is attempted for it, and it is reported as skipped, not Error.

**Result attribution** (issue risk #2): success is determined by a variant's
``id`` appearing in the mutation response's own ``productVariants`` list -
this is Shopify's own confirmation of what actually got written, not a
best-effort inference. ``userErrors[].field`` is used only to build a nicer
message for whichever variants did NOT come back in that list; if no
``userErrors`` are present at all, every variant missing from the response
falls back to one static, generic error. This "never echo a raw remote error
body" rule is scoped specifically to THIS per-mapping/per-variant error path
(``_format_user_errors()``/``REMOTE_ERROR``) - it is not a blanket rule the
rest of this module follows. The whole-job failure path (``push_catalog()``'s
own ``except Exception as exc: _update_push_status(..., error=str(exc))``)
can include a length-bounded slice of Shopify's own raw response body via
``ShopifyClient``'s exception message - matching the existing precedent in
``products.py`` - which is fine (it is Shopify's own response, not internal
data), just a different rule for a different failure surface. This sidesteps
the unverified ``userErrors[].field`` -> variant attribution shape flagged in
the issue: variant identification always uses the documented ``id`` field on
the payload's own ``productVariants`` list, which needs no positional/field-path
guessing.

External GraphQL research citation: ``productVariantsBulkUpdate``'s
``productId``/``variants``/``allowPartialUpdates`` arguments and
``ProductVariantsBulkInput``'s ``id``/``price`` fields were verified against
shopify.dev on 2026-09-22 (see this issue's locked plan). The exact response
payload shape used here (``product``, ``productVariants { id }``,
``userErrors { field message }``) follows Shopify's well-established Bulk-
mutation payload convention (the same shape used by e.g.
``productVariantsBulkCreate``) but was NOT independently re-verified against
a live call or a fresh shopify.dev page in this session - worth confirming
against a real/test store before relying on it in production, per the
issue's own flagged risk.
"""

import re

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from marketplace_integrations.integrations.shopify.client import ShopifyClient
from marketplace_integrations.integrations.shopify.constants import SHOPIFY_CATALOG_PUSH_VARIANT_CHUNK_SIZE
from marketplace_integrations.services.listing_price import CatalogBuildError, resolve_listing_price
from marketplace_integrations.services.mapping_push import group_mappings_by_account, parse_mapping_names

MAPPING_DOCTYPE = "Marketplace Product Mapping"

REMOTE_ERROR = _("Shopify did not accept the price update.")

# Structured userErrors messages are Shopify's own admin-facing text, not a
# raw response body - still bounded defensively so a pathological/unexpected
# response can never blow up an Error Log entry or the job-status payload.
ERROR_MESSAGE_MAX_LENGTH = 500

RESULT_TTL = 60 * 60
PUSH_PROGRESS_EVENT = "marketplace_shopify_catalog_push_progress"

# Same rationale/value as products.py's MAX_EXCEPTIONS_RETURNED - bounds what
# is cached/published/rendered, not what the worker actually processes.
MAX_PUSH_RESULTS_RETURNED = 500

# How often (in product groups processed) to push a realtime/cache progress
# update while pushing - same rationale/naming as products.py's
# PROGRESS_UPDATE_INTERVAL (review finding #1 on this PR): without a
# throttle, a bulk push spanning ~3,000 distinct Shopify products would
# rewrite the Redis status cache AND call frappe.publish_realtime() once per
# product group - pure waste, since nothing in this codebase actually listens
# for PUSH_PROGRESS_EVENT (the dialog polls get_catalog_push_status on a
# timer instead). The FINAL update after the loop always runs unconditionally
# (status="completed"), so the dialog's poll always sees the true final state
# regardless of this throttle.
PUSH_PROGRESS_UPDATE_INTERVAL = 50

# Namespaces the RQ-level job id used for deduplication, scoped per Account -
# same shape as products.py's _SWEEP_RQ_JOB_ID_PREFIX, so two overlapping
# pushes of the SAME account can't race each other's writes, while pushes of
# different accounts run independently.
_PUSH_RQ_JOB_ID_PREFIX = "marketplace-shopify-catalog-push"

_VARIANT_GID_RE = re.compile(r"^gid://shopify/ProductVariant/\d+$")
_PRODUCT_GID_RE = re.compile(r"^gid://shopify/Product/\d+$")

_SHOP_CURRENCY_QUERY = """
query MarketplaceCatalogPushShopCurrency {
  shop {
    currencyCode
  }
}
"""

_PRODUCT_VARIANTS_BULK_UPDATE_MUTATION = """
mutation MarketplaceCatalogPushVariantPrice(
  $productId: ID!
  $variants: [ProductVariantsBulkInput!]!
  $allowPartialUpdates: Boolean!
) {
  productVariantsBulkUpdate(
    productId: $productId
    variants: $variants
    allowPartialUpdates: $allowPartialUpdates
  ) {
    product {
      id
    }
    productVariants {
      id
    }
    userErrors {
      field
      message
    }
  }
}
"""


def _is_variant_gid(value):
	return bool(_VARIANT_GID_RE.match(cstr(value or "")))


def _is_product_gid(value):
	return bool(_PRODUCT_GID_RE.match(cstr(value or "")))


def _as_account(value):
	if getattr(value, "doctype", None) == "Marketplace Account":
		return value
	return frappe.get_doc("Marketplace Account", value)


def validate_push_account(account):
	"""Return the fully configured Shopify account, connection, and price list.

	Runs ``_validate_push_account_local()``'s fast, non-network checks and
	THEN the Shopify shop-currency check (a live GraphQL call - see
	``_validate_shop_currency()``). Only the background worker
	(``push_catalog()``) calls this full version; the Desk request path
	(``queue_catalog_push()``) calls the local-only half instead, so a
	throttled shop can't block the Desk request itself - see that function's
	docstring.

	Unlike Talabat's ``validate_catalog_sync_account()``, there is no
	Warehouse check here - this push is price-only, with no quantity/stock
	concept (decisions 1/4).
	"""
	account_doc, connection, price_list = _validate_push_account_local(account)
	_validate_shop_currency(connection, price_list)
	return account_doc, connection, price_list


def _validate_push_account_local(account):
	"""Return the configured Shopify account, connection, and price list,
	checking only local platform/connection/Price List configuration - no
	network call. Shared by ``validate_push_account()`` (which adds the
	currency check on top) and ``queue_catalog_push()`` (which intentionally
	stops here, before any network call, in the synchronous Desk request)."""
	account_doc = _as_account(account)
	if account_doc.platform != "Shopify":
		frappe.throw(_("Marketplace Account {0} is not a Shopify account.").format(account_doc.name))
	if account_doc.connection_doctype != "Shopify Connection" or not account_doc.connection:
		frappe.throw(_("Marketplace Account {0} does not use a Shopify Connection.").format(account_doc.name))
	if not account_doc.enabled:
		frappe.throw(_("Marketplace Account {0} is disabled.").format(account_doc.name))
	if not account_doc.erpnext_selling_price_list:
		frappe.throw(
			_(
				"Marketplace Account {0} requires an ERPNext Selling Price List for Shopify catalog push."
			).format(account_doc.name)
		)

	connection = frappe.get_doc("Shopify Connection", account_doc.connection)
	if not connection.enabled:
		frappe.throw(_("Shopify Connection {0} is disabled.").format(connection.name))

	price_list = frappe.get_doc("Price List", account_doc.erpnext_selling_price_list)
	if not price_list.enabled:
		frappe.throw(_("Price List {0} is disabled.").format(price_list.name))
	if not price_list.selling:
		frappe.throw(_("Price List {0} is not a selling price list.").format(price_list.name))

	return account_doc, connection, price_list


def _validate_shop_currency(connection, price_list):
	client = ShopifyClient(connection.name)
	data = client.execute(_SHOP_CURRENCY_QUERY)
	shop_currency = cstr(((data or {}).get("shop") or {}).get("currencyCode")).strip().upper()
	if not shop_currency:
		frappe.throw(
			_("Could not determine the Shopify shop's currency for Connection {0}.").format(connection.name)
		)
	price_list_currency = cstr(price_list.currency).strip().upper()
	if not price_list_currency:
		frappe.throw(_("Price List {0} has no currency configured.").format(price_list.name))
	if price_list_currency != shop_currency:
		frappe.throw(
			_("Price List {0}'s currency ({1}) does not match the Shopify shop's currency ({2}).").format(
				price_list.name, price_list.currency, shop_currency
			)
		)


def group_by_product(mappings):
	"""Split mappings into ``(groups, local_errors)``.

	``groups`` is ``{external_product_id: [mapping, ...]}`` - only mappings
	whose identity is actually shaped like a Shopify variant/product GID pair
	(decision 1: push only ever targets a mapping already carrying Shopify
	GIDs written by #105's discovery). ``local_errors`` is a list of
	``(mapping, message)`` for any mapping that isn't - a malformed identity
	is a LOCAL build problem, exactly like Talabat's ``CatalogBuildError``
	handling, and must never reach the network call.
	"""
	groups = {}
	local_errors = []
	for mapping in mappings:
		if not _is_variant_gid(mapping.external_reference):
			local_errors.append(
				(
					mapping,
					_(
						"Marketplace Product Mapping {0} does not have a valid Shopify product variant GID."
					).format(mapping.name),
				)
			)
			continue
		if not _is_product_gid(mapping.external_product_id):
			local_errors.append(
				(
					mapping,
					_("Marketplace Product Mapping {0} does not have a valid Shopify product GID.").format(
						mapping.name
					),
				)
			)
			continue
		groups.setdefault(mapping.external_product_id, []).append(mapping)
	return groups, local_errors


def queue_catalog_push(mapping_names):
	"""Authorize and enqueue a Shopify price push for the given Marketplace
	Product Mapping names, one background job per affected Marketplace
	Account (decision 8's shared grouping shape), each independently tracked
	via its own job-status entry (decision 7).

	Only ``_validate_push_account_local()``'s fast, local checks (platform,
	Connection, Price List configured/enabled/selling, account enabled) run
	in this request - no Shopify HTTP call happens here. The Shopify
	shop-currency check is a live GraphQL call and runs only inside the
	background worker (``push_catalog()``, via its own
	``validate_push_account()`` call), so a throttled shop can't block this
	Desk request.

	Per-account tolerant, not all-or-nothing (review finding #2): a
	misconfigured account (missing/disabled Price List, disabled Connection,
	...) is added to ``skipped_accounts`` with a reason distinguishing it
	from an "already running" rejection, while every other, validly
	configured account in the same request still gets queued and pushed
	normally - a single misconfigured account out of several selected can no
	longer cause zero accounts to be pushed. Enqueues every account that can
	proceed even if another account in the same request is rejected (either
	as misconfigured or as already-running via RQ dedup) - a mid-loop
	``frappe.throw()`` would otherwise discard the job ids of accounts
	already enqueued earlier in the same loop, losing the caller's visibility
	into pushes that are genuinely running. Returns both ``jobs`` (queued,
	pollable) and ``skipped_accounts`` (misconfigured or already-running) so
	the caller can show both outcomes together.

	Named ``skipped_accounts`` (review finding #3), not ``skipped``, to stay
	textually distinct from ``_push_result_payload()``'s ``skipped_mappings``
	- the two mean different things (accounts rejected here vs. mappings
	excluded as disabled there) but both flow into the same
	``ShopifyCatalogPushStatus`` dialog.
	"""
	mapping_names = parse_mapping_names(mapping_names)
	accounts = group_mappings_by_account(mapping_names)
	return _enqueue_account_pushes(accounts)


def _enqueue_account_pushes(accounts):
	"""Validate each account's local config and enqueue its price-push job.

	``accounts`` is ``{account_name: {"account": account_doc, "mapping_names": [...]}}``
	as returned by ``group_mappings_by_account()`` or built directly by a caller
	that already loaded the account and its mapping names.
	"""
	owner = frappe.session.user
	jobs = []
	skipped_accounts = []
	valid_accounts = {}
	for account_name, selected in accounts.items():
		try:
			_validate_push_account_local(selected["account"])
		except frappe.ValidationError as error:
			frappe.clear_last_message()
			skipped_accounts.append(
				{"account": account_name, "reason": _config_error_message(error, account_name)}
			)
			continue
		valid_accounts[account_name] = selected

	for account_name, selected in valid_accounts.items():
		job_id = frappe.generate_hash(length=16)
		# Seed the "queued" cache entry before enqueuing - same reasoning as
		# products.py's queue_catalog_sweep(): a fast worker could otherwise
		# start publishing "running" progress before this write ever lands.
		_set_push_status(job_id, {"owner": owner, "status": "queued", "processed": 0})
		job = frappe.enqueue(
			push_catalog,
			queue="long",
			timeout=3600,
			job_id=f"{_PUSH_RQ_JOB_ID_PREFIX}-{account_name}",
			deduplicate=True,
			job_id_for_result=job_id,
			owner=owner,
			account_name=account_name,
			mapping_names=selected["mapping_names"],
		)
		if job is None:
			frappe.cache().delete_value(_push_cache_key(job_id))
			skipped_accounts.append(
				{
					"account": account_name,
					"reason": _("A catalog push is already running for this Marketplace Account."),
				}
			)
			continue
		jobs.append(
			{"account": account_name, "job_id": job_id, "mapping_count": len(selected["mapping_names"])}
		)
	return {"jobs": jobs, "skipped_accounts": skipped_accounts}


def queue_account_catalog_push(account_name):
	"""Price-push every enabled Marketplace Product Mapping of one Shopify
	Marketplace Account (the "Sync All" price leg, #168). Same per-account
	job/skip shape and the same local-only validation as ``queue_catalog_push()``
	(both enqueue through ``_enqueue_account_pushes()``), so the
	no-HTTP-in-the-request and per-account error reporting guarantees are
	inherited unchanged.

	Authorization is one account-level write check plus one doctype-level write
	check on Marketplace Product Mapping, instead of loading and checking every
	mapping individually. The mapping names come from a single permission-scoped
	``frappe.get_list``, and the account document is loaded once.
	"""
	account_doc = frappe.get_doc("Marketplace Account", account_name)
	account_doc.check_permission("write")
	if not frappe.has_permission(MAPPING_DOCTYPE, ptype="write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	mapping_names = frappe.get_list(
		MAPPING_DOCTYPE,
		filters={"marketplace_account": account_doc.name, "enabled": 1},
		pluck="name",
		order_by="name asc",
		limit_page_length=0,
	)
	if not mapping_names:
		return {"jobs": [], "skipped_accounts": []}
	return _enqueue_account_pushes(
		{account_doc.name: {"account": account_doc, "mapping_names": list(mapping_names)}}
	)


def get_push_status(job_id):
	state = frappe.cache().get_value(_push_cache_key(job_id))
	if not state or state.get("owner") != frappe.session.user:
		frappe.throw(_("Catalog push job not found."), frappe.PermissionError)
	return {key: value for key, value in state.items() if key != "owner"}


def push_catalog(job_id_for_result, owner, account_name, mapping_names):
	"""Background job body (the worker): resolve the current price and push
	it to Shopify for every given mapping via ``productVariantsBulkUpdate``,
	grouped one mutation call per Shopify product. See module docstring for
	the full worker shape and the idempotency mechanism."""
	try:
		_update_push_status(
			job_id_for_result, owner, status="running", processed=0, result=_push_result_payload([])
		)
		account_doc, connection, price_list = validate_push_account(account_name)
		client = ShopifyClient(connection.name)

		mappings = _mappings_for_push(account_doc.name, mapping_names)
		item_by_name = _fetch_items({mapping.item for mapping in mappings if mapping.item})
		mappings, results = _split_active_mappings(mappings, item_by_name)
		groups, local_errors = group_by_product(mappings)

		for mapping, message in local_errors:
			_mark_error(mapping.name, message)
			results.append({"mapping_name": mapping.name, "ok": False, "error": message})
		_update_push_status(
			job_id_for_result,
			owner,
			status="running",
			processed=len(results),
			result=_push_result_payload(results),
		)

		for group_index, (product_id, group_mappings) in enumerate(groups.items(), start=1):
			results.extend(_push_product_group(client, price_list, product_id, group_mappings, item_by_name))
			# Throttled (review finding #1) - the unconditional "completed"
			# update right after this loop always reports the true final
			# state regardless, so skipping intermediate ticks here is safe.
			if group_index % PUSH_PROGRESS_UPDATE_INTERVAL == 0:
				_update_push_status(
					job_id_for_result,
					owner,
					status="running",
					processed=len(results),
					result=_push_result_payload(results),
				)

		_update_push_status(
			job_id_for_result,
			owner,
			status="completed",
			processed=len(results),
			result=_push_result_payload(results),
		)
	except Exception as exc:
		_update_push_status(job_id_for_result, owner, status="failed", error=str(exc))
		raise


def _push_product_group(client, price_list, product_id, group_mappings, item_by_name):
	"""Push price for one Shopify product's mapped variants and persist every
	result (local build errors plus remote outcome) together, under one
	savepoint/commit for the whole group - a later group's failure in the
	same run can't roll back this group's already-finished writes.

	``item_by_name`` is the caller's already-bulk-fetched
	``{item_name: frappe._dict(name, disabled, stock_uom)}`` map (review
	finding #1) - this function never issues its own per-mapping ``Item``
	document load.

	Variants are sent to ``productVariantsBulkUpdate`` in chunks of
	``SHOPIFY_CATALOG_PUSH_VARIANT_CHUNK_SIZE`` (one call per chunk) so a
	product with more mapped variants than Shopify's per-call limit doesn't
	fail as a single oversized mutation - the savepoint/commit boundary below
	still covers the whole group, only the mutation call count changes."""
	local_results = []
	valid_variants = []
	mapping_by_variant_id = {}
	for mapping in group_mappings:
		item = item_by_name.get(mapping.item)
		if item is None:
			local_results.append(
				(mapping, False, _build_error_message(_("Item {0} does not exist.").format(mapping.item)))
			)
			continue
		try:
			price = resolve_listing_price(price_list, item, item.stock_uom)
		except CatalogBuildError as error:
			local_results.append((mapping, False, _build_error_message(error)))
			continue
		variant_id = mapping.external_reference
		valid_variants.append({"id": variant_id, "price": _format_money(price)})
		mapping_by_variant_id[variant_id] = mapping

	remote_results = []
	for chunk_start in range(0, len(valid_variants), SHOPIFY_CATALOG_PUSH_VARIANT_CHUNK_SIZE):
		chunk = valid_variants[chunk_start : chunk_start + SHOPIFY_CATALOG_PUSH_VARIANT_CHUNK_SIZE]
		try:
			response = client.execute(
				_PRODUCT_VARIANTS_BULK_UPDATE_MUTATION,
				{"productId": product_id, "variants": chunk, "allowPartialUpdates": True},
			)
		except Exception:
			for variant in chunk:
				remote_results.append((mapping_by_variant_id[variant["id"]], False, REMOTE_ERROR))
			continue

		payload = (response or {}).get("productVariantsBulkUpdate") or {}
		returned_ids = {
			node.get("id")
			for node in (payload.get("productVariants") or [])
			if isinstance(node, dict) and node.get("id")
		}
		user_errors = payload.get("userErrors") or []
		error_message = _format_user_errors(user_errors) if user_errors else REMOTE_ERROR
		for variant in chunk:
			mapping = mapping_by_variant_id[variant["id"]]
			if variant["id"] in returned_ids:
				remote_results.append((mapping, True, None))
			else:
				remote_results.append((mapping, False, error_message))

	all_results = local_results + remote_results
	if not all_results:
		return []

	savepoint = "shopify_catalog_push_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(savepoint)
	try:
		for mapping, ok, error in all_results:
			if ok:
				_mark_synced(mapping.name)
			else:
				_mark_error(mapping.name, error)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(title="Shopify catalog push: write failed", message=frappe.get_traceback())
		# Best-effort: the group's real outcome couldn't be persisted under its
		# own savepoint, so at minimum mark every mapping in it Error (outside
		# any savepoint) rather than silently leaving stale sync_status in the
		# database while still reporting "Error" to the job-status result.
		for mapping, *_rest in all_results:
			try:
				_mark_error(mapping.name, REMOTE_ERROR)
			except Exception:
				pass
		frappe.db.commit()
		return [
			{"mapping_name": mapping.name, "ok": False, "error": REMOTE_ERROR}
			for mapping, *_rest in all_results
		]
	else:
		frappe.db.release_savepoint(savepoint)
		frappe.db.commit()

	return [
		{"mapping_name": mapping.name, "ok": ok, **({} if ok else {"error": error})}
		for mapping, ok, error in all_results
	]


def _mappings_for_push(account_name, mapping_names):
	"""Return the pushable fields for every selected mapping in ONE query
	(review finding #1) - only ``name``/``item``/``enabled``/
	``external_reference``/``external_product_id`` are ever read anywhere in
	this module's push path, so a full ``frappe.get_doc()`` per row (which
	also materializes every child table - barcodes, UOM conversions, taxes -
	that nothing here uses) is unnecessary."""
	filters = {"marketplace_account": account_name, "name": ["in", list(mapping_names)]}
	return frappe.get_all(
		MAPPING_DOCTYPE,
		filters=filters,
		fields=["name", "item", "enabled", "external_reference", "external_product_id"],
		order_by="name asc",
	)


def _fetch_items(item_names):
	"""Return ``{item_name: frappe._dict(name, disabled, stock_uom)}`` for every
	given Item name in ONE query (review finding #1) - only ``name`` and
	``stock_uom`` are ever used by ``resolve_listing_price()``/
	``_push_product_group()``, and ``disabled`` by ``_split_active_mappings()``,
	so a full per-mapping ``frappe.get_doc("Item", ...)`` (materializing every
	child table) is unnecessary."""
	if not item_names:
		return {}
	rows = frappe.get_all(
		"Item", filters={"name": ["in", list(item_names)]}, fields=["name", "disabled", "stock_uom"]
	)
	return {row.name: row for row in rows}


def _split_active_mappings(mappings, item_by_name):
	"""Return ``(active_mappings, skipped_results)``, excluding from the push
	any mapping that is disabled or whose linked Item is disabled - matching
	Talabat's own active semantics
	(``integrations/talabat/catalog_sync.py``'s ``build_catalog_product()``:
	``active = bool(mapping_doc.enabled) and not bool(item.disabled)``).
	Nothing is attempted for a skipped mapping (no network call, no local
	build, no sync_status write) - it is reported as ``skipped`` in the
	result, distinguishable from an actual push failure.

	``item_by_name`` is the caller's already-bulk-fetched Item map (review
	finding #1); a mapping whose ``item`` isn't in that map (e.g. a stale
	link to a deleted Item) is treated as not-disabled here, same as the
	prior per-row ``frappe.db.get_value()`` lookup returning ``None`` - the
	missing Item itself still surfaces as a local build error later, in
	``_push_product_group()``."""
	active = []
	skipped_results = []
	for mapping in mappings:
		item = item_by_name.get(mapping.item)
		item_disabled = item.disabled if item else 0
		if mapping.enabled and not item_disabled:
			active.append(mapping)
		else:
			skipped_results.append({"mapping_name": mapping.name, "ok": False, "skipped": True})
	return active, skipped_results


def _build_error_message(error):
	message = cstr(error).strip()
	return message or REMOTE_ERROR


def _config_error_message(error, account_name):
	"""Return a misconfiguration error's own message, or a generic fallback
	when that message is blank - used by ``queue_catalog_push()``'s
	per-account tolerant validation (review finding #2)."""
	message = cstr(error).strip()
	return message or _("Marketplace Account {0} is not configured for Shopify catalog push.").format(
		account_name
	)


def _format_money(price):
	"""Format a Decimal price as a plain fixed-point decimal string, as the
	GraphQL Admin API's Money scalar expects - never scientific notation. See
	docs/frappe-conventions.md's Decimal-formatting-for-external-payloads
	entry for why ``str(price)``/f-string interpolation is NOT safe here."""
	return format(price, "f")


def _format_user_errors(user_errors):
	messages = []
	for error in user_errors:
		if isinstance(error, dict):
			message = cstr(error.get("message")).strip()
			if message:
				messages.append(message)
	combined = "; ".join(messages)
	if not combined:
		return REMOTE_ERROR
	return combined[:ERROR_MESSAGE_MAX_LENGTH]


def _mark_synced(mapping_name):
	# Deliberately does not touch last_sync_reference: unlike Talabat's
	# _mark_queued() (which stores a genuinely new remote job id there), this
	# synchronous per-group mutation has no equivalent "job id" concept - the
	# product GID already lives on external_product_id, so writing it again
	# here would only duplicate data already on the row.
	frappe.db.set_value(
		MAPPING_DOCTYPE,
		mapping_name,
		{
			"sync_status": "Synced",
			"last_synced_on": now_datetime(),
			"last_error": None,
		},
		update_modified=False,
	)


def _mark_error(mapping_name, error):
	frappe.db.set_value(
		MAPPING_DOCTYPE,
		mapping_name,
		{"sync_status": "Error", "last_error": cstr(error).strip()},
		update_modified=False,
	)


def _push_result_payload(results):
	"""Named ``skipped_mappings`` (review finding #3), not ``skipped``, to stay
	textually distinct from ``queue_catalog_push()``'s ``skipped_accounts`` -
	this one counts MAPPINGS excluded as disabled (or disabled Item), not
	accounts rejected by RQ dedup."""
	total = len(results)
	truncated = total > MAX_PUSH_RESULTS_RETURNED
	skipped_mappings = sum(1 for row in results if row.get("skipped"))
	return {
		"results": results[:MAX_PUSH_RESULTS_RETURNED],
		"total": total,
		"succeeded": sum(1 for row in results if row["ok"]),
		"failed": sum(1 for row in results if not row["ok"] and not row.get("skipped")),
		"skipped_mappings": skipped_mappings,
		"truncated": truncated,
	}


def _set_push_status(job_id, state):
	frappe.cache().set_value(_push_cache_key(job_id), state, expires_in_sec=RESULT_TTL)


def _update_push_status(job_id, owner, **changes):
	state = frappe.cache().get_value(_push_cache_key(job_id)) or {"owner": owner}
	state.update(changes)
	_set_push_status(job_id, state)
	frappe.publish_realtime(
		PUSH_PROGRESS_EVENT,
		{"job_id": job_id, **{key: value for key, value in state.items() if key != "owner"}},
		user=owner,
	)


def _push_cache_key(job_id):
	return f"marketplace_integrations:shopify:catalog_push_job:{job_id}"
