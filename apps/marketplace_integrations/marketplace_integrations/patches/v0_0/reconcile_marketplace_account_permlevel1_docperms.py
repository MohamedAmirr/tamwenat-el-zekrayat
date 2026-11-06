# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Copy the permission-level 1 rows shipped for Marketplace Account into its Custom DocPerm rows.

The Shopify "Sync Orders From" field (``shopify_orders_sync_from``) is permission level 1, editable
by System Manager only. A site whose Marketplace Account already has Custom DocPerm rows ignores the
permission rows in the doctype JSON, so the new level 1 rows would never apply there. This re-runs
the #322 reconciliation restricted to the shipped permission level 1 rows (permission level 0 rows
a site removed are not re-granted). It is idempotent and only inserts missing rows or raises ptypes
for the platform roles and System Manager (see ``reconcile_platform_custom_docperms``). A site
without Custom DocPerm rows for the doctype is left alone.
"""

from marketplace_integrations.patches.v0_0.reconcile_platform_custom_docperms import reconcile


def execute():
	reconcile(["Marketplace Account"], permlevel=1)
