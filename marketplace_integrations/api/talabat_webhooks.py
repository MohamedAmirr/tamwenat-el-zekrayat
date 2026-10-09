# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Public Talabat webhook transport boundary for order events and catalog
update feedback.

Authentication and minimum identity/schema validation finish synchronously so
an invalid request never reaches a worker. The worker owns normalization/CSV
processing and idempotent persistence; this module never logs credentials or
raw payloads."""

import hmac

import frappe
from frappe import _
from frappe.utils import cstr

from marketplace_integrations.integrations.talabat.catalog_feedback import process_catalog_feedback
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


@frappe.whitelist(allow_guest=True, methods=["POST"])
def catalog_update_feedback():
	"""Authenticate and enqueue one Talabat catalog update feedback callback.

	Does not download the feedback CSV synchronously - see
	``integrations/talabat/catalog_feedback.py::process_catalog_feedback`` for
	that background work.
	"""
	payload = frappe.request.get_json(silent=True)
	if not isinstance(payload, dict):
		_invalid_catalog_webhook()

	job_id = cstr(payload.get("job_id")).strip()
	status = cstr(payload.get("status")).strip()
	platform_vendor_id = cstr(payload.get("platform_vendor_id")).strip()
	download_url = cstr(payload.get("download_url")).strip()
	if not job_id or not status or not platform_vendor_id or not download_url:
		_invalid_catalog_webhook()

	connection = _resolve_catalog_connection(platform_vendor_id)
	secret = connection.get_password("catalog_webhook_secret", raise_exception=False)
	authorization = frappe.get_request_header("Authorization", "")
	if not secret or not hmac.compare_digest(cstr(authorization).encode(), cstr(secret).encode()):
		_invalid_catalog_webhook()

	frappe.enqueue(
		process_catalog_feedback,
		queue="default",
		timeout=600,
		payload=payload,
	)
	return {"accepted": True}


def _resolve_catalog_connection(platform_vendor_id):
	"""Resolve exactly one enabled Talabat account/connection from the
	catalog webhook's single ``platform_vendor_id`` field - unlike the order
	webhook's ``client.id``/``client.store_id`` pair."""
	accounts = frappe.get_all(
		"Marketplace Account",
		filters={
			"platform": "Talabat",
			"enabled": 1,
			"connection_doctype": "Talabat Connection",
			"external_account_id": platform_vendor_id,
		},
		fields=["name", "connection"],
	)
	if len(accounts) != 1:
		_invalid_catalog_webhook()

	connection = frappe.get_doc("Talabat Connection", accounts[0].connection)
	if not connection.enabled:
		_invalid_catalog_webhook()
	return connection


def _invalid_catalog_webhook():
	frappe.throw(_("Invalid Talabat catalog feedback webhook."), frappe.PermissionError)
