# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe

from talabat_integration.api.client import talabat_request

# Talabat's API caps page_size at 500 (docs/talabat-integration/openapi.json).
# Fetching more than one page is out of scope for this issue - a vendor with
# more products than this is a real future limitation, not handled here.
MAX_PAGE_SIZE = 500


@frappe.whitelist()
def get_vendor_products(vendor):
	"""Return {sku, title} for every product Talabat already has on file for
	this vendor, so mapping creation can offer them as a pick-list instead of
	the user hand-typing a Talabat SKU.

	Fetches a single page (up to MAX_PAGE_SIZE products). Returns plain dicts,
	not the raw API response, per the app's whitelisted-method convention.
	"""
	vendor_doc = frappe.get_doc("Talabat Vendor", vendor)
	chain_id = frappe.get_single("Talabat Settings").chain_id

	response = talabat_request(
		"GET",
		f"/v2/chains/{chain_id}/vendors/{vendor_doc.vendor_id}/catalog",
		params={"page_size": MAX_PAGE_SIZE},
	)

	if not response.ok:
		frappe.throw(frappe._("Failed to fetch Talabat's product catalog for this vendor."))

	body = response.json()
	products = [{"sku": p["sku"], "title": p["title"]} for p in body.get("products", [])]

	return {
		"products": products,
		"truncated": body.get("total_pages", 1) > 1,
	}
