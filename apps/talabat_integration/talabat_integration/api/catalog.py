# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import cint

from talabat_integration.api.client import talabat_request

# Talabat's API caps page_size at 500 (docs/talabat-integration/openapi.json).
# Fetching more than one page is out of scope for this issue - a vendor with
# more products than this is a real future limitation, not handled here.
MAX_PAGE_SIZE = 500


@frappe.whitelist()
def get_vendor_products(vendor, page=1):
	"""Return {sku, title} for every product Talabat already has on file for
	this vendor, so mapping creation can offer them as a pick-list instead of
	the user hand-typing a Talabat SKU.

	Fetches one page (up to MAX_PAGE_SIZE products) - page defaults to 1 for
	backward compatibility with the single-fetch flow this was originally
	built for. Returns plain dicts, not the raw API response, per the app's
	whitelisted-method convention.
	"""
	vendor_doc = frappe.get_doc("Talabat Vendor", vendor)
	vendor_doc.check_permission("read")
	chain_id = frappe.get_single("Talabat Settings").chain_id

	response = talabat_request(
		"GET",
		f"/v2/chains/{chain_id}/vendors/{vendor_doc.vendor_id}/catalog",
		params={"page_size": MAX_PAGE_SIZE, "page": page},
	)

	if not response.ok:
		frappe.throw(frappe._("Failed to fetch Talabat's product catalog for this vendor."))

	body = response.json()
	products = [{"sku": p.get("sku"), "title": p.get("title")} for p in body.get("products", [])]

	return {
		"products": products,
		"truncated": body.get("total_pages", 1) > 1,
	}


@frappe.whitelist()
def get_vendor_catalog_page(vendor, page=1, page_size=20):
	"""Live Talabat catalog page for `vendor`, cross-referenced against local
	Talabat Product Mapping rows so the catalog-mapping page can show
	mapped/not-mapped status and the current item without storing anything
	from Talabat locally (title/price are display-only, per this app's
	convention - see architecture.md).

	`page_size` is chosen by the user on the catalog-mapping page and passed
	through to Talabat (whose spec allows 1-500; clamped here so an
	out-of-range value fails closed instead of erroring remotely).
	"""
	vendor_doc = frappe.get_doc("Talabat Vendor", vendor)
	vendor_doc.check_permission("read")
	chain_id = frappe.get_single("Talabat Settings").chain_id
	page_size = min(max(cint(page_size), 1), MAX_PAGE_SIZE)

	response = talabat_request(
		"GET",
		f"/v2/chains/{chain_id}/vendors/{vendor_doc.vendor_id}/catalog",
		params={"page_size": page_size, "page": page},
	)
	if not response.ok:
		frappe.throw(frappe._("Failed to fetch Talabat's product catalog for this vendor."))

	body = response.json()

	# Every mapping of this vendor (not just the SKUs on the requested page) -
	# the mapping page uses this to keep already-mapped Items out of the item
	# picker so the same product can't be mapped to a second SKU.
	mappings = frappe.get_all(
		"Talabat Product Mapping",
		filters={"vendor": vendor},
		fields=["talabat_sku", "item"],
	)
	item_by_sku = {row.talabat_sku: row.item for row in mappings}

	rows = [
		{
			"sku": p.get("sku"),
			"title": p.get("title"),
			"price": p.get("price"),
			"item": item_by_sku.get(p.get("sku")),
		}
		for p in body.get("products", [])
	]

	return {
		"rows": rows,
		"has_more": body.get("total_pages", 1) > int(page),
		"mappings": mappings,
	}


@frappe.whitelist()
def upsert_mapping(vendor, talabat_sku, item):
	"""Create or update the Talabat Product Mapping for (vendor, talabat_sku),
	setting/changing its item. Used by the catalog-mapping page's inline
	item picker.
	"""
	existing = frappe.db.get_value(
		"Talabat Product Mapping", {"vendor": vendor, "talabat_sku": talabat_sku}, "name"
	)
	if existing:
		doc = frappe.get_doc("Talabat Product Mapping", existing)
		doc.item = item
		doc.save()
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"vendor": vendor,
				"talabat_sku": talabat_sku,
				"item": item,
			}
		).insert()

	return {"name": doc.name}
