# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Backfill #85's fulfilled_total_price_missing flag for existing Marketplace
Order Item rows.

`upsert_marketplace_order()`/`normalize_order()` only started computing
`Marketplace Order Item.fulfilled_total_price_missing` (whether Talabat's raw
`item.pricing.total_price` was absent/null, as opposed to a real `0`) once
#85 shipped. Every existing order already has the complete raw Talabat
payload it was created from stored in `latest_payload`; this patch re-derives
just this one new field from that stored payload through the same shared
`normalize_order()` mapper (never reimplementing the mapping itself) and
writes back only that field, leaving every other stored field untouched.

Each order is processed independently, in its own try/except: a malformed
``latest_payload`` (non-JSON, or JSON that isn't the expected order.Order
shape - `frappe.parse_json()`/`normalize_order()` can raise on either) is
logged and skipped rather than aborting the whole site migration over one
historical row (#85 review finding #3). The write itself is a direct
``frappe.db.set_value()`` per child row rather than loading/validating/saving
the parent ``Marketplace Order`` - that document's own `before_save()`
re-runs full account/price-list/warehouse/mapping validation on every save,
which is both unnecessary and a second, larger failure surface for backfilling
one read-only flag (#85 review finding #4).
"""

import frappe

from marketplace_integrations.integrations.talabat.order_mapper import normalize_order


def execute():
	"""Idempotent: re-deriving the same field from the same stored
	``latest_payload`` on a later run yields the same value again - a row
	already matching its derived value is simply not written to again."""
	if not frappe.db.table_exists("Marketplace Order"):
		return

	for name in frappe.get_all("Marketplace Order", pluck="name"):
		try:
			_backfill_one_order(name)
		except Exception:
			frappe.log_error(
				title="Backfill fulfilled_total_price_missing",
				message=frappe.get_traceback(),
				reference_doctype="Marketplace Order",
				reference_name=name,
			)


def _backfill_one_order(name):
	latest_payload = frappe.db.get_value("Marketplace Order", name, "latest_payload")
	if not latest_payload:
		return
	payload = frappe.parse_json(latest_payload)
	if not isinstance(payload, dict):
		return

	normalized = normalize_order(payload)
	normalized_items_by_id = {
		frappe.utils.cstr(item.get("external_item_id")).strip(): item
		for item in normalized.get("items") or []
	}
	if not normalized_items_by_id:
		return

	rows = frappe.get_all(
		"Marketplace Order Item",
		filters={"parent": name, "parenttype": "Marketplace Order"},
		fields=["name", "external_item_id", "fulfilled_total_price_missing"],
	)
	for row in rows:
		normalized_item = normalized_items_by_id.get(frappe.utils.cstr(row.external_item_id).strip())
		if not normalized_item:
			continue
		missing = normalized_item.get("fulfilled_total_price_missing") or 0
		if row.fulfilled_total_price_missing != missing:
			frappe.db.set_value(
				"Marketplace Order Item",
				row.name,
				"fulfilled_total_price_missing",
				missing,
				update_modified=False,
			)
