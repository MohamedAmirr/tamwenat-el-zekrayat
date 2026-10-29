# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api import noon_webhooks
from marketplace_integrations.integrations.noon import orders, returns
from marketplace_integrations.integrations.noon.orders import (
	_ORDER_IMPORT_RQ_JOB_PREFIX,
	_ORDER_REFRESH_RQ_JOB_PREFIX,
	run_order_import,
	run_order_refresh,
)
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order

_PRIVATE_KEY = (
	"-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds1xfn8fH9gFKvKHxsXNpKZDqz5Fng0Z5HqL8H7"
	"\n-----END RSA PRIVATE KEY-----"
)

_SENTINEL_PAYLOAD_VALUE = "SENTINEL-NEVER-STORED-OR-LOGGED"


def _make_connection(connection_name, **kwargs):
	doc = {
		"doctype": "Noon Connection",
		"connection_name": connection_name,
		"project_code": f"test-project-{frappe.generate_hash(length=8)}",
		"key_id": "test-key-id",
		"private_key": _PRIVATE_KEY,
		"environment": "Static Sandbox",
		"webhook_secret": "test-webhook-secret",
		"enabled": 1,
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


def _make_account(account_name, connection, **kwargs):
	doc = {
		"doctype": "Marketplace Account",
		"account_name": account_name,
		"platform": "Noon",
		"external_account_id": connection.project_code,
		"connection_doctype": "Noon Connection",
		"connection": connection.name,
		"noon_warehouse_code": "NOON-WH-001",
		"noon_country_code": "ae",
		"enabled": 1,
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


def _envelope(connection, event_type="FBPI::ORDER_SYNC", message_id="msg-001", payload=None, **overrides):
	envelope = {
		"event_schema_version": "1.0",
		"event_type": event_type,
		"metadata": {
			"message_id": message_id,
			"destination_id": "dest-001",
			"published_at": "2026-10-03T00:00:00Z",
			"project_code": connection.project_code,
		},
		"payload": payload if payload is not None else {"sentinel": _SENTINEL_PAYLOAD_VALUE},
	}
	envelope.update(overrides)
	return envelope


class IntegrationTestNoonWebhookReceiver(IntegrationTestCase):
	def setUp(self):
		# Marketplace Account creation for Noon calls out to validate_warehouse_code(),
		# which hits the real Noon API - patch it out like test_orders.py does, so
		# fixture setup never depends on network access or a real signing key.
		warehouse_patch = patch("marketplace_integrations.integrations.noon.catalog.validate_warehouse_code")
		warehouse_patch.start()
		self.addCleanup(warehouse_patch.stop)

		suffix = frappe.generate_hash(length=8)
		self.connection = _make_connection(f"Webhook Connection {suffix}")
		self.account = _make_account(f"Webhook Account {suffix}", self.connection)
		self._other_connection = None
		self._second_account = None

	def tearDown(self):
		# _log_accepted_event() commits on the accepted path, so the class-
		# scoped rollback never undoes setUp's inserts here - clean up
		# explicitly, following the same order as
		# integrations/noon/test_orders.py's tearDown (orders/logs before the
		# account, account before the connection), plus the extra
		# other_connection/second_account the ambiguous-account test creates.
		for account_name in (self.account.name, self._second_account):
			if not account_name:
				continue
			frappe.db.delete("Marketplace Order", filters={"marketplace_account": account_name})
			frappe.db.delete("Marketplace Sync Log", filters={"marketplace_account": account_name})
			frappe.delete_doc("Marketplace Account", account_name, ignore_permissions=True, force=True)
		for connection_name in (self.connection.name, self._other_connection):
			if not connection_name:
				continue
			frappe.delete_doc("Noon Connection", connection_name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _call_webhook(self, envelope, header_value):
		request = MagicMock()
		request.get_json.return_value = envelope

		def mock_get_request_header(header_name, default=""):
			# Only X-Api-Key is recognized; any other header name (case-insensitive)
			# returns the default, simulating Frappe's get_request_header behavior
			if header_name.lower() == "x-api-key":
				return header_value
			return default

		with (
			patch.object(noon_webhooks.frappe, "request", request),
			patch.object(noon_webhooks.frappe, "get_request_header", side_effect=mock_get_request_header),
		):
			return noon_webhooks.event_notification()

	@patch.object(orders.frappe, "enqueue")
	def test_valid_order_sync_enqueues_run_order_import(self, enqueue):
		result = self._call_webhook(_envelope(self.connection), "test-webhook-secret")

		self.assertEqual(result, {"accepted": True})
		enqueue.assert_called_once_with(
			run_order_import,
			queue="long",
			timeout=3600,
			job_id=f"{_ORDER_IMPORT_RQ_JOB_PREFIX}-{self.account.name}",
			deduplicate=True,
			account_name=self.account.name,
			trigger_source="Noon Webhook",
		)

	@patch.object(orders.frappe, "enqueue")
	def test_missing_secret_header_is_rejected_before_enqueue(self, enqueue):
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(_envelope(self.connection), "")
		enqueue.assert_not_called()

	@patch.object(orders.frappe, "enqueue")
	def test_wrong_secret_header_is_rejected_before_enqueue(self, enqueue):
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(_envelope(self.connection), "wrong-secret")
		enqueue.assert_not_called()

	@patch.object(orders.frappe, "enqueue")
	def test_unknown_project_code_is_rejected_before_enqueue(self, enqueue):
		envelope = _envelope(self.connection)
		envelope["metadata"]["project_code"] = "unknown-project-code"
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(envelope, "test-webhook-secret")
		enqueue.assert_not_called()

	# No "ambiguous project_code resolves to >1 Noon Connection" case is
	# testable here: ``Noon Connection.project_code`` has a DB-level
	# ``unique: 1`` constraint, so two connections can never actually share
	# one - the receiver's ``len(connections) != 1`` check still defends
	# against it, but ambiguity at this specific step is structurally
	# prevented by the schema rather than something a test can construct.
	# ``test_ambiguous_account_for_connection_is_rejected_before_enqueue``
	# below exercises the one resolution step where ambiguity (>1 enabled
	# account bound to the same connection) is actually reachable.

	@patch.object(orders.frappe, "enqueue")
	def test_disabled_connection_is_rejected_before_enqueue(self, enqueue):
		self.connection.enabled = 0
		self.connection.save()

		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(_envelope(self.connection), "test-webhook-secret")
		enqueue.assert_not_called()

	@patch.object(orders.frappe, "enqueue")
	def test_disabled_account_is_rejected_before_enqueue(self, enqueue):
		self.account.enabled = 0
		self.account.save()

		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(_envelope(self.connection), "test-webhook-secret")
		enqueue.assert_not_called()

	@patch.object(orders.frappe, "enqueue")
	def test_ambiguous_account_for_connection_is_rejected_before_enqueue(self, enqueue):
		# Two enabled Marketplace Accounts bound to the same Noon Connection is
		# structurally unreachable via normal document creation:
		# validate_noon_account() forces external_account_id == the
		# connection's project_code, and validate_unique_account_identity()'s
		# DB-level unique constraint on (platform, external_account_id) then
		# rejects a second account on that same connection at insert time.
		# To exercise the receiver's defensive len(accounts) != 1 check
		# anyway, insert the second account against its own throwaway
		# connection (so insert-time validation passes), then use
		# frappe.db.set_value() - which bypasses validate_unique_account_identity()
		# since it writes directly, not via .save() - to repoint it at
		# self.connection, simulating the otherwise-impossible ambiguous state.
		# This mirrors the frappe.db.set_value() bypass pattern used in
		# test_inventory_sync.py for other defense-in-depth checks.
		other_connection = _make_connection(f"Other Connection {frappe.generate_hash(length=8)}")
		second_account = _make_account(f"Second Account {frappe.generate_hash(length=8)}", other_connection)
		frappe.db.set_value("Marketplace Account", second_account.name, "connection", self.connection.name)
		self._other_connection = other_connection.name
		self._second_account = second_account.name

		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(_envelope(self.connection), "test-webhook-secret")
		enqueue.assert_not_called()

	@patch.object(orders.frappe, "enqueue")
	def test_missing_required_envelope_field_is_rejected_before_enqueue(self, enqueue):
		for mutate in (
			lambda e: e.pop("event_type"),
			lambda e: e["metadata"].pop("message_id"),
			lambda e: e["metadata"].pop("project_code"),
			lambda e: e.update(payload="not-an-object"),
		):
			envelope = _envelope(self.connection)
			mutate(envelope)
			with self.subTest(envelope=envelope):
				with self.assertRaises(frappe.PermissionError):
					self._call_webhook(envelope, "test-webhook-secret")
		enqueue.assert_not_called()

	@patch.object(orders.frappe, "enqueue")
	def test_unhandled_event_type_returns_success_without_enqueuing(self, enqueue):
		envelope = _envelope(self.connection, event_type="FBPI::SOMETHING_ELSE")
		result = self._call_webhook(envelope, "test-webhook-secret")

		self.assertEqual(result, {"accepted": True})
		enqueue.assert_not_called()

	@patch.object(orders.frappe, "enqueue")
	def test_redelivered_message_id_still_uses_the_same_dedup_job_id(self, enqueue):
		"""Redelivery (or a burst) of events for the same account always
		targets the identical per-account job_id - the same job_id RQ's own
		``deduplicate=True`` collapses, per #145's existing job-dedup
		mechanism. This receiver intentionally adds no new dedup bookkeeping
		of its own (#163 scope)."""
		envelope = _envelope(self.connection, message_id="redelivered-msg")

		self._call_webhook(envelope, "test-webhook-secret")
		self._call_webhook(envelope, "test-webhook-secret")
		self._call_webhook(
			_envelope(self.connection, message_id="a-different-message"), "test-webhook-secret"
		)

		self.assertEqual(enqueue.call_count, 3)
		job_ids = {call.kwargs["job_id"] for call in enqueue.call_args_list}
		self.assertEqual(job_ids, {f"{_ORDER_IMPORT_RQ_JOB_PREFIX}-{self.account.name}"})

	def _imported_order(self, order_nr):
		order = upsert_marketplace_order(
			self.account.name,
			{
				"external_order_id": order_nr,
				"received_on": frappe.utils.nowdate(),
				"latest_payload": {"items": []},
				"items": [],
			},
		)
		frappe.db.commit()
		return order

	@patch.object(orders.frappe, "enqueue")
	def test_order_sync_for_an_imported_order_queues_one_deduplicated_refresh_of_that_order(self, enqueue):
		order = self._imported_order(f"ORD-WH-{frappe.generate_hash(length=8)}")
		envelope = _envelope(self.connection, payload={"order_nr": order.external_order_id})

		self._call_webhook(envelope, "test-webhook-secret")

		enqueue.assert_called_once_with(
			run_order_refresh,
			queue="long",
			timeout=3600,
			job_id=f"{_ORDER_REFRESH_RQ_JOB_PREFIX}-{order.name}",
			deduplicate=True,
			marketplace_order_name=order.name,
		)
		log = frappe.get_last_doc(
			"Marketplace Sync Log",
			filters={"marketplace_account": self.account.name, "trigger_source": "Noon Webhook"},
		)
		self.assertEqual(
			frappe.parse_json(log.request_data),
			{
				"message_id": envelope["metadata"]["message_id"],
				"event_type": "FBPI::ORDER_SYNC",
				"import_queued": False,
				"refresh_queued": True,
			},
		)

	@patch.object(orders.frappe, "enqueue", return_value=None)
	def test_a_redelivered_order_sync_queues_nothing_extra(self, enqueue):
		order = self._imported_order(f"ORD-WH-{frappe.generate_hash(length=8)}")
		self._call_webhook(_envelope(self.connection, payload={"order_nr": order.external_order_id}), "test-webhook-secret")
		# Only the refresh job id was used (a duplicate returns None), never the account import.
		self.assertEqual(
			{call.kwargs["job_id"] for call in enqueue.call_args_list}, {f"{_ORDER_REFRESH_RQ_JOB_PREFIX}-{order.name}"}
		)
		log = frappe.get_last_doc(
			"Marketplace Sync Log",
			filters={"marketplace_account": self.account.name, "trigger_source": "Noon Webhook"},
		)
		self.assertEqual(frappe.parse_json(log.request_data)["refresh_queued"], False)

	@patch.object(orders.frappe, "enqueue")
	def test_an_unknown_or_unusable_order_nr_queues_the_account_import(self, enqueue):
		for payload in ({"order_nr": "NO-SUCH-ORDER"}, {"order_nr": 12345}, {"order_nr": "  "}, {}):
			enqueue.reset_mock()
			self._call_webhook(_envelope(self.connection, payload=payload or None), "test-webhook-secret")
			enqueue.assert_called_once()
			self.assertEqual(enqueue.call_args.args[0], run_order_import, payload)

	@patch.object(orders.frappe, "enqueue")
	def test_payload_contents_are_never_read_stored_or_logged(self, enqueue):
		envelope = _envelope(self.connection, message_id=f"payload-check-{frappe.generate_hash(length=8)}")

		self._call_webhook(envelope, "test-webhook-secret")

		log = frappe.get_last_doc(
			"Marketplace Sync Log",
			filters={"marketplace_account": self.account.name, "trigger_source": "Noon Webhook"},
		)
		self.assertNotIn(_SENTINEL_PAYLOAD_VALUE, log.request_data or "")
		self.assertNotIn(_SENTINEL_PAYLOAD_VALUE, frappe.as_json(log.as_dict()))
		self.assertEqual(
			frappe.parse_json(log.request_data),
			{
				"message_id": envelope["metadata"]["message_id"],
				"event_type": "FBPI::ORDER_SYNC",
				"import_queued": True,
			},
		)

	@patch.object(orders.frappe, "enqueue")
	def test_accepted_event_writes_one_committed_sync_log_row(self, enqueue):
		message_id = f"log-check-{frappe.generate_hash(length=8)}"
		envelope = _envelope(self.connection, message_id=message_id)

		self._call_webhook(envelope, "test-webhook-secret")

		self.assertEqual(
			frappe.db.count(
				"Marketplace Sync Log",
				{
					"marketplace_account": self.account.name,
					"sync_type": "Order Import",
					"trigger_source": "Noon Webhook",
					"request_data": ["like", f"%{message_id}%"],
				},
			),
			1,
		)

	@patch.object(orders.frappe, "enqueue")
	def test_unhandled_event_type_logs_import_queued_false(self, enqueue):
		envelope = _envelope(self.connection, event_type="FBPI::SOMETHING_ELSE")

		self._call_webhook(envelope, "test-webhook-secret")

		log = frappe.get_last_doc(
			"Marketplace Sync Log",
			filters={"marketplace_account": self.account.name, "trigger_source": "Noon Webhook"},
		)
		self.assertEqual(frappe.parse_json(log.request_data)["import_queued"], False)

	@patch.object(orders.frappe, "enqueue", return_value=None)
	def test_deduplicated_delivery_logs_import_queued_false(self, enqueue):
		envelope = _envelope(self.connection)

		self._call_webhook(envelope, "test-webhook-secret")

		log = frappe.get_last_doc(
			"Marketplace Sync Log",
			filters={"marketplace_account": self.account.name, "trigger_source": "Noon Webhook"},
		)
		self.assertEqual(frappe.parse_json(log.request_data)["import_queued"], False)

	@patch.object(orders.frappe, "enqueue", side_effect=RuntimeError("redis down"))
	def test_enqueue_failure_propagates_without_writing_a_success_log_row(self, enqueue):
		envelope = _envelope(self.connection, message_id=f"enqueue-fails-{frappe.generate_hash(length=8)}")

		with self.assertRaises(RuntimeError):
			self._call_webhook(envelope, "test-webhook-secret")

		self.assertEqual(
			frappe.db.count(
				"Marketplace Sync Log",
				{
					"marketplace_account": self.account.name,
					"trigger_source": "Noon Webhook",
					"request_data": ["like", f"%{envelope['metadata']['message_id']}%"],
				},
			),
			0,
		)

	@patch.object(orders.frappe, "enqueue")
	def test_bare_order_body_with_valid_key_returns_accepted_without_enqueuing(self, enqueue):
		"""Bare order body ({"order_nr": ...} only) is authenticated but never
		enqueues: there's no project_code to resolve a connection/account, so the
		response is always {"accepted": True} after validating the key."""
		bare_body = {"order_nr": "order-12345"}
		result = self._call_webhook(bare_body, "test-webhook-secret")

		self.assertEqual(result, {"accepted": True})
		enqueue.assert_not_called()

	@patch.object(orders.frappe, "enqueue")
	def test_bare_order_body_with_second_enabled_connection_key_returns_accepted(self, enqueue):
		"""Bare order body whose X-Api-Key matches the SECOND of two enabled
		Noon Connections (with different secrets) returns {"accepted": True} and
		never enqueues, regardless of which connection the key belongs to."""
		# Create a second connection with a different secret
		suffix = frappe.generate_hash(length=8)
		second_connection = _make_connection(
			f"Second Webhook Connection {suffix}", webhook_secret="second-webhook-secret"
		)
		self._other_connection = second_connection.name
		# Create a second account for this second connection
		second_account = _make_account(f"Second Webhook Account {suffix}", second_connection)
		self._second_account = second_account.name

		# Call with the second connection's secret
		bare_body = {"order_nr": "order-12345"}
		result = self._call_webhook(bare_body, "second-webhook-secret")

		self.assertEqual(result, {"accepted": True})
		enqueue.assert_not_called()

	@patch.object(orders.frappe, "enqueue")
	def test_bare_order_body_missing_key_header_is_rejected_before_responding(self, enqueue):
		"""Bare order body requires X-Api-Key header authentication."""
		bare_body = {"order_nr": "order-12345"}

		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(bare_body, "")
		enqueue.assert_not_called()

	@patch.object(orders.frappe, "enqueue")
	def test_bare_order_body_wrong_key_header_is_rejected_before_responding(self, enqueue):
		"""Bare order body with a non-matching X-Api-Key is rejected."""
		bare_body = {"order_nr": "order-12345"}

		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(bare_body, "wrong-key")
		enqueue.assert_not_called()

	@patch.object(orders.frappe, "enqueue")
	def test_bare_order_body_with_disabled_connection_key_is_rejected(self, enqueue):
		"""Bare order body authentication scans only enabled connections, so a
		disabled connection's key is treated as invalid."""
		self.connection.enabled = 0
		self.connection.save()
		bare_body = {"order_nr": "order-12345"}

		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(bare_body, "test-webhook-secret")
		enqueue.assert_not_called()

	@patch.object(orders.frappe, "enqueue")
	def test_old_webhook_secret_header_does_not_authenticate(self, enqueue):
		"""The X-Noon-Webhook-Secret header (from previous webhook receiver
		implementations) is not recognized; only X-Api-Key works. Even if
		X-Noon-Webhook-Secret has the valid secret, the receiver rejects it
		because only X-Api-Key is checked."""
		envelope = _envelope(self.connection)
		request = MagicMock()
		request.get_json.return_value = envelope

		def mock_get_request_header(header_name, default=""):
			# X-Noon-Webhook-Secret has the valid secret, but it's ignored;
			# only X-Api-Key is recognized, and it's missing here.
			if header_name.lower() == "x-noon-webhook-secret":
				return "test-webhook-secret"  # Valid secret, but wrong header
			if header_name.lower() == "x-api-key":
				return ""  # No X-Api-Key header present in this request
			return default

		with (
			patch.object(noon_webhooks.frappe, "request", request),
			patch.object(noon_webhooks.frappe, "get_request_header", side_effect=mock_get_request_header),
		):
			with self.assertRaises(frappe.PermissionError):
				noon_webhooks.event_notification()
		enqueue.assert_not_called()

	# --- #263: RETURNS::REFERENCE_UPDATE (payload shape is an unverified assumption) ---

	def _return_event_logs(self):
		return frappe.get_all(
			"Marketplace Sync Log",
			filters={
				"marketplace_account": self.account.name,
				"trigger_source": "Noon Webhook",
				"sync_type": "Return Lookup",
			},
			fields=["name", "status", "request_data"],
		)

	@patch.object(returns.frappe, "enqueue")
	def test_return_reference_event_queues_one_deduplicated_lookup_and_logs_the_payload(
		self, returns_enqueue
	):
		payload = {"barcode": "RMS-12345", "barcode_type": "rms_barcode"}
		envelope = _envelope(self.connection, event_type="RETURNS::REFERENCE_UPDATE", payload=payload)

		result = self._call_webhook(envelope, "test-webhook-secret")

		self.assertEqual(result, {"accepted": True})
		returns_enqueue.assert_called_once()
		kwargs = returns_enqueue.call_args.kwargs
		self.assertEqual(kwargs["barcode"], "RMS-12345")
		self.assertEqual(kwargs["account_name"], self.account.name)
		self.assertTrue(kwargs["deduplicate"])
		# One job only, and it is the return lookup, not an order import.
		self.assertTrue(kwargs["job_id"].startswith("marketplace-noon-return-webhook-"))

		logs = self._return_event_logs()
		self.assertEqual(len(logs), 1)
		logged = frappe.parse_json(logs[0].request_data)
		self.assertEqual(logged["payload"], payload)
		self.assertTrue(logged["lookup_queued"])
		self.assertNotIn("test-webhook-secret", logs[0].request_data)

	@patch.object(returns.frappe, "enqueue")
	def test_return_reference_event_without_a_string_barcode_is_logged_and_ignored(self, enqueue):
		for payload in (
			{"barcode_type": "awb_nr"},
			{"barcode": 123},
			{"barcode": "  "},
			{"barcode": "x" * 300},
		):
			with self.subTest(payload=payload):
				envelope = _envelope(self.connection, event_type="RETURNS::REFERENCE_UPDATE", payload=payload)
				result = self._call_webhook(envelope, "test-webhook-secret")
				self.assertEqual(result, {"accepted": True})
		enqueue.assert_not_called()
		for log in self._return_event_logs():
			self.assertFalse(frappe.parse_json(log.request_data)["lookup_queued"])

	@patch.object(returns.frappe, "enqueue")
	def test_return_reference_payload_is_capped_in_the_log(self, enqueue):
		payload = {"barcode": "RMS-1", "extra": "y" * 10000}
		envelope = _envelope(self.connection, event_type="RETURNS::REFERENCE_UPDATE", payload=payload)

		self._call_webhook(envelope, "test-webhook-secret")

		logged = frappe.parse_json(self._return_event_logs()[0].request_data)
		self.assertEqual(list(logged["payload"]), ["truncated_payload"])
		self.assertLessEqual(len(logged["payload"]["truncated_payload"]), 4096)

	@patch.object(returns.frappe, "enqueue")
	def test_return_reference_event_still_requires_the_webhook_key(self, enqueue):
		envelope = _envelope(
			self.connection, event_type="RETURNS::REFERENCE_UPDATE", payload={"barcode": "RMS-1"}
		)
		with self.assertRaises(frappe.PermissionError):
			self._call_webhook(envelope, "wrong-secret")
		enqueue.assert_not_called()
		self.assertEqual(self._return_event_logs(), [])

	def test_webhook_lookup_records_returns_found_via_webhook(self):
		item = {"purchase_item_nr": "PIN-1", "partner_sku": "SKU-1", "order_nr": "ORD-1"}
		with (
			patch.object(returns, "get_account", return_value=self.account),
			patch.object(returns, "validate_connection_enabled"),
			patch.object(returns, "list_return_references", return_value=[item, "not-a-dict"]) as listed,
			patch.object(returns, "upsert_return_item") as upsert,
		):
			returns.run_webhook_lookup(self.account.name, "RMS-1")

		listed.assert_called_once_with(self.account, "RMS-1", trigger_source="Noon Webhook")
		upsert.assert_called_once_with(self.account, item, "Webhook")
