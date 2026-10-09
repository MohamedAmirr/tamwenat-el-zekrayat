# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Read-only current-catalog-price comparison for Marketplace Order presentation.

Reuses the exact #6 Talabat catalog price resolver
(``integrations.talabat.catalog_sync.get_listing_price``) so the "Current
ERPNext Catalog Price" shown on a Marketplace Order is always the same value
catalog sync itself would compute for that mapping right now - never a
separately maintained price lookup, and never a fallback to
``Item.standard_rate`` or any Price List other than the account's configured
``erpnext_selling_price_list``.

This module never writes to any document. It is a pure read/compare helper
for presentation only.
"""

import frappe
from frappe import _
from frappe.utils import cstr

from marketplace_integrations.integrations.talabat.catalog_sync import (
    get_listing_price,
    validate_catalog_sync_account,
)
from marketplace_integrations.services.listing_price import CatalogBuildError


def not_mapped_reason():
    """Return the "no mapping" unavailable-price reason, translated for the current request.

    Called per-request rather than cached at import time - a module-level
    ``_()`` binds whatever language the first request to load this module
    happened to be in, for every later request in the same worker
    regardless of that request's own language.
    """
    return _("No ERPNext Item is mapped to this Talabat listing.")


def resolve_catalog_sync_account(marketplace_account):
    """Resolve and validate a Marketplace Order's Talabat account once per order.

    Returns ``(account_doc, error_reason)``. ``account_doc`` is the fully
    validated Marketplace Account (per #6's
    ``validate_catalog_sync_account()``) when catalog sync configuration is
    intact, else ``None`` with a short, already-translated ``error_reason``.

    Callers should call this exactly once per order and pass the resulting
    ``account_doc`` into ``current_catalog_price()`` for every line, instead
    of re-resolving/re-validating the account (and its Talabat Connection,
    including secret decryption, and Price List) once per line - mirrors the
    same hoisting ``integrations/talabat/catalog_sync.py``'s
    ``build_catalog_batch()`` already does for its own per-mapping loop. When
    the account itself is misconfigured, every order line should report this
    same ``error_reason`` rather than the endpoint crashing outright.
    """
    try:
        account_doc, _connection, _price_list = validate_catalog_sync_account(marketplace_account)
        return account_doc, None
    except frappe.ValidationError as error:
        frappe.clear_last_message()
        return None, _error_message(
            error,
            _("Marketplace Account {0} could not be validated for catalog price presentation.").format(
                marketplace_account
            ),
        )


def current_catalog_price(account, mapping_name, mapped_item):
    """Return ``(price, unavailable_reason)`` for one order line's live catalog reference.

    ``account`` is the caller's already-resolved and validated Marketplace
    Account document from ``resolve_catalog_sync_account()`` - this function
    does not re-validate it. ``mapping_name`` and ``mapped_item`` are the
    caller's already-resolved ``resolve_mapping_and_item()`` result (or
    ``None``/``None`` when there is no mapping) - this function does not
    re-resolve them.

    ``price`` is a ``float`` when resolvable, else ``None``.
    ``unavailable_reason`` is a short, already-translated, user-facing
    string when ``price`` is ``None``, else ``None``. This is exactly #6's
    own ``get_listing_price()`` validation failure message (e.g. no current
    Item Price for the listing UOM, no positive UOM conversion factor) -
    never a raw traceback - or this module's own "not mapped" message when
    there is no mapping at all. Nothing here falls back to
    ``Item.standard_rate`` or another Price List; an unresolvable price is
    always reported as unavailable, not guessed.
    """
    if not mapped_item or not mapping_name:
        return None, not_mapped_reason()

    try:
        price = get_listing_price(account, mapping_name, mapped_item)
    except (CatalogBuildError, frappe.DoesNotExistError) as error:
        # Only a local mapping/item-level build problem or a deleted
        # mapping/item is expected here - account-level misconfiguration is
        # already handled once by resolve_catalog_sync_account() before this
        # is ever called. An unrelated real bug (e.g. a renamed doctype or a
        # broken link) is deliberately left to propagate as an actual
        # exception rather than being folded into "Catalog Price
        # Unavailable".
        frappe.clear_last_message()
        return None, _error_message(
            error,
            _(
                "Marketplace Product Mapping {0} could not be priced from its ERPNext Item configuration."
            ).format(mapping_name),
        )

    return float(price), None


def _error_message(error, fallback):
    """Return ``error``'s message, or ``fallback`` when that message is empty.

    Mirrors ``integrations/talabat/catalog_sync.py``'s ``_build_error()`` -
    a caught validation exception's message can legitimately be blank (e.g.
    a bare ``frappe.ValidationError()``), and the UI must never show
    "Catalog Price Unavailable: " with nothing after the colon.
    """
    message = cstr(error).strip()
    return message or fallback
