# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Public Talabat webhook transport boundary for order events.

Authentication and minimum identity validation finish synchronously so an
invalid request never reaches a worker. The worker owns normalization and
idempotent persistence; this module never logs credentials or raw payloads.
"""

import hmac

import frappe
from frappe import _
from frappe.utils import cstr

from marketplace_integrations.integrations.talabat.order_events import process_order_event


@frappe.whitelist(allow_guest=True, methods=["POST"])
def order_event():
	"""Authenticate and enqueue one Talabat order webhook payload."""
	payload = frappe.request.get_json(silent=True)
	if not isinstance(payload, dict):
		_invalid_webhook()

	order_id = cstr(payload.get("order_id")).strip()
	client = payload.get("client") or {}
	if not order_id or not isinstance(client, dict):
		_invalid_webhook()

	account, connection = _resolve_account_and_connection(client)
	secret = connection.get_password("order_webhook_secret", raise_exception=False)
	authorization = frappe.get_request_header("Authorization", "")
	if not secret or not hmac.compare_digest(cstr(authorization).encode(), cstr(secret).encode()):
		_invalid_webhook()

	frappe.enqueue(
		process_order_event,
		queue="default",
		timeout=300,
		marketplace_account=account.name,
		payload=payload,
	)
	return {"accepted": True}


def _resolve_account_and_connection(client):
	"""Resolve exactly one enabled Talabat account from webhook vendor IDs."""
	vendor_ids = {cstr(client.get("id")).strip(), cstr(client.get("store_id")).strip()}
	vendor_ids.discard("")
	if not vendor_ids:
		_invalid_webhook()

	accounts = frappe.get_all(
		"Marketplace Account",
		filters={
			"platform": "Talabat",
			"enabled": 1,
			"connection_doctype": "Talabat Connection",
			"external_account_id": ["in", sorted(vendor_ids)],
		},
		fields=["name", "connection"],
	)
	if len(accounts) != 1:
		_invalid_webhook()

	account = frappe.get_doc("Marketplace Account", accounts[0].name)
	connection = frappe.get_doc("Talabat Connection", account.connection)
	if not connection.enabled:
		_invalid_webhook()
	return account, connection


def _invalid_webhook():
	frappe.throw(_("Invalid Talabat order webhook."), frappe.PermissionError)
