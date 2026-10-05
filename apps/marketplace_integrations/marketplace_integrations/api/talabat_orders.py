# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized API boundary for manual Talabat order refresh and vendor
order-history import.

Both methods here are thin: they authorize the caller against the specific
document/account involved, delegate HTTP retrieval to
integrations/talabat/orders.py, normalize the response via
integrations/talabat/order_mapper.py, and persist through
services/marketplace_orders.py's upsert_marketplace_order(). Neither
retrieval nor normalization has any side effects of its own - only this
module calls persistence, and only in response to an explicit manual action
(refresh/import), never automatically.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from marketplace_integrations.integrations.talabat import orders as talabat_orders
from marketplace_integrations.integrations.talabat.order_events import log_order_import_failure
from marketplace_integrations.integrations.talabat.order_mapper import normalize_order
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order


@frappe.whitelist(methods=["POST"])
def refresh_order(marketplace_order: str):
	"""Re-fetch one Marketplace Order's current Talabat state and reconcile
	it against the stored record.

	This is a manual reconciliation/recovery action, not the normal webhook
	path (#58).
	"""
	doc = frappe.get_doc("Marketplace Order", marketplace_order)
	doc.check_permission("write")

	raw_order = talabat_orders.get_order(doc.marketplace_account, doc.external_order_id)
	normalized = normalize_order(raw_order)
	if normalized["external_order_id"] != cstr(doc.external_order_id).strip():
		frappe.throw(
			_(
				"Talabat returned order_id {0}, which does not match Marketplace Order {1}'s "
				"External Order ID {2}."
			).format(normalized["external_order_id"], doc.name, doc.external_order_id)
		)

	updated = upsert_marketplace_order(doc.marketplace_account, normalized)
	return {"name": updated.name}


@frappe.whitelist(methods=["POST"])
def import_vendor_orders(account: str, start: str, end: str):
	"""Import Talabat vendor order history for an explicit date range,
	idempotently upserting every order across every page.

	Re-importing the same range is safe: each order is upserted by its
	(Marketplace Account, Talabat order_id) identity, the same identity a
	live webhook (#58) will use.

	``start``/``end`` are passed straight through to Talabat's
	``start_time``/``end_time`` query parameters (see
	integrations/talabat/orders.py's get_vendor_orders()) and are
	interpreted as UTC directly, matching Talabat's own wire format - no
	local-timezone conversion is applied to them here.

	Runs synchronously in the request (unbounded paging, still inside the
	whitelisted call) - queuing this as a background job, following the
	job/progress/errors pattern already established in
	services/item_import_jobs.py, is deferred to a follow-up issue rather
	than done here.

	Each order is normalized and upserted inside its own DB savepoint
	(same pattern as api/item_import.py's _apply_items_import() - see
	docs/frappe-conventions.md's "Bulk-write loops..." note): a failure on
	one order (caught as ValidationError/PermissionError/
	DuplicateEntryError) is rolled back to that order's savepoint and
	reported in the returned ``errors`` list, without discarding orders
	already imported earlier in the same call or aborting the rest of the
	page/import.
	"""
	account_doc = frappe.get_doc("Marketplace Account", account)
	account_doc.check_permission("read")
	if not (
		frappe.has_permission("Marketplace Order", ptype="create")
		and frappe.has_permission("Marketplace Order", ptype="write")
	):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	start = cstr(start).strip()
	end = cstr(end).strip()
	if not start or not end:
		frappe.throw(_("Start and end are both required to import vendor order history."))

	imported = []
	errors = []
	page = 1
	page_size = talabat_orders.MAX_PAGE_SIZE
	while True:
		body = talabat_orders.get_vendor_orders(
			account_doc.name,
			start_created_at_datetime=start,
			end_created_at_datetime=end,
			page=page,
			page_size=page_size,
		)
		for raw_order in body.get("orders") or []:
			order_id = cstr((raw_order or {}).get("order_id")).strip()
			savepoint_name = "talabat_import_order_" + frappe.generate_hash(length=10)
			frappe.db.savepoint(savepoint_name)
			try:
				normalized = normalize_order(raw_order)
				updated = upsert_marketplace_order(account_doc.name, normalized)
			except (frappe.ValidationError, frappe.PermissionError, frappe.DuplicateEntryError) as exc:
				frappe.db.rollback(save_point=savepoint_name)
				errors.append({"order_id": order_id, "error": str(exc)})
				# Persisted for the Needs Attention report (#169). Not
				# committed here: the request commits at its end, like the
				# successfully imported orders it sits beside.
				log_order_import_failure(account_doc.name, order_id, str(exc), "Manual")
			else:
				frappe.db.release_savepoint(savepoint_name)
				imported.append(updated.name)

		total_pages = cint(body.get("total_pages") or 1)
		if page >= total_pages:
			break
		page += 1

	return {"imported": imported, "count": len(imported), "errors": errors}


@frappe.whitelist(methods=["POST"])
def update_order(marketplace_order: str, status: str, cancellation_reason: str = "", items=None):
	"""Update a Talabat order, then reconcile the returned order locally."""
	doc = frappe.get_doc("Marketplace Order", marketplace_order)
	doc.check_permission("write")
	status = cstr(status).strip()
	if status not in talabat_orders.ORDER_UPDATE_STATUSES:
		frappe.throw(_("Unsupported Talabat order update status."))
	if doc.external_status in {"CANCELLED", "DISPATCHED", "READY_FOR_PICKUP", "DELIVERED"}:
		frappe.throw(_("This order cannot be updated from its current status."))
	if status == "DISPATCHED" and doc.transport_type != "VENDOR_DELIVERY":
		frappe.throw(_("DISPATCHED is only valid for Vendor Delivery orders."))
	if status == "READY_FOR_PICKUP" and doc.transport_type != "LOGISTICS_DELIVERY":
		frappe.throw(_("READY_FOR_PICKUP is only valid for Logistics Delivery orders."))
	if status in {"DISPATCHED", "READY_FOR_PICKUP"} and doc.external_status not in {"RECEIVED", "UPDATE_CART"}:
		frappe.throw(_("This fulfillment update is not valid from the current order status."))
	try:
		items = frappe.parse_json(items) if isinstance(items, str) else items
	except (TypeError, ValueError):
		frappe.throw(_("Order update items must be valid JSON."))
	if items is None:
		items = [_item_update_payload(row) for row in doc.items]
	payload = {"order_id": doc.external_order_id, "status": status, "items": items}
	if status == "CANCELLED":
		payload["cancellation"] = {"reason": cstr(cancellation_reason).strip()}
	talabat_orders.validate_order_update(payload)
	_validate_current_item_updates(doc, items)
	raw_order = talabat_orders.update_order(doc.marketplace_account, doc.external_order_id, payload)
	normalized = normalize_order(raw_order)
	if normalized["external_order_id"] != cstr(doc.external_order_id).strip():
		frappe.throw(_("Talabat returned a different order ID."))
	updated = upsert_marketplace_order(doc.marketplace_account, normalized)
	return {"name": updated.name}


def _item_update_payload(row):
	pricing = {"pricing_type": row.pricing_type}
	if row.external_status not in {"NOT_FOUND", "REPLACED"}:
		if row.pricing_type == "KG":
			pricing["weight"] = flt(row.fulfilled_weight)
			pricing["weighted_pieces"] = cint(row.fulfilled_weighted_pieces)
		else:
			pricing["quantity"] = flt(row.fulfilled_quantity)
	pricing["unit_price"] = flt(row.fulfilled_unit_price)
	return {
		"_id": row.external_item_id,
		"sku": row.external_sku,
		"status": row.external_status if row.external_status in {"IN_CART", "NOT_FOUND", "REPLACED", "ADDITION"} else "IN_CART",
		"pricing": pricing,
		**({"replaced_id": row.replaced_external_item_id} if row.replaced_external_item_id else {}),
	}


def _validate_current_item_updates(doc, items):
	by_id = {cstr(row.external_item_id).strip(): row for row in doc.items}
	for item in items:
		row = by_id.get(cstr(item.get("_id")).strip())
		if not row:
			if not cstr(item.get("sku")).strip() or item.get("status") not in {"IN_CART", "ADDITION"}:
				frappe.throw(_("New order items require a SKU and IN_CART or ADDITION status."))
			continue
		pricing = item.get("pricing") or {}
		if cstr(item.get("status")).strip() in {"NOT_FOUND", "REPLACED"}:
			if "quantity" in pricing or "weight" in pricing:
				frappe.throw(_("NOT_FOUND and REPLACED items cannot include fulfilled quantity or weight."))
		if pricing.get("pricing_type") != row.pricing_type:
			frappe.throw(_("An item's pricing type cannot be changed."))
		if row.pricing_type == "KG" and float(pricing.get("weight", 0)) > float(row.original_weight or 0):
			frappe.throw(_("Fulfilled weight cannot exceed the original weight."))
		if row.pricing_type == "UNIT" and float(pricing.get("quantity", 0)) > float(row.original_quantity or 0):
			frappe.throw(_("Fulfilled quantity cannot exceed the original quantity."))
