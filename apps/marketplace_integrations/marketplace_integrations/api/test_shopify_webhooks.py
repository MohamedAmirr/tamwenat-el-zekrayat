# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import base64
import hashlib
import hmac
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api import shopify_webhooks
from marketplace_integrations.integrations.shopify.webhooks import process_shopify_webhook


def make_connection(connection_name, **kwargs):
	doc = {
		"doctype": "Shopify Connection",
		"connection_name": connection_name,
		"shop_domain": "test-shop.myshopify.com",
		"client_id": "test-client-id",
		"client_secret": "test-client-secret",
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


def _sign(body, secret):
	digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
	return base64.b64encode(digest).decode("utf-8")


class IntegrationTestShopifyWebhookEvent(IntegrationTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.connection = make_connection(
			f"Webhook Transport {suffix}",
			shop_domain=f"test-shop-{suffix}.myshopify.com"
		)
		self.secret = "test-client-secret"
		self.body = b'{"id": 12345}'

	def _call_webhook(self, body, headers):
		# Body is real bytes and frappe.parse_json() is NOT mocked here -
		# deliberately, so this test exercises the real bytes-decode fix
		# (frappe.parse_json() only accepts str, not bytes) rather than a
		# faithful-looking stand-in that could mask the same class of bug
		# again.
		request = MagicMock()
		request.get_data.return_value = body
		header_map = {
			"X-Shopify-Shop-Domain": self.connection.shop_domain,
			# Real delivery header form (lowercase, slash) - NOT the
			# "ORDERS_CREATE" GraphQL enum form used for
			# webhookSubscriptionCreate. See constants.py's
			# SHOPIFY_WEBHOOK_TOPIC_HEADER_TO_ENUM.
			"X-Shopify-Topic": "orders/create",
			"X-Shopify-Webhook-Id": "webhook-id-1",
			"X-Shopify-Hmac-Sha256": _sign(self.body, self.secret),
		}
		header_map.update(headers)

		with (
			patch.object(shopify_webhooks.frappe, "request", request),
			patch.object(
				shopify_webhooks.frappe,
				"get_request_header",
				side_effect=lambda name, default=None: header_map.get(name, default),
			),
		):
			return shopify_webhooks.webhook_event()

	@patch.object(shopify_webhooks.frappe, "enqueue")
	def test_valid_signed_webhook_enqueues_processing(self, enqueue):
		result = self._call_webhook(self.body, {})

		self.assertEqual(result, {"accepted": True})
		enqueue.assert_called_once_with(
			process_shopify_webhook,
			queue="default",
			timeout=300,
			connection_name=self.connection.name,
			topic="ORDERS_CREATE",
			webhook_id="webhook-id-1",
			shop_domain=self.connection.shop_domain,
		)

	@patch.object(shopify_webhooks.frappe, "enqueue")
	def test_real_slash_form_topic_header_is_mapped_to_graphql_enum(self, enqueue):
		# X-Shopify-Topic on a real delivery is "orders/create", not
		# "ORDERS_CREATE" - the enqueued job must still receive the enum
		# form ("ORDERS_CREATE"), since that's what process_shopify_webhook /
		# _handle_topic compare against.
		result = self._call_webhook(self.body, {"X-Shopify-Topic": "Orders/Create"})
		self.assertEqual(result, {"accepted": True})
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["topic"], "ORDERS_CREATE")

	@patch.object(shopify_webhooks.frappe, "enqueue")
	def test_graphql_enum_form_header_is_not_recognized(self, enqueue):
		# The GraphQL enum form ("ORDERS_CREATE") is not a valid
		# X-Shopify-Topic delivery header value - only the header/slash form
		# is in SHOPIFY_WEBHOOK_TOPIC_HEADER_TO_ENUM, so sending the enum
		# form itself as the header must be treated as unrecognized, not
		# accidentally matched.
		result = self._call_webhook(self.body, {"X-Shopify-Topic": "ORDERS_CREATE"})
		self.assertEqual(result, {"accepted": True})
		enqueue.assert_not_called()

	@patch.object(shopify_webhooks.frappe, "enqueue")
	def test_invalid_hmac_is_rejected_before_enqueue(self, enqueue):
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(self.body, {"X-Shopify-Hmac-Sha256": "totally-wrong"})
		enqueue.assert_not_called()

	@patch.object(shopify_webhooks.frappe, "enqueue")
	def test_hmac_verified_against_raw_body_not_reparsed_body(self, enqueue):
		# The signature was computed over self.body; a tampered body with a
		# matching Content-Length-independent shape must still fail even
		# though it's still valid JSON, proving verification runs against the
		# raw bytes rather than some re-serialized form.
		tampered_body = b'{"id": 99999}'
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(tampered_body, {})
		enqueue.assert_not_called()

	@patch.object(shopify_webhooks.frappe, "enqueue")
	def test_unknown_shop_domain_is_rejected_before_enqueue(self, enqueue):
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(self.body, {"X-Shopify-Shop-Domain": f"unknown-{frappe.generate_hash(length=8)}.myshopify.com"})
		enqueue.assert_not_called()

	@patch.object(shopify_webhooks.frappe, "enqueue")
	def test_missing_webhook_id_is_rejected_before_enqueue(self, enqueue):
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(self.body, {"X-Shopify-Webhook-Id": ""})
		enqueue.assert_not_called()

	@patch.object(shopify_webhooks.frappe, "enqueue")
	def test_unregistered_topic_is_accepted_but_not_enqueued(self, enqueue):
		result = self._call_webhook(self.body, {"X-Shopify-Topic": "products/update"})
		self.assertEqual(result, {"accepted": True})
		enqueue.assert_not_called()

	@patch.object(shopify_webhooks.frappe, "enqueue")
	def test_previous_secret_authenticates_during_rotation_when_current_fails(self, enqueue):
		self.connection.previous_client_secret = "old-client-secret"
		self.connection.save()

		signed_with_old_secret = _sign(self.body, "old-client-secret")
		result = self._call_webhook(
			self.body, {"X-Shopify-Hmac-Sha256": signed_with_old_secret}
		)

		self.assertEqual(result, {"accepted": True})
		enqueue.assert_called_once()

	@patch.object(shopify_webhooks.frappe, "enqueue")
	def test_invalid_against_both_current_and_previous_secret_is_rejected(self, enqueue):
		self.connection.previous_client_secret = "old-client-secret"
		self.connection.save()

		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(self.body, {"X-Shopify-Hmac-Sha256": "totally-wrong"})
		enqueue.assert_not_called()

	@patch.object(shopify_webhooks.frappe, "enqueue")
	def test_no_previous_secret_set_only_current_secret_authenticates(self, enqueue):
		# self.connection has no previous_client_secret set (default setUp).
		result = self._call_webhook(self.body, {})
		self.assertEqual(result, {"accepted": True})
		enqueue.assert_called_once()

	@patch.object(shopify_webhooks.frappe, "enqueue")
	def test_disabled_connection_still_authenticates_for_app_uninstalled(self, enqueue):
		self.connection.enabled = 0
		self.connection.save()

		result = self._call_webhook(self.body, {"X-Shopify-Topic": "app/uninstalled"})
		self.assertEqual(result, {"accepted": True})
		enqueue.assert_called_once()
