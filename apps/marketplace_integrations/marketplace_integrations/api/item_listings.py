# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Marketplace Listings tab of the Item form (#374): the Item's Marketplace Product Mappings.

Read-only. The rows come from ``frappe.get_list`` so the mapping row filter of the platform roles
(``permissions.marketplace_product_mapping_query_conditions``, #322) applies: a Talabat User sees only
Talabat listings. Nothing here writes; the tab's sync controls call the platform endpoints that
already exist.
"""

import frappe

MAPPING = "Marketplace Product Mapping"

LISTING_FIELDS = [
	"name",
	"marketplace_account",
	"external_reference",
	"platform",
	"enabled",
	"sales_uom",
	"sync_status",
	"last_synced_on",
	"last_error",
	"inventory_sync_status",
	"inventory_last_synced_on",
	"inventory_last_error",
]

# The account's own switches for the automatic pushes. A manual sync runs even when one is off, so
# the tab only needs to tell the person ("This runs once even though automatic push is off").
ACCOUNT_FIELDS = [
	"account_name",
	"platform",
	"noon_inventory_push_enabled",
	"noon_price_push_enabled",
	"shopify_inventory_push_enabled",
]


@frappe.whitelist(methods=["GET"])
def get_item_listings(item: str):
	"""Return the Marketplace Product Mappings of ``item`` the session user may read."""
	# Document-level check (not doc-less) so a missing Item is refused and Item user permissions apply.
	frappe.has_permission("Item", "read", doc=item, throw=True)
	if not frappe.has_permission(MAPPING, "read"):
		return []

	rows = frappe.get_list(
		MAPPING,
		filters={"item": item},
		fields=LISTING_FIELDS,
		order_by="platform, marketplace_account, external_reference",
		limit_page_length=0,
	)

	accounts = {}
	listings = []
	for row in rows:
		account = accounts.get(row.marketplace_account)
		if account is None:
			account = accounts[row.marketplace_account] = _account_summary(row.marketplace_account)
		listings.append(
			{
				**row,
				"account_name": account["account_name"],
				"price_push_off": account["price_push_off"],
				"stock_push_off": account["stock_push_off"],
				"can_write": bool(frappe.has_permission(MAPPING, "write", doc=row.name)),
			}
		)
	return listings


def _account_summary(account_name):
	"""Display name and automatic-push state of one account.

	Trusted read: the account was reached through a mapping row the user is allowed to see (same
	platform), and only a title and three switches are taken from it.
	"""
	account = frappe.db.get_value("Marketplace Account", account_name, ACCOUNT_FIELDS, as_dict=True)
	if not account:
		return {"account_name": account_name, "price_push_off": False, "stock_push_off": False}
	price_push_off = stock_push_off = False
	if account.platform == "Noon":
		price_push_off = not account.noon_price_push_enabled
		stock_push_off = not account.noon_inventory_push_enabled
	elif account.platform == "Shopify":
		# Only the inventory push has a switch; the price push is manual-only.
		stock_push_off = not account.shopify_inventory_push_enabled
	return {
		"account_name": account.account_name or account_name,
		"price_push_off": bool(price_push_off),
		"stock_push_off": bool(stock_push_off),
	}
