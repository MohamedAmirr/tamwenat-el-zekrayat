# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for #181's services/marketplace_outbound.py and api/marketplace_outbound.py.

``frappe.db.commit`` is patched to a recorder in every test that reaches the platform
call: the service commits on purpose (before the lock and after the platform call), and a
real commit would make the fixture rows permanent on the shared test site (see
``docs/frappe-conventions.md``, Tests).  Recording the calls also lets the ordering
guarantee (marker committed before the returned order is upserted) be asserted.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.model.document import Document
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api import marketplace_outbound as outbound_api
from marketplace_integrations.services import marketplace_outbound as outbound
from marketplace_integrations.services.linked_sales_order_fixture import LinkedSalesOrderFixture
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order

SEND_TARGET = "marketplace_integrations.api.talabat_orders.send_order_update"
UPSERT_TARGET = "marketplace_integrations.services.marketplace_orders.upsert_marketplace_order"
TALABAT_PUT_TARGET = "marketplace_integrations.integrations.talabat.orders.update_order"
BOUNDARY_STATUSES = ("RECEIVED", "UPDATE_CART")
BLOCKED_STATUSES = ("READY_FOR_PICKUP", "DISPATCHED", "DELIVERED", "CANCELLED", "SOMETHING_NEW", "")


class IntegrationTestMarketplaceOutbound(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.company = frappe.defaults.get_global_default("company")
		self.account = self._make_talabat_account()
		self.fixture = LinkedSalesOrderFixture()
		self.orders = []

	def tearDown(self):
		frappe.db.set_value("Sales Order", self.fixture.name, "custom_marketplace_order", None)
		self.fixture.cleanup()
		for name in self.orders:
			frappe.db.delete("Marketplace Sync Log", {"marketplace_order": name})
			frappe.delete_doc("Marketplace Order", name, force=True, ignore_permissions=True)
		frappe.delete_doc("Marketplace Account", self.account.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Talabat Connection", self.account.connection, force=True, ignore_permissions=True)

	# -- fixtures -----------------------------------------------------------

	def _make_talabat_account(self):
		connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Outbound {self.suffix}",
				"client_id": f"outbound-client-{self.suffix}",
				"client_secret": "test-secret",
				"chain_id": f"outbound-chain-{self.suffix}",
			}
		).insert()
		return frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Outbound Account {self.suffix}",
				"platform": "Talabat",
				"external_account_id": f"outbound-vendor-{self.suffix}",
				"connection_doctype": "Talabat Connection",
				"connection": connection.name,
				"erpnext_company": self.company,
			}
		).insert()

	def _linked_order(self, status="RECEIVED", platform=None, link=True):
		order = upsert_marketplace_order(
			self.account.name,
			{
				"external_order_id": f"outbound-order-{self.suffix}-{frappe.generate_hash(length=4)}",
				"latest_payload": {"order_id": "external-order"},
				"external_status": status,
				"items": [],
			},
		)
		self.orders.append(order.name)
		if platform:
			# The account's platform is fixed; only the order's own platform is varied.
			frappe.db.set_value("Marketplace Order", order.name, "platform", platform)
		if link:
			frappe.db.set_value("Sales Order", self.fixture.name, "custom_marketplace_order", order.name)
		return order

	def _raw_cancelled_order(self, order):
		return {"order_id": order.external_order_id, "status": "CANCELLED"}

	def _set_execution(self, **fields):
		item = frappe.get_doc("Sales Order", self.fixture.name).items[0].name
		frappe.db.set_value("Sales Order Item", item, fields)

	def _marker(self, order):
		return frappe.db.get_value("Marketplace Order", order.name, "outbound_cancelled_on")

	def _action_logs(self, order):
		return frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name, "sync_type": "Order Action"},
			fields=[
				"status",
				"trigger_source",
				"request_data",
				"response_data",
				"marketplace_account",
				"traceback",
			],
			# get_all defaults to modified desc; the tests read the logs oldest first.
			order_by="creation asc",
		)

	# -- option: Talabat lifecycle boundary ---------------------------------

	def test_talabat_is_eligible_only_from_received_or_update_cart(self):
		for status in BOUNDARY_STATUSES:
			with self.subTest(status=status):
				order = self._linked_order(status)
				option = outbound.get_platform_cancel_option(self.fixture.name)
				self.assertEqual(option["state"], outbound.ELIGIBLE)
				self.assertEqual(option["platform"], "Talabat")
				self.assertEqual(option["marketplace_order"], order.name)
				self.assertEqual(option["reasons"], [])
				self.assertEqual(option["cancellation_reasons"], ["CLOSED", "ITEM_UNAVAILABLE", "TOO_BUSY"])

	def test_every_other_or_unknown_talabat_status_is_blocked(self):
		for status in BLOCKED_STATUSES:
			with self.subTest(status=status):
				self._linked_order(status)
				option = outbound.get_platform_cancel_option(self.fixture.name)
				self.assertEqual(option["state"], outbound.BLOCKED)
				self.assertTrue(option["reasons"])
				self.assertTrue(option["instruction"])

	def test_noon_is_blocked_and_shopify_returns_its_cancellation_reasons(self):
		self._linked_order("RECEIVED", platform="Noon")
		option = outbound.get_platform_cancel_option(self.fixture.name)
		self.assertEqual(option["state"], outbound.BLOCKED)
		self.assertEqual(option["platform"], "Noon")
		self.assertTrue(option["reasons"])

		self._linked_order("RECEIVED", platform="Shopify")
		option = outbound.get_platform_cancel_option(self.fixture.name)
		self.assertEqual(option["state"], outbound.ELIGIBLE)
		self.assertEqual(option["cancellation_reasons"], [
			"CUSTOMER", "DECLINED", "FRAUD", "INVENTORY", "OTHER", "STAFF"
		])

	def test_shopify_cancel_option_blocks_cancelled_or_authorized_orders(self):
		for fields in (
			{"cancelled_at": "2026-10-24 12:00:00"},
			{"external_financial_status": "AUTHORIZED"},
			{
				"payment_transactions": [
					{"kind": "AUTHORIZATION", "status": "PENDING"}
				]
			},
		):
			with self.subTest(fields=fields):
				order = self._linked_order("RECEIVED", platform="Shopify")
				# A JSON field takes its serialized text; set_value cannot store a Python list.
				values = {
					key: frappe.as_json(value) if isinstance(value, (list, dict)) else value
					for key, value in fields.items()
				}
				frappe.db.set_value("Marketplace Order", order.name, values)
				option = outbound.get_platform_cancel_option(self.fixture.name)
				self.assertEqual(option["state"], outbound.BLOCKED)
				self.assertTrue(option["reasons"])

	def test_shopify_fulfillment_preflight_blocks_started_and_unknown_lines(self):
		from marketplace_integrations.services.marketplace_outbound import (
			_shopify_fulfillment_problem,
		)

		for item, expected in (
			(frappe._dict(fulfillment_quantities_known=1, current_quantity=2, unfulfilled_quantity=1), "started"),
			(frappe._dict(fulfillment_quantities_known=0), "unknown"),
		):
			with self.subTest(expected=expected):
				# Not frappe._dict(items=...): its own .items method would shadow the row list.
				problem = _shopify_fulfillment_problem(SimpleNamespace(items=[item]))
				self.assertIn(expected, problem)

	def test_a_sales_order_without_a_marketplace_order_is_not_applicable(self):
		option = outbound.get_platform_cancel_option(self.fixture.name)

		self.assertEqual(option["state"], outbound.NOT_APPLICABLE)
		self.assertIsNone(option["marketplace_order"])

	def test_delivered_or_billed_quantity_blocks_the_cancel(self):
		self._linked_order("RECEIVED")
		for fields in ({"delivered_qty": 1}, {"billed_amt": 10}):
			with self.subTest(fields=fields):
				self._set_execution(**fields)
				option = outbound.get_platform_cancel_option(self.fixture.name)
				self.assertEqual(option["state"], outbound.BLOCKED)
				self.assertIn("delivered quantity or a billed amount", " ".join(option["reasons"]))
				self._set_execution(delivered_qty=0, billed_amt=0)

	def test_option_does_not_write_anything(self):
		order = self._linked_order("RECEIVED")

		with patch(SEND_TARGET) as send:
			outbound.get_platform_cancel_option(self.fixture.name)

		send.assert_not_called()
		self.assertIsNone(self._marker(order))
		self.assertEqual(self._action_logs(order), [])

	# -- request: success path ----------------------------------------------

	def test_request_calls_the_platform_once_sets_the_marker_and_logs_an_order_action(self):
		order = self._linked_order("RECEIVED")

		with (
			patch(SEND_TARGET, return_value=self._raw_cancelled_order(order)) as send,
			patch.object(frappe.db, "commit"),
		):
			result = outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")

		self.assertTrue(result["ok"])
		self.assertFalse(result["already_requested"])
		self.assertTrue(result["refreshed"])
		send.assert_called_once()
		self.assertEqual(send.call_args.args[1:3], ("CANCELLED", "TOO_BUSY"))
		self.assertIsNotNone(self._marker(order))
		self.assertEqual(frappe.db.get_value("Marketplace Order", order.name, "external_status"), "CANCELLED")
		logs = self._action_logs(order)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].status, "Success")
		self.assertEqual(logs[0].trigger_source, "Manual")
		self.assertEqual(logs[0].marketplace_account, self.account.name)
		self.assertEqual(frappe.parse_json(logs[0].request_data)["action"], "platform_cancel")
		self.assertEqual(frappe.parse_json(logs[0].request_data)["cancellation_reason"], "TOO_BUSY")

	def test_shopify_request_commits_the_marker_then_refreshes_by_order_gid(self):
		order = self._linked_order("RECEIVED", platform="Shopify")
		job = {"job_id": "gid://shopify/Job/1"}
		with (
			patch.object(outbound, "_send_cancel", return_value=job) as send,
			patch(
				"marketplace_integrations.integrations.shopify.orders.import_order",
				return_value=order,
			) as refresh,
			patch(
				"marketplace_integrations.integrations.shopify.orders.poll_order_cancel_job",
				return_value={"status": "SUCCEEDED", "done": True, "errors": []},
			),
			patch.object(frappe.db, "commit"),
		):
			result = outbound.request_platform_cancel(self.fixture.name, "CUSTOMER")

		# The service reloads the order, so compare by name, not by Document identity.
		send.assert_called_once()
		self.assertEqual(send.call_args.args[0].name, order.name)
		self.assertEqual(send.call_args.args[1:], ("CUSTOMER",))
		refresh.assert_called_once_with(order.marketplace_account, order.external_order_id)
		self.assertTrue(result["refreshed"])
		self.assertEqual(result["job_id"], job["job_id"])
		self.assertIsNotNone(self._marker(order))
		self.assertEqual(self._action_logs(order)[0].status, "Success")
		self.assertEqual(
			frappe.parse_json(self._action_logs(order)[0].response_data)["job_id"], job["job_id"]
		)

	def test_shopify_confirmed_job_failure_clears_marker_and_logs_error(self):
		order = self._linked_order("RECEIVED", platform="Shopify")
		with (
			patch.object(outbound, "_send_cancel", return_value={"job_id": "gid://shopify/OrderCancelJobResult/1"}),
			patch(
				"marketplace_integrations.integrations.shopify.orders.poll_order_cancel_job",
				return_value={
					"status": "FAILED",
					"done": True,
					"errors": [{"message": "Order has an active return"}],
				},
			),
			patch.object(frappe.db, "commit"),
		):
			with self.assertRaises(frappe.ValidationError):
				outbound.request_platform_cancel(self.fixture.name, "CUSTOMER")

		self.assertIsNone(self._marker(order))
		self.assertEqual([row.status for row in self._action_logs(order)], ["Success", "Error"])
		self.assertIn("active return", self._action_logs(order)[-1].traceback)

	def test_shopify_job_failure_text_is_escaped_in_the_thrown_message_but_logged_as_is(self):
		order = self._linked_order("RECEIVED", platform="Shopify")
		text = "<img src=x onerror=alert(1)>"
		with (
			patch.object(outbound, "_send_cancel", return_value={"job_id": "gid://shopify/OrderCancelJobResult/1"}),
			patch(
				"marketplace_integrations.integrations.shopify.orders.poll_order_cancel_job",
				return_value={"status": "FAILED", "done": True, "errors": [{"message": text}]},
			),
			patch.object(frappe.db, "commit"),
		):
			with self.assertRaises(frappe.ValidationError) as raised:
				outbound.request_platform_cancel(self.fixture.name, "CUSTOMER")

		self.assertNotIn("<img", str(raised.exception))
		self.assertIn("&lt;img", str(raised.exception))
		self.assertIn(text, self._action_logs(order)[-1].traceback)

	def test_shopify_cancel_user_errors_are_escaped_in_the_thrown_message(self):
		from marketplace_integrations.integrations.shopify import orders as shopify_orders

		result = {"orderCancel": {"orderCancelUserErrors": [{"message": "<b>bad</b>"}]}}
		account = SimpleNamespace(
			name="A", platform="Shopify", connection_doctype="Shopify Connection", connection="C"
		)
		with patch.object(shopify_orders, "ShopifyClient") as client:
			client.return_value.execute.return_value = result
			with self.assertRaises(frappe.ValidationError) as raised:
				shopify_orders.cancel_order(account, "gid://shopify/Order/1", "CUSTOMER")

		self.assertNotIn("<b>", str(raised.exception))
		self.assertIn("&lt;b&gt;bad", str(raised.exception))

	def test_shopify_job_timeout_keeps_marker_and_skips_refresh(self):
		order = self._linked_order("RECEIVED", platform="Shopify")
		with (
			patch.object(outbound, "_send_cancel", return_value={"job_id": "gid://shopify/OrderCancelJobResult/1"}),
			patch(
				"marketplace_integrations.integrations.shopify.orders.poll_order_cancel_job",
				return_value=None,
			),
			patch(
				"marketplace_integrations.integrations.shopify.orders.import_order"
			) as refresh,
			patch.object(frappe.db, "commit"),
		):
			result = outbound.request_platform_cancel(self.fixture.name, "CUSTOMER")

		self.assertTrue(result["job_pending"])
		self.assertFalse(result["refreshed"])
		self.assertIsNotNone(self._marker(order))
		refresh.assert_not_called()

	def test_shopify_poll_graphql_error_keeps_marker_and_is_logged(self):
		order = self._linked_order("RECEIVED", platform="Shopify")
		with (
			patch.object(outbound, "_send_cancel", return_value={"job_id": "gid://shopify/OrderCancelJobResult/1"}),
			patch(
				"marketplace_integrations.integrations.shopify.orders.poll_order_cancel_job",
				side_effect=frappe.ValidationError("top-level GraphQL error"),
			),
			patch.object(frappe.db, "commit"),
		):
			result = outbound.request_platform_cancel(self.fixture.name, "CUSTOMER")

		self.assertTrue(result["job_pending"])
		self.assertIsNotNone(self._marker(order))
		self.assertEqual(self._action_logs(order)[-1].status, "Error")
		self.assertIn("top-level GraphQL error", self._action_logs(order)[-1].traceback)

	def test_shopify_mutation_failure_is_logged_without_setting_the_marker(self):
		order = self._linked_order("RECEIVED", platform="Shopify")
		with (
			patch.object(outbound, "_send_cancel", side_effect=frappe.ValidationError("scope denied")),
			patch.object(frappe.db, "commit"),
		):
			with self.assertRaises(frappe.ValidationError):
				outbound.request_platform_cancel(self.fixture.name, "CUSTOMER")

		self.assertIsNone(self._marker(order))
		logs = self._action_logs(order)
		self.assertEqual([row.status for row in logs], ["Error"])
		self.assertIn("scope denied", logs[0].traceback)

	def test_a_second_request_sends_no_api_call(self):
		order = self._linked_order("RECEIVED")
		with (
			patch(SEND_TARGET, return_value=self._raw_cancelled_order(order)) as send,
			patch.object(frappe.db, "commit"),
		):
			outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")
			second = outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")

		send.assert_called_once()
		self.assertTrue(second["already_requested"])
		self.assertEqual(len(self._action_logs(order)), 1)

	def test_request_is_rechecked_under_the_lock_and_a_marker_set_meanwhile_sends_nothing(self):
		order = self._linked_order("RECEIVED")
		real_lock = outbound._lock_and_load_order

		def lock_after_a_concurrent_request(name):
			# Another session finished its request while this one waited for the lock.
			frappe.db.set_value(
				"Marketplace Order", name, "outbound_cancelled_on", frappe.utils.now_datetime()
			)
			return real_lock(name)

		with (
			patch.object(outbound, "_lock_and_load_order", side_effect=lock_after_a_concurrent_request),
			patch(SEND_TARGET) as send,
			patch.object(frappe.db, "commit"),
		):
			result = outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")

		send.assert_not_called()
		self.assertTrue(result["already_requested"])
		self.assertEqual(self._action_logs(order), [])

	def test_request_rechecks_the_erp_policy_under_the_lock(self):
		order = self._linked_order("RECEIVED")
		self._set_execution(delivered_qty=1)

		with patch(SEND_TARGET) as send, patch.object(frappe.db, "commit"):
			with self.assertRaises(frappe.ValidationError):
				outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")

		send.assert_not_called()
		self.assertIsNone(self._marker(order))

	def test_request_for_a_blocked_status_or_platform_throws_without_a_call(self):
		for status, platform in (("DISPATCHED", None), ("RECEIVED", "Noon"), ("RECEIVED", "Shopify")):
			with self.subTest(status=status, platform=platform):
				order = self._linked_order(status, platform=platform)
				with patch(SEND_TARGET) as send, patch.object(frappe.db, "commit"):
					with self.assertRaises(frappe.ValidationError):
						outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")
				send.assert_not_called()
				self.assertIsNone(self._marker(order))
				self.assertEqual(self._action_logs(order), [])

	# -- request: ordering and failure --------------------------------------

	def test_marker_and_log_are_committed_before_the_returned_order_is_upserted(self):
		order = self._linked_order("RECEIVED")
		events = []
		seen_at_upsert = {}

		def record_commit():
			events.append("commit")

		def fake_upsert(account, normalized):
			events.append("upsert")
			seen_at_upsert["marker"] = self._marker(order)
			seen_at_upsert["logs"] = len(self._action_logs(order))

		with (
			patch(SEND_TARGET, return_value=self._raw_cancelled_order(order)),
			patch(UPSERT_TARGET, side_effect=fake_upsert),
			patch.object(frappe.db, "commit", side_effect=record_commit),
		):
			outbound.request_platform_cancel(self.fixture.name, "CLOSED")

		self.assertIsNotNone(seen_at_upsert["marker"])
		self.assertEqual(seen_at_upsert["logs"], 1)
		# lock commit, then the commit after the platform call, then the upsert
		self.assertEqual(events, ["commit", "commit", "upsert"])

	def test_a_rejected_platform_call_writes_one_error_row_and_sets_no_marker(self):
		order = self._linked_order("RECEIVED")

		with (
			patch(SEND_TARGET, side_effect=frappe.ValidationError("Talabat rejected the order update.")),
			patch.object(frappe.db, "commit"),
		):
			with self.assertRaises(frappe.ValidationError):
				outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")

		self.assertIsNone(self._marker(order))
		logs = self._action_logs(order)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].status, "Error")
		self.assertEqual(logs[0].trigger_source, "Manual")
		self.assertEqual(logs[0].marketplace_account, self.account.name)
		request_data = frappe.parse_json(logs[0].request_data)
		self.assertEqual(request_data["action"], "platform_cancel")
		self.assertEqual(request_data["sales_order"], self.fixture.name)
		self.assertEqual(request_data["cancellation_reason"], "TOO_BUSY")
		self.assertIn("Talabat rejected the order update.", logs[0].traceback)

	def test_a_platform_exception_is_logged_truncated_and_reraised_as_is(self):
		order = self._linked_order("RECEIVED")

		with patch(SEND_TARGET, side_effect=RuntimeError("x" * 800)), patch.object(frappe.db, "commit"):
			with self.assertRaises(RuntimeError):
				outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")

		self.assertIsNone(self._marker(order))
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name, "sync_type": "Order Action"},
			fields=["status", "traceback"],
		)
		self.assertEqual([log.status for log in logs], ["Error"])
		self.assertEqual(len(logs[0].traceback), 500)

	def test_the_error_row_is_committed_before_the_error_reaches_the_caller(self):
		order = self._linked_order("RECEIVED")
		events = []
		seen_at_commit = []

		def record_commit():
			events.append("commit")
			seen_at_commit.append(len(self._action_logs(order)))

		with (
			patch(SEND_TARGET, side_effect=frappe.ValidationError("rejected")),
			patch.object(frappe.db, "commit", side_effect=record_commit),
		):
			try:
				outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")
			except frappe.ValidationError:
				events.append("raised")

		# lock commit (no row yet), then the error row's commit, then the caller sees the error
		self.assertEqual(events, ["commit", "commit", "raised"])
		self.assertEqual(seen_at_commit, [0, 1])

	def test_a_retry_after_a_failed_call_still_calls_the_platform(self):
		order = self._linked_order("RECEIVED")

		with patch.object(frappe.db, "commit"):
			with patch(SEND_TARGET, side_effect=frappe.ValidationError("rejected")):
				with self.assertRaises(frappe.ValidationError):
					outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")
			with patch(SEND_TARGET, return_value=self._raw_cancelled_order(order)) as send:
				result = outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")

		send.assert_called_once()
		self.assertFalse(result["already_requested"])
		self.assertIsNotNone(self._marker(order))
		self.assertEqual(sorted(log.status for log in self._action_logs(order)), ["Error", "Success"])

	def test_an_ineligible_request_writes_no_error_row(self):
		order = self._linked_order("DISPATCHED")

		with patch(SEND_TARGET) as send, patch.object(frappe.db, "commit"):
			with self.assertRaises(frappe.ValidationError):
				outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")

		send.assert_not_called()
		self.assertEqual(self._action_logs(order), [])

	def test_an_invalid_cancellation_reason_sends_no_call_and_records_nothing(self):
		order = self._linked_order("RECEIVED")

		with patch(TALABAT_PUT_TARGET) as put, patch.object(frappe.db, "commit"):
			for reason in (None, "", "BECAUSE"):
				with self.assertRaises(frappe.ValidationError):
					outbound.request_platform_cancel(self.fixture.name, reason)

		put.assert_not_called()
		self.assertIsNone(self._marker(order))
		# Rejected before the try: no Order Action row of any status.
		self.assertEqual(self._action_logs(order), [])

	def test_an_invalid_shopify_cancellation_reason_is_rejected_without_a_call_or_log(self):
		order = self._linked_order("RECEIVED", platform="Shopify")

		with patch.object(outbound, "_send_cancel") as send, patch.object(frappe.db, "commit"):
			with self.assertRaises(frappe.ValidationError):
				outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")

		send.assert_not_called()
		self.assertIsNone(self._marker(order))
		self.assertEqual(self._action_logs(order), [])

	def test_a_failing_error_log_does_not_mask_the_platform_error(self):
		order = self._linked_order("RECEIVED")

		with (
			patch(SEND_TARGET, side_effect=frappe.ValidationError("Talabat rejected it.")),
			patch.object(outbound, "_log_order_action_error", side_effect=RuntimeError("log insert failed")),
			patch.object(frappe.db, "commit") as commit,
			patch("frappe.log_error") as log_error,
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")

		self.assertIn("Talabat rejected it.", str(ctx.exception))
		log_error.assert_called_once()
		self.assertEqual(commit.call_count, 2)  # lock commit + commit before the re-raise
		self.assertIsNone(self._marker(order))
		self.assertEqual(self._action_logs(order), [])

	def test_a_lost_savepoint_when_refreshing_still_reports_refreshed_false_with_the_marker(self):
		order = self._linked_order("RECEIVED")
		rollbacks = []

		def rollback(*args, save_point=None, **kwargs):
			rollbacks.append(save_point)
			if save_point:
				raise Exception("SAVEPOINT does not exist")  # MariaDB 1020 dropped it
			# The plain fallback rollback is a no-op here: a real one would undo the test data.

		with (
			patch(SEND_TARGET, return_value=self._raw_cancelled_order(order)),
			patch(UPSERT_TARGET, side_effect=Exception("Deadlock found (1020)")),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback", side_effect=rollback),
			patch("frappe.log_error"),
		):
			result = outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")

		self.assertTrue(result["ok"])
		self.assertFalse(result["refreshed"])
		self.assertIn(None, rollbacks)  # fell back to a plain rollback
		self.assertIsNotNone(self._marker(order))

	def test_clear_outbound_cancelled_marker_clears_it_where_a_save_cannot(self):
		order = self._linked_order("RECEIVED")
		with patch(SEND_TARGET, return_value=self._raw_cancelled_order(order)), patch.object(
			frappe.db, "commit"
		), patch(UPSERT_TARGET):
			outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")
		self.assertIsNotNone(self._marker(order))

		outbound.clear_outbound_cancelled_marker(order.name)

		self.assertIsNone(self._marker(order))

	def test_a_failing_upsert_keeps_the_marker_and_reports_refreshed_false(self):
		order = self._linked_order("RECEIVED")

		with (
			patch(SEND_TARGET, return_value=self._raw_cancelled_order(order)),
			patch(UPSERT_TARGET, side_effect=frappe.ValidationError("boom")),
			patch.object(frappe.db, "commit"),
			patch("frappe.log_error"),
		):
			result = outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")

		self.assertTrue(result["ok"])
		self.assertFalse(result["refreshed"])
		self.assertIsNotNone(self._marker(order))
		self.assertEqual(len(self._action_logs(order)), 1)

	def test_a_returned_order_with_another_id_is_not_upserted(self):
		order = self._linked_order("RECEIVED")

		with (
			patch(SEND_TARGET, return_value={"order_id": "someone-elses", "status": "CANCELLED"}),
			patch(UPSERT_TARGET) as upsert,
			patch.object(frappe.db, "commit"),
			patch("frappe.log_error"),
		):
			result = outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")

		upsert.assert_not_called()
		self.assertFalse(result["refreshed"])
		self.assertIsNotNone(self._marker(order))

	def test_after_a_request_the_option_is_blocked_as_already_requested(self):
		order = self._linked_order("RECEIVED")
		with (
			patch(SEND_TARGET, return_value=self._raw_cancelled_order(order)),
			patch.object(frappe.db, "commit"),
		):
			outbound.request_platform_cancel(self.fixture.name, "TOO_BUSY")

		option = outbound.get_platform_cancel_option(self.fixture.name)

		self.assertEqual(option["state"], outbound.BLOCKED)
		self.assertIn("already requested", " ".join(option["reasons"]))

	# -- no-dialog flags and wording -------------------------------------------

	def test_option_flags_are_all_false_for_a_fresh_eligible_order(self):
		self._linked_order("RECEIVED")

		option = outbound.get_platform_cancel_option(self.fixture.name)

		self.assertFalse(option["already_requested"])
		self.assertFalse(option["platform_cancelled"])
		self.assertFalse(option["erp_executed"])

	def test_option_flags_are_false_when_not_applicable(self):
		option = outbound.get_platform_cancel_option(self.fixture.name)

		self.assertFalse(option["already_requested"])
		self.assertFalse(option["platform_cancelled"])
		self.assertFalse(option["erp_executed"])

	def test_already_requested_flag_follows_the_marker(self):
		order = self._linked_order("RECEIVED")
		frappe.db.set_value(
			"Marketplace Order", order.name, "outbound_cancelled_on", frappe.utils.now_datetime()
		)

		option = outbound.get_platform_cancel_option(self.fixture.name)

		self.assertTrue(option["already_requested"])
		self.assertFalse(option["platform_cancelled"])

	def test_platform_cancelled_flag_follows_the_cancellation_evidence(self):
		self._linked_order("CANCELLED")

		option = outbound.get_platform_cancel_option(self.fixture.name)

		self.assertTrue(option["platform_cancelled"])
		self.assertFalse(option["already_requested"])
		self.assertEqual(option["state"], outbound.BLOCKED)

	def test_erp_executed_flag_follows_delivered_or_billed_quantity(self):
		self._linked_order("RECEIVED")
		for fields in ({"delivered_qty": 1}, {"billed_amt": 10}):
			with self.subTest(fields=fields):
				self._set_execution(**fields)
				self.assertTrue(outbound.get_platform_cancel_option(self.fixture.name)["erp_executed"])
				self._set_execution(delivered_qty=0, billed_amt=0)
				self.assertFalse(outbound.get_platform_cancel_option(self.fixture.name)["erp_executed"])

	def test_a_platform_cancelled_talabat_order_says_it_is_already_cancelled(self):
		self._linked_order("CANCELLED")

		option = outbound.get_platform_cancel_option(self.fixture.name)

		self.assertIn("already cancelled on Talabat", option["instruction"])
		self.assertNotIn("handle it with Talabat", option["instruction"])
		self.assertNotIn("no longer be cancelled", option["instruction"])
		self.assertIn("already cancelled", " ".join(option["reasons"]))

	# -- instruction ---------------------------------------------------------

	def test_platform_instruction_per_platform(self):
		order = frappe._dict(platform="Talabat", external_status="RECEIVED", outbound_cancelled_on=None)
		self.assertIn("Cancel the order on Talabat", outbound.platform_instruction(order))
		order.external_status = "DELIVERED"
		self.assertIn("can no longer be cancelled", outbound.platform_instruction(order))
		order.external_status = "CANCELLED"
		self.assertIn("already cancelled on Talabat", outbound.platform_instruction(order))
		order.external_status = "DELIVERED"
		self.assertIn("Cancel the Shopify order", outbound.platform_instruction(frappe._dict(platform="Shopify")))
		self.assertIn(
			"already cancelled",
			outbound.platform_instruction(frappe._dict(platform="Shopify", cancelled_at="2026-10-24")),
		)
		self.assertIn("Noon", outbound.platform_instruction(frappe._dict(platform="Noon")))
		order.outbound_cancelled_on = "2026-10-08"
		self.assertIn("already asked", outbound.platform_instruction(order))

	# -- API permissions -----------------------------------------------------

	def _deny(self, doctype_name, denied_ptype):
		"""Deny one permission type on one doctype; every other check is the real one."""
		real = Document.has_permission

		def has_permission(doc, permtype="read", **kwargs):
			if doc.doctype == doctype_name and permtype == denied_ptype:
				return False
			return real(doc, permtype, **kwargs)

		return patch.object(Document, "has_permission", has_permission)

	def test_api_request_requires_submit_on_the_sales_order_and_write_on_the_marketplace_order(self):
		order = self._linked_order("RECEIVED")

		for doctype_name, ptype in (("Sales Order", "submit"), ("Marketplace Order", "write")):
			with self.subTest(doctype=doctype_name, ptype=ptype):
				with (
					self._deny(doctype_name, ptype),
					patch(SEND_TARGET) as send,
					patch.object(outbound, "request_platform_cancel") as service,
				):
					with self.assertRaises(frappe.PermissionError):
						outbound_api.request_platform_cancel(self.fixture.name, "TOO_BUSY")
				send.assert_not_called()
				service.assert_not_called()
				self.assertIsNone(self._marker(order))

	def test_api_option_requires_read_on_both_documents(self):
		self._linked_order("RECEIVED")

		for doctype_name in ("Sales Order", "Marketplace Order"):
			with self.subTest(doctype=doctype_name), self._deny(doctype_name, "read"):
				with self.assertRaises(frappe.PermissionError):
					outbound_api.get_platform_cancel_option(self.fixture.name)

	def test_api_request_delegates_for_an_authorized_user(self):
		self._linked_order("RECEIVED")

		with patch.object(outbound, "request_platform_cancel", return_value={"ok": True}) as service:
			result = outbound_api.request_platform_cancel(self.fixture.name, "TOO_BUSY")

		self.assertEqual(result, {"ok": True})
		service.assert_called_once_with(self.fixture.name, "TOO_BUSY")

	def test_api_methods_are_whitelisted_for_post(self):
		for method in (outbound_api.get_platform_cancel_option, outbound_api.request_platform_cancel):
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["POST"])
