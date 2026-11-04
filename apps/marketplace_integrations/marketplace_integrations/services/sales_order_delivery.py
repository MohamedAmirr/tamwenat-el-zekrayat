# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Keep ``Sales Order.custom_earliest_delivery_date`` (the list countdown date) equal to the earliest item date.

``services/sales_order_sync.py::_apply_snapshot`` sets the initial value on a new order and keeps the field
out of the sync snapshot and hash.  From then on the field is derived from the item rows, whoever changes
them: staff (Draft edit, "Update Items") or the system (a line removed by the reconciliation, for
instance Noon Out of Stock).  The item rows hold the truth: ERPNext's ``validate_delivery_date`` resets the
header ``delivery_date`` to the LATEST item date, so a header-only edit is reverted.

``hooks.py`` registers ``sync_earliest_delivery_date`` for Sales Order:

- ``validate``: a Draft being saved (and a submit).
- ``before_update_after_submit``: a submitted order.  Frappe runs the controller method (ERPNext's
  ``validate_delivery_date``) before the ``doc_events`` handler.  "Update Items"
  (``update_child_qty_rate``) saves the item rows first, reloads the parent and saves it, so
  ``doc.items`` already holds the new dates when this runs.  A before/after comparison of the item dates
  would see nothing; the item minimum is compared with the STORED field value instead.

The value is assigned on the document and saved by the same save (the field is read-only and
``allow_on_submit``), and only when it differs, so any other edit leaves it alone.  Only marketplace orders
(``custom_marketplace_order`` set) are touched.  The handler never raises.
"""

import frappe
from frappe.utils import getdate

FIELD = "custom_earliest_delivery_date"


def earliest_delivery_date(item_dates, header_date=None):
	"""Earliest of ``item_dates`` (empty values ignored); the header date when no item has one; else None."""
	dates = [getdate(value) for value in item_dates if value]
	if dates:
		return min(dates)
	return getdate(header_date) if header_date else None


def needs_update(stored, earliest):
	"""True when there is an earliest date and it differs from the stored value."""
	if not earliest:
		return False
	return not stored or getdate(stored) != earliest


def sync_earliest_delivery_date(doc, method=None):
	"""``doc_events`` handler: set the field to the earliest item delivery date when it differs.  Never raises."""
	try:
		if not doc.get("custom_marketplace_order"):
			return
		earliest = earliest_delivery_date(
			[row.get("delivery_date") for row in doc.get("items") or []], doc.get("delivery_date")
		)
		if needs_update(doc.get(FIELD), earliest):
			doc.set(FIELD, earliest)
	except Exception:
		frappe.log_error(title="Sales Order earliest delivery date", message=frappe.get_traceback())
