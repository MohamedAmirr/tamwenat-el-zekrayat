# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Platform-neutral HTTP helpers shared by every marketplace integration client.

Every marketplace-specific client (``TalabatClient``, ``ShopifyClient``, ...)
needs the same three primitives: a bounded-timeout/network-error request
wrapper, a length-bounded error-message extractor for non-2xx responses, and
a client-credentials token fetch with expiry-buffer caching math. None of
this is platform-specific - it is the same shape every OAuth2
client-credentials integration needs - so it lives here once instead of
being copy-pasted per platform client and drifting (as it briefly did
between TalabatClient and ShopifyClient before this module existed).
"""

import time

import frappe
import requests

REQUEST_TIMEOUT_SECONDS = 10
# Bound how much of a non-JSON response body ever gets echoed into an
# operator-facing error/Error Log entry. A response from an unexpected host
# (e.g. a shop_domain that resolves somewhere other than intended, or a
# WAF/captive-portal page) can carry an arbitrarily large HTML body; without
# this bound its entire content would land in the thrown error message.
ERROR_BODY_MAX_LENGTH = 200


def http_request(method, url, *, service_name="the remote service", **kwargs):
	"""Make an HTTP request, raising a clean frappe error on any network-level
	failure (connection error, timeout, SSL error, ...).

	``service_name`` names whichever platform this request is actually
	against (e.g. "Talabat", "Shopify") so the thrown message attributes the
	failure correctly instead of every integration's network errors reading
	"Failed to reach Talabat" regardless of which platform failed.

	Shared by every marketplace client's token fetch and general request
	choke point alike, plus any other plain (non-authenticated) HTTP call
	made by this app, e.g. ``integrations/talabat/catalog_feedback.py``'s
	feedback CSV download. Public (no leading underscore) specifically
	because it is called across module boundaries.
	"""
	kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)
	try:
		return requests.request(method, url, **kwargs)
	except requests.exceptions.RequestException as e:
		frappe.throw(frappe._("Failed to reach {0}: {1}").format(service_name, str(e)))


def error_message_from_response(response):
	"""Extract a human-readable error message from a non-2xx response.

	Falls back to a length-bounded raw body when the response isn't JSON, or
	is JSON but carries none of the recognized error fields - this never
	echoes an unbounded remote response body into an operator-facing error
	message.
	"""
	try:
		body = response.json()
	except ValueError:
		return _truncated(response.text)

	message = body.get("error_description") or body.get("error")
	if message:
		return message
	return _truncated(response.text)


def _truncated(text):
	text = (text or "").strip()
	if not text:
		return "no response body"
	if len(text) > ERROR_BODY_MAX_LENGTH:
		return text[:ERROR_BODY_MAX_LENGTH] + "..."
	return text


def fetch_client_credentials_token(
	url, client_id, client_secret, *, service_name, default_expires_in, expiry_buffer_seconds
):
	"""Fetch and shape one OAuth2 client-credentials access token.

	Shared token-lifecycle logic for every marketplace client using the
	client-credentials grant (Talabat, Shopify): make the request, raise a
	clean error on a non-2xx response or a malformed body - including a
	non-integer/null/string ``expires_in``, which would otherwise raise an
	uncaught ``TypeError`` from the ``expires_in - expiry_buffer_seconds``
	subtraction - and compute the cache TTL using the shared expiry-buffer
	pattern. Returns ``{"access_token", "ttl", "expires_at"}``, ready to hand
	straight to ``frappe.cache().set_value()``.
	"""
	response = http_request(
		"POST",
		url,
		data={
			"grant_type": "client_credentials",
			"client_id": client_id,
			"client_secret": client_secret,
		},
		service_name=service_name,
	)

	if not response.ok:
		frappe.throw(
			frappe._("Failed to authenticate with {0}: {1}").format(
				service_name, error_message_from_response(response)
			)
		)

	try:
		body = response.json()
		expires_in = body.get("expires_in", default_expires_in)
		ttl = max(expires_in - expiry_buffer_seconds, 1)
		return {
			"access_token": body["access_token"],
			"ttl": ttl,
			"expires_at": time.time() + ttl,
		}
	except (ValueError, KeyError, TypeError) as e:
		frappe.throw(
			frappe._("Failed to authenticate with {0}: unexpected response ({1})").format(
				service_name, str(e)
			)
		)
