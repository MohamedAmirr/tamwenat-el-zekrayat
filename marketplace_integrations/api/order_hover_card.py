# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Read-only summary of a Sales Order for the list hover card (#325)."""

from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

MAX_IMAGE_URL_LENGTH = 2000
FILE_PATH_PREFIXES = ("/files/", "/private/files/")


def safe_image_url(value):
	"""The item image URL if it is safe to put in an <img src>, else "".

	Allowed: absolute http(s) URLs with a host (Shopify CDN) and ERPNext file paths under
	/files/ or /private/files/. Anything else (javascript:, data:, protocol-relative, paths
	with "..", control characters, double quotes, backslash, angle brackets, userinfo, very long
	values) gives no image. File paths may hold a space and an apostrophe (Frappe keeps both in
	file names); an http(s) URL may not.
	"""
	url = cstr(value).strip()
	if not url or len(url) > MAX_IMAGE_URL_LENGTH:
		return ""
	if ".." in url or any(ord(ch) < 32 or ch in '\\"<>`' for ch in url):
		return ""
	if url.startswith(FILE_PATH_PREFIXES):
		return url
	if any(ch in " '" for ch in url):
		return ""
	try:
		parsed = urlparse(url)
	except ValueError:
		return ""
	if parsed.scheme in ("http", "https") and parsed.netloc and not parsed.username:
		return url
	return ""


@frappe.whitelist()
def get_sales_order_card(sales_order: str):
	"""Everything the Sales Order list hover card shows, for one order.

	The caller must be able to read this Sales Order. The Marketplace Order and Sync Log parts are included only when the caller can read those doctypes; without
	that permission the key is None and nothing from it is returned. All of it is on the
	Sales Order form and the linked documents too: the card never holds the only copy.
	"""
	# A missing order raises ValidationError (HTTP 417), not DoesNotExistError (HTTP 404):
	# Desk's request layer opens a modal for every 404 even with silent=True, while a 417 is
	# left to the caller, so the card can show the message itself (see docs/frappe-conventions.md).
	# Doctype-level check first, so a user who cannot read Sales Orders learns nothing about
	# which names exist; the document-level check (permission rules) follows.
	frappe.has_permission("Sales Order", "read", throw=True)
	if not frappe.db.exists("Sales Order", sales_order):
		frappe.throw(_("Sales Order {0} was not found. It may have been deleted.").format(sales_order))
	frappe.has_permission("Sales Order", "read", doc=sales_order, throw=True)

	order = frappe.db.get_value(
		"Sales Order",
		sales_order,
		["customer", "customer_name", "contact_mobile", "status", "currency", "grand_total", "rounded_total"],
		as_dict=True,
	)
	# Every item in one query, no description. Limit 0 means no limit: no cap on the item count.
	rows = frappe.get_list(
		"Sales Order Item",
		parent_doctype="Sales Order",
		filters={"parent": sales_order},
		fields=["item_code", "item_name", "qty", "image"],
		order_by="idx asc",
		limit_page_length=0,
	)
	return {
		"sales_order": sales_order,
		"customer": order.customer_name or order.customer,
		"phone": order.contact_mobile or "",
		"status": order.status,
		"currency": order.currency,
		"total": flt(order.rounded_total or order.grand_total),
		"items": [
			{
				"item_code": r.item_code or "",
				"item_name": r.item_name or "",
				"qty": flt(r.qty),
				"image": safe_image_url(r.image),
			}
			for r in rows
		],
		"marketplace": _marketplace_part(sales_order),
		"bosta": _bosta_part(sales_order),
	}


def _marketplace_part(sales_order):
	"""The linked Marketplace Order's readable summary, or None (no order, or not readable)."""
	if not frappe.has_permission("Marketplace Order", "read"):
		return None
	rows = frappe.get_list(
		"Marketplace Order",
		filters={"erpnext_sales_order": sales_order},
		fields=[
			"name",
			"platform",
			"platform_order_reference",
			"external_order_id",
			"external_financial_status",
			"last_sync_error",
		],
		order_by="creation desc, name desc",
		limit_page_length=1,
	)
	if not rows:
		return None
	row = rows[0]
	return {
		# Never the MKTORD- name: staff know the platform's own number.
		"reference": row.platform_order_reference or row.external_order_id or "",
		"platform": row.platform or "",
		"payment_status": row.external_financial_status or "",
		"last_sync_error": cstr(row.last_sync_error),
		"last_sync": _last_sync(row.name),
	}


def _last_sync(marketplace_order):
	"""Status of the latest Sync Log of the order, only when the caller can read Sync Logs."""
	if not frappe.has_permission("Marketplace Sync Log", "read"):
		return None
	rows = frappe.get_list(
		"Marketplace Sync Log",
		filters={"marketplace_order": marketplace_order},
		fields=["status"],
		order_by="attempted_at desc, creation desc",
		limit_page_length=1,
	)
	if not rows:
		return None
	return {"status": rows[0].status}


def _bosta_part(sales_order):
	"""The order's Bosta delivery counts ``{state: count}`` (the stored Sales Order summary), or None.
	It lives on the Sales Order the caller can already read, so it needs no other permission."""
	value = frappe.db.get_value("Sales Order", sales_order, "custom_shipping_status_summary")
	try:
		counts = frappe.parse_json(value) if value else None
	except Exception:
		return None
	if not isinstance(counts, dict) or not counts:
		return None
	return {"summary": {cstr(k): cint(v) for k, v in counts.items()}}
