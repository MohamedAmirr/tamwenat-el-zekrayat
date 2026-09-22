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
