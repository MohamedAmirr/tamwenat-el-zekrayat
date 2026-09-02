# Copyright (c) 2026, Tamwenat El Zekrayat and contributors

"""Request compatibility hooks for external marketplace callbacks."""

import frappe

from marketplace_integrations.integrations.talabat.constants import (
	TALABAT_CATALOG_WEBHOOK_PATH,
	TALABAT_ORDER_WEBHOOK_PATH,
)

TALABAT_WEBHOOK_PATHS = (TALABAT_ORDER_WEBHOOK_PATH, TALABAT_CATALOG_WEBHOOK_PATH)


def normalize_talabat_webhook_authorization():
	"""Expose Talabat's Bearer credential as the raw webhook credential to Frappe.

	Talabat labels its shared webhook secret as a Bearer credential, while Frappe
	interprets every Bearer header as an OAuth token during global authentication.
	This hook runs before that authentication and is restricted to Talabat's
	webhook routes - both the order and catalog feedback webhooks document the
	exact same ``WebhookKeyAuth`` scheme in openapi.json, and the order webhook
	has empirically been observed sending a ``Bearer`` prefix despite that
	scheme documenting a raw static token, so the same normalization applies to
	both rather than duplicating it per route. The webhook handler then
	validates the raw value against the connection's stored secret.
	"""
	request = frappe.local.request
	if request.path not in TALABAT_WEBHOOK_PATHS:
		return

	authorization = request.headers.get("Authorization")
	if not authorization:
		return

	scheme, separator, credential = authorization.partition(" ")
	if scheme.lower() != "bearer" or not separator or not credential:
		return

	request.environ["HTTP_AUTHORIZATION"] = credential
