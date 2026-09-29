# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Platform-neutral HTTP helpers shared by every marketplace integration client.

Every marketplace-specific client needs a bounded-timeout/network-error
request wrapper and safe error presentation. Talabat and Shopify also share
OAuth2 client-credentials token caching; Noon uses the same request wrapper
with its own cookie session, but not the OAuth2 helper.
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


def http_request(
	method, url, *, service_name="the remote service", session=None, include_exception_details=True, **kwargs
):
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

	``session`` lets cookie-authenticated clients use their own requests.Session
	without bypassing this timeout/error boundary. ``include_exception_details``
	defaults to the existing behavior; clients whose request carries a secret
	can suppress exception text that might echo it.
	"""
	kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)
	try:
		request = session.request if session is not None else requests.request
		return request(method, url, **kwargs)
	except requests.exceptions.RequestException as e:
		if include_exception_details:
			frappe.throw(frappe._("Failed to reach {0}: {1}").format(service_name, str(e)))
	# Throw outside the exception handler so a sensitive request exception is
	# not retained as this new error's implicit traceback context.
	frappe.throw(frappe._("Failed to reach {0}.").format(service_name))


def error_message_from_response(response):
	"""Extract a human-readable error message from a non-2xx response.

	Falls back to a length-bounded raw body when the response isn't JSON, or
	is JSON but carries none of the recognized error fields - this never
	echoes an unbounded remote response body into an operator-facing error
	message. The recognized `error`/`error_description` fields are
	themselves coerced to a string and run through the same length bound -
	they are still remote-controlled values (could be an unexpectedly large
	string, or even a nested object) and must never bypass the bound just
	because the field name was recognized.
	"""
	try:
		body = response.json()
	except ValueError:
		return truncate_response_text(response.text)

	if not isinstance(body, dict):
		return truncate_response_text(response.text)

	message = body.get("error_description") or body.get("error")
	if message:
		return truncate_response_text(str(message))
	return truncate_response_text(response.text)


def truncate_response_text(text):
	"""Bound a raw response body to ``ERROR_BODY_MAX_LENGTH`` characters.

	Public so every marketplace client can reuse the same bound when it needs
	to surface a raw (non-JSON, or unexpectedly-shaped JSON) response body in
	a clean error message, instead of each client re-deriving its own bound.
	"""
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
