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
