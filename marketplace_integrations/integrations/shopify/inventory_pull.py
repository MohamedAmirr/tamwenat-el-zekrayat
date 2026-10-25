# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""One-time setup helper: pull Shopify ``on_hand`` quantities into ONE draft
Stock Reconciliation (Marketplace Account button "Pull Shopify Inventory to
Draft Stock Reconciliation").

Read-only towards Shopify (only the existing inventory READ query is sent - no
mutation), never submits and never schedules anything. It works with the
account's "Push Inventory to Shopify" switch off, because it is a read.

Quantities are in the Item's stock UOM on both sides: the push
(``services/listing_quantity.py::get_stock_quantities``) writes the stock-UOM
quantity with no UOM conversion, so the inverse is the identity and there is no
ambiguous UOM case. Every row is created with valuation rate 0 and
``allow_zero_valuation_rate`` set (decision of the user): submitting the draft
sets the valuation of the included Items to 0.
"""

import frappe
from frappe import _
from frappe.utils import cstr, escape_html, flt, get_url_to_form, nowdate

from marketplace_integrations.integrations.shopify import inventory_sync
from marketplace_integrations.integrations.shopify.client import ShopifyClient
from marketplace_integrations.integrations.shopify.constants import SHOPIFY_INVENTORY_SYNC_CHUNK_SIZE
from marketplace_integrations.integrations.shopify.products import _is_variant_gid

RECONCILIATION_DOCTYPE = "Stock Reconciliation"
PULL_RQ_JOB_ID_PREFIX = "marketplace-shopify-inventory-pull"


def _marker(account_doc):
	return f"Shopify inventory pull {account_doc.name} / {account_doc.warehouse}"


def find_existing_drafts(account):
	"""Names of draft Stock Reconciliations previously created by this feature
	for the account's warehouse (marked by a Comment, since the document has no
	field we can rely on). Permission-aware."""
	account_doc = frappe.get_doc("Marketplace Account", account)
	account_doc.check_permission("write")
	names = frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": RECONCILIATION_DOCTYPE,
			"comment_type": "Comment",
			"content": _marker(account_doc),
		},
		pluck="reference_name",
	)
	if not names:
		return []
	return frappe.get_list(
		RECONCILIATION_DOCTYPE,
		filters={"name": ["in", names], "docstatus": 0},
		pluck="name",
		order_by="creation desc",
	)


def queue_inventory_pull(account):
	"""Authorize and enqueue the pull. No Shopify call in this request."""
	account_doc = frappe.get_doc("Marketplace Account", account)
	account_doc.check_permission("write")
	frappe.has_permission(RECONCILIATION_DOCTYPE, "create", throw=True)
	inventory_sync._validate_inventory_account_local(account_doc, require_push_enabled=False)

	job = frappe.enqueue(
		pull_inventory_to_draft,
		queue="long",
		timeout=3600,
		job_id=f"{PULL_RQ_JOB_ID_PREFIX}-{account_doc.name}",
		deduplicate=True,
		account_name=account_doc.name,
		owner=frappe.session.user,
	)
	if job is None:
		frappe.throw(_("An inventory pull is already running for this Marketplace Account."))
	return {"queued": True}


def pull_inventory_to_draft(account_name, owner=None):
	"""Background job body. Notifies ``owner`` with the outcome."""
	try:
		result = _pull(account_name)
	except frappe.ValidationError as error:
		_publish(owner, escape_html(cstr(error)), "red")
		return None
	except Exception:
		frappe.log_error(title="Shopify inventory pull failed", message=frappe.get_traceback())
		_publish(
			owner,
			escape_html(cstr(_("Shopify inventory pull failed. See the Error Log for details."))),
			"red",
		)
		return None
	_notify(owner, result)
	return result


def _pull(account_name):
	account_doc, connection = inventory_sync._validate_inventory_account_local(
		account_name, require_push_enabled=False
	)
	inventory_sync._validate_location_in_use(account_doc, connection)

	company = frappe.db.get_value("Warehouse", account_doc.warehouse, "company")
	company_values = frappe.db.get_value(
		"Company", company, ["stock_adjustment_account", "cost_center"], as_dict=True
	)
	if not company_values or not company_values.stock_adjustment_account:
		frappe.throw(_("Company {0} has no default Stock Adjustment Account.").format(company))

	skipped = []
	candidates = _candidates(account_doc.name, skipped)
	state = _read_state(
		ShopifyClient(connection.name), account_doc.shopify_location_gid, [c["gid"] for c in candidates]
	)
	candidates = _filter_by_shopify(candidates, state, skipped)

	bins = (
		frappe.get_all(
			"Bin",
			filters={
				"warehouse": account_doc.warehouse,
				"item_code": ["in", [c["item"] for c in candidates]],
			},
			fields=["item_code", "actual_qty"],
		)
		if candidates
		else []
	)
	current = {row.item_code: flt(row.actual_qty) for row in bins}

	rows = [
		{
			"item_code": candidate["item"],
			"warehouse": account_doc.warehouse,
			"qty": candidate["on_hand"],
			"valuation_rate": 0,
			"allow_zero_valuation_rate": 1,
		}
		for candidate in candidates
		if flt(candidate["on_hand"]) != current.get(candidate["item"], 0)
	]

	result = {
		"included": len(rows),
		"unchanged": len(candidates) - len(rows),
		"skipped": skipped,
		"draft": None,
	}
	if not rows:
		return result

	doc = frappe.get_doc(
		{
			"doctype": RECONCILIATION_DOCTYPE,
			"purpose": "Stock Reconciliation",
			"company": company,
			"posting_date": nowdate(),
			"expense_account": company_values.stock_adjustment_account,
			"cost_center": company_values.cost_center,
			"items": rows,
		}
	)
	if doc.meta.has_field("remarks"):
		doc.remarks = _marker(account_doc)
	doc.insert()
	doc.add_comment("Comment", _marker(account_doc))
	result["draft"] = doc.name
	return result


def _candidates(account_name, skipped):
	"""One ``{item, gid}`` per eligible Item. Anything else is appended to
	``skipped`` as ``{item, reason}``."""
	# Trusted read: the entry point authorized the account.
	mappings = frappe.get_all(
		inventory_sync.MAPPING_DOCTYPE,
		filters={"marketplace_account": account_name, "enabled": 1},
		fields=["name", "item", "external_reference"],
		order_by="name asc",
	)
	gids_by_item = {}
	for mapping in mappings:
		if mapping.item and _is_variant_gid(mapping.external_reference):
			gids_by_item.setdefault(mapping.item, set()).add(mapping.external_reference)
	items = {
		row.name: row
		for row in frappe.get_all(
			"Item",
			filters={"name": ["in", list(gids_by_item)]},
			fields=["name", "disabled", "is_stock_item", "has_variants", "has_batch_no", "has_serial_no"],
		)
	}
	candidates = []
	for item_code, gids in gids_by_item.items():
		item = items.get(item_code)
		reason = None
		if not item:
			reason = _("Item does not exist.")
		elif item.disabled:
			reason = _("Item is disabled.")
		elif not item.is_stock_item:
			reason = _("Item is not a stock Item.")
		elif item.has_variants:
			reason = _("Item is a template (has variants).")
		elif item.has_batch_no or item.has_serial_no:
			reason = _("Item uses batches or serial numbers.")
		elif len(gids) > 1:
			reason = _("Item is mapped to more than one Shopify variant.")
		if reason:
			skipped.append({"item": item_code, "reason": cstr(reason)})
		else:
			candidates.append({"item": item_code, "gid": next(iter(gids))})
	return candidates


def _read_state(client, location_id, gids):
	state = {}
	for start in range(0, len(gids), SHOPIFY_INVENTORY_SYNC_CHUNK_SIZE):
		chunk = gids[start : start + SHOPIFY_INVENTORY_SYNC_CHUNK_SIZE]
		state.update(inventory_sync._read_inventory_state(client, location_id, chunk))
	return state


def _filter_by_shopify(candidates, state, skipped):
	kept = []
	for candidate in candidates:
		node = state.get(candidate["gid"])
		reason = None
		if not node:
			reason = _("Shopify variant was not found.")
		elif not node["tracked"]:
			reason = _("Inventory is not tracked in Shopify.")
		elif not node["has_level"] or node["on_hand"] is None:
			reason = _("No Shopify inventory level at the account's Location.")
		if reason:
			skipped.append({"item": candidate["item"], "reason": cstr(reason)})
		else:
			kept.append({**candidate, "on_hand": node["on_hand"]})
	return kept


def _notify(owner, result):
	if result["draft"]:
		link = get_url_to_form(RECONCILIATION_DOCTYPE, result["draft"])
		head = _(
			"Draft Stock Reconciliation {0} created with {1} Item(s). Valuation rate is 0 on every row."
		).format(f'<a href="{link}">{escape_html(result["draft"])}</a>', result["included"])
	else:
		head = _("No difference between Shopify and ERPNext stock: no Stock Reconciliation was created.")
	lines = [head, _("Unchanged Items: {0}").format(result["unchanged"])]
	if result["skipped"]:
		lines.append(_("Skipped Items: {0}").format(len(result["skipped"])))
		lines.extend(
			f"{escape_html(row['item'])}: {escape_html(row['reason'])}" for row in result["skipped"]
		)
	_publish(owner, "<br>".join(lines), "green" if result["draft"] else "blue")


def _publish(owner, message, indicator):
	frappe.publish_realtime(
		"msgprint",
		{"message": message, "title": cstr(_("Shopify Inventory Pull")), "indicator": indicator},
		user=owner,
	)
