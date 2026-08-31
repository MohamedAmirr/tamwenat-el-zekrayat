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
from frappe.utils import cint, cstr

from marketplace_integrations.integrations.talabat import orders as talabat_orders
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
			else:
				frappe.db.release_savepoint(savepoint_name)
				imported.append(updated.name)

		total_pages = cint(body.get("total_pages") or 1)
		if page >= total_pages:
			break
		page += 1

	return {"imported": imported, "count": len(imported), "errors": errors}
