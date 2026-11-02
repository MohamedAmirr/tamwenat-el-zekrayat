# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Give Item Manager its shipped Marketplace Product Mapping permission row (#373).

A site with Custom DocPerm rows on Marketplace Product Mapping ignores the DocPerm rows in the
doctype JSON, so the new Item Manager row would never apply there. This reuses the reconcile
of ``reconcile_platform_custom_docperms`` for that one doctype and role. A doctype without
Custom DocPerm rows is left alone (migrate syncs its JSON). A second run changes nothing.
"""

from marketplace_integrations.patches.v0_0.reconcile_platform_custom_docperms import reconcile

DOCTYPE = "Marketplace Product Mapping"


def execute():
	reconcile([DOCTYPE], roles=("Item Manager",))
