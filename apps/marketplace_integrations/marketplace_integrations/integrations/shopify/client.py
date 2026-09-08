# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Shopify client-credentials token client, scoped to one Shopify Connection.

Shopify's client-credentials grant issues a token that expires (~24h,
`expires_in` on the wire), unlike the old static `shpat_...` token - see
docs/shopify/README.md. This mirrors TalabatClient's token-cache-with-expiry-
buffer pattern, keyed per Shopify Connection document so two connections
never share a token. The token fetch/expiry-buffer math and the underlying
bounded-timeout/network-error request wrapper are platform-neutral and
shared with TalabatClient via ``integrations/http.py`` - see that module for
the single implementation of both.

Deliberately out of scope here (see #101): the GraphQL execute()/query layer,
API version pinning, throttle handling, userErrors, and reactive 401 retry -
all #102.
"""

import time

import frappe

from marketplace_integrations.integrations.http import fetch_client_credentials_token
from marketplace_integrations.integrations.shopify.constants import SHOPIFY_TOKEN_PATH

CACHE_KEY_PREFIX = "marketplace_integrations:shopify:access_token"
# Refresh a bit before actual expiry so a request never races a token that
# expires mid-flight. Same rationale/value as TalabatClient's buffer.
EXPIRY_BUFFER_SECONDS = 60
DEFAULT_EXPIRES_IN = 86399
SERVICE_NAME = "Shopify"


def get_access_token_cache_key(connection_name):
	return f"{CACHE_KEY_PREFIX}:{connection_name}"


class ShopifyClient:
	"""Client-credentials access token client for one Shopify Connection.

	Usage:
		client = ShopifyClient(connection.name)
		token = client.get_access_token()

	No `request()`/GraphQL execute method exists yet - that's #102. This module
	only owns the token lifecycle.
	"""

	def __init__(self, connection_name):
		self.connection_name = connection_name

	def get_connection(self):
		return frappe.get_doc("Shopify Connection", self.connection_name)

	def get_access_token(self):
		"""Return a valid access token for this connection, using the cache
		when possible.

		Note: this does NOT check ``Shopify Connection.enabled`` - there is no
		caller yet to make that reachable (#101 adds no request()/GraphQL
		layer). Enforcing the flag must be added by whichever future issue
		(#102/#103) adds the first real caller of this client - see
		docs/shopify/README.md's "Open questions".
		"""
		cache_key = self._cache_key()
		cached = frappe.cache().get_value(cache_key)
		if cached and cached["expires_at"] > time.time():
			return cached["access_token"]

		token = self._fetch_access_token()
		frappe.cache().set_value(cache_key, token, expires_in_sec=token["ttl"])
		return token["access_token"]

	def invalidate_access_token(self):
		"""Drop this connection's cached token, forcing the next
		get_access_token() call to fetch a fresh one."""
		frappe.cache().delete_value(self._cache_key())

	def _fetch_access_token(self):
		connection = self.get_connection()
		return fetch_client_credentials_token(
			f"{connection.get_store_base_url()}{SHOPIFY_TOKEN_PATH}",
			connection.client_id,
			connection.get_password("client_secret"),
			service_name=SERVICE_NAME,
			default_expires_in=DEFAULT_EXPIRES_IN,
			expiry_buffer_seconds=EXPIRY_BUFFER_SECONDS,
		)

	def _cache_key(self):
		return get_access_token_cache_key(self.connection_name)
