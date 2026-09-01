# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api import talabat_webhooks
from marketplace_integrations.integrations.talabat.order_events import process_order_event


def _make_connection(connection_name, **kwargs):
	doc = {
		"doctype": "Talabat Connection",
		"connection_name": connection_name,
		"client_id": "test-client-id",
		"client_secret": "test-client-secret",
		"chain_id": "test-chain-id",
		"environment": "Sandbox",
		"enabled": 1,
		"order_webhook_secret": "test-order-webhook-secret",
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


def _make_account(account_name, connection, **kwargs):
	doc = {
		"doctype": "Marketplace Account",
		"account_name": account_name,
		"platform": "Talabat",
		"external_account_id": f"test-vendor-{account_name}",
		"connection_doctype": "Talabat Connection",
		"connection": connection.name,
		"enabled": 1,
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


def _payload(account, order_id="webhook-order-001", **overrides):
	payload = {
		"order_id": order_id,
		"client": {"id": account.external_account_id},
		"customer": {"delivery_address": {}},
		"payment": {},
		"sys": {},
		"cancellation": {},
		"items": [],
	}
	payload.update(overrides)
	return payload


class IntegrationTestTalabatOrderWebhook(IntegrationTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.connection = _make_connection(f"Webhook Connection {suffix}")
		self.account = _make_account(f"Webhook Account {suffix}", self.connection)

	def _call_webhook(self, payload, authorization):
		request = MagicMock()
		request.get_json.return_value = payload
		with (
			patch.object(talabat_webhooks.frappe, "request", request),
			patch.object(talabat_webhooks.frappe, "get_request_header", return_value=authorization),
		):
			return talabat_webhooks.order_event()

	@patch.object(talabat_webhooks.frappe, "enqueue")
	def test_valid_webhook_enqueues_shared_processor(self, enqueue):
		result = self._call_webhook(_payload(self.account), "test-order-webhook-secret")

		self.assertEqual(result, {"accepted": True})
		enqueue.assert_called_once_with(
			process_order_event,
			queue="default",
			timeout=300,
			marketplace_account=self.account.name,
			payload=_payload(self.account),
		)

	@patch.object(talabat_webhooks.frappe, "enqueue")
	def test_invalid_authorization_is_rejected_before_enqueue(self, enqueue):
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(_payload(self.account), "wrong-secret")
		enqueue.assert_not_called()

	@patch.object(talabat_webhooks.frappe, "enqueue")
	def test_unknown_or_disabled_account_is_rejected_before_enqueue(self, enqueue):
		unknown = _payload(self.account, client={"id": "unknown-vendor"})
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(unknown, "test-order-webhook-secret")

		self.account.enabled = 0
		self.account.save()
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(_payload(self.account), "test-order-webhook-secret")
		enqueue.assert_not_called()

	@patch.object(talabat_webhooks.frappe, "enqueue")
	def test_missing_connection_secret_is_rejected_before_enqueue(self, enqueue):
		connection = _make_connection(
			f"Secretless Connection {frappe.generate_hash(length=8)}", order_webhook_secret=None
		)
		frappe.db.delete(
			"__Auth",
			{"doctype": "Talabat Connection", "name": connection.name, "fieldname": "order_webhook_secret"},
		)
		account = _make_account(f"Secretless Account {frappe.generate_hash(length=8)}", connection)

		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(_payload(account), "any-token")
		enqueue.assert_not_called()

	@patch.object(talabat_webhooks.frappe, "enqueue")
	def test_store_id_can_resolve_account_and_ambiguous_matches_are_rejected(self, enqueue):
		payload = _payload(self.account, client={"id": "internal-id", "store_id": self.account.external_account_id})
		self._call_webhook(payload, "test-order-webhook-secret")
		enqueue.assert_called_once()

		other_connection = _make_connection(f"Other Connection {frappe.generate_hash(length=8)}")
		_make_account(
			f"Other Account {frappe.generate_hash(length=8)}",
			other_connection,
			external_account_id="internal-id",
		)
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(payload, "test-order-webhook-secret")

	@patch("marketplace_integrations.integrations.talabat.order_events.upsert_marketplace_order")
	@patch("marketplace_integrations.integrations.talabat.order_events.normalize_order")
	@patch("marketplace_integrations.integrations.talabat.order_events.frappe.set_user")
	def test_worker_sets_system_user_before_shared_mapper_and_upsert(
		self, set_user, normalize_order, upsert_marketplace_order
	):
		payload = _payload(self.account)
		normalized = {"external_order_id": "webhook-order-001", "items": [], "latest_payload": payload}
		call_order = []
		set_user.side_effect = lambda user: call_order.append(("set_user", user))
		normalize_order.side_effect = lambda event_payload: call_order.append(("normalize", event_payload)) or normalized
		upsert_marketplace_order.side_effect = lambda account, order: call_order.append(("upsert", account, order))

		process_order_event(self.account.name, payload)

		self.assertEqual(
			call_order,
			[
				("set_user", "Administrator"),
				("normalize", payload),
				("upsert", self.account.name, normalized),
			],
		)

	def test_worker_persists_an_authenticated_webhook_started_as_guest(self):
		payload = _payload(self.account, order_id=f"guest-worker-{frappe.generate_hash(length=8)}")
		original_user = frappe.session.user

		with self.set_user("Guest"):
			process_order_event(self.account.name, payload)
			self.assertEqual(frappe.session.user, "Administrator")

		self.assertEqual(frappe.session.user, original_user)
		self.assertTrue(
			frappe.db.exists(
				"Marketplace Order",
				{"marketplace_account": self.account.name, "external_order_id": payload["order_id"]},
			)
		)

	def test_duplicate_redelivery_creates_one_marketplace_order(self):
		payload = _payload(self.account, order_id=f"duplicate-{frappe.generate_hash(length=8)}")

		process_order_event(self.account.name, payload)
		process_order_event(self.account.name, payload)

		self.assertEqual(
			frappe.db.count(
				"Marketplace Order",
				{"marketplace_account": self.account.name, "external_order_id": payload["order_id"]},
			),
			1,
		)
