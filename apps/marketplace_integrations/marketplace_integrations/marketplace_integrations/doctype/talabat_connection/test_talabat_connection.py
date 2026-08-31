# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase


def make_connection(connection_name, **kwargs):
	doc = {
		"doctype": "Talabat Connection",
		"connection_name": connection_name,
		"client_id": "test-client-id",
		"client_secret": "test-client-secret",
		"chain_id": "test-chain-id",
		"environment": "Sandbox",
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


class IntegrationTestTalabatConnection(IntegrationTestCase):
	def test_fields_persist_and_naming_is_expression_based(self):
		connection = make_connection("Test Connection 1")
		connection.reload()

		self.assertEqual(connection.connection_name, "Test Connection 1")
		self.assertEqual(connection.client_id, "test-client-id")
		self.assertEqual(connection.chain_id, "test-chain-id")
		self.assertEqual(connection.environment, "Sandbox")
		self.assertEqual(connection.enabled, 1)
		# Naming is expression-based (TALCONN-.#####), independent of
		# connection_name/client_id/chain_id - none of those should leak into
		# `name`, and a rename of connection_name after creation must not be
		# blocked or silently reverted the way a field-based autoname would.
		self.assertTrue(connection.name.startswith("TALCONN-"))
		self.assertNotIn("Test Connection 1", connection.name)

	def test_client_secret_round_trips_without_xss_corruption(self):
		# Same footgun documented in frappe-conventions.md and previously hit
		# for real on a Talabat client secret - Document.save()'s HTML/XSS
		# sanitizer silently strips substrings that merely look like an HTML
		# tag unless ignore_xss_filter is set.
		tricky_secret = "s3cr3t-<p<+X>-value"
		connection = make_connection("Test XSS Secret", client_secret=tricky_secret)
		connection.reload()

		self.assertEqual(connection.get_password("client_secret"), tricky_secret)

	def test_client_secret_not_returned_in_plain_document(self):
		connection = make_connection("Test Secret Hidden")
		connection.reload()

		# Password fields never round-trip their plaintext value through the
		# ordinary document representation - only get_password() (which reads
		# from the separate encrypted __Auth table) can retrieve it.
		self.assertNotEqual(connection.client_secret, "test-client-secret")
		as_dict = connection.as_dict()
		self.assertNotEqual(as_dict.get("client_secret"), "test-client-secret")

	def test_get_base_url_sandbox(self):
		connection = make_connection("Test Base URL Sandbox", environment="Sandbox")
		self.assertEqual(connection.get_base_url(), "https://sandbox.partner.deliveryhero.io")

	def test_get_base_url_production(self):
		connection = make_connection("Test Base URL Production", environment="Production")
		self.assertEqual(connection.get_base_url(), "https://talabat.partner.deliveryhero.io")

	def test_two_connections_are_independent_documents(self):
		connection_a = make_connection("Test Independent A", client_id="client-a")
		connection_b = make_connection("Test Independent B", client_id="client-b")

		self.assertNotEqual(connection_a.name, connection_b.name)
		self.assertEqual(connection_a.client_id, "client-a")
		self.assertEqual(connection_b.client_id, "client-b")

	def test_order_webhook_url_is_generated_on_insert_and_refreshes_on_demand(self):
		with patch(
			"marketplace_integrations.marketplace_integrations.doctype.talabat_connection.talabat_connection.get_url",
			return_value="https://old.example.com/api/method/marketplace_integrations.api.talabat_webhooks.order_event",
		) as get_url:
			connection = make_connection("Test Order Webhook URL")

		self.assertEqual(
			connection.order_webhook_url,
			"https://old.example.com/api/method/marketplace_integrations.api.talabat_webhooks.order_event",
		)
		get_url.assert_called_once_with(
			"/api/method/marketplace_integrations.api.talabat_webhooks.order_event",
			allow_header_override=False,
		)

		with patch(
			"marketplace_integrations.marketplace_integrations.doctype.talabat_connection.talabat_connection.get_url",
			return_value="https://new.example.com/api/method/marketplace_integrations.api.talabat_webhooks.order_event",
		):
			result = connection.refresh_order_webhook_url()

		connection.reload()
		self.assertEqual(
			connection.order_webhook_url,
			"https://new.example.com/api/method/marketplace_integrations.api.talabat_webhooks.order_event",
		)
		self.assertEqual(result["order_webhook_url"], connection.order_webhook_url)

	def test_order_webhook_secret_round_trips_without_xss_corruption(self):
		secret = "webhook-<static>-secret"
		connection = make_connection("Test Webhook Secret", order_webhook_secret=secret)
		connection.reload()

		self.assertEqual(connection.get_password("order_webhook_secret"), secret)
