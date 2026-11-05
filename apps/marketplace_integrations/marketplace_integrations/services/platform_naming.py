"""Platform-specific document names: Sales Order, Delivery Note and Sales Invoice of a marketplace order
get a naming series per platform (SO-SHP-2026-00001, DN-NOON-2026-00001, SI-TLB-2026-00001).

Uses ERPNext's own ``naming_series`` and the ``before_naming`` doc event (Frappe runs it in
``set_new_name`` before the name is built, so setting ``doc.naming_series`` there picks the series).
Frappe does not check ``naming_series`` against the Select options, but the options are still shipped
(fixtures/property_setter.json) so the form dropdown can show the series of an existing document.

Documents with no platform (a manual order, its note and invoice) and return documents keep the series
ERPNext chose. The hook never raises: a naming problem must not stop an order sync or a shipment.
"""

import frappe

# The ``.YYYY.-`` series syntax and the default five-digit counter match ERPNext's own series, so the
# result is e.g. SO-SHP-2026-00001. The platform is the Marketplace Order ``platform`` value.
SERIES_BY_PLATFORM = {
	"Shopify": {
		"Sales Order": "SO-SHP-.YYYY.-",
		"Delivery Note": "DN-SHP-.YYYY.-",
		"Sales Invoice": "SI-SHP-.YYYY.-",
	},
	"Noon": {
		"Sales Order": "SO-NOON-.YYYY.-",
		"Delivery Note": "DN-NOON-.YYYY.-",
		"Sales Invoice": "SI-NOON-.YYYY.-",
	},
	"Talabat": {
		"Sales Order": "SO-TLB-.YYYY.-",
		"Delivery Note": "DN-TLB-.YYYY.-",
		"Sales Invoice": "SI-TLB-.YYYY.-",
	},
}

# The Sales Order link field on each document's item rows.
_ORDER_LINK_FIELD = {"Delivery Note": "against_sales_order", "Sales Invoice": "sales_order"}


def series_for(platform, doctype):
	"""The naming series of this platform and doctype, or None (unknown platform or doctype)."""
	return SERIES_BY_PLATFORM.get(platform or "", {}).get(doctype)


def _order_platform(platform, marketplace_order):
	"""A Sales Order's platform: its own ``custom_marketplace_platform``, else the one of its Marketplace Order."""
	if not platform and marketplace_order:
		platform = frappe.db.get_value("Marketplace Order", marketplace_order, "platform")
	return platform or None


def platform_of(doc):
	"""The marketplace platform a document belongs to, or None.

	A Sales Order carries it in ``custom_marketplace_platform`` (fetched from its Marketplace Order during
	insert, before naming; the Marketplace Order is read as a fallback). A Delivery Note or Sales Invoice
	takes it from the Sales Order of its first item row that has one, with the same fallback."""
	if doc.doctype == "Sales Order":
		return _order_platform(doc.get("custom_marketplace_platform"), doc.get("custom_marketplace_order"))

	link_field = _ORDER_LINK_FIELD.get(doc.doctype)
	if not link_field:
		return None
	for row in doc.get("items") or []:
		sales_order = row.get(link_field)
		if sales_order:
			values = frappe.db.get_value(
				"Sales Order", sales_order, ["custom_marketplace_platform", "custom_marketplace_order"]
			)
			return _order_platform(*values) if values else None
	return None


def set_naming_series(doc, method=None):
	"""``before_naming`` hook: a marketplace document gets its platform series. Anything else is left alone."""
	try:
		if doc.get("is_return"):
			return
		series = series_for(platform_of(doc), doc.doctype)
		if series:
			doc.naming_series = series
	except Exception:
		frappe.log_error(title="Platform naming series", message=frappe.get_traceback())
