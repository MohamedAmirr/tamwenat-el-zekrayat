# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import time

import frappe
import requests

CACHE_KEY_PREFIX = "talabat_integration:access_token"
# Refresh a bit before actual expiry so a request never races a token that
# expires mid-flight.
EXPIRY_BUFFER_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 10


def get_access_token():
	"""Return a valid Talabat Partner API access token, using the cache when possible."""
	settings = frappe.get_single("Talabat Settings")
	cache_key = _cache_key(settings.environment)

	cached = frappe.cache().get_value(cache_key)
	if cached and cached["expires_at"] > time.time():
		return cached["access_token"]

	token = _fetch_access_token(settings)
	frappe.cache().set_value(cache_key, token, expires_in_sec=token["ttl"])
	return token["access_token"]


def invalidate_access_token():
	"""Drop the cached token for the current environment, forcing the next
	get_access_token() call to fetch a fresh one instead of returning a token
	that a caller has determined is no longer accepted (e.g. a 401)."""
	settings = frappe.get_single("Talabat Settings")
	frappe.cache().delete_value(_cache_key(settings.environment))


def _request(method, url, **kwargs):
	"""Make an HTTP request to Talabat, raising a clean frappe error on any
	network-level failure (connection error, timeout, SSL error, ...).

	Shared by every Talabat API call - the token fetch below and the general
	`talabat_request` choke point in `client.py` - so timeout/network-error
	handling has exactly one implementation instead of drifting copies.
	"""
	kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)
	try:
		return requests.request(method, url, **kwargs)
	except requests.exceptions.RequestException as e:
		frappe.throw(frappe._("Failed to reach Talabat: {0}").format(str(e)))


def _fetch_access_token(settings):
	response = _request(
		"POST",
		f"{settings.get_base_url()}/v2/oauth/token",
		data={
			"grant_type": "client_credentials",
			"client_id": settings.client_id,
			"client_secret": settings.get_password("client_secret"),
		},
	)

	if not response.ok:
		frappe.throw(frappe._("Failed to authenticate with Talabat: {0}").format(_error_message(response)))

	try:
		body = response.json()
		expires_in = body.get("expires_in", 7200)
		ttl = max(expires_in - EXPIRY_BUFFER_SECONDS, 1)
		return {
			"access_token": body["access_token"],
			"ttl": ttl,
			"expires_at": time.time() + ttl,
		}
	except (ValueError, KeyError) as e:
		frappe.throw(
			frappe._("Failed to authenticate with Talabat: unexpected response ({0})").format(str(e))
		)


def _error_message(response):
	try:
		body = response.json()
	except ValueError:
		return response.text
	return body.get("error_description") or body.get("error") or response.text


def _cache_key(environment):
	return f"{CACHE_KEY_PREFIX}:{environment}"
