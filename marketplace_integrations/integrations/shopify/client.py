# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Shopify client, scoped to one Shopify Connection.

Shopify's client-credentials grant issues a token that expires (~24h,
`expires_in` on the wire), unlike the old static `shpat_...` token - see
docs/shopify/README.md. This mirrors TalabatClient's token-cache-with-expiry-
buffer pattern, keyed per Shopify Connection document so two connections
never share a token. The token fetch/expiry-buffer math and the underlying
bounded-timeout/network-error request wrapper are platform-neutral and
shared with TalabatClient via ``integrations/http.py`` - see that module for
the single implementation of both.

``execute()`` is the single choke point for every Admin GraphQL call (#102) -
see docs/shopify/README.md's "GraphQL Admin API (for #102)" section for the
endpoint, auth header, request/response shape, and error/throttle shapes this
implements. No other module should open a `requests` call site against
Shopify.

Deliberately out of scope here (see #102's issue body): a cursor pagination
helper (#105), a connection-test method (validated instead via #103's
Location loading), Marketplace Account wiring, webhook registration, and any
product/inventory/order queries.
"""

import time

import frappe

from marketplace_integrations.integrations.http import (
	error_message_from_response,
	fetch_client_credentials_token,
	http_request,
	truncate_response_text,
)
from marketplace_integrations.integrations.shopify.constants import (
	SHOPIFY_GRAPHQL_PATH,
	SHOPIFY_TOKEN_PATH,
)

CACHE_KEY_PREFIX = "marketplace_integrations:shopify:access_token"
# Refresh a bit before actual expiry so a request never races a token that
# expires mid-flight. Same rationale/value as TalabatClient's buffer.
EXPIRY_BUFFER_SECONDS = 60
DEFAULT_EXPIRES_IN = 86399
SERVICE_NAME = "Shopify"

# Bounded cap on throttled-retry attempts - Shopify's `retryAfter` tells us
# how long to wait, but never how many times it's safe to wait; without a
# cap a persistently-throttled query would retry forever instead of
# surfacing a clear error to the caller.
MAX_THROTTLE_ATTEMPTS = 3
# Fallback backoff if a throttled response is missing, or carries an
# invalid, `extensions.retryAfter` - keeps the retry loop from calling
# time.sleep(None) or on an unparseable/remote-controlled value.
DEFAULT_THROTTLE_BACKOFF_SECONDS = 1
# Hard cap on how long a single throttled-retry sleep will ever wait, no
# matter what `retryAfter` Shopify's response reports - `retryAfter` is a
# remote-controlled value with no documented upper bound, so without a cap a
# malicious/misbehaving response could stall this call far longer than a
# GraphQL request should ever legitimately wait. Real Shopify `retryAfter`
# values are sub-second to a few seconds, so this cap is set low enough that
# it never truncates a legitimate wait while keeping execute()'s worst-case
# synchronous blocking time (this cap times MAX_THROTTLE_ATTEMPTS) inside a
# normal web-request budget.
MAX_THROTTLE_BACKOFF_SECONDS = 10


def get_access_token_cache_key(connection_name):
	return f"{CACHE_KEY_PREFIX}:{connection_name}"


class ShopifyClient:
	"""Authenticated Admin GraphQL client for one Shopify Connection.

	Usage:
		client = ShopifyClient(connection.name)
		data = client.execute(query, variables)

	Do not call `requests` directly against Shopify anywhere else - `execute()`
	is the single choke point for every Admin GraphQL call, same as
	TalabatClient's `request()`.
	"""

	def __init__(self, connection_name):
		self.connection_name = connection_name

	def get_connection(self):
		return frappe.get_doc("Shopify Connection", self.connection_name)

	def get_access_token(self):
		"""Return a valid access token for this connection, using the cache
		when possible.

		Note: this does NOT check ``Shopify Connection.enabled`` itself - that
		is enforced by ``execute()``, the first real caller of this client
		(see docs/shopify/README.md's "Open questions" on #101). A direct
		``get_access_token()`` call bypasses that check.
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

	def execute(self, query, variables=None):
		"""Run one Admin GraphQL query/mutation against this connection.

		Single choke point for every Admin GraphQL call - see module
		docstring. Returns the response's top-level ``data`` object as-is,
		including any nested per-mutation ``userErrors`` - those are the
		caller's concern to inspect, never swallowed or raised here.

		Raises on:
		- a network-level failure (via http_request)
		- a non-2xx response other than a 401 (which gets one reactive
		  invalidate-and-retry; a second 401 raises)
		- a malformed 2xx body (not valid JSON, or valid JSON that isn't a
		  top-level object)
		- a HTTP 200 carrying a non-empty top-level ``errors[]`` that is not
		  a throttle response
		- a throttled response that is still throttled after
		  MAX_THROTTLE_ATTEMPTS
		- a 2xx body carrying neither ``data`` nor ``errors``

		The token-refresh budget from the 401 handling above is shared across
		every throttle-retry attempt within this one ``execute()`` call, not
		reset per attempt - otherwise throttling and token expiry occurring
		together could refresh the token once per throttle attempt instead of
		once total.

		No hard timeout bounds this call's own worst-case blocking time
		beyond the throttle backoff itself: in the worst case a caller can
		block synchronously for roughly
		``MAX_THROTTLE_BACKOFF_SECONDS * MAX_THROTTLE_ATTEMPTS`` seconds (plus
		normal request/network latency and, on this project, at most one
		reactive 401 token refresh) before either succeeding or raising. This
		is a documented tradeoff, not an oversight - see #103's Marketplace
		Account review finding #3: no enqueue/background-job infrastructure
		exists to bound this further, and building one is out of scope until
		a real caller actually needs it.
		"""
		connection = self.get_connection()
		if not connection.enabled:
			frappe.throw(frappe._("Shopify Connection {0} is disabled.").format(self.connection_name))

		url = f"{connection.get_store_base_url()}{SHOPIFY_GRAPHQL_PATH}"
		payload = {"query": query, "variables": variables or {}}

		attempts = 0
		token_refresh_state = {"used": False}
		while True:
			attempts += 1
			response = self._send_with_auth_retry(url, payload, token_refresh_state)

			if not response.ok:
				frappe.throw(
					frappe._("Shopify GraphQL request failed: {0}").format(
						error_message_from_response(response)
					)
				)

			body = _parse_graphql_body(response)
			errors = _normalized_errors(body.get("errors"))
			retry_after = _throttled_retry_after(errors)

			if retry_after is not None:
				if attempts >= MAX_THROTTLE_ATTEMPTS:
					frappe.throw(
						frappe._(
							"Shopify GraphQL request was throttled and did not succeed after {0} attempts."
						).format(MAX_THROTTLE_ATTEMPTS)
					)
				time.sleep(retry_after)
				continue

			if errors:
				frappe.throw(
					frappe._("Shopify GraphQL request returned errors: {0}").format(
						_format_graphql_errors(errors)
					)
				)

			if body.get("data") is None:
				frappe.throw(frappe._("Shopify GraphQL response carried neither data nor errors."))

			return body.get("data")

	def _send_with_auth_retry(self, url, payload, token_refresh_state):
		"""Send one request, reactively refreshing the access token on a 401.

		``token_refresh_state`` is a mutable dict shared by the caller across
		every throttle-retry attempt in the same ``execute()`` call, so the
		one-refresh budget is spent at most once per ``execute()`` call, not
		once per attempt - see ``execute()``'s docstring.
		"""
		response = self._send(url, payload, self.get_access_token())
		if response.status_code == 401:
			if token_refresh_state["used"]:
				frappe.throw(
					frappe._(
						"Shopify rejected the request as unauthorized even after refreshing the access token."
					)
				)
			token_refresh_state["used"] = True
			self.invalidate_access_token()
			response = self._send(url, payload, self.get_access_token())
			if response.status_code == 401:
				frappe.throw(
					frappe._(
						"Shopify rejected the request as unauthorized even after refreshing the access token."
					)
				)
		return response

	def _send(self, url, payload, access_token):
		headers = {
			"X-Shopify-Access-Token": access_token,
			"Content-Type": "application/json",
		}
		return http_request("POST", url, service_name=SERVICE_NAME, headers=headers, json=payload)

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


def _parse_graphql_body(response):
	"""Parse a 2xx GraphQL response body, raising a clean error on anything
	that isn't a JSON object.

	A 2xx response is never guaranteed to actually carry a well-formed
	GraphQL body - it could be non-JSON (e.g. an upstream proxy/WAF page) or
	valid JSON that isn't a top-level object (e.g. a bare list or ``null``),
	both of which would otherwise raise an uncaught ``JSONDecodeError``/
	``AttributeError`` deep in ``execute()`` instead of a clean error here.
	"""
	try:
		body = response.json()
	except ValueError:
		frappe.throw(
			frappe._("Shopify GraphQL response was not valid JSON: {0}").format(
				truncate_response_text(response.text)
			)
		)

	if not isinstance(body, dict):
		frappe.throw(
			frappe._("Shopify GraphQL response had an unexpected shape: {0}").format(
				truncate_response_text(response.text)
			)
		)

	return body


def _normalized_errors(errors):
	"""Coerce a GraphQL response's top-level `errors` field into a clean list.

	`errors` comes straight off the wire with no documented type guarantee -
	Shopify's own contract is a list of error objects, but a malformed/
	unexpected response could carry a single error object (a dict) or a bare
	string instead. Without normalizing here, a dict-shaped `errors` would
	pass `errors[0]` in `_throttled_retry_after()` as `errors[0]` being a
	single character of a dict's iteration (raising `KeyError`/nonsense) and
	a string-shaped `errors` would similarly be misindexed - and either shape
	would otherwise never actually reach `_format_graphql_errors()` with the
	original message intact. Wrap a non-list value into a single-item list so
	both the throttle check and the generic error-message path always see a
	list of dict-like entries (or a string entry, still shown verbatim by
	`_format_graphql_errors()`) and nothing is silently dropped.
	"""
	if errors is None:
		return []
	if isinstance(errors, list):
		return errors
	return [errors]


def _throttled_retry_after(errors):
	"""Return the throttle backoff (seconds) if `errors` is a THROTTLED
	response, else None. See docs/shopify/README.md's throttle shape:
	``errors: [{"extensions": {"code": "THROTTLED", "retryAfter": <seconds>}}]``.

	Always returns a valid, capped, non-None number when the response is
	THROTTLED, even if ``retryAfter`` is missing or malformed - see
	``_sanitized_retry_after()``. This is what makes
	``DEFAULT_THROTTLE_BACKOFF_SECONDS`` reachable: a THROTTLED response with
	no usable ``retryAfter`` must still be treated as throttled (and retried
	using the default backoff), not fall through to the generic error path.
	"""
	if not errors:
		return None
	first_error = errors[0]
	if not isinstance(first_error, dict):
		return None
	extensions = first_error.get("extensions")
	if not isinstance(extensions, dict) or extensions.get("code") != "THROTTLED":
		return None
	return _sanitized_retry_after(extensions.get("retryAfter"))


def _sanitized_retry_after(retry_after):
	"""Coerce a THROTTLED response's remote-controlled `retryAfter` into a
	safe `time.sleep()` duration.

	Shopify's `retryAfter` comes straight off the wire with no documented
	type guarantee or upper bound - never pass it to `time.sleep()` directly.
	Falls back to `DEFAULT_THROTTLE_BACKOFF_SECONDS` when it's missing or not
	a real number (rejecting `bool`, since `True`/`False` are technically
	`int` in Python but not a meaningful duration), and is always capped at
	`MAX_THROTTLE_BACKOFF_SECONDS` so a huge/negative value can't stall the
	retry loop.
	"""
	if isinstance(retry_after, bool) or not isinstance(retry_after, (int, float)):
		retry_after = DEFAULT_THROTTLE_BACKOFF_SECONDS
	return min(max(retry_after, 0), MAX_THROTTLE_BACKOFF_SECONDS)


def _format_graphql_errors(errors):
	messages = []
	for error in errors:
		if isinstance(error, dict):
			messages.append(error.get("message", ""))
		elif isinstance(error, str):
			messages.append(error)
	return "; ".join(message for message in messages if message) or "unknown error"
