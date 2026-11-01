# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon account binding and read-only warehouse discovery."""

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import cint, cstr, escape_html, now_datetime

from marketplace_integrations.integrations.noon.client import SERVICE_NAME, NoonClient
from marketplace_integrations.services import carrier_log
from marketplace_integrations.services.mappings import upsert_mapping_for_listing

WAREHOUSES_PATH = "/warehouse-platform/v1/warehouses/list"
MAPPING_DOCTYPE = "Marketplace Product Mapping"


def get_account(account_name, permission=None):
	"""Load an enabled Noon Marketplace Account with its typed connection.

	``permission`` (e.g. ``"read"``) runs the caller's document-level check on the account
	BEFORE any platform/enabled/connection validation, so a user who may not see the account
	learns nothing about its platform or state from the validation messages.
	"""
	account = frappe.get_doc("Marketplace Account", account_name)
	if permission:
		account.check_permission(permission)
	if account.platform != "Noon" or account.connection_doctype != "Noon Connection":
		frappe.throw(_("Marketplace Account {0} is not bound to a Noon Connection.").format(account.name))
	if not account.enabled:
		frappe.throw(_("Marketplace Account {0} is disabled.").format(account.name))
	if not account.connection:
		frappe.throw(_("Marketplace Account {0} requires a Noon Connection.").format(account.name))
	return account


def validate_connection_enabled(account):
	"""Raise a visible setup error if the bound Noon Connection cannot run."""
	connection = frappe.get_doc("Noon Connection", account.connection)
	if not connection.enabled:
		frappe.throw(_("Noon Connection {0} is disabled.").format(connection.name))


def list_warehouses(connection_name):
	"""Read all warehouse pages for one enabled Noon Connection."""
	connection = frappe.get_doc("Noon Connection", connection_name)
	if not connection.enabled:
		frappe.throw(_("Noon Connection {0} is disabled.").format(connection.name))
	client = NoonClient(connection.name)
	warehouses = []
	next_token = None
	seen_tokens = set()
	while True:
		payload = {"next_token": next_token} if next_token else {}
		response = client.request("POST", WAREHOUSES_PATH, json=payload)
		if response.status_code != 200:
			frappe.throw(client.error_message(response))
		try:
			body = response.json()
		except ValueError:
			frappe.throw(_("Noon returned an invalid warehouse response."))
		if not isinstance(body, dict) or not isinstance(body.get("warehouses"), list):
			frappe.throw(_("Noon returned an invalid warehouse response."))
		for warehouse in body["warehouses"]:
			if not isinstance(warehouse, dict) or not all(
				key in warehouse
				for key in ("warehouse_code", "display_name", "fulfillment_system_code", "is_active")
			):
				frappe.throw(_("Noon returned an invalid warehouse response."))
			warehouses.append(
				{
					"warehouse_code": warehouse["warehouse_code"],
					"display_name": warehouse["display_name"],
					"fulfillment_system_code": warehouse["fulfillment_system_code"],
					"is_active": warehouse["is_active"],
				}
			)
		next_token = body.get("next_token")
		if not next_token:
			return warehouses
		if not isinstance(next_token, str) or next_token in seen_tokens:
			frappe.throw(_("Noon returned an invalid warehouse pagination cursor."))
		seen_tokens.add(next_token)


def _fail_warehouse_check(log, message):
	"""Close the log row as Failed and commit it (a throw rolls the request back and would lose it), then
	throw ``message`` (already escaped)."""
	carrier_log.close_log(log, "Failed", error=message)
	frappe.db.commit()
	frappe.throw(message, title=_("Noon Warehouse"))


def check_account_warehouse(account_name):
	"""Ask Noon (ListWarehouses, all pages) for the account's ``noon_warehouse_code`` and store the answer on
	the account: ``noon_fulfillment_system_code``, ``noon_warehouse_active`` and ``noon_warehouse_checked_on``
	(sub-issue #416). ListWarehouses can only filter by fulfillment system code, so the account's warehouse is
	picked from the list here. The write is a system write-back (``db_set``-style, no validate: the account's
	own validate calls Noon again). The call is logged as an Integration Request (operation "Warehouse",
	reference the account) without any secret. A Noon error is thrown escaped. Returns the stored values."""
	account = get_account(account_name, permission="write")
	code = cstr(account.noon_warehouse_code).strip()
	if not code:
		frappe.throw(_("Marketplace Account {0} requires a Noon Warehouse Code.").format(account.name))
	log = carrier_log.open_log(
		SERVICE_NAME, account.name, "Warehouse", reference=code, reference_doctype="Marketplace Account"
	)
	frappe.db.commit()  # no transaction open across the call
	try:
		warehouses = list_warehouses(account.connection)
	except frappe.ValidationError as exc:
		frappe.clear_messages()
		_fail_warehouse_check(log, escape_html(cstr(exc)))
	except Exception:
		_fail_warehouse_check(log, _("Noon did not answer the warehouse request. Try again."))
	match = next((row for row in warehouses if cstr(row["warehouse_code"]).strip() == code), None)
	if not match:
		# A warehouse Noon no longer lists must not stay "checked": the stored answer is cleared.
		frappe.db.set_value(
			"Marketplace Account",
			account.name,
			{"noon_fulfillment_system_code": "", "noon_warehouse_active": 0, "noon_warehouse_checked_on": None},
			update_modified=False,
		)
		_fail_warehouse_check(
			log, _("Noon has no warehouse with code {0}.").format(frappe.bold(escape_html(code)))
		)
	values = {
		"noon_fulfillment_system_code": cstr(match["fulfillment_system_code"]).strip(),
		"noon_warehouse_active": 1 if match["is_active"] is True else 0,
		"noon_warehouse_checked_on": now_datetime(),
	}
	frappe.db.set_value("Marketplace Account", account.name, values, update_modified=False)
	carrier_log.close_log(
		log,
		"Success",
		output=f"fulfillment_system_code={values['noon_fulfillment_system_code']} is_active={values['noon_warehouse_active']}",
	)
	frappe.db.commit()
	return {
		"fulfillment_system_code": values["noon_fulfillment_system_code"],
		"is_active": values["noon_warehouse_active"],
		"checked_on": values["noon_warehouse_checked_on"],
	}


def fbpi_ready(account):
	"""``(ok, reason)``: may FBPI work (a shipment, Mark Out of Stock, an invoice) go to Noon for this account?

	``ok`` only when the account's FBPI Code is set, equals the stored Noon fulfillment system code, and the
	stored warehouse is active. Otherwise ``reason`` is a translated, user-readable sentence (Noon's value is
	escaped). ``account`` is a Marketplace Account document or its name. Reads stored values only: no call."""
	if isinstance(account, str):
		account = frappe.get_doc("Marketplace Account", account)
	fbpi_code = cstr(account.get("noon_fbpi_code")).strip()
	stored_code = cstr(account.get("noon_fulfillment_system_code")).strip()
	if not fbpi_code:
		return False, _(
			"Marketplace Account {0} has no FBPI Code. Press Check Noon Warehouse, then type the code that means FBPI."
		).format(account.name)
	if not stored_code:
		return False, _("The Noon warehouse of Marketplace Account {0} was never checked. Press Check Noon Warehouse.").format(
			account.name
		)
	if stored_code != fbpi_code:
		return False, _(
			"The Noon fulfillment system code of Marketplace Account {0} ({1}) is not its FBPI Code ({2}). "
			"Press Check Noon Warehouse again, or correct the FBPI Code."
		).format(account.name, escape_html(stored_code), escape_html(fbpi_code))
	if not cint(account.get("noon_warehouse_active")):
		return False, _("The Noon warehouse of Marketplace Account {0} is not active at Noon.").format(account.name)
	return True, ""


def validate_warehouse_code(connection_name, warehouse_code):
	"""Require the selected Noon warehouse to exist and be active."""
	if not warehouse_code:
		return
	if not any(
		row["warehouse_code"] == warehouse_code and row["is_active"]
		for row in list_warehouses(connection_name)
	):
		frappe.throw(
			_("Noon Warehouse {0} is not active on Connection {1}.").format(warehouse_code, connection_name)
		)


def list_unmapped_order_skus(account_name):
	"""Return distinct Noon ``partner_sku`` values (stored as
	``Marketplace Order Item.external_sku``) on this account's Marketplace
	Orders that have no enabled Marketplace Product Mapping yet (#168 "Needs
	Mapping" for Noon).

	Order-derived only: it does NOT enumerate the Noon catalog (see
	docs/noon-integration/catalog-export-capability.md - Noon catalog export is
	blocked). No Noon HTTP call is made. A SKU is "unmapped" exactly when the
	resolver ``services/mappings.py::_resolve()`` would find no enabled mapping
	for it: an enabled mapping on an enabled account with the same
	``external_reference`` (both are stored stripped, and resolution is exact,
	so a Python set difference matches the resolver). Two set-based queries
	replace a per-SKU resolver loop, and the child rows are joined to their
	parents on the account/platform filters rather than read via an IN list of
	every parent name.
	"""
	account = get_account(account_name, permission="read")
	if not frappe.has_permission(MAPPING_DOCTYPE, ptype="read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	# Parent visibility stays permission-scoped via frappe.get_list, which applies
	# Marketplace Order's platform row filter (permissions.py, #322), so a visible
	# parent here means the join below covers the same rows.
	if not frappe.get_list(
		"Marketplace Order",
		filters={"marketplace_account": account.name, "platform": "Noon"},
		pluck="name",
		limit_page_length=1,
	):
		return []

	# Query builder reads below skip permissions on purpose: account read, mapping read and the
	# permission-scoped Order probe above already authorized this caller for this account.
	ORDER = DocType("Marketplace Order")
	ITEM = DocType("Marketplace Order Item")
	MPM = DocType(MAPPING_DOCTYPE)
	ACCOUNT = DocType("Marketplace Account")

	order_skus = (
		frappe.qb.from_(ITEM)
		.join(ORDER)
		.on(ORDER.name == ITEM.parent)
		.select(ITEM.external_sku)
		.distinct()
		.where(ITEM.parenttype == "Marketplace Order")
		.where(ORDER.marketplace_account == account.name)
		.where(ORDER.platform == "Noon")
		.run(as_dict=True)
	)
	mapped_references = (
		frappe.qb.from_(MPM)
		.join(ACCOUNT)
		.on(ACCOUNT.name == MPM.marketplace_account)
		.select(MPM.external_reference)
		.where(MPM.marketplace_account == account.name)
		.where(MPM.enabled == 1)
		.where(ACCOUNT.enabled == 1)
		.run(as_dict=True)
	)
	mapped = {cstr(row.external_reference).strip() for row in mapped_references}
	return sorted({cstr(row.external_sku).strip() for row in order_skus} - mapped - {""})


def upsert_mapping(account_name, external_reference, item):
	"""Create or update one Noon ``partner_sku`` -> ERPNext Item mapping.

	``external_reference`` is the Noon ``partner_sku`` exactly as
	``integrations/noon/order_mapper.py`` resolves it, so the same value is
	also written to ``external_sku``.
	"""
	account = get_account(account_name, permission="read")
	external_reference = cstr(external_reference).strip()
	item = cstr(item).strip()
	if not external_reference:
		frappe.throw(_("Noon partner SKU is required."))
	if not item:
		frappe.throw(_("Item is required."))

	item_doc = frappe.get_doc("Item", item)
	item_doc.check_permission("read")
	mapping = upsert_mapping_for_listing(account.name, external_reference, item_doc.name)
	return {"name": mapping.name}
