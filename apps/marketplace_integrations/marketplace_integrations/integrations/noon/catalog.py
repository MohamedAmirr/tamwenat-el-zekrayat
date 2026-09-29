# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon account binding and read-only warehouse discovery."""

import frappe
from frappe import _

from marketplace_integrations.integrations.noon.client import NoonClient

WAREHOUSES_PATH = "/warehouse-platform/v1/warehouses/list"


def get_account(account_name):
	"""Load an enabled Noon Marketplace Account with its typed connection."""
	account = frappe.get_doc("Marketplace Account", account_name)
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
