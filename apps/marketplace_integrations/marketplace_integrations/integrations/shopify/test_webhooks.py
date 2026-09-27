# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from marketplace_integrations.integrations.shopify.webhooks import (
	MAX_WEBHOOK_DELIVERY_ATTEMPTS,
	STUCK_RECEIVED_THRESHOLD_MINUTES,
	process_shopify_webhook,
	retry_errored_webhook_deliveries,
	sync_webhook_subscriptions,
	verify_shopify_hmac,
)

EXECUTE_TARGET = "marketplace_integrations.integrations.shopify.client.ShopifyClient.execute"


def make_connection(connection_name, **kwargs):
	# shop_domain defaults to a call-unique value - it carries a DB-level
	# unique: 1 constraint, and IntegrationTestCase only rolls back once at
	# class teardown (see docs/frappe-conventions.md's Tests section), so a
	# fixed literal default would collide across this class's test methods.
	doc = {
		"doctype": "Shopify Connection",
		"connection_name": connection_name,
		"shop_domain": f"test-shop-{frappe.generate_hash(length=8)}.myshopify.com",
		"client_id": "test-client-id",
		"client_secret": "test-client-secret",
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


class UnitTestVerifyShopifyHmac(UnitTestCase):
	def test_correct_hmac_matches(self):
		import base64
		import hashlib
		import hmac

		body = b'{"id":1}'
		secret = "shhh"
		digest = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
		self.assertTrue(verify_shopify_hmac(body, secret, digest))

	def test_wrong_hmac_is_rejected(self):
		self.assertFalse(verify_shopify_hmac(b'{"id":1}', "shhh", "not-a-real-digest"))

	def test_missing_secret_or_header_is_rejected(self):
		self.assertFalse(verify_shopify_hmac(b'{"id":1}', "", "some-digest"))
		self.assertFalse(verify_shopify_hmac(b'{"id":1}', "shhh", ""))
		self.assertFalse(verify_shopify_hmac(b"", "shhh", "some-digest"))

	def test_current_secret_matches_without_previous_secret(self):
		import base64
		import hashlib
		import hmac

		body = b'{"id":1}'
		digest = base64.b64encode(
			hmac.new(b"current", body, hashlib.sha256).digest()
		).decode()
		self.assertTrue(verify_shopify_hmac(body, "current", digest))
		self.assertTrue(verify_shopify_hmac(body, "current", digest, previous_secret=None))

	def test_previous_secret_matches_when_current_fails(self):
		import base64
		import hashlib
		import hmac

		body = b'{"id":1}'
		digest = base64.b64encode(
			hmac.new(b"old-secret", body, hashlib.sha256).digest()
		).decode()
		self.assertTrue(
			verify_shopify_hmac(body, "new-secret", digest, previous_secret="old-secret")
		)

	def test_wrong_hmac_rejected_against_both_secrets(self):
		self.assertFalse(
			verify_shopify_hmac(
				b'{"id":1}', "current", "not-a-real-digest", previous_secret="old"
			)
		)

	def test_non_ascii_provided_hmac_does_not_raise(self):
		# hmac.compare_digest() raises TypeError on non-ASCII str input;
		# provided_hmac_b64 is attacker-controlled (werkzeug decodes headers
		# as latin-1, so non-ASCII is reachable) - this must return False,
		# never raise, per verify_shopify_hmac()'s own docstring promise.
		self.assertFalse(
			verify_shopify_hmac(b'{"id":1}', "shhh", "nöt-ascii-é")
		)
		self.assertFalse(
			verify_shopify_hmac(
				b'{"id":1}', "current", "nöt-ascii", previous_secret="old"
			)
		)


class IntegrationTestSyncWebhookSubscriptions(IntegrationTestCase):
	def setUp(self):
		self.connection = make_connection(f"Sync Webhooks {frappe.generate_hash(length=8)}")

	def test_creates_only_missing_topics(self):
		callback_url = "https://example.com/api/method/marketplace_integrations.api.shopify_webhooks.webhook_event"
		existing_response = {
			"webhookSubscriptions": {
				"edges": [
					{
						"node": {
							"id": "gid://shopify/WebhookSubscription/1",
							"topic": "ORDERS_CREATE",
							"uri": callback_url,
						}
					}
				]
			}
		}
		create_response = {"webhookSubscriptionCreate": {"webhookSubscription": {"id": "x"}, "userErrors": []}}

		with (
			patch(
				"marketplace_integrations.integrations.shopify.webhooks.get_webhook_callback_url",
				return_value=callback_url,
			),
			patch(EXECUTE_TARGET, side_effect=[existing_response] + [create_response] * 6) as execute,
		):
			result = sync_webhook_subscriptions(self.connection)

		# 1 query + 6 creates (7 topics minus the 1 already registered)
		self.assertEqual(execute.call_count, 7)
		self.assertEqual(result["status"], "Complete")
		self.assertEqual(result["missing_topics"], [])
		self.connection.reload()
		self.assertEqual(self.connection.webhook_registration_status, "Complete")
		self.assertEqual(self.connection.webhook_missing_topics, "")

	def test_running_twice_creates_nothing_the_second_time(self):
		callback_url = "https://example.com/api/method/marketplace_integrations.api.shopify_webhooks.webhook_event"
		all_registered_response = {
			"webhookSubscriptions": {
				"edges": [
					{"node": {"id": f"gid://shopify/WebhookSubscription/{i}", "topic": t, "uri": callback_url}}
					for i, t in enumerate(
						[
							"ORDERS_CREATE",
							"ORDERS_UPDATED",
							"ORDERS_CANCELLED",
							"ORDERS_PAID",
							"REFUNDS_CREATE",
							"APP_UNINSTALLED",
							"APP_SCOPES_UPDATE",
						]
					)
				]
			}
		}
		with (
			patch(
				"marketplace_integrations.integrations.shopify.webhooks.get_webhook_callback_url",
				return_value=callback_url,
			),
			patch(EXECUTE_TARGET, return_value=all_registered_response) as execute,
		):
			result = sync_webhook_subscriptions(self.connection)

		# Only the inspection query ran - nothing was missing, so no
		# webhookSubscriptionCreate mutation should fire.
		self.assertEqual(execute.call_count, 1)
		self.assertEqual(result["status"], "Complete")

	def test_user_errors_mark_status_error_and_still_report_progress(self):
		callback_url = "https://example.com/webhook"
		empty_response = {"webhookSubscriptions": {"edges": []}}
		failing_create = {
			"webhookSubscriptionCreate": {
				"webhookSubscription": None,
				"userErrors": [{"field": ["topic"], "message": "already exists"}],
			}
		}
		with (
			patch(
				"marketplace_integrations.integrations.shopify.webhooks.get_webhook_callback_url",
				return_value=callback_url,
			),
			patch(EXECUTE_TARGET, side_effect=[empty_response] + [failing_create] * 7),
		):
			result = sync_webhook_subscriptions(self.connection)

		self.assertEqual(result["status"], "Error")
		self.assertEqual(len(result["missing_topics"]), 7)
		self.connection.reload()
		self.assertEqual(self.connection.webhook_registration_status, "Error")

	def test_exception_during_sync_still_persists_error_status(self):
		# db_set() defaults to commit=False, and Frappe's request handler
		# rolls back the whole transaction on any exception escaping the
		# request (the frappe.throw() at the end of the except block) -
		# without commit=True on these specific db_set() calls, none of
		# them would survive and the form would show stale status after a
		# real failure.
		with (
			patch(
				"marketplace_integrations.integrations.shopify.webhooks.get_webhook_callback_url",
				return_value="https://example.com/webhook",
			),
			patch(EXECUTE_TARGET, side_effect=RuntimeError("boom: shopify unreachable")),
		):
			with self.assertRaises(frappe.ValidationError):
				sync_webhook_subscriptions(self.connection)

		self.connection.reload()
		self.assertEqual(self.connection.webhook_registration_status, "Error")
		self.assertIn("boom", self.connection.last_webhook_error)

	def test_stale_uri_subscription_is_deleted_and_recreated_at_new_url(self):
		old_url = "https://old-tunnel.example.com/webhook"
		new_url = "https://new-tunnel.example.com/webhook"
		existing_response = {
			"webhookSubscriptions": {
				"edges": [
					{
						"node": {
							"id": "gid://shopify/WebhookSubscription/stale-1",
							"topic": "ORDERS_CREATE",
							"uri": old_url,
						}
					}
				]
			}
		}
		delete_response = {
			"webhookSubscriptionDelete": {
				"deletedWebhookSubscriptionId": "gid://shopify/WebhookSubscription/stale-1",
				"userErrors": [],
			}
		}
		create_response = {
			"webhookSubscriptionCreate": {"webhookSubscription": {"id": "x"}, "userErrors": []}
		}

		with (
			patch(
				"marketplace_integrations.integrations.shopify.webhooks.get_webhook_callback_url",
				return_value=new_url,
			),
			patch(
				EXECUTE_TARGET,
				side_effect=[existing_response, delete_response] + [create_response] * 7,
			) as execute,
		):
			result = sync_webhook_subscriptions(self.connection)

		# 1 inspection query + 1 delete (the stale ORDERS_CREATE sub at
		# old_url) + 7 creates (all topics are "missing" at new_url, since
		# the only prior ORDERS_CREATE registration was stale and just
		# deleted).
		self.assertEqual(execute.call_count, 9)
		self.assertEqual(result["status"], "Complete")
		self.assertEqual(result["missing_topics"], [])

	def test_failed_delete_marks_status_error_even_when_nothing_is_missing(self):
		# Second review pass finding #1: a failed webhookSubscriptionDelete
		# (stale-subscription cleanup) must mark status "Error" even when
		# every topic's create-pass still succeeds and nothing ends up
		# "missing" - otherwise a dead stale subscription stays live and
		# double-delivering behind a green "Complete" status.
		old_url = "https://old-tunnel.example.com/webhook"
		new_url = "https://new-tunnel.example.com/webhook"
		existing_response = {
			"webhookSubscriptions": {
				"edges": [
					{
						"node": {
							"id": "gid://shopify/WebhookSubscription/stale-1",
							"topic": "ORDERS_CREATE",
							"uri": old_url,
						}
					}
				]
			}
		}
		failing_delete_response = {
			"webhookSubscriptionDelete": {
				"deletedWebhookSubscriptionId": None,
				"userErrors": [{"field": ["id"], "message": "could not delete"}],
			}
		}
		create_response = {
			"webhookSubscriptionCreate": {"webhookSubscription": {"id": "x"}, "userErrors": []}
		}

		with (
			patch(
				"marketplace_integrations.integrations.shopify.webhooks.get_webhook_callback_url",
				return_value=new_url,
			),
			patch(
				EXECUTE_TARGET,
				side_effect=[existing_response, failing_delete_response] + [create_response] * 7,
			),
		):
			result = sync_webhook_subscriptions(self.connection)

		self.assertEqual(result["status"], "Error")
		self.assertEqual(result["missing_topics"], [])
		self.assertTrue(result["errors"])
		self.connection.reload()
		self.assertEqual(self.connection.webhook_registration_status, "Error")


class IntegrationTestProcessShopifyWebhook(IntegrationTestCase):
	def setUp(self):
		self.connection = make_connection(f"Process Webhook {frappe.generate_hash(length=8)}")

	def test_duplicate_webhook_id_is_harmless(self):
		process_shopify_webhook(self.connection.name, "ORDERS_CREATE", "webhook-dup-1", "test-shop.myshopify.com")
		# A second delivery of the same webhook_id must not raise and must not
		# create a second Shopify Webhook Delivery record.
		process_shopify_webhook(self.connection.name, "ORDERS_CREATE", "webhook-dup-1", "test-shop.myshopify.com")

		count = frappe.db.count("Shopify Webhook Delivery", {"webhook_id": "webhook-dup-1"})
		self.assertEqual(count, 1)

	def test_non_uninstall_topic_is_recorded_processed(self):
		# ORDERS_CREATE is an order topic (#109 decision 4) - it requires an
		# order_gid; account_for_connection() resolves to None here (no
		# Marketplace Account exists for this connection), so this is still a
		# silent Processed no-op end-to-end, same as before #109's order
		# handling existed.
		process_shopify_webhook(
			self.connection.name,
			"ORDERS_CREATE",
			"webhook-order-1",
			"test-shop.myshopify.com",
			order_gid="gid://shopify/Order/1",
		)

		delivery = frappe.get_doc(
			"Shopify Webhook Delivery", {"webhook_id": "webhook-order-1"}
		)
		self.assertEqual(delivery.status, "Processed")
		self.assertEqual(delivery.topic, "ORDERS_CREATE")

		self.connection.reload()
		self.assertTrue(self.connection.enabled)
		self.assertEqual(self.connection.last_webhook_delivery_topic, "ORDERS_CREATE")
		self.assertTrue(self.connection.last_webhook_processed_at)

	def test_app_uninstalled_disables_connection_idempotently(self):
		process_shopify_webhook(self.connection.name, "APP_UNINSTALLED", "webhook-uninstall-1", "test-shop.myshopify.com")

		self.connection.reload()
		self.assertFalse(self.connection.enabled)

		# A second, distinct delivery (different webhook_id, same effective
		# event) must be a safe no-op against an already-disabled connection.
		process_shopify_webhook(self.connection.name, "APP_UNINSTALLED", "webhook-uninstall-2", "test-shop.myshopify.com")
		self.connection.reload()
		self.assertFalse(self.connection.enabled)

	def test_previously_errored_delivery_is_reprocessed_on_retry(self):
		# Second review pass finding #2: a duplicate webhook_id must not be a
		# permanent no-op when the existing row never reached "Processed" -
		# otherwise a delivery that errored once could never be reprocessed
		# by any of Shopify's own retries of that same event.
		import marketplace_integrations.integrations.shopify.webhooks as webhooks_module

		call_count = {"n": 0}

		def flaky_handle_topic(connection, topic, order_gid=None):
			call_count["n"] += 1
			if call_count["n"] == 1:
				raise RuntimeError("boom: simulated failure")

		with patch.object(webhooks_module, "_handle_topic", side_effect=flaky_handle_topic):
			process_shopify_webhook(
				self.connection.name, "ORDERS_CREATE", "webhook-retry-1", "test-shop.myshopify.com"
			)

		delivery = frappe.get_doc("Shopify Webhook Delivery", {"webhook_id": "webhook-retry-1"})
		self.assertEqual(delivery.status, "Error")

		with patch.object(webhooks_module, "_handle_topic", side_effect=flaky_handle_topic):
			process_shopify_webhook(
				self.connection.name, "ORDERS_CREATE", "webhook-retry-1", "test-shop.myshopify.com"
			)

		self.assertEqual(call_count["n"], 2)
		# Still exactly one row - the unique index guarantees this - but its
		# status has moved on to "Processed" via the retry, not a second row.
		count = frappe.db.count("Shopify Webhook Delivery", {"webhook_id": "webhook-retry-1"})
		self.assertEqual(count, 1)
		delivery.reload()
		self.assertEqual(delivery.status, "Processed")

	def test_previously_processed_delivery_is_not_reprocessed_on_retry(self):
		import marketplace_integrations.integrations.shopify.webhooks as webhooks_module

		with patch.object(webhooks_module, "_handle_topic", return_value=None) as mocked_handle_topic:
			process_shopify_webhook(
				self.connection.name, "ORDERS_CREATE", "webhook-processed-1", "test-shop.myshopify.com"
			)
			process_shopify_webhook(
				self.connection.name, "ORDERS_CREATE", "webhook-processed-1", "test-shop.myshopify.com"
			)

		self.assertEqual(mocked_handle_topic.call_count, 1)

	def test_reprocess_does_not_restamp_last_webhook_delivery_at(self):
		# Per explicit product decision (review pass on PR #117):
		# last_webhook_delivery_at/last_webhook_delivery_topic mean "has
		# Shopify stopped delivering to us" - they must only ever be stamped
		# from the delivery's own received_at/topic on the ORIGINAL
		# processing attempt, never re-stamped with the current time on a
		# reprocess/retry (that would make a stale delivery falsely look
		# fresh). This reverses an earlier version of this test that
		# asserted the opposite.
		import marketplace_integrations.integrations.shopify.webhooks as webhooks_module

		call_count = {"n": 0}

		def flaky_handle_topic(connection, topic, order_gid=None):
			call_count["n"] += 1
			if call_count["n"] == 1:
				raise RuntimeError("boom: simulated failure")

		original_received_at = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-3)
		with patch.object(webhooks_module, "now_datetime", return_value=original_received_at):
			with patch.object(webhooks_module, "_handle_topic", side_effect=flaky_handle_topic):
				process_shopify_webhook(
					self.connection.name, "ORDERS_CREATE", "webhook-ts-1", "test-shop.myshopify.com"
				)

		delivery = frappe.get_doc("Shopify Webhook Delivery", {"webhook_id": "webhook-ts-1"})
		self.assertEqual(delivery.received_at, original_received_at)

		self.connection.reload()
		# Original attempt: last_webhook_delivery_at was stamped from the
		# delivery's own received_at.
		self.assertEqual(self.connection.last_webhook_delivery_at, original_received_at)

		with patch.object(webhooks_module, "_handle_topic", side_effect=flaky_handle_topic):
			process_shopify_webhook(
				self.connection.name, "ORDERS_CREATE", "webhook-ts-1", "test-shop.myshopify.com"
			)

		delivery.reload()
		self.connection.reload()
		# received_at is untouched by the reprocess - still the original time.
		self.assertEqual(delivery.received_at, original_received_at)
		# last_webhook_delivery_at must NOT have moved - the reprocess is not
		# an original delivery.
		self.assertEqual(self.connection.last_webhook_delivery_at, original_received_at)
		# The reprocess is still tracked via last_webhook_processed_at, which
		# does update on every attempt.
		self.assertTrue(self.connection.last_webhook_processed_at)


def make_delivery(connection, webhook_id, **kwargs):
	doc = {
		"doctype": "Shopify Webhook Delivery",
		"connection": connection.name,
		"shop_domain": connection.shop_domain,
		"topic": "ORDERS_CREATE",
		"webhook_id": webhook_id,
		"received_at": frappe.utils.now_datetime(),
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


class IntegrationTestRetryErroredWebhookDeliveries(IntegrationTestCase):
	def setUp(self):
		self.connection = make_connection(f"Retry Sweep {frappe.generate_hash(length=8)}")

	@staticmethod
	def _calls_for(mocked_handle_topic, connection_name):
		# The sweep runs against the whole site, so other status="Error" (or
		# stuck "Received") rows left over from other tests/dev-DB state can
		# legitimately also get processed in the same call - see
		# docs/frappe-conventions.md's "IntegrationTestCase only rolls back
		# once at class teardown" gotcha. Scope assertions to only the calls
		# this test's own connection triggered, rather than a raw call count.
		return [c for c in mocked_handle_topic.call_args_list if c.args[0].name == connection_name]

	def test_sweep_reprocesses_an_errored_delivery(self):
		delivery = make_delivery(
			self.connection, f"webhook-sweep-error-{frappe.generate_hash(length=6)}", status="Error"
		)

		import marketplace_integrations.integrations.shopify.webhooks as webhooks_module

		with patch.object(webhooks_module, "_handle_topic", return_value=None) as mocked_handle_topic:
			retry_errored_webhook_deliveries()

		self.assertEqual(len(self._calls_for(mocked_handle_topic, self.connection.name)), 1)
		delivery.reload()
		self.assertEqual(delivery.status, "Processed")
		self.assertEqual(delivery.attempt_count, 1)

	def test_sweep_skips_a_processed_delivery(self):
		delivery = make_delivery(
			self.connection, f"webhook-sweep-processed-{frappe.generate_hash(length=6)}", status="Processed"
		)

		import marketplace_integrations.integrations.shopify.webhooks as webhooks_module

		with patch.object(webhooks_module, "_handle_topic", return_value=None) as mocked_handle_topic:
			retry_errored_webhook_deliveries()

		self.assertEqual(self._calls_for(mocked_handle_topic, self.connection.name), [])
		delivery.reload()
		self.assertEqual(delivery.status, "Processed")
		self.assertEqual(delivery.attempt_count, 0)

	def test_sweep_respects_the_max_attempt_bound(self):
		delivery = make_delivery(
			self.connection,
			f"webhook-sweep-exhausted-{frappe.generate_hash(length=6)}",
			status="Error",
			attempt_count=MAX_WEBHOOK_DELIVERY_ATTEMPTS,
		)

		import marketplace_integrations.integrations.shopify.webhooks as webhooks_module

		with patch.object(webhooks_module, "_handle_topic", return_value=None) as mocked_handle_topic:
			retry_errored_webhook_deliveries()

		# The sweep's own query filter already excludes an exhausted row, and
		# _process_delivery()'s own cap would no-op it even if that filter
		# somehow didn't - neither path should ever call _handle_topic again.
		self.assertEqual(self._calls_for(mocked_handle_topic, self.connection.name), [])
		delivery.reload()
		self.assertEqual(delivery.status, "Error")
		self.assertEqual(delivery.attempt_count, MAX_WEBHOOK_DELIVERY_ATTEMPTS)

	def test_sweep_recovers_a_stuck_received_row_past_the_threshold(self):
		old_creation = frappe.utils.add_to_date(
			frappe.utils.now_datetime(), minutes=-(STUCK_RECEIVED_THRESHOLD_MINUTES + 5)
		)
		stuck = make_delivery(
			self.connection,
			f"webhook-sweep-stuck-{frappe.generate_hash(length=6)}",
			status="Received",
		)
		frappe.db.set_value(
			"Shopify Webhook Delivery", stuck.name, "creation", old_creation, update_modified=False
		)
		frappe.clear_document_cache("Shopify Webhook Delivery", stuck.name)

		fresh = make_delivery(
			self.connection,
			f"webhook-sweep-fresh-{frappe.generate_hash(length=6)}",
			status="Received",
		)

		import marketplace_integrations.integrations.shopify.webhooks as webhooks_module

		with patch.object(webhooks_module, "_handle_topic", return_value=None) as mocked_handle_topic:
			retry_errored_webhook_deliveries()

		# Only the old stuck row was picked up - a fresh "Received" row could
		# still be actively mid-processing by a worker right now and must not
		# be raced.
		self.assertEqual(len(self._calls_for(mocked_handle_topic, self.connection.name)), 1)
		stuck.reload()
		self.assertEqual(stuck.status, "Processed")
		fresh.reload()
		self.assertEqual(fresh.status, "Received")
		self.assertEqual(fresh.attempt_count, 0)

	def test_poison_row_does_not_block_other_rows_in_the_same_sweep(self):
		# Savepoint isolation: one row whose processing raises must not roll
		# back another row already successfully processed earlier in the
		# same sweep run.
		good = make_delivery(
			self.connection, f"webhook-sweep-good-{frappe.generate_hash(length=6)}", status="Error"
		)
		poison = make_delivery(
			self.connection, f"webhook-sweep-poison-{frappe.generate_hash(length=6)}", status="Error"
		)

		import marketplace_integrations.integrations.shopify.webhooks as webhooks_module

		# Site-wide leftover rows from other tests/dev-DB state can also be
		# picked up by the same sweep run (see _calls_for()'s docstring
		# note) - scope the flaky behavior to only this test's own
		# connection so ordering assumptions below stay deterministic
		# regardless of what else the sweep happens to touch.
		call_order = []

		def flaky(connection, topic, order_gid=None):
			if connection.name != self.connection.name:
				return
			call_order.append(connection.name)
			if len(call_order) == 1:
				return
			raise RuntimeError("boom: poison row")

		with patch.object(webhooks_module, "_handle_topic", side_effect=flaky):
			retry_errored_webhook_deliveries()

		good.reload()
		poison.reload()
		# Both rows were attempted (creation-ascending order), and whichever
		# one failed did not roll back the other's already-committed success.
		statuses = {good.name: good.status, poison.name: poison.status}
		self.assertIn("Processed", statuses.values())
		self.assertIn("Error", statuses.values())
		self.assertEqual(good.attempt_count, 1)
		self.assertEqual(poison.attempt_count, 1)


class UnitTestProcessDeliveryAtomicIncrement(IntegrationTestCase):
	def test_attempt_count_increment_is_atomic_under_a_simulated_race(self):
		# Simulates two concurrent callers (e.g. the sweep and a simultaneous
		# Shopify redelivery) both trying to process the same already-
		# exhausted-minus-one delivery: only one of the two atomic UPDATEs
		# should succeed in incrementing attempt_count, the other must bail
		# out cleanly without double-counting or calling _handle_topic.
		import marketplace_integrations.integrations.shopify.webhooks as webhooks_module

		connection = make_connection(f"Atomic Increment {frappe.generate_hash(length=8)}")
		delivery = make_delivery(
			connection,
			f"webhook-race-{frappe.generate_hash(length=6)}",
			status="Error",
			attempt_count=MAX_WEBHOOK_DELIVERY_ATTEMPTS - 1,
		)

		# Simulate the "loser" of the race by pre-incrementing attempt_count
		# on the DB directly (as if a concurrent caller's atomic UPDATE won
		# first), while this in-memory `delivery` object still holds the
		# pre-race value.
		frappe.db.set_value(
			"Shopify Webhook Delivery",
			delivery.name,
			"attempt_count",
			MAX_WEBHOOK_DELIVERY_ATTEMPTS,
			update_modified=False,
		)

		with patch.object(webhooks_module, "_handle_topic", return_value=None) as mocked_handle_topic:
			webhooks_module._process_delivery(delivery, connection)

		mocked_handle_topic.assert_not_called()
		delivery.reload()
		# The loser's own atomic UPDATE found attempt_count already at the
		# cap (WHERE attempt_count < %s matched 0 rows) and did not
		# increment further - still exactly at the cap, not over it.
		self.assertEqual(delivery.attempt_count, MAX_WEBHOOK_DELIVERY_ATTEMPTS)
