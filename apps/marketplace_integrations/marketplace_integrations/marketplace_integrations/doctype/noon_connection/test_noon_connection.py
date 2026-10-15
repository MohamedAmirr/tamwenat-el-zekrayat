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
	def setUp(self):
		super().setUp()
		self._connections_before = set(frappe.get_all("Noon Connection", pluck="name"))

	def tearDown(self):
		"""Delete every connection the test created (with its accounts and sync logs) instead
		of relying on a rollback."""
		for name in set(frappe.get_all("Noon Connection", pluck="name")) - self._connections_before:
			for account in frappe.get_all(
				"Marketplace Account",
				filters={"connection_doctype": "Noon Connection", "connection": name},
				pluck="name",
			):
				frappe.db.delete("Marketplace Sync Log", {"marketplace_account": account})
				frappe.delete_doc("Marketplace Account", account, force=True, ignore_permissions=True)
			frappe.delete_doc("Noon Connection", name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

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

	def test_refresh_webhook_status_caches_an_active_destination(self):
		connection = make_connection("Webhook Status Active")
		destination = {
			"destination_code": "dest-active",
			"url": connection.webhook_url,
			"is_active": True,
			"event_types": ["FBPI::ORDER_SYNC"],
			"credentials": {"values": {"X-Api-Key": "******"}},
		}
		response = SimpleNamespace(status_code=200, json=lambda: {"destinations": [destination]})
		with patch("marketplace_integrations.integrations.noon.client.NoonClient") as client_class:
			client_class.return_value.list_destinations.return_value = response
			result = connection.refresh_webhook_status()

		self.assertEqual(result["status"], "Active")
		connection.reload()
		self.assertEqual(connection.noon_destination_code, "dest-active")
		self.assertEqual(connection.webhook_status, "Active")

	def test_refresh_webhook_status_detects_stored_destination_url_mismatch(self):
		connection = make_connection("Webhook Status URL Mismatch", noon_destination_code="dest-stored")
		response = SimpleNamespace(
			status_code=200,
			json=lambda: {
				"destinations": [
					{
						"destination_code": "dest-stored",
						"url": "https://old.example.test/noon",
						"is_active": True,
						"event_types": ["FBPI::ORDER_SYNC"],
					}
				]
			},
		)
		with patch("marketplace_integrations.integrations.noon.client.NoonClient") as client_class:
			client_class.return_value.list_destinations.return_value = response
			result = connection.refresh_webhook_status()

		self.assertEqual(result["status"], "URL mismatch")

	def test_refresh_webhook_status_returns_safe_noon_failure(self):
		connection = make_connection("Webhook Status Failure")
		response = SimpleNamespace(status_code=503)
		with patch("marketplace_integrations.integrations.noon.client.NoonClient") as client_class:
			client_class.return_value.list_destinations.return_value = response
			client_class.return_value.error_message.return_value = "Noon request failed (HTTP 503)."
			result = connection.refresh_webhook_status()

		self.assertEqual(result["status"], "Could not reach Noon")
		self.assertEqual(result["error"], "Noon request failed (HTTP 503).")

	def _client_patch(self):
		return patch("marketplace_integrations.integrations.noon.client.NoonClient")

	def _list_response(self, destinations):
		return SimpleNamespace(status_code=200, json=lambda: {"destinations": destinations})

	def test_register_webhook_creates_then_subscribes_without_exposing_secret(self):
		connection = make_connection("Webhook Register")
		secret = connection.get_password("webhook_secret")
		active = {
			"destination_code": "dest-new",
			"url": connection.webhook_url,
			"is_active": True,
			"event_types": ["FBPI::ORDER_SYNC"],
		}
		with self._client_patch() as client_class:
			client = client_class.return_value
			client.list_destinations.side_effect = [self._list_response([]), self._list_response([active])]
			client.create_https_destination.return_value = SimpleNamespace(
				status_code=200, json=lambda: {"destination_code": "dest-new"}
			)
			client.update_destination.return_value = SimpleNamespace(status_code=200)
			result = connection.register_webhook()

		self.assertEqual(result["status"], "Active")
		_name, url, headers = client.create_https_destination.call_args.args
		self.assertEqual(url, connection.webhook_url)
		self.assertEqual(headers, {"X-Api-Key": secret})
		client.update_destination.assert_called_once_with(
			"dest-new", event_types=[{"event_type": "FBPI::ORDER_SYNC", "is_active": True}]
		)
		connection.reload()
		self.assertEqual(connection.noon_destination_code, "dest-new")

	def test_register_webhook_failure_after_create_leaves_subscribe_possible(self):
		connection = make_connection("Webhook Register Partial")
		with self._client_patch() as client_class:
			client = client_class.return_value
			client.list_destinations.return_value = self._list_response([])
			client.create_https_destination.return_value = SimpleNamespace(
				status_code=200, json=lambda: {"destination_code": "dest-partial"}
			)
			client.update_destination.return_value = SimpleNamespace(status_code=500)
			client.error_message.return_value = "Noon request failed (HTTP 500)."
			with self.assertRaises(frappe.ValidationError) as raised:
				connection.register_webhook()

		self.assertIn("created", str(raised.exception))
		connection.reload()
		self.assertEqual(connection.noon_destination_code, "dest-partial")

	def test_register_webhook_refuses_when_already_registered_or_full(self):
		connection = make_connection("Webhook Register Refused")
		ours = {
			"destination_code": "dest-ours",
			"url": connection.webhook_url,
			"is_active": True,
			"event_types": ["FBPI::ORDER_SYNC"],
		}
		with self._client_patch() as client_class:
			client = client_class.return_value
			client.list_destinations.return_value = self._list_response([ours])
			with self.assertRaises(frappe.ValidationError):
				connection.register_webhook()

		others = [{"destination_code": f"other-{i}", "url": f"https://x.test/{i}"} for i in range(5)]
		with self._client_patch() as client_class:
			client = client_class.return_value
			client.list_destinations.return_value = self._list_response(others)
			with self.assertRaises(frappe.ValidationError):
				connection.register_webhook()
			client.create_https_destination.assert_not_called()

	def test_update_webhook_destination_sends_credentials_only_when_asked(self):
		connection = make_connection("Webhook Update", noon_destination_code="dest-upd")
		secret = connection.get_password("webhook_secret")
		with self._client_patch() as client_class:
			client = client_class.return_value
			client.list_destinations.return_value = self._list_response([])
			client.update_destination.return_value = SimpleNamespace(status_code=200)
			connection.update_webhook_destination()
			client.update_destination.assert_called_with("dest-upd", url=connection.webhook_url)
			connection.update_webhook_destination(send_key=1)
			client.update_destination.assert_called_with(
				"dest-upd",
				url=connection.webhook_url,
				credentials={"values": {"X-Api-Key": secret}},
			)

	def test_set_webhook_active_and_subscribe_need_a_known_destination(self):
		connection = make_connection("Webhook Active")
		with self.assertRaises(frappe.ValidationError):
			connection.set_webhook_active(0)
		with self.assertRaises(frappe.ValidationError):
			connection.subscribe_webhook()

		connection.db_set("noon_destination_code", "dest-act")
		with self._client_patch() as client_class:
			client = client_class.return_value
			client.list_destinations.return_value = self._list_response([])
			client.update_destination.return_value = SimpleNamespace(status_code=200)
			connection.set_webhook_active(0)
			client.update_destination.assert_called_once_with("dest-act", is_active=False)

	def test_webhook_actions_require_write_permission(self):
		connection = make_connection("Webhook Perm")
		with patch.object(type(connection), "check_permission", side_effect=frappe.PermissionError):
			for call in (
				connection.register_webhook,
				connection.subscribe_webhook,
				connection.update_webhook_destination,
				lambda: connection.set_webhook_active(1),
			):
				with self.assertRaises(frappe.PermissionError):
					call()

	def test_refresh_stores_every_destination_and_replaces_rows_on_next_refresh(self):
		connection = make_connection("Webhook Rows")
		ours = {
			"destination_code": "dest-ours",
			"destination_name": "ours",
			"url": connection.webhook_url,
			"is_active": True,
			"event_types": ["FBPI::ORDER_SYNC"],
		}
		other = {
			"destination_code": "dest-other",
			"destination_name": "probe",
			"url": "https://other.example.test/hook",
			"is_active": False,
			"event_types": [],
		}
		with self._client_patch() as client_class:
			client = client_class.return_value
			client.list_destinations.return_value = self._list_response([ours, other])
			connection.refresh_webhook_status()
			connection.reload()
			rows = {row.destination_code: row for row in connection.noon_destinations}
			self.assertEqual(set(rows), {"dest-ours", "dest-other"})
			self.assertEqual(rows["dest-ours"].is_ours, 1)
			self.assertEqual(rows["dest-ours"].event_types, "FBPI::ORDER_SYNC")
			self.assertEqual((rows["dest-other"].is_ours, rows["dest-other"].is_active), (0, 0))

			client.list_destinations.return_value = self._list_response([ours])
			connection.refresh_webhook_status()
			connection.reload()
			self.assertEqual([row.destination_code for row in connection.noon_destinations], ["dest-ours"])

	def test_set_destination_active_works_for_any_listed_destination_only(self):
		connection = make_connection("Webhook Row Toggle")
		other = {
			"destination_code": "dest-other",
			"destination_name": "probe",
			"url": "https://other.example.test/hook",
			"is_active": True,
			"event_types": [],
		}
		with self._client_patch() as client_class:
			client = client_class.return_value
			client.list_destinations.return_value = self._list_response([other])
			client.update_destination.return_value = SimpleNamespace(status_code=200)
			connection.refresh_webhook_status()
			connection.reload()
			with self.assertRaises(frappe.ValidationError):
				connection.set_destination_active("dest-unlisted", 0)
			client.update_destination.assert_not_called()
			connection.set_destination_active("dest-other", 0)
			client.update_destination.assert_called_once_with("dest-other", is_active=False)

	def _refresh_with(self, connection, destinations):
		with self._client_patch() as client_class:
			client_class.return_value.list_destinations.return_value = self._list_response(destinations)
			return connection.refresh_webhook_status()

	def test_refresh_webhook_status_reports_each_remaining_state(self):
		connection = make_connection("Webhook Status States")
		ours = {
			"destination_code": "dest-ours",
			"url": connection.webhook_url,
			"is_active": True,
			"event_types": ["FBPI::ORDER_SYNC"],
		}
		self.assertEqual(self._refresh_with(connection, [])["status"], "Not registered")
		self.assertEqual(self._refresh_with(connection, [{**ours, "is_active": False}])["status"], "Inactive")
		self.assertEqual(
			self._refresh_with(connection, [{**ours, "event_types": []}])["status"],
			"Registered, not subscribed",
		)

	def test_refresh_webhook_status_counts_other_destinations(self):
		connection = make_connection("Webhook Status Others")
		ours = {
			"destination_code": "dest-ours",
			"url": connection.webhook_url,
			"is_active": True,
			"event_types": ["FBPI::ORDER_SYNC"],
		}
		others = [{"destination_code": f"other-{i}", "url": f"https://x.test/{i}"} for i in range(4)]
		result = self._refresh_with(connection, [ours, *others])
		self.assertEqual((result["status"], result["other_destinations"]), ("Active", 4))
		self.assertEqual(self._refresh_with(connection, others)["other_destinations"], 4)
		connection.reload()
		self.assertEqual(connection.other_noon_destinations, 4)

	def test_refresh_webhook_status_reports_last_webhook_event_received(self):
		with patch("marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"):
			connection = make_connection("Webhook Status Last Event")
			account = frappe.get_doc(
				{
					"doctype": "Marketplace Account",
					"account_name": "Webhook Status Last Event Account",
					"platform": "Noon",
					"external_account_id": connection.project_code,
					"connection_doctype": "Noon Connection",
					"connection": connection.name,
					"noon_warehouse_code": "NOON-WH-001",
					"noon_country_code": "ae",
					"enabled": 1,
				}
			).insert()
		self.addCleanup(account.delete)
		self.assertIsNone(self._refresh_with(connection, [])["last_event_received_on"])

		log = frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"marketplace_account": account.name,
				"sync_type": "Order Import",
				"status": "Success",
				"trigger_source": "Noon Webhook",
				"attempted_at": frappe.utils.now_datetime(),
				"completed_at": "2026-10-03 10:00:00",
			}
		).insert()
		self.addCleanup(log.delete)
		self.assertEqual(
			str(self._refresh_with(connection, [])["last_event_received_on"]), "2026-10-03 10:00:00"
		)

	def test_status_prefers_the_order_sync_destination_when_a_returns_destination_shares_the_url(self):
		connection = make_connection("Webhook Status Shared Url")
		orders = {
			"destination_code": "dest-orders",
			"url": connection.webhook_url,
			"is_active": True,
			"event_types": ["FBPI::ORDER_SYNC"],
		}
		returns_only = {
			"destination_code": "dest-returns",
			"url": connection.webhook_url,
			"is_active": True,
			"event_types": ["RETURNS::REFERENCE_UPDATE"],
		}
		for destinations in ([returns_only, orders], [orders, returns_only]):
			result = self._refresh_with(connection, destinations)
			self.assertEqual(result["status"], "Active")
			connection.reload()
			self.assertEqual(connection.noon_destination_code, "dest-orders")
			connection.db_set("noon_destination_code", "")
			rows = {row.destination_code: row.is_ours for row in connection.noon_destinations}
			self.assertEqual(rows, {"dest-orders": 1, "dest-returns": 1})

	def test_returns_only_destination_on_our_url_is_not_adopted_as_ours(self):
		connection = make_connection("Webhook Status Returns Only")
		returns_only = {
			"destination_code": "dest-returns",
			"url": connection.webhook_url,
			"is_active": True,
			"event_types": ["RETURNS::REFERENCE_UPDATE"],
		}
		result = self._refresh_with(connection, [returns_only])
		self.assertEqual(result["status"], "Not registered")
		connection.reload()
		self.assertFalse(connection.noon_destination_code)
		rows = {row.destination_code: row.is_ours for row in connection.noon_destinations}
		self.assertEqual(rows, {"dest-returns": 1})

	def test_refresh_webhook_status_shows_a_noon_login_failure(self):
		connection = make_connection("Webhook Status Login Failure")
		with self._client_patch() as client_class:
			client_class.return_value.list_destinations.side_effect = frappe.ValidationError(
				"Noon authentication failed (HTTP 401)."
			)
			result = connection.refresh_webhook_status()
		self.assertEqual(result["status"], "Could not reach Noon")
		self.assertEqual(result["error"], "Noon authentication failed (HTTP 401).")

	def test_last_event_ignores_worker_written_webhook_rows(self):
		with patch("marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"):
			connection = make_connection("Webhook Last Event Receiver Only")
			account = frappe.get_doc(
				{
					"doctype": "Marketplace Account",
					"account_name": "Webhook Last Event Receiver Account",
					"platform": "Noon",
					"external_account_id": connection.project_code,
					"connection_doctype": "Noon Connection",
					"connection": connection.name,
					"noon_warehouse_code": "NOON-WH-001",
					"noon_country_code": "ae",
					"enabled": 1,
				}
			).insert()

		def log(sync_type, status, completed_at, request_data=None):
			frappe.get_doc(
				{
					"doctype": "Marketplace Sync Log",
					"marketplace_account": account.name,
					"sync_type": sync_type,
					"status": status,
					"trigger_source": "Noon Webhook",
					"attempted_at": frappe.utils.now_datetime(),
					"completed_at": completed_at,
					"request_data": request_data,
				}
			).insert()

		# Worker rows: a webhook-triggered lookup and an import failure.
		log("Return Lookup", "Success", "2026-10-05 10:00:00", '{"barcode": "RMS-1"}')
		log("Order Import", "Error", "2026-10-06 10:00:00")
		self.assertIsNone(self._refresh_with(connection, [])["last_event_received_on"])

		# Receiver rows: an accepted order event and an accepted return event.
		log("Order Import", "Success", "2026-10-03 10:00:00")
		log("Return Lookup", "Success", "2026-10-04 10:00:00", '{"message_id": "m-1", "payload": {}}')
		self.assertEqual(
			str(self._refresh_with(connection, [])["last_event_received_on"]), "2026-10-04 10:00:00"
		)
