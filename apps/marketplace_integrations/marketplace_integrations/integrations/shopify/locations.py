# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Shopify shop identity and inventory Location lookups (#103).

Both `shop { id }` and `locations { ... }` are read-only Admin GraphQL
queries - they never mutate anything on the live store - so, per this
project's read-vs-write rule, it's safe for them to run automatically
wherever they're needed: `Marketplace Account.validate()` (cross-checking
`external_account_id` against the connection's real shop, and an assigned
Location against the store's real active Locations) as well as the Desk
Location picker (`api/shopify_locations.py`). Only a future Shopify
*mutation* (e.g. #104's webhook registration, or any later product/inventory
write) must be gated behind an explicit user-triggered action instead.

One combined query covers both concerns so validating a Shopify Marketplace
Account on save costs exactly one Shopify API call, not two.
"""

from marketplace_integrations.integrations.shopify.client import ShopifyClient

# 250 is Shopify's normal max page size for a paginated connection field. A
# cursor-pagination helper is explicitly out of scope here (#105, no caller
# yet) - one real store realistically has well under 250 Locations, so a
# single page is enough for this lookup and this is not a reusable pagination
# pattern.
LOCATIONS_PAGE_SIZE = 250

SHOP_AND_ACTIVE_LOCATIONS_QUERY = """
query MarketplaceAccountShopifyVerification($first: Int!) {
  shop {
    id
  }
  locations(first: $first) {
    edges {
      node {
        id
        name
        isActive
      }
    }
  }
}
"""


def fetch_shop_and_active_locations(connection_name):
    """Return ``{"shop_id": <shop GID or None>, "active_locations": [{"id", "name"}, ...]}``
    for one Shopify Connection, via a single live read-only Admin GraphQL call.

    Only Locations with ``isActive == true`` are ever returned - filtered here
    explicitly rather than trusted to Shopify's own `locations` query default,
    so a change in that default can never silently start surfacing an
    inactive Location as selectable/valid.
    """
    client = ShopifyClient(connection_name)
    data = client.execute(SHOP_AND_ACTIVE_LOCATIONS_QUERY, {"first": LOCATIONS_PAGE_SIZE})

    shop = data.get("shop") or {}
    edges = (data.get("locations") or {}).get("edges") or []
    nodes = [edge.get("node") for edge in edges if isinstance(edge, dict) and edge.get("node")]
    active_locations = [
        {"id": node.get("id"), "name": node.get("name")} for node in nodes if node.get("isActive")
    ]

    return {"shop_id": shop.get("id"), "active_locations": active_locations}
