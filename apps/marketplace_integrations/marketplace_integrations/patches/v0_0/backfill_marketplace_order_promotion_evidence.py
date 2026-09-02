# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Backfill #83 promotion/financial evidence for existing Marketplace Orders.

`upsert_marketplace_order()`/`normalize_order()` only started populating
`Marketplace Order.promotion_status` and `Marketplace Order Item`'s
`container_deposit`/`promotions` going forward once #83 shipped. Every
existing order already has the complete raw Talabat payload it was created
from stored in `latest_payload`, which already carries `promotion_status`,
`container_deposit`, and `promotion` - this patch re-derives just those new
fields from that stored payload through the same shared `normalize_order()`
mapper (never reimplementing the mapping itself) and writes back only the
new fields, leaving every other stored field untouched. A stored payload
that predates promotion evidence entirely still normalizes cleanly to
None/blank rather than raising.
"""

import frappe

from marketplace_integrations.integrations.talabat.order_mapper import normalize_order
from marketplace_integrations.services.marketplace_orders import _as_json


def execute():
	"""Idempotent: re-deriving the same fields from the same stored
	``latest_payload`` on a later run yields the same values again."""
	if not frappe.db.table_exists("Marketplace Order"):
		return

	for name in frappe.get_all("Marketplace Order", pluck="name"):
		doc = frappe.get_doc("Marketplace Order", name)
		payload = frappe.parse_json(doc.latest_payload) if doc.latest_payload else None
		if not isinstance(payload, dict):
			continue

		normalized = normalize_order(payload)
		doc.promotion_status = normalized.get("promotion_status")

		normalized_items_by_id = {
			frappe.utils.cstr(item.get("external_item_id")).strip(): item
			for item in normalized.get("items") or []
		}
		for row in doc.items or []:
			normalized_item = normalized_items_by_id.get(frappe.utils.cstr(row.external_item_id).strip())
			if not normalized_item:
				continue
			row.container_deposit = normalized_item.get("container_deposit")
			row.promotions = _as_json(normalized_item.get("promotions"))

		doc.save(ignore_permissions=True)
