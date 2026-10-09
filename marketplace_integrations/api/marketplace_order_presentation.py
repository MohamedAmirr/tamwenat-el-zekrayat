# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized, read-only live mapping and catalog-price presentation for the Marketplace Order form."""

import frappe
from frappe import _

from marketplace_integrations.services.mappings import resolve_item, resolve_mapping_and_item
from marketplace_integrations.services.order_price_presentation import (
    current_catalog_price,
    resolve_catalog_sync_account,
)


@frappe.whitelist()
def get_item_presentation(marketplace_order: str):
    """Return current exact mapping status and live catalog price reference for every stored order line.

    "Current ERPNext Catalog Price" (``current_catalog_price``) is resolved
    through #6's exact catalog price resolver, using the mapped Item and the
    Marketplace Account's configured Selling Price List - it is always a
    live, current reference, never Talabat's historical order pricing, and
    is never written back onto Marketplace Order Item. When it cannot be
    resolved (no mapping, or #6's own validation fails), no fallback to
    Item.standard_rate or another Price List is attempted;
    ``catalog_price_unavailable_reason`` carries a sanitized reason instead
    for the client to display as "Catalog Price Unavailable". Talabat's own
    stored order values (``original_list_price``, ``fulfilled_unit_price``,
    etc.) already live on Marketplace Order Item and are not recomputed or
    duplicated here - the client reads those directly off the form, as does
    ``promotion_status`` (a plain field already on ``frm.doc``, not
    duplicated into this response).

    The order's Talabat account/connection/Price List configuration is
    resolved and validated once for the whole order (not once per line) -
    see ``resolve_catalog_sync_account()`` - so a misconfigured account
    reports the same reason for every line instead of re-running the same
    expensive validation (including Talabat Connection secret decryption)
    once per line, or crashing the endpoint outright.
    """
    order = frappe.get_doc("Marketplace Order", marketplace_order)
    order.check_permission("read")
    if not frappe.has_permission("Marketplace Product Mapping", ptype="read"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    if order.platform == "Shopify":
        return {
            "items": [
                {
                    "external_item_id": row.external_item_id,
                    "external_variant_id": row.external_variant_id,
                    "mapped_item": _resolve_shopify_item(order.marketplace_account, row.external_variant_id),
                }
                for row in order.items or []
            ]
        }
    if not frappe.has_permission("Item Price", ptype="read"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    # #108: this presentation (live mapping/catalog price comparison against
    # a Talabat SKU) is Talabat-specific. A blank platform is treated as
    # Talabat, matching every existing Marketplace Order's real origin today
    # (see services/sales_order_sync.py's identical rationale) - only a
    # platform explicitly known to be something else short-circuits here.
    if order.platform and order.platform != "Talabat":
        return {"items": []}

    account, account_error = resolve_catalog_sync_account(order.marketplace_account)

    items = []
    for row in order.items or []:
        resolved = resolve_mapping_and_item(order.marketplace_account, row.external_sku)
        mapped_item = resolved["item"] if resolved else None
        mapping_name = resolved["mapping_name"] if resolved else None

        if account_error:
            price, reason = None, account_error
        else:
            price, reason = current_catalog_price(account, mapping_name, mapped_item)

        items.append(
            {
                "external_item_id": row.external_item_id,
                "external_sku": row.external_sku,
                "mapped_item": mapped_item,
                "current_catalog_price": price,
                "catalog_price_unavailable_reason": reason,
            }
        )

    return {
        "items": items,
    }


def _resolve_shopify_item(account, variant_gid):
    """Resolve a Shopify line's mapped Item via the shared resolve_item()
    (#109 decision 9), not a hand-rolled query - resolve_item()'s documented
    contract also fails closed when the mapping's OWN Marketplace Account is
    disabled, not just when the mapping row itself is disabled, which a
    hand-rolled ``{"enabled": 1}`` filter on the mapping alone would miss.

    A Shopify variant's ``variant.id`` GID is exactly what #105 stores as
    ``Marketplace Product Mapping.external_reference`` for a Shopify mapping
    (see #109's issue body "Note on variant identity") - so the variant GID
    is passed straight through as the external_reference resolve_item()
    expects, matching how every other resolve_item()/resolve_mapping_and_item()
    caller in this app already calls it.
    """
    if not variant_gid:
        return None
    return resolve_item(account, variant_gid)
