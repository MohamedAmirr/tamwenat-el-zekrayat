# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Service-account and cookie-session client for one Noon Connection.

Noon uses a signed API JWT for login, then session cookies for subsequent
requests. It does not use Talabat/Shopify's OAuth client-credentials flow.
The cookie jar belongs only to this client instance; it is never put in a
site-wide cache or exposed through a whitelisted method.
"""

import time
import uuid
from urllib.parse import urlsplit

import frappe
import jwt
import requests

from marketplace_integrations import __version__
from marketplace_integrations.integrations.http import (
	REQUEST_TIMEOUT_SECONDS,
	http_request,
	truncate_response_text,
)

LOGIN_PATH = "/identity/public/v1/api/login"
WHOAMI_PATH = "/identity/v1/whoami"
SERVICE_NAME = "Noon"
USER_AGENT = f"marketplace_integrations/{__version__}"


class NoonClient:
	"""Authenticated Noon API transport scoped to a single connection."""

	def __init__(self, connection_name):
		self.connection_name = connection_name
		self._session = requests.Session()
		self._session.headers.update({"User-Agent": USER_AGENT})
		self._authenticated = False

	def get_connection(self):
		return frappe.get_doc("Noon Connection", self.connection_name)

	def _authenticate(self, connection):
		"""Exchange a fresh signed JWT for cookies at the production host."""
		private_key = connection.get_password("private_key", raise_exception=False)
		if not private_key:
			frappe.throw(frappe._("Noon Connection has no signing key."))

		token = None
		try:
			token = jwt.encode(
				{"sub": connection.key_id, "iat": int(time.time()), "jti": str(uuid.uuid4())},
				private_key.replace("\\n", "\n"),
				algorithm="RS256",
			)
		except Exception:
			# Signing libraries can include key material in exception details.
			token = None
		if token is None:
			frappe.throw(frappe._("Noon Connection signing key is invalid."))

		self._session.cookies.clear()
		response = http_request(
			"POST",
			f"{connection.get_auth_base_url()}{LOGIN_PATH}",
			session=self._session,
			service_name=SERVICE_NAME,
			include_exception_details=False,
			allow_redirects=False,
			timeout=REQUEST_TIMEOUT_SECONDS,
			json={"token": token, "default_project_code": connection.project_code},
		)
		if response.status_code != 200:
			frappe.throw(frappe._("Noon authentication failed (HTTP {0}).").format(response.status_code))
		if not any(cookie.value for cookie in self._session.cookies):
			frappe.throw(frappe._("Noon authentication succeeded without a session cookie."))
		self._authenticated = True

	def request(self, method, path, **kwargs):
		"""Send one documented API request; return its raw HTTP response.

		401 and 429 responses are returned unchanged. In particular, this
		method does not automatically replay a request that may have side
		effects. A 401 marks the session stale, so a later separate call can
		obtain a new cookie.
		"""
		parsed_path = urlsplit(path)
		if not path.startswith("/") or path.startswith("//") or parsed_path.scheme or parsed_path.netloc:
			frappe.throw(frappe._("Noon request path must be relative to the API host."))
		if "timeout" in kwargs or "session" in kwargs or "allow_redirects" in kwargs:
			frappe.throw(frappe._("Noon request transport options are managed by the client."))
		headers = kwargs.pop("headers", {}) or {}
		if any(key.lower() == "cookie" for key in headers):
			frappe.throw(frappe._("Noon cookies are managed by the client."))
		headers = {key: value for key, value in headers.items() if key.lower() != "user-agent"}

		connection = self.get_connection()
		if not connection.enabled:
			frappe.throw(frappe._("Noon Connection {0} is disabled.").format(self.connection_name))
		api_base_url = connection.get_api_base_url()
		if not self._authenticated:
			self._authenticate(connection)

		headers["User-Agent"] = USER_AGENT
		response = http_request(
			method,
			f"{api_base_url}{path}",
			session=self._session,
			service_name=SERVICE_NAME,
			include_exception_details=False,
			allow_redirects=False,
			timeout=REQUEST_TIMEOUT_SECONDS,
			headers=headers,
			**kwargs,
		)
		if response.status_code == 401:
			self._authenticated = False
		return response

	def whoami(self):
		"""GET the documented authenticated identity endpoint."""
		return self.request("GET", WHOAMI_PATH)

	def error_message(self, response):
		"""Format a non-login error without echoing a raw response body.

		Noon's documented error envelope nests ``code`` and ``message`` under
		``error``. The shared top-level error extractor would stringify the
		whole object (including field-level details), so extract only these
		two fields and apply the shared length bound after redaction.
		"""
		status = frappe._("Noon request failed (HTTP {0}).").format(response.status_code)
		try:
			body = response.json()
		except ValueError:
			return status
		if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
			return status
		error = body["error"]
		parts = [value for key in ("code", "message") if isinstance((value := error.get(key)), str) and value]
		if not parts:
			return status
		message = ": ".join(parts)
		connection = self.get_connection()
		secrets = (
			connection.get_password("private_key", raise_exception=False),
			connection.key_id,
			connection.project_code,
			*(cookie.value for cookie in self._session.cookies),
		)
		for secret in secrets:
			if secret:
				message = message.replace(secret, "[redacted]")
		return f"{status} {truncate_response_text(message)}"
