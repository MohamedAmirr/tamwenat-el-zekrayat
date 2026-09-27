# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Shared Shopify integration constants."""

SHOPIFY_TOKEN_PATH = "/admin/oauth/access_token"

# Pinned Admin GraphQL API version - see docs/shopify/README.md's "GraphQL
# Admin API (for #102)" section for the source and support window. Re-check
# docs/api/usage/versioning before bumping this later; do not assume it stays
# current.
SHOPIFY_API_VERSION = "2026-07"
SHOPIFY_GRAPHQL_PATH = f"/admin/api/{SHOPIFY_API_VERSION}/graphql.json"

# Public webhook transport boundary for #104 - see
# api/shopify_webhooks.py::webhook_event(). One endpoint handles every
# subscribed topic; Shopify's X-Shopify-Topic header tells the receiver which
# one a given delivery is.
SHOPIFY_WEBHOOK_PATH = "/api/method/marketplace_integrations.api.shopify_webhooks.webhook_event"

# SHOPIFY_WEBHOOK_TOPIC_HEADER_TO_ENUM below is the single source of truth
# for the 7 topics #104 registers/accepts - order lifecycle events, refunds,
# and the two app-lifecycle topics (APP_UNINSTALLED to safely disable a
# connection on uninstall; APP_SCOPES_UPDATE is registered/acknowledged for
# future use and as evidence of a scope change, not because it currently
# triggers any reaction - see _handle_topic()). Deliberately does not include
# inventory/fulfillment/tracking topics - out of scope per #104's issue body.
#
# This mapping's keys are Shopify's real webhook DELIVERY topic vocabulary -
# a lowercase slash form (e.g. "orders/create") read off the X-Shopify-Topic
# header - which is a different wire format from the GraphQL
# WebhookSubscriptionTopic enum form used to register/inspect subscriptions
# (e.g. "ORDERS_CREATE", this mapping's values). These are two distinct wire
# formats for the same set of topics, not a typo. The header-form strings are
# not sourced from docs/shopify/README.md (which only documents the enum
# form) - they're Shopify's well-known, publicly documented webhook delivery
# topic naming convention (lowercase "resource/action"), disclosed here per
# this project's ground rule on external information.
SHOPIFY_WEBHOOK_TOPIC_HEADER_TO_ENUM = {
	"orders/create": "ORDERS_CREATE",
	"orders/updated": "ORDERS_UPDATED",
	"orders/cancelled": "ORDERS_CANCELLED",
	"orders/paid": "ORDERS_PAID",
	"refunds/create": "REFUNDS_CREATE",
	"app/uninstalled": "APP_UNINSTALLED",
	"app/scopes_update": "APP_SCOPES_UPDATE",
}

# The GraphQL WebhookSubscriptionTopic enum form of the same 7 topics, used
# only for webhookSubscriptions/webhookSubscriptionCreate/
# webhookSubscriptionDelete calls - derived from
# SHOPIFY_WEBHOOK_TOPIC_HEADER_TO_ENUM.values() (the "richer" structure, keyed
# by the wire format an actual delivery arrives in) rather than kept as an
# independent tuple, so the registration list and the delivery-routing table
# can never silently drift apart - a topic added to only one of the two would
# otherwise 200-and-drop every delivery for it with no error anywhere.
SHOPIFY_WEBHOOK_TOPICS = tuple(SHOPIFY_WEBHOOK_TOPIC_HEADER_TO_ENUM.values())

# Catalog sweep (#105) - see integrations/shopify/products.py.
#
# 250 is Shopify's normal max page size for a paginated connection field -
# same rationale/value as integrations/shopify/locations.py's
# LOCATIONS_PAGE_SIZE, but here it bounds a cursor-paginated walk of the
# WHOLE catalog rather than a single page.
SHOPIFY_VARIANTS_PAGE_SIZE = 250

# Bounded chunk size for the batched `nodes(ids: [...])` deleted-mapping
# lookup - the first such batched/multi-id lookup in this codebase (#105
# gap-check #2), so there is no existing chunking helper to reuse. Kept well
# under any reasonable GraphQL query-complexity/list-argument-length limit.
SHOPIFY_NODES_LOOKUP_CHUNK_SIZE = 50

# Catalog push (#106) - see integrations/shopify/catalog_push.py.
#
# Bounded chunk size for the `variants` array passed to one
# `productVariantsBulkUpdate` call, so a single Shopify product with more
# mapped variants than this doesn't fail (or silently truncate) as one
# oversized mutation call. shopify.dev does not clearly document a hard
# per-call limit for `ProductVariantsBulkInput` arrays as of this writing, so
# this is a conservative, not a verified, default - re-check against
# shopify.dev before raising it.
SHOPIFY_CATALOG_PUSH_VARIANT_CHUNK_SIZE = 100

# Inventory sync (#107) - see integrations/shopify/inventory_sync.py. Every
# value below marked "unverified" could not be checked against a live store;
# see that module's docstring and docs/shopify/README.md "Inventory (#107)".
#
# Mappings per read/`inventorySetQuantities` call. shopify.dev did not
# document a maximum `quantities` length per call in what was fetched
# (unverified), so this is a conservative default equal to the `nodes(ids:)`
# lookup chunk above - re-check before raising it.
SHOPIFY_INVENTORY_SYNC_CHUNK_SIZE = SHOPIFY_NODES_LOOKUP_CHUNK_SIZE

# Bounded retries after a stale-compare (`COMPARE_FAILED`) rejection: re-read
# the whole chunk and resend it, at most this many times, then mark the chunk's
# mappings Error (#107 decision 8).
SHOPIFY_INVENTORY_MAX_STALE_RETRIES = 3

# The quantity name written. `on_hand` (not `available`) so Shopify derives
# `available = on_hand - committed` itself (#107 decision 3). Verified as a
# valid `InventorySetQuantitiesInput.name` value on shopify.dev 2026-09-24.
SHOPIFY_INVENTORY_QUANTITY_NAME = "on_hand"

# `InventorySetQuantitiesInput.reason` (String!). VERIFIED 2026-09-24
# (shopify.dev `mutations/inventorySetQuantities`): "correction" is a documented
# example value; the full list of accepted values is not stated there.
SHOPIFY_INVENTORY_REASON = "correction"

# `InventorySetQuantitiesInput.referenceDocumentUri` (String!). VERIFIED
# 2026-09-24 (same page): the documented example style is
# `logistics://some.warehouse/take/<timestamp>`; no format rules are stated and
# the URI below matches that style.
SHOPIFY_INVENTORY_REFERENCE_DOCUMENT_URI = "logistics://marketplace-integrations/erpnext-stock-sync"

# The stale-compare userError code. UNVERIFIED: seen in one shopify.dev fetch on
# 2026-09-24, while a later fetch said the code enum is not listed. A wrong
# guess is bounded: a non-matching code takes the per-item fallback, which
# skip-if-equals on its own re-read.
SHOPIFY_INVENTORY_STALE_COMPARE_CODE = "COMPARE_FAILED"

# Automatic triggers (#107 PR2, decisions 1/2) - see
# integrations/shopify/inventory_sync.py's sweep_changed_bins() /
# reconcile_all_accounts().
#
# Changed-Bin sweep interval: every 2 minutes, registered via hooks.py's
# scheduler_events["cron"] (Frappe has no built-in "every_n_minutes" bucket
# like "hourly"/"daily", so cron syntax is required for a 2-minute cadence).
SHOPIFY_INVENTORY_SWEEP_CRON = "*/2 * * * *"

# Overlap window (decision 1's "start at 10"): each sweep tick re-reads Bins
# back to (watermark - this many minutes), not just since the watermark
# itself - re-scanning a bit of already-covered time is cheap (a Bin query)
# and is what makes a missed/failed enqueue on one tick recoverable on a
# later tick within this window, without a second retry mechanism.
SHOPIFY_INVENTORY_SWEEP_OVERLAP_MINUTES = 10

# Deliberately NO account-count limit on the sweep/reconciliation account
# list (unlike integrations/shopify/webhooks.py's SWEEP_BATCH_LIMIT): this
# project's Shopify Connection is scoped to a single store (see
# docs/marketplace-integrations/architecture.md), so the number of enabled
# Shopify Marketplace Accounts is expected to stay tiny. Unlike the webhook
# retry sweep (which processes a bounded batch and drains any remainder over
# subsequent hourly runs because handled ROWS leave the query's WHERE
# clause), every enabled Shopify Marketplace Account matches this query on
# EVERY tick forever - an `ORDER BY ... LIMIT N` here would silently starve
# any account sorted past position N rather than "drain" anything. Revisit if
# this project ever manages more than a handful of Shopify accounts.
