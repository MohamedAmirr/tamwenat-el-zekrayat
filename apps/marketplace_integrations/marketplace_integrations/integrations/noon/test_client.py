# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import uuid
from unittest.mock import patch

import frappe
import requests
from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.http import REQUEST_TIMEOUT_SECONDS, http_request
from marketplace_integrations.integrations.noon.client import (
	LOGIN_PATH,
	USER_AGENT,
	WHOAMI_PATH,
	NoonClient,
)

PRIVATE_KEY = "-----BEGIN PRIVATE KEY-----\nfixture-only\n-----END PRIVATE KEY-----\n"


class FakeConnection:
	def __init__(self, project_code="PRJ-ONE", environment="Static Sandbox", enabled=1):
		self.name = f"Noon {project_code}"
		self.project_code = project_code
		self.key_id = f"KEY-{project_code}"
		self.environment = environment
		self.enabled = enabled

	def get_password(self, fieldname, raise_exception=False):
		assert fieldname == "private_key"
		return PRIVATE_KEY

	def get_auth_base_url(self):
		return "https://noon-api-gateway.noon.partners"

	def get_api_base_url(self):
		if self.environment == "Static Sandbox":
			return "https://noon-sandbox-api-gateway.noon.partners"
		return "https://noon-api-gateway.noon.partners"


class FakeResponse:
	def __init__(self, status_code, body=None, text=""):
		self.status_code = status_code
		self.body = body
		self.text = text

	def json(self):
		if self.body is None:
			raise ValueError("not JSON")
		return self.body


class UnitTestNoonClient(UnitTestCase):
	def setUp(self):
		self.connection = FakeConnection()
		self.get_doc_patcher = patch(
			"marketplace_integrations.integrations.noon.client.frappe.get_doc",
			return_value=self.connection,
		)
		self.get_doc_patcher.start()
		self.addCleanup(self.get_doc_patcher.stop)
		self.client = NoonClient(self.connection.name)

	def test_escaped_pem_line_breaks_are_restored_only_for_signing(self):
		escaped_key = PRIVATE_KEY.replace("\n", "\\n")
		self.connection.get_password = lambda _fieldname, raise_exception=False: escaped_key

		def send(method, _url, **kwargs):
			if method == "POST":
				kwargs["session"].cookies.set("sid", "session-one")
			return FakeResponse(200, {})

		with patch(
			"marketplace_integrations.integrations.noon.client.jwt.encode", return_value="jwt"
		) as encode:
			with patch("marketplace_integrations.integrations.noon.client.http_request", side_effect=send):
				self.client.whoami()

		self.assertEqual(encode.call_args.args[1], PRIVATE_KEY)
		self.assertEqual(self.connection.get_password("private_key"), escaped_key)

	def test_documented_jwt_login_cookie_and_whoami_sandbox_host(self):
		calls = []

		def send(method, url, **kwargs):
			calls.append((method, url, kwargs))
			if url.endswith(LOGIN_PATH):
				kwargs["session"].cookies.set("sid", "session-one", domain=".noon.partners")
				return FakeResponse(200, {})
			return FakeResponse(200, {"user_code": "U1", "username": "Noon Tester"})

		with patch(
			"marketplace_integrations.integrations.noon.client.jwt.encode", return_value="signed-jwt"
		) as encode:
			with patch("marketplace_integrations.integrations.noon.client.http_request", side_effect=send):
				first = self.client.whoami()
				second = self.client.whoami()

		self.assertEqual(first.body["user_code"], second.body["user_code"])
		self.assertEqual(len(calls), 3)
		claims = encode.call_args.args[0]
		self.assertEqual(claims["sub"], self.connection.key_id)
		self.assertIsInstance(claims["iat"], int)
		uuid.UUID(claims["jti"])
		self.assertEqual(encode.call_args.args[1], PRIVATE_KEY)
		self.assertEqual(encode.call_args.kwargs["algorithm"], "RS256")
		self.assertEqual(calls[0][0:2], ("POST", "https://noon-api-gateway.noon.partners" + LOGIN_PATH))
		self.assertEqual(calls[0][2]["json"], {"token": "signed-jwt", "default_project_code": "PRJ-ONE"})
		self.assertEqual(
			calls[1][0:2], ("GET", "https://noon-sandbox-api-gateway.noon.partners" + WHOAMI_PATH)
		)
		self.assertIs(calls[0][2]["session"], calls[1][2]["session"])
		self.assertEqual(calls[0][2]["session"].headers["User-Agent"], USER_AGENT)
		self.assertEqual(calls[1][2]["headers"]["User-Agent"], USER_AGENT)
		self.assertEqual(calls[0][2]["timeout"], REQUEST_TIMEOUT_SECONDS)
		self.assertEqual(calls[1][2]["timeout"], REQUEST_TIMEOUT_SECONDS)
		self.assertFalse(calls[0][2]["allow_redirects"])
		self.assertFalse(calls[1][2]["allow_redirects"])

	def test_production_whoami_uses_production_host(self):
		self.connection.environment = "Production"
		urls = []

		def send(method, url, **kwargs):
			urls.append(url)
			if method == "POST":
				kwargs["session"].cookies.set("sid", "session-one")
			return FakeResponse(200, {})

		with patch("marketplace_integrations.integrations.noon.client.jwt.encode", return_value="jwt"):
			with patch("marketplace_integrations.integrations.noon.client.http_request", side_effect=send):
				self.client.whoami()
		self.assertEqual(
			urls,
			[
				f"https://noon-api-gateway.noon.partners{LOGIN_PATH}",
				f"https://noon-api-gateway.noon.partners{WHOAMI_PATH}",
			],
		)

	def test_two_clients_keep_cookies_separate(self):
		other = FakeConnection(project_code="PRJ-TWO")
		clients = [self.client, NoonClient(other.name)]
		connections = {self.connection.name: self.connection, other.name: other}

		def send(method, url, **kwargs):
			if method == "POST":
				project = kwargs["json"]["default_project_code"]
				kwargs["session"].cookies.set("sid", f"cookie-{project}")
			return FakeResponse(200, {})

		with patch(
			"marketplace_integrations.integrations.noon.client.frappe.get_doc",
			side_effect=lambda _dt, name: connections[name],
		):
			with patch("marketplace_integrations.integrations.noon.client.jwt.encode", return_value="jwt"):
				with patch(
					"marketplace_integrations.integrations.noon.client.http_request", side_effect=send
				):
					for client in clients:
						client.whoami()
		self.assertEqual(clients[0]._session.cookies.get("sid"), "cookie-PRJ-ONE")
		self.assertEqual(clients[1]._session.cookies.get("sid"), "cookie-PRJ-TWO")
		self.assertIsNot(clients[0]._session, clients[1]._session)

	def test_rejected_login_surfaces_status_without_response_body_or_secret(self):
		response = FakeResponse(401, {"error": {"message": PRIVATE_KEY + "signed-jwt"}})
		with patch("marketplace_integrations.integrations.noon.client.jwt.encode", return_value="signed-jwt"):
			with patch(
				"marketplace_integrations.integrations.noon.client.http_request", return_value=response
			) as send:
				with self.assertRaises(frappe.ValidationError) as ctx:
					self.client.whoami()
		self.assertIn("HTTP 401", str(ctx.exception))
		self.assertNotIn(PRIVATE_KEY, str(ctx.exception))
		self.assertNotIn("signed-jwt", str(ctx.exception))
		send.assert_called_once()

	def test_successful_login_without_cookie_is_rejected(self):
		for cookie_value in (None, ""):
			client = NoonClient(self.connection.name)

			def send(method, url, **kwargs):
				if cookie_value is not None:
					kwargs["session"].cookies.set("sid", cookie_value)
				return FakeResponse(200, {})

			with patch("marketplace_integrations.integrations.noon.client.jwt.encode", return_value="jwt"):
				with patch(
					"marketplace_integrations.integrations.noon.client.http_request", side_effect=send
				):
					with self.assertRaisesRegex(frappe.ValidationError, "session cookie"):
						client.whoami()

	def test_401_and_429_are_returned_without_automatic_retry(self):
		for status in (401, 429):
			client = NoonClient(self.connection.name)
			calls = []

			def send(method, url, **kwargs):
				calls.append(method)
				if method == "POST":
					kwargs["session"].cookies.set("sid", "cookie")
					return FakeResponse(200, {})
				return FakeResponse(status, {"error": {"code": "RESOURCE_EXHAUSTED"}})

			with patch("marketplace_integrations.integrations.noon.client.jwt.encode", return_value="jwt"):
				with patch(
					"marketplace_integrations.integrations.noon.client.http_request", side_effect=send
				):
					response = client.whoami()
			self.assertEqual(response.status_code, status)
			self.assertEqual(calls, ["POST", "GET"])
			self.assertEqual(client._authenticated, status != 401)

	def test_disabled_connection_does_not_send_or_sign(self):
		self.connection.enabled = 0
		with patch("marketplace_integrations.integrations.noon.client.jwt.encode") as encode:
			with patch("marketplace_integrations.integrations.noon.client.http_request") as send:
				with self.assertRaisesRegex(frappe.ValidationError, "disabled"):
					self.client.whoami()
		encode.assert_not_called()
		send.assert_not_called()

	def test_malformed_signing_key_has_static_error(self):
		with patch(
			"marketplace_integrations.integrations.noon.client.jwt.encode",
			side_effect=ValueError(PRIVATE_KEY),
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self.client.whoami()
		self.assertNotIn(PRIVATE_KEY, str(ctx.exception))
		self.assertIn("signing key is invalid", str(ctx.exception))

	def test_nonlogin_error_uses_bounded_redacted_code_and_message(self):
		self.client._session.cookies.set("sid", "session-one")
		message = f"bad {self.connection.key_id} {self.connection.project_code} {PRIVATE_KEY} session-one"
		response = FakeResponse(400, {"error": {"code": "INVALID_ARGUMENT", "message": message}})
		formatted = self.client.error_message(response)
		self.assertIn("HTTP 400", formatted)
		self.assertIn("INVALID_ARGUMENT", formatted)
		self.assertIn("[redacted]", formatted)
		for secret in (self.connection.key_id, self.connection.project_code, PRIVATE_KEY, "session-one"):
			self.assertNotIn(secret, formatted)
		self.assertLess(len(formatted), 250)
		self.assertEqual(
			self.client.error_message(FakeResponse(500, None, text=PRIVATE_KEY)),
			"Noon request failed (HTTP 500).",
		)

	def test_gateway_timeout_has_a_clear_translated_reason(self):
		formatted = self.client.error_message(FakeResponse(504, None, text="<html>Gateway Time-out</html>"))
		self.assertEqual(
			formatted, "Noon did not answer in time (HTTP 504)."
		)

	def test_explicit_timeout_applies_to_the_call_but_not_to_login(self):
		calls = []

		def send(method, url, **kwargs):
			calls.append((method, kwargs["timeout"]))
			if method == "POST" and url.endswith(LOGIN_PATH):
				kwargs["session"].cookies.set("sid", "session-one")
			return FakeResponse(200, {})

		with patch("marketplace_integrations.integrations.noon.client.jwt.encode", return_value="jwt"):
			with patch("marketplace_integrations.integrations.noon.client.http_request", side_effect=send):
				self.client.request("POST", "/stock/v1/stock-update", timeout=60, json={})
				self.client.whoami()
		self.assertEqual(
			calls, [("POST", REQUEST_TIMEOUT_SECONDS), ("POST", 60), ("GET", REQUEST_TIMEOUT_SECONDS)]
		)

	def test_session_and_redirect_options_stay_rejected(self):
		with patch("marketplace_integrations.integrations.noon.client.http_request") as send:
			for option in ({"session": object()}, {"allow_redirects": True}):
				with self.assertRaises(frappe.ValidationError):
					self.client.request("GET", WHOAMI_PATH, **option)
		send.assert_not_called()

	def test_rejects_external_path_and_caller_cookie_before_network(self):
		with patch("marketplace_integrations.integrations.noon.client.http_request") as send:
			with self.assertRaises(frappe.ValidationError):
				self.client.request("GET", "https://evil.example/x")
			with self.assertRaises(frappe.ValidationError):
				self.client.request("GET", WHOAMI_PATH, headers={"Cookie": "other"})
		send.assert_not_called()


class UnitTestNoonHttpHelper(UnitTestCase):
	def test_session_transport_keeps_timeout_and_suppresses_sensitive_exception_text(self):
		session = requests.Session()
		with patch.object(session, "request", side_effect=requests.Timeout("private-key-in-error")) as send:
			with self.assertRaises(frappe.ValidationError) as ctx:
				http_request(
					"POST",
					"https://noon-api-gateway.noon.partners/test",
					service_name="Noon",
					session=session,
					include_exception_details=False,
				)
		self.assertNotIn("private-key-in-error", str(ctx.exception))
		self.assertIn("Failed to reach Noon", str(ctx.exception))
		self.assertEqual(send.call_args.kwargs["timeout"], REQUEST_TIMEOUT_SECONDS)

	def test_destination_write_calls_use_documented_paths_and_bodies(self):
		client = NoonClient("unused")
		with patch.object(client, "request") as request:
			client.create_https_destination("name", "https://x.test/hook", {"X-Api-Key": "k"})
			request.assert_called_with(
				"POST",
				"/event-notifications/v1/destination/https-destination/create",
				json={
					"destination_name": "name",
					"url": "https://x.test/hook",
					"credentials": {"values": {"X-Api-Key": "k"}},
				},
			)
			client.update_destination("DEST/1", is_active=False)
			request.assert_called_with(
				"POST",
				"/event-notifications/v1/destination/https-destination/DEST%2F1/update",
				json={"is_active": False},
			)
