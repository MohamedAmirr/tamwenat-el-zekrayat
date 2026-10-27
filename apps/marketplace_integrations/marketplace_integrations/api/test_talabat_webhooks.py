# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api import talabat_webhooks
from marketplace_integrations.integrations.talabat.catalog_feedback import process_catalog_feedback
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
		"catalog_webhook_secret": "test-catalog-webhook-secret",
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
		payload = _payload(
			self.account, client={"id": "internal-id", "store_id": self.account.external_account_id}
		)
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

	@patch("marketplace_integrations.integrations.talabat.order_events.bump_sync_debounce")
	@patch("marketplace_integrations.integrations.talabat.order_events.upsert_talabat_order")
	@patch("marketplace_integrations.integrations.talabat.order_events.normalize_order")
	@patch("marketplace_integrations.integrations.talabat.order_events.frappe.set_user")
	def test_worker_sets_system_user_before_shared_mapper_and_upsert(
		self, set_user, normalize_order, upsert_talabat_order, bump_sync_debounce
	):
		payload = _payload(self.account)
		normalized = {"external_order_id": "webhook-order-001", "items": [], "latest_payload": payload}
		upserted_order = MagicMock()
		upserted_order.name = "webhook-order-001"
		call_order = []
		set_user.side_effect = lambda user: call_order.append(("set_user", user))
		normalize_order.side_effect = (
			lambda event_payload: call_order.append(("normalize", event_payload)) or normalized
		)
		upsert_talabat_order.side_effect = (
			lambda account, order, trigger_source, **_kwargs: call_order.append(
				("upsert", account, order, trigger_source)
			)
			or (upserted_order, False)
		)
		bump_sync_debounce.side_effect = lambda marketplace_order, trigger_source: call_order.append(
			("bump_sync_debounce", marketplace_order, trigger_source)
		)

		process_order_event(self.account.name, payload)

		self.assertEqual(
			call_order,
			[
				("set_user", "Administrator"),
				("normalize", payload),
				("upsert", self.account.name, normalized, "Talabat Webhook"),
				("bump_sync_debounce", upserted_order.name, "Talabat Webhook"),
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

	def test_webhook_bumps_only_a_new_order_or_one_whose_evidence_changed(self):
		payload = _payload(self.account, order_id=f"echo-{frappe.generate_hash(length=8)}")
		filters = {"marketplace_account": self.account.name, "external_order_id": payload["order_id"]}

		process_order_event(self.account.name, payload)
		name = frappe.db.get_value("Marketplace Order", filters, "name")
		self.assertEqual(frappe.db.get_value("Marketplace Order", name, "erpnext_sync_status"), "Syncing")
		self.assertTrue(frappe.db.get_value("Marketplace Order", name, "sync_debounce_until"))

		# The echo of our own action: the same state arrives again, nothing is queued.
		frappe.db.set_value(
			"Marketplace Order",
			name,
			{"sync_debounce_until": None, "pending_sync_trigger_source": None, "erpnext_sync_status": "Synced"},
		)
		process_order_event(self.account.name, payload)
		values = frappe.db.get_value(
			"Marketplace Order", name, ["erpnext_sync_status", "sync_debounce_until"], as_dict=True
		)
		self.assertEqual(values.erpnext_sync_status, "Synced")
		self.assertFalse(values.sync_debounce_until)

		# A real change is queued.
		process_order_event(self.account.name, {**payload, "status": "CANCELLED"})
		values = frappe.db.get_value(
			"Marketplace Order", name, ["erpnext_sync_status", "sync_debounce_until"], as_dict=True
		)
		self.assertEqual(values.erpnext_sync_status, "Syncing")
		self.assertTrue(values.sync_debounce_until)

	def test_webhook_echo_of_our_own_update_skips_the_bump_and_the_replan(self):
		from marketplace_integrations.integrations.talabat.order_mapper import normalize_order
		from marketplace_integrations.services import echo_guard

		payload = _payload(self.account, order_id=f"echo-guard-{frappe.generate_hash(length=8)}")
		process_order_event(self.account.name, payload)
		name = frappe.db.get_value(
			"Marketplace Order",
			{"marketplace_account": self.account.name, "external_order_id": payload["order_id"]},
			"name",
		)
		echoed = {**payload, "status": "CANCELLED", "cancellation": {"reason": "CLOSED"}}
		frappe.db.set_value(
			"Marketplace Order",
			name,
			{
				"erpnext_sync_status": "Synced",
				"sync_debounce_until": None,
				"erpnext_sales_order": "SO-ECHO-FIXTURE",  # set_value skips link validation
			},
		)
		echo_guard.record_expected_change(name, echo_guard.talabat_incoming_hash(normalize_order(echoed)), None)

		with (
			# Saving a linked order would validate the stand-in Sales Order link.
			patch("frappe.model.document.Document._validate_links"),
			patch("marketplace_integrations.services.echo_guard.is_job_enqueued", return_value=False),
			patch("marketplace_integrations.services.echo_guard.frappe.enqueue") as enqueue,
			patch("marketplace_integrations.integrations.talabat.order_events.bump_sync_debounce") as bump,
		):
			process_order_event(self.account.name, echoed)

		bump.assert_not_called()
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["trigger_source"], "Talabat Webhook")
		values = frappe.db.get_value(
			"Marketplace Order",
			name,
			["erpnext_sync_status", "sync_debounce_until", "expected_change_hash"],
			as_dict=True,
		)
		self.assertEqual(values.erpnext_sync_status, "Synced")
		self.assertFalse(values.sync_debounce_until)
		self.assertFalse(values.expected_change_hash)

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

	def test_webhook_payload_converges_through_shared_mapper_for_promotion_evidence(self):
		"""A webhook-delivered order.Order's promotion_status and item
		container_deposit/promotion fields land correctly end-to-end through
		the webhook path (mapper identity with manual retrieval's path is
		proven separately by test_worker_sets_system_user_before_shared_mapper_and_upsert)."""
		order_id = f"promo-webhook-{frappe.generate_hash(length=8)}"
		payload = _payload(
			self.account,
			order_id=order_id,
			promotion_status="UNAVAILABLE",
			items=[
				{
					"_id": "webhook-item-001",
					"sku": "SKU-001",
					"name": "Product",
					"container_deposit": 0.25,
					"promotion": [],
				}
			],
		)

		process_order_event(self.account.name, payload)

		order = frappe.get_doc(
			"Marketplace Order",
			{"marketplace_account": self.account.name, "external_order_id": order_id},
		)
		self.assertEqual(order.promotion_status, "UNAVAILABLE")
		item = order.items[0]
		self.assertEqual(item.container_deposit, 0.25)
		self.assertEqual(frappe.parse_json(item.promotions), [])

	def test_successful_upsert_bumps_the_sales_order_sync_debounce(self):
		"""#134: a successful upsert schedules the eventual Sales Order sync via
		the shared per-order debounce (#132) rather than enqueueing directly."""
		order_id = f"debounce-{frappe.generate_hash(length=8)}"
		payload = _payload(self.account, order_id=order_id)

		process_order_event(self.account.name, payload)

		order = frappe.get_doc(
			"Marketplace Order",
			{"marketplace_account": self.account.name, "external_order_id": order_id},
		)
		self.assertIsNotNone(order.sync_debounce_until)
		self.assertEqual(order.pending_sync_trigger_source, "Talabat Webhook")

	def test_successful_upsert_calls_bump_sync_debounce_with_talabat_webhook_trigger_source(self):
		# Direct call-site assertion, complementing the state-based assertion
		# above - confirms process_order_event() wires bump_sync_debounce() to
		# trigger_source="Talabat Webhook" specifically (#135).
		payload = _payload(self.account)

		import marketplace_integrations.integrations.talabat.order_events as order_events_module

		with patch.object(order_events_module, "bump_sync_debounce") as mocked_bump:
			order = process_order_event(self.account.name, payload)

		mocked_bump.assert_called_once_with(order.name, trigger_source="Talabat Webhook")

	@patch("marketplace_integrations.integrations.talabat.order_events.bump_sync_debounce")
	@patch("frappe.db.commit")  # the failure-log write commits; keep this test's fixtures uncommitted
	@patch("marketplace_integrations.integrations.talabat.order_events.upsert_talabat_order")
	def test_bump_sync_debounce_is_not_called_when_upsert_fails(
		self, upsert_talabat_order, _commit, bump_sync_debounce
	):
		upsert_talabat_order.side_effect = frappe.ValidationError
		payload = _payload(self.account)

		with self.assertRaises(frappe.ValidationError):
			process_order_event(self.account.name, payload)

		bump_sync_debounce.assert_not_called()

	@patch("marketplace_integrations.integrations.talabat.order_events.bump_sync_debounce")
	@patch("frappe.db.commit")  # the failure-log write commits; keep this test's fixtures uncommitted
	@patch("marketplace_integrations.integrations.talabat.order_events.normalize_order")
	def test_bump_sync_debounce_is_not_called_on_malformed_payload(
		self, normalize_order, _commit, bump_sync_debounce
	):
		normalize_order.side_effect = frappe.ValidationError
		payload = _payload(self.account)

		with self.assertRaises(frappe.ValidationError):
			process_order_event(self.account.name, payload)

		bump_sync_debounce.assert_not_called()

	@patch("frappe.db.commit")
	@patch("marketplace_integrations.integrations.talabat.order_events.normalize_order")
	def test_failed_worker_event_is_persisted_as_an_order_import_error_log(self, normalize_order, commit):
		normalize_order.side_effect = frappe.ValidationError("bad order")
		order_id = f"failing-{frappe.generate_hash(length=8)}"
		payload = _payload(self.account, order_id=order_id)

		with self.assertRaises(frappe.ValidationError):
			process_order_event(self.account.name, payload)

		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={
				"marketplace_account": self.account.name,
				"sync_type": "Order Import",
				"status": "Error",
			},
			fields=["name", "trigger_source", "request_data"],
		)
		try:
			self.assertEqual(len(logs), 1)
			self.assertEqual(logs[0].trigger_source, "Talabat Webhook")
			self.assertEqual(frappe.parse_json(logs[0].request_data)["order_id"], order_id)
			commit.assert_called()
		finally:
			for log in logs:
				frappe.delete_doc("Marketplace Sync Log", log.name, force=True)

	@patch("frappe.db.commit")
	@patch("frappe.db.rollback")
	@patch("marketplace_integrations.integrations.talabat.order_events.normalize_order")
	def test_deadlock_rollback_failure_falls_back_to_full_rollback_and_reraises_original(
		self, normalize_order, rollback, commit
	):
		normalize_order.side_effect = frappe.ValidationError("original failure")

		# The savepoint rollback raises (transaction already gone); the plain
		# full rollback succeeds.
		def fake_rollback(*args, **kwargs):
			if kwargs.get("save_point"):
				raise RuntimeError("savepoint does not exist")

		rollback.side_effect = fake_rollback
		payload = _payload(self.account, order_id=f"deadlock-{frappe.generate_hash(length=8)}")

		with patch(
			"marketplace_integrations.integrations.talabat.order_events.log_order_import_failure"
		) as log_failure:
			with self.assertRaises(frappe.ValidationError) as raised:
				process_order_event(self.account.name, payload)

		self.assertIn("original failure", str(raised.exception))
		self.assertTrue(any(not call.kwargs.get("save_point") for call in rollback.call_args_list))
		log_failure.assert_called_once()


def _catalog_payload(account, job_id="job-001", **overrides):
	payload = {
		"job_id": job_id,
		"status": "COMPLETED",
		"platform_vendor_id": account.external_account_id,
		"download_url": "https://example.com/feedback/job.csv",
	}
	payload.update(overrides)
	return payload


class IntegrationTestTalabatCatalogFeedbackWebhook(IntegrationTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.connection = _make_connection(f"Catalog Webhook Connection {suffix}")
		self.account = _make_account(f"Catalog Webhook Account {suffix}", self.connection)

	def _call_webhook(self, payload, authorization):
		request = MagicMock()
		request.get_json.return_value = payload
		with (
			patch.object(talabat_webhooks.frappe, "request", request),
			patch.object(talabat_webhooks.frappe, "get_request_header", return_value=authorization),
		):
			return talabat_webhooks.catalog_update_feedback()

	@patch.object(talabat_webhooks.frappe, "enqueue")
	def test_valid_webhook_enqueues_catalog_feedback_processor(self, enqueue):
		payload = _catalog_payload(self.account)
		result = self._call_webhook(payload, "test-catalog-webhook-secret")

		self.assertEqual(result, {"accepted": True})
		enqueue.assert_called_once_with(
			process_catalog_feedback,
			queue="default",
			timeout=600,
			payload=payload,
		)

	@patch.object(talabat_webhooks.frappe, "enqueue")
	def test_missing_authorization_is_rejected_before_enqueue(self, enqueue):
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(_catalog_payload(self.account), "")
		enqueue.assert_not_called()

	@patch.object(talabat_webhooks.frappe, "enqueue")
	def test_invalid_authorization_is_rejected_before_enqueue(self, enqueue):
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(_catalog_payload(self.account), "wrong-secret")
		enqueue.assert_not_called()

	@patch.object(talabat_webhooks.frappe, "enqueue")
	def test_order_webhook_secret_cannot_authenticate_catalog_callback(self, enqueue):
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(_catalog_payload(self.account), "test-order-webhook-secret")
		enqueue.assert_not_called()

	@patch.object(talabat_webhooks.frappe, "enqueue")
	def test_missing_required_field_is_rejected_before_enqueue(self, enqueue):
		payload = _catalog_payload(self.account)
		del payload["download_url"]
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(payload, "test-catalog-webhook-secret")
		enqueue.assert_not_called()

	@patch.object(talabat_webhooks.frappe, "enqueue")
	def test_unknown_or_disabled_account_is_rejected_before_enqueue(self, enqueue):
		unknown = _catalog_payload(self.account, platform_vendor_id="unknown-vendor")
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(unknown, "test-catalog-webhook-secret")

		self.account.enabled = 0
		self.account.save()
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(_catalog_payload(self.account), "test-catalog-webhook-secret")
		enqueue.assert_not_called()
