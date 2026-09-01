# Copyright (c) 2026, Tamwenat El Zekrayat and contributors

"""Request compatibility hooks for external marketplace callbacks."""

import frappe

from marketplace_integrations.integrations.talabat.constants import TALABAT_ORDER_WEBHOOK_PATH


def normalize_talabat_webhook_authorization():
	"""Expose Talabat's Bearer credential as the raw webhook credential to Frappe.

	Talabat labels its shared webhook secret as a Bearer credential, while Frappe
	interprets every Bearer header as an OAuth token during global authentication.
	This hook runs before that authentication and is restricted to the exact
	Talabat order webhook route. The webhook handler then validates the raw value
	against the connection's stored secret.
	"""
	request = frappe.local.request
	if request.path != TALABAT_ORDER_WEBHOOK_PATH:
		return

	authorization = request.headers.get("Authorization")
	if not authorization:
		return

	scheme, separator, credential = authorization.partition(" ")
	if scheme.lower() != "bearer" or not separator or not credential:
		return

	request.environ["HTTP_AUTHORIZATION"] = credential
