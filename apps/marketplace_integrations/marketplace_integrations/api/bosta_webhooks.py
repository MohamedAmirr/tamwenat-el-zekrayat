# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Public Bosta webhook authentication boundary.

The supplied Bosta contract documents a merchant-configured callback URL and
custom header, but no webhook event/request schema. We therefore authenticate
and acknowledge callbacks without interpreting or persisting their bodies.
Polling the documented delivery-read endpoint is the source of status updates.
"""

import hmac

import frappe
from frappe import _
from frappe.utils import cstr

from marketplace_integrations.integrations.bosta.constants import BOSTA_WEBHOOK_SECRET_HEADER


@frappe.whitelist(allow_guest=True, methods=["POST"])
def delivery_event():
	"""Authenticate and acknowledge a Bosta dashboard callback."""
	settings = frappe.get_single("Bosta Settings")
	secret = settings.get_password("webhook_secret", raise_exception=False)
	provided = frappe.get_request_header(BOSTA_WEBHOOK_SECRET_HEADER, "")
	if (
		not settings.enabled
		or not secret
		or not hmac.compare_digest(cstr(provided).encode(), cstr(secret).encode())
	):
		frappe.throw(_("Invalid Bosta webhook."), frappe.PermissionError)
	return {"accepted": True}
