# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Public Shopify webhook transport boundary (#104).

HMAC verification against the RAW request body happens before any JSON
parsing - see docs/shopify/README.md's "Webhooks (for #104)" section and
integrations/shopify/webhooks.py::verify_shopify_hmac(). Authentication and
minimal shape validation finish synchronously so an unsigned/invalid request
never reaches a worker and never gets its payload parsed; the worker
(process_shopify_webhook) owns idempotent persistence - via Shopify Webhook
Delivery's unique webhook_id - and per-topic processing. This module never
logs the HMAC header, the client secret, or a webhook payload."""

import frappe
from frappe import _

from marketplace_integrations.integrations.shopify.constants import (
	SHOPIFY_ORDER_WEBHOOK_TOPICS,
	SHOPIFY_WEBHOOK_TOPIC_HEADER_TO_ENUM,
)

from marketplace_integrations.integrations.shopify.webhooks import (
	process_shopify_webhook,
	verify_shopify_hmac,
)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook_event():
	"""Authenticate one Shopify webhook delivery and enqueue its processing.

	Every subscribed topic (SHOPIFY_WEBHOOK_TOPICS) shares this single
	endpoint - X-Shopify-Topic tells the receiver which one a given delivery
	is, matching how webhookSubscriptionCreate registers all of them against
	the same callback URL (see integrations/shopify/webhooks.py).
	"""
	raw_body = frappe.request.get_data()

	shop_domain = (frappe.get_request_header("X-Shopify-Shop-Domain") or "").strip()
	topic_header = (frappe.get_request_header("X-Shopify-Topic") or "").strip()
	webhook_id = (frappe.get_request_header("X-Shopify-Webhook-Id") or "").strip()
	provided_hmac = frappe.get_request_header("X-Shopify-Hmac-Sha256") or ""

	if not shop_domain or not topic_header or not webhook_id:
		_invalid_webhook()

	connection = _resolve_connection(shop_domain)
	secret = connection.get_password("client_secret", raise_exception=False)
	previous_secret = connection.get_password("previous_client_secret", raise_exception=False)
	if not verify_shopify_hmac(raw_body, secret, provided_hmac, previous_secret):
		_invalid_webhook()

	# Only parsed now, after the signature is verified against the RAW body -
	# a minimal shape check, not something the enqueued job needs: canonical
	# order retrieval/document creation from webhook payloads is out of scope
	# for #104, so the payload itself is never persisted or forwarded.
	# frappe.parse_json() only decodes str, not the bytes raw_body actually
	# is (frappe.request.get_data() returns bytes) - it must be decoded first,
	# or a real delivery's payload silently fails the isinstance(dict) check
	# below and every real delivery gets rejected.
	try:
		payload = frappe.parse_json(raw_body.decode("utf-8")) if raw_body else None
	except (UnicodeDecodeError, ValueError):
		payload = None
	if not isinstance(payload, dict):
		_invalid_webhook()

	# X-Shopify-Topic arrives in Shopify's lowercase slash form (e.g.
	# "orders/create"), a different wire vocabulary from the GraphQL enum
	# form (SHOPIFY_WEBHOOK_TOPICS, e.g. "ORDERS_CREATE") used to register
	# subscriptions - see constants.py's SHOPIFY_WEBHOOK_TOPIC_HEADER_TO_ENUM.
	topic = SHOPIFY_WEBHOOK_TOPIC_HEADER_TO_ENUM.get(topic_header.lower())
	if topic is None:
		# Accept (so Shopify does not retry forever) but never enqueue a topic
		# we didn't register/expect - e.g. a stale subscription left over from
		# before a topic list change.
		return {"accepted": True}

	order_gid = _extract_order_gid(topic, payload)

	job_kwargs = {
		"queue": "default",
		"timeout": 300,
		"connection_name": connection.name,
		"topic": topic,
		"webhook_id": webhook_id,
		"shop_domain": shop_domain,
	}
	if order_gid is not None or topic in SHOPIFY_ORDER_WEBHOOK_TOPICS:
		job_kwargs["order_gid"] = order_gid
	frappe.enqueue(
		process_shopify_webhook,
		**job_kwargs,
	)
	return {"accepted": True}


def _resolve_connection(shop_domain):
	"""Resolve exactly one Shopify Connection by shop_domain. Deliberately
	does not require connection.enabled - APP_UNINSTALLED must still be
	accepted and processed (to disable the connection) even though disabling
	it is itself what would otherwise make a later filter reject it."""
	connections = frappe.get_all(
		"Shopify Connection", filters={"shop_domain": shop_domain}, fields=["name"]
	)
	if len(connections) != 1:
		_invalid_webhook()
	return frappe.get_doc("Shopify Connection", connections[0].name)


def _invalid_webhook():
	frappe.throw(_("Invalid Shopify webhook."), frappe.PermissionError)


def _extract_order_gid(topic, payload):
	"""Extract only the stable order identity; payload remains non-canonical."""
	if topic not in SHOPIFY_ORDER_WEBHOOK_TOPICS:
		return None
	value = payload.get("admin_graphql_api_id")
	if topic == "REFUNDS_CREATE":
		value = f"gid://shopify/Order/{payload.get('order_id')}" if payload.get("order_id") else None
	return str(value).strip() if value else None
