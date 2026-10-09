# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase


def make_connection(connection_name, **kwargs):
	doc = {
		"doctype": "Noon Connection",
		"connection_name": connection_name,
		"project_code": f"test-project-{frappe.generate_hash(length=8)}",
		"key_id": "test-key-id",
		"private_key": "-----BEGIN PRIVATE KEY-----\nexample\n-----END PRIVATE KEY-----\n",
		"environment": "Static Sandbox",
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


class IntegrationTestNoonConnection(IntegrationTestCase):
	def test_connection_uses_whoami_and_returns_no_remote_identity(self):
		connection = make_connection("Test Noon Whoami")
		response = SimpleNamespace(status_code=200)
		with patch("marketplace_integrations.integrations.noon.client.NoonClient") as client_class:
			client_class.return_value.whoami.return_value = response
			result = connection.test_connection()

		client_class.assert_called_once_with(connection.name)
		client_class.return_value.whoami.assert_called_once_with()
		self.assertEqual(result, {"connected": True})

	def test_connection_reports_only_client_sanitized_failure(self):
		connection = make_connection("Test Noon Whoami Failure")
		response = SimpleNamespace(status_code=429)
		with patch("marketplace_integrations.integrations.noon.client.NoonClient") as client_class:
			client_class.return_value.whoami.return_value = response
			client_class.return_value.error_message.return_value = "Noon request failed (HTTP 429)."
			with self.assertRaisesRegex(frappe.ValidationError, "HTTP 429"):
				connection.test_connection()

		client_class.return_value.error_message.assert_called_once_with(response)

	def test_connection_requires_saved_document(self):
		connection = frappe.new_doc("Noon Connection")
		with patch("marketplace_integrations.integrations.noon.client.NoonClient") as client_class:
			with self.assertRaisesRegex(frappe.ValidationError, "Save the Noon Connection"):
				connection.test_connection()
		client_class.assert_not_called()

	def test_connection_rejects_user_without_write_permission_before_network(self):
		connection = make_connection("Restricted Noon Whoami")
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"noon-whoami-{frappe.generate_hash(length=8)}@example.com",
				"first_name": "Noon Whoami Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()
		with patch("marketplace_integrations.integrations.noon.client.NoonClient") as client_class:
			with self.set_user(user.name), self.assertRaises(frappe.PermissionError):
				connection.test_connection()
		client_class.assert_not_called()

	def test_fields_persist_and_naming_is_expression_based(self):
		connection = make_connection("Test Noon Connection")
		connection.reload()

		self.assertEqual(connection.connection_name, "Test Noon Connection")
		self.assertEqual(connection.key_id, "test-key-id")
		self.assertEqual(connection.environment, "Static Sandbox")
		self.assertEqual(connection.enabled, 1)
		self.assertTrue(connection.name.startswith("NOONCONN-"))
		self.assertNotIn(connection.project_code, connection.name)

	def test_two_connections_have_independent_private_keys(self):
		first_key = "-----BEGIN PRIVATE KEY-----\nfirst\n-----END PRIVATE KEY-----\n"
		second_key = "-----BEGIN PRIVATE KEY-----\nsecond\n-----END PRIVATE KEY-----\n"
		first = make_connection("First Noon Connection", private_key=first_key)
		second = make_connection("Second Noon Connection", private_key=second_key)
		first.reload()
		second.reload()

		self.assertNotEqual(first.name, second.name)
		self.assertNotEqual(first.project_code, second.project_code)
		self.assertEqual(first.get_password("private_key"), first_key)
		self.assertEqual(second.get_password("private_key"), second_key)
		self.assertNotIn(first_key, str(first.as_dict()))
		self.assertNotIn(second_key, str(second.as_dict()))

	def test_multiline_private_key_preserves_exact_bytes(self):
		private_key = "-----BEGIN PRIVATE KEY-----\nline-with-<p<+X>\nline-two\n-----END PRIVATE KEY-----\n"
		connection = make_connection("Noon PEM Roundtrip", private_key=private_key)
		connection.reload()

		self.assertEqual(connection.get_password("private_key"), private_key)
		self.assertNotEqual(connection.private_key, private_key)

	def test_project_code_is_opaque_and_unique(self):
		project_code = "project-<p<+X>"
		first = make_connection("First Project", project_code=project_code)
		first.reload()
		self.assertEqual(first.project_code, project_code)
		with self.assertRaises(frappe.UniqueValidationError):
			make_connection("Duplicate Project", project_code=project_code)

	def test_environment_requires_explicit_valid_choice(self):
		for environment in (None, "", "Sandbox", "production"):
			with self.subTest(environment=environment), self.assertRaises(frappe.ValidationError):
				make_connection("Invalid Noon Environment", environment=environment)

	def test_auth_and_api_hosts_for_both_environments(self):
		production = make_connection("Production Noon", environment="Production")
		sandbox = make_connection("Sandbox Noon", environment="Static Sandbox")
		self.assertEqual(production.get_auth_base_url(), "https://noon-api-gateway.noon.partners")
		self.assertEqual(sandbox.get_auth_base_url(), "https://noon-api-gateway.noon.partners")
		self.assertEqual(production.get_api_base_url(), "https://noon-api-gateway.noon.partners")
		self.assertEqual(sandbox.get_api_base_url(), "https://noon-sandbox-api-gateway.noon.partners")

	def test_url_methods_fail_closed_if_environment_is_changed_in_memory(self):
		connection = make_connection("Invalid URL Host Noon")
		for environment in (None, "", "Unknown"):
			connection.environment = environment
			with self.subTest(environment=environment):
				with self.assertRaises(frappe.ValidationError):
					connection.get_auth_base_url()
				with self.assertRaises(frappe.ValidationError):
					connection.get_api_base_url()

	def test_save_makes_no_noon_request(self):
		with patch("requests.sessions.Session.request") as request:
			connection = make_connection("Noon Save Without Network")
			connection.connection_name = "Noon Save Without Network Updated"
			connection.save()
		request.assert_not_called()

	def test_only_system_manager_has_document_permissions(self):
		permissions = frappe.get_meta("Noon Connection").permissions
		self.assertEqual({row.role for row in permissions}, {"System Manager"})
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"noon-connection-{frappe.generate_hash(length=8)}@example.com",
				"first_name": "Noon Connection Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()
		connection = make_connection("Restricted Noon Connection")
		with self.set_user(user.name):
			for ptype in ("read", "write", "create", "delete"):
				with self.subTest(ptype=ptype):
					self.assertFalse(frappe.has_permission("Noon Connection", ptype, doc=connection))

	def test_new_connection_generates_webhook_url_when_empty(self):
		"""A new connection with no webhook_url is automatically filled with the
		webhook receiver URL when saved."""
		connection = make_connection("Webhook URL Auto Generation")
		connection.webhook_url = ""
		connection.validate()

		self.assertTrue(connection.webhook_url)
		self.assertIn(
			"api/method/marketplace_integrations.api.noon_webhooks.event_notification",
			connection.webhook_url,
		)
		self.assertTrue(connection.webhook_url.startswith("http"))

	def test_new_connection_generates_webhook_secret_when_empty(self):
		"""A new connection with no webhook_secret is automatically filled with a
		random secret when saved."""
		connection = make_connection("Webhook Secret Auto Generation", webhook_secret="")
		connection.reload()

		secret = connection.get_password("webhook_secret")
		self.assertTrue(secret)
		self.assertGreater(len(secret), 20)

	def test_new_connection_webhook_url_and_secret_both_generated(self):
		"""Both webhook_url and webhook_secret are generated for a new connection."""
		connection = frappe.new_doc("Noon Connection")
		connection.connection_name = "Both Webhook Fields Auto Generation"
		connection.project_code = f"test-project-{frappe.generate_hash(length=8)}"
		connection.key_id = "test-key-id"
		connection.private_key = "-----BEGIN PRIVATE KEY-----\nexample\n-----END PRIVATE KEY-----\n"
		connection.environment = "Static Sandbox"
		connection.insert()

		self.assertTrue(connection.webhook_url)
		self.assertTrue(connection.get_password("webhook_secret"))
		self.assertIn(
			"api/method/marketplace_integrations.api.noon_webhooks.event_notification",
			connection.webhook_url,
		)

	def test_existing_connection_keeps_webhook_secret_on_update(self):
		"""An existing connection with a webhook_secret does not regenerate it
		when saved again - the secret persists across updates."""
		connection = make_connection("Keep Secret On Update")
		connection.reload()
		original_secret = connection.get_password("webhook_secret")

		connection.connection_name = "Keep Secret On Update - Updated"
		connection.save()
		connection.reload()
		updated_secret = connection.get_password("webhook_secret")

		self.assertEqual(original_secret, updated_secret)

	def test_get_webhook_secret_for_copy_returns_secret(self):
		"""get_webhook_secret_for_copy() returns the webhook_secret to a caller
		with write permission."""
		connection = make_connection("Secret Copy")
		result = connection.get_webhook_secret_for_copy()

		self.assertIn("webhook_secret", result)
		self.assertEqual(result["webhook_secret"], connection.get_password("webhook_secret"))

	def test_get_webhook_secret_for_copy_requires_write_permission(self):
		"""get_webhook_secret_for_copy() checks write permission and raises
		PermissionError for a non-editor."""
		connection = make_connection("Secret Copy Restricted")
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"noon-secret-copy-{frappe.generate_hash(length=8)}@example.com",
				"first_name": "Noon Secret Copy Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()

		with self.set_user(user.name), self.assertRaises(frappe.PermissionError):
			connection.get_webhook_secret_for_copy()

	def test_explicit_webhook_secret_is_kept_on_save(self):
		"""When a connection is created with an explicit webhook_secret, that
		secret is preserved and not regenerated."""
		manual_secret = "manual-secret-value"
		connection = make_connection("Explicit Secret", webhook_secret=manual_secret)
		connection.reload()

		secret = connection.get_password("webhook_secret")
		self.assertEqual(secret, manual_secret)

	def test_refresh_webhook_url_rewrites_blank_url(self):
		"""refresh_webhook_url() rewrites a blank/stale webhook_url with the
		current site-derived URL and returns the new URL."""
		connection = make_connection("Webhook URL Refresh")
		connection.reload()

		# Blank the URL and call refresh
		frappe.db.set_value("Noon Connection", connection.name, "webhook_url", "")
		connection.reload()
		self.assertEqual(connection.webhook_url, "")

		result = connection.refresh_webhook_url()

		self.assertIn("webhook_url", result)
		self.assertTrue(result["webhook_url"])
		self.assertIn(
			"api/method/marketplace_integrations.api.noon_webhooks.event_notification",
			result["webhook_url"],
		)

	def test_refresh_webhook_url_requires_write_permission(self):
		"""refresh_webhook_url() checks write permission and raises
		PermissionError for a non-editor."""
		connection = make_connection("Webhook URL Refresh Restricted")
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"noon-url-refresh-{frappe.generate_hash(length=8)}@example.com",
				"first_name": "Noon URL Refresh Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()

		with self.set_user(user.name), self.assertRaises(frappe.PermissionError):
			connection.refresh_webhook_url()

	def test_refresh_webhook_url_ignores_request_host_header(self):
		"""refresh_webhook_url() builds the URL with allow_header_override=False,
		so a request Host header never affects the persisted URL."""
		from werkzeug.wrappers import Request

		connection = make_connection("Webhook URL Host Header Check")
		connection.reload()

		# Create a fake request with a spoofed Host header. If get_url() respects
		# allow_header_override=True, this header would change the URL result.
		# Since refresh_webhook_url() passes allow_header_override=False, this
		# spoofed host must be ignored.
		spoofed_host = "fake-attacker-host.evil.com"
		web_request = Request.from_values(
			path="/api/method/test",
			headers={"Host": spoofed_host},
		)

		# Bind the fake request and patch frappe.request to use it
		frappe.local.request = web_request
		try:
			with patch.object(frappe, "request", web_request):
				result = connection.refresh_webhook_url()
		finally:
			del frappe.local.request

		# The URL should NOT contain the spoofed host, proving allow_header_override=False works
		self.assertNotIn(spoofed_host, result["webhook_url"])
		self.assertIn(
			"api/method/marketplace_integrations.api.noon_webhooks.event_notification",
			result["webhook_url"],
		)
