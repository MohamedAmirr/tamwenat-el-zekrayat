# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Talabat Partner API client, scoped to one Talabat Connection.

This is a faithful port of the proven auth/request logic from the legacy
talabat_integration app (api/auth.py + api/client.py) - same token-caching-
with-expiry-buffer, same one-retry-on-401 contract, same shared
timeout/network-error wrapper, same "raw non-401 response returned to the
caller" contract. See those two modules for the original, still-untouched
implementation this migrates from.

The one deliberate behavior change: token caching is keyed per Talabat
Connection document (`TalabatClient.connection_name`), not globally per
environment. The legacy app is single-tenant (one Talabat Settings Single
record per site) so a per-environment cache key was sufficient; this app
supports multiple concurrent Talabat Connections (e.g. two vendors both on
Production), so two connections in the same environment must not share a
token.
"""

import time

import frappe
import requests

CACHE_KEY_PREFIX = "marketplace_integrations:talabat:access_token"
# Refresh a bit before actual expiry so a request never races a token that
# expires mid-flight.
EXPIRY_BUFFER_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 10


def get_access_token_cache_key(connection_name):
	return f"{CACHE_KEY_PREFIX}:{connection_name}"


def _request(method, url, **kwargs):
	"""Make an HTTP request to Talabat, raising a clean frappe error on any
	network-level failure (connection error, timeout, SSL error, ...).

	Shared by every Talabat call a TalabatClient makes - token fetch and the
	general `request()` choke point alike - so timeout/network-error handling
	has exactly one implementation instead of drifting copies.
	"""
	kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)
	try:
		return requests.request(method, url, **kwargs)
	except requests.exceptions.RequestException as e:
		frappe.throw(frappe._("Failed to reach Talabat: {0}").format(str(e)))


def _error_message(response):
	try:
		body = response.json()
	except ValueError:
		return response.text
	return body.get("error_description") or body.get("error") or response.text


class TalabatClient:
	"""Authenticated Talabat Partner API client for one Talabat Connection.

	Usage:
		client = TalabatClient(connection.name)
		response = client.request("GET", "/v2/chains/.../vendors/v1/status")

	Do not call `requests` directly against Talabat anywhere else - `request()`
	is the single choke point for every Talabat endpoint (catalog, orders,
	promotions, ...), same as legacy client.py's `talabat_request()`.
	"""

	def __init__(self, connection_name):
		self.connection_name = connection_name

	def get_connection(self):
		return frappe.get_doc("Talabat Connection", self.connection_name)

	def get_access_token(self):
		"""Return a valid access token for this connection, using the cache
		when possible."""
		cache_key = self._cache_key()
		cached = frappe.cache().get_value(cache_key)
		if cached and cached["expires_at"] > time.time():
			return cached["access_token"]

		token = self._fetch_access_token()
		frappe.cache().set_value(cache_key, token, expires_in_sec=token["ttl"])
		return token["access_token"]

	def invalidate_access_token(self):
		"""Drop this connection's cached token, forcing the next
		get_access_token() call to fetch a fresh one instead of returning a
		token a caller has determined is no longer accepted (e.g. a 401)."""
		frappe.cache().delete_value(self._cache_key())

	def request(self, method, path, **kwargs):
		"""Make an authenticated request to the Talabat Partner API.

		Retries exactly once on a 401 with a freshly-fetched token. Returns
		the raw response for any non-401 status (200, 4xx, 5xx alike) - unlike
		get_access_token(), this does NOT raise on a non-2xx response.
		Different Talabat endpoints use different success codes, so callers
		are responsible for checking `response.ok`/status themselves.
		"""
		connection = self.get_connection()
		path = path if path.startswith("/") else f"/{path}"
		url = f"{connection.get_base_url()}{path}"

		response = self._send(method, url, self.get_access_token(), **kwargs)

		if response.status_code == 401:
			self.invalidate_access_token()
			response = self._send(method, url, self.get_access_token(), **kwargs)
			if response.status_code == 401:
				frappe.throw(
					frappe._(
						"Talabat rejected the request as unauthorized even after refreshing the access token."
					)
				)

		return response

	def _send(self, method, url, access_token, **kwargs):
		headers = kwargs.pop("headers", {}) or {}
		headers["Authorization"] = f"Bearer {access_token}"
		return _request(method, url, headers=headers, **kwargs)

	def _fetch_access_token(self):
		connection = self.get_connection()
		response = _request(
			"POST",
			f"{connection.get_base_url()}/v2/oauth/token",
			data={
				"grant_type": "client_credentials",
				"client_id": connection.client_id,
				"client_secret": connection.get_password("client_secret"),
			},
		)

		if not response.ok:
			frappe.throw(
				frappe._("Failed to authenticate with Talabat: {0}").format(_error_message(response))
			)

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

	def _cache_key(self):
		return get_access_token_cache_key(self.connection_name)
