# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized Desk API boundary for the Shopify Location picker (#103).

Read-only: listing a Shopify Connection's active Locations is a live but
non-mutating Admin GraphQL query, so it's safe to expose as a plain
Desk-triggered lookup here rather than gating it like a future write would
need to be.

Permission is checked as `write` on `Marketplace Account` (#103 review
finding #7), not `read` on `Shopify Connection` - matching the analogous
Talabat precedent (`api/talabat_outlet.py`'s outlet-status actions, both
gated on Marketplace Account write), since Marketplace Account is the
document this action is actually being performed for, not the Connection it
happens to read through. This is a doctype-level check rather than a
specific-document check, deliberately - unlike Talabat's outlet actions
(which always act on an already-saved account), the picker also needs to
work on a brand-new, unsaved Marketplace Account that has no document name
yet to check permission against.

On top of that, the caller must be able to read the Shopify Connection document itself
(#322): the platform roles pass the Marketplace Account check, but Connections stay System
Manager only, and the request is made with the Connection's token.
"""

import frappe

from marketplace_integrations.integrations.shopify.locations import fetch_shop_and_active_locations


@frappe.whitelist(methods=["POST"])
def list_active_locations(connection: str):
    """Return this Shopify Connection's currently active Locations as
    ``[{"id", "name"}, ...]`` for the Marketplace Account form's Location
    picker."""
    _require_marketplace_account_write_permission()
    _require_shopify_connection_exists(connection)
    # The doc-less check above passes for any platform user (#322); the call uses this
    # Connection's token, so the caller must also be able to read the Connection itself.
    frappe.has_permission("Shopify Connection", "read", doc=connection, throw=True)
    return fetch_shop_and_active_locations(connection)["active_locations"]


def _require_shopify_connection_exists(connection):
    # #103 review finding #8: without this, a bad/nonexistent Connection name
    # (e.g. a stale value from an unsaved form) would otherwise surface as an
    # unhandled frappe.DoesNotExistError deep inside ShopifyClient instead of
    # a clean validation error here.
    if not frappe.db.exists("Shopify Connection", connection):
        frappe.throw(frappe._("Shopify Connection {0} does not exist.").format(connection))


def _require_marketplace_account_write_permission():
    if not frappe.has_permission("Marketplace Account", "write"):
        raise frappe.PermissionError
