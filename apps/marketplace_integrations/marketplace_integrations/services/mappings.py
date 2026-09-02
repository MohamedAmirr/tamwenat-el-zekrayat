# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.query_builder import DocType
from frappe.utils import cstr

from marketplace_integrations.marketplace_integrations.doctype.marketplace_product_mapping.\
marketplace_product_mapping import compute_mapping_identity_key


def _resolve(marketplace_account, external_reference):
    """Shared exact-identity resolution behind resolve_item()/resolve_mapping()/
    resolve_mapping_and_item().

    This is the one query implementation for "this marketplace account said
    this external reference" - per
    docs/marketplace-integrations/architecture.md's "exact mapping
    resolution" boundary. Resolution is exact and deliberately dumb: it looks
    up the single stored Marketplace Product Mapping row whose
    (marketplace_account, external_reference) pair matches, via the same
    normalized identity key the doctype itself enforces uniqueness on. There
    is no fuzzy/partial matching and no fallback to external_sku/
    external_barcode/external_variant_id if external_reference doesn't match
    - core mapping code must not guess which identifier a platform's
    external_reference actually is (SKU, barcode, product ID, variant ID,
    ...); that decision is entirely the calling platform adapter's
    responsibility when it decides what to pass as external_reference in the
    first place.

    Only *enabled* mappings resolve, and only when the mapping's Marketplace
    Account is *also* enabled - a disabled mapping OR a disabled account is
    treated the same as no mapping at all, so callers fail closed rather than
    silently continuing to sync/fulfil against a mapping an operator has
    intentionally turned off, or whose whole account has been disabled while
    its individual mappings were left enabled.

    Blank/missing marketplace_account or external_reference are treated as
    "no mapping" rather than an error - this normalizes its own inputs and
    returns None early for either, rather than propagating
    compute_mapping_identity_key()'s frappe.throw() (that throw is correct
    for the doctype's own before_save() write-path guard, but every public
    resolver here is a read-only "mapping or None" contract).

    Returns a dict with both ``name`` (the mapping document name) and
    ``item`` (the mapped Item name), or None if no matching mapping with
    both the mapping and its account enabled exists for this exact pair.
    """
    marketplace_account = cstr(marketplace_account).strip()
    external_reference = cstr(external_reference).strip()
    if not marketplace_account or not external_reference:
        return None

    key = compute_mapping_identity_key(marketplace_account, external_reference)

    MPM = DocType("Marketplace Product Mapping")
    MA = DocType("Marketplace Account")

    result = (
        frappe.qb.from_(MPM)
        .join(MA)
        .on(MA.name == MPM.marketplace_account)
        .select(MPM.name, MPM.item)
        .where(MPM.mapping_identity_key == key)
        .where(MPM.enabled == 1)
        .where(MA.enabled == 1)
        .limit(1)
        .run(as_dict=True)
    )
    return result[0] if result else None


def resolve_item(marketplace_account, external_reference):
    """Resolve the ERPNext Item mapped to an external marketplace listing.

    This is the one runtime entry point platform-specific adapter code (e.g.
    Talabat order/catalog handling) should call to go from
    "this marketplace account said this external reference" to "this ERPNext
    Item". See _resolve() above for the shared resolution semantics.

    Returns the mapped Item's name (str), or None if no matching mapping
    with both the mapping and its account enabled exists for this exact
    pair.
    """
    resolved = _resolve(marketplace_account, external_reference)
    return resolved["item"] if resolved else None


def resolve_mapping(marketplace_account, external_reference):
    """Resolve the Marketplace Product Mapping document name for an external listing.

    Same exact resolution semantics as resolve_item() above (see _resolve())
    - just returns the mapping's own document name instead of the mapped
    Item. Callers that need both facts together for the same external
    reference (e.g. #84's catalog price comparison) should call
    resolve_mapping_and_item() instead of calling this and resolve_item()
    separately, which would otherwise run the identical query twice.
    """
    resolved = _resolve(marketplace_account, external_reference)
    return resolved["name"] if resolved else None


def resolve_mapping_and_item(marketplace_account, external_reference):
    """Resolve both the mapping name and the mapped Item in a single query.

    Same exact resolution semantics as resolve_item()/resolve_mapping()
    above (see _resolve()). Returns a dict {"mapping_name": str, "item":
    str}, or None if there is no matching enabled mapping with its enabled
    Marketplace Account - use this instead of calling resolve_item() and
    resolve_mapping() separately for the same (marketplace_account,
    external_reference) pair.
    """
    resolved = _resolve(marketplace_account, external_reference)
    if not resolved:
        return None
    return {"mapping_name": resolved["name"], "item": resolved["item"]}
