# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Public Bosta webhook authentication and JSON transport boundary."""

import hmac

import frappe
from frappe import _
from frappe.utils import cstr

from marketplace_integrations.integrations.bosta.constants import BOSTA_WEBHOOK_SECRET_HEADER
from marketplace_integrations.integrations.bosta.statuses import process_webhook_event


@frappe.whitelist(allow_guest=True, methods=["POST"])
def delivery_event():
	"""Authenticate, validate, and process a Bosta dashboard callback."""
	settings = _get_bosta_settings()
	secret = settings.get_password("webhook_secret", raise_exception=False)
	provided = frappe.get_request_header(BOSTA_WEBHOOK_SECRET_HEADER, "")
	if (
		not settings.enabled
		or not secret
		or not hmac.compare_digest(cstr(provided).encode(), cstr(secret).encode())
	):
		frappe.throw(_("Invalid Bosta webhook."), frappe.PermissionError)
	process_webhook_event(_webhook_payload())
	return {"accepted": True}


def _get_bosta_settings():
	return frappe.get_single("Bosta Settings")


def _webhook_payload():
	return frappe.request.get_json(silent=True)
