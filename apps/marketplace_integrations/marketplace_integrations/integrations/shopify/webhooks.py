# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Shopify webhook subscription management and asynchronous delivery
processing (#104).

Scope, per the issue body: register/inspect/repair subscriptions for
``SHOPIFY_WEBHOOK_TOPICS``, verify each delivery's HMAC, deduplicate by
``X-Shopify-Webhook-Id`` (via ``Shopify Webhook Delivery``'s unique
``webhook_id``), and safely disable the connection on ``APP_UNINSTALLED``.
Canonical order retrieval and ERPNext Sales Order/document creation from
webhook payloads are explicitly out of scope - every other topic is
acknowledged and recorded only. See
docs/marketplace-integrations/architecture.md and docs/shopify/README.md's
"Webhooks (for #104)" section.

Because ``webhook_event()`` (api/shopify_webhooks.py) always responds 200 as
soon as processing is enqueued - correct, "respond promptly" - Shopify never
retries a delivery whose ASYNC processing later fails. A scheduled sweep,
``retry_errored_webhook_deliveries()``, gives an errored delivery an
automatic path back to "Processed" without waiting on a Shopify-initiated
redelivery; see its own docstring and ``_process_delivery()``'s.

The GraphQL field/argument names below were verified against
https://shopify.dev/docs/api/admin-graphql/latest (input-objects/
WebhookSubscriptionInput, enums/WebhookSubscriptionTopic) on 2026-09-19:
``WebhookSubscriptionInput.uri`` is the current endpoint field -
``callbackUrl`` is documented as deprecated on that same input object (still
present as a legacy alias, but not used here).
"""

import hashlib
import hmac as hmac_lib
from base64 import b64encode

import frappe
from frappe.utils import cstr, escape_html, now_datetime

from marketplace_integrations.integrations.http import truncate_response_text
from marketplace_integrations.integrations.shopify.client import ShopifyClient
from marketplace_integrations.integrations.shopify.constants import (
	SHOPIFY_WEBHOOK_PATH,
	SHOPIFY_WEBHOOK_TOPICS,
)

_WEBHOOK_SUBSCRIPTIONS_QUERY = """
query webhookSubscriptions($first: Int!, $topics: [WebhookSubscriptionTopic!]) {
  webhookSubscriptions(first: $first, topics: $topics) {
    edges {
      node {
        id
        topic
        uri
      }
    }
  }
}
"""

_WEBHOOK_SUBSCRIPTION_CREATE_MUTATION = """
mutation webhookSubscriptionCreate($topic: WebhookSubscriptionTopic!, $webhookSubscription: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $webhookSubscription) {
    webhookSubscription {
      id
      topic
    }
    userErrors {
      field
      message
    }
  }
}
"""

# webhookSubscriptionDelete's shape below was verified against
# https://shopify.dev/docs/api/admin-graphql/latest/mutations/
# webhookSubscriptionDelete on 2026-09-19: a single `id: ID!` argument,
# `deletedWebhookSubscriptionId`/`userErrors` on the payload.
_WEBHOOK_SUBSCRIPTION_DELETE_MUTATION = """
mutation webhookSubscriptionDelete($id: ID!) {
  webhookSubscriptionDelete(id: $id) {
    deletedWebhookSubscriptionId
    userErrors {
      field
      message
    }
  }
}
"""


def get_webhook_callback_url():
	"""Build the public webhook endpoint from the configured site URL.

	``allow_header_override=False`` mirrors Talabat Connection's webhook URL
	construction - a request Host header must never become persisted
	configuration.
	"""
	return frappe.utils.get_url(SHOPIFY_WEBHOOK_PATH, allow_header_override=False)


def verify_shopify_hmac(raw_body, secret, provided_hmac_b64, previous_secret=None):
	"""Verify a webhook delivery's ``X-Shopify-Hmac-Sha256`` header against
	the RAW request body, per docs/shopify/README.md: ``HMAC-SHA256(raw_body,
	client_secret)``, base64-encoded. Must run before the body is parsed as
	JSON - see api/shopify_webhooks.py::webhook_event().

	Checks ``secret`` (the connection's current ``client_secret``) first. If
	that fails to match and ``previous_secret`` is set, also checks against
	it - during a secret-rotation window Shopify signs deliveries with the
	app's oldest unrevoked secret, not necessarily the current one (see
	docs/shopify/README.md). The delivery is accepted if either matches.

	Returns False (never raises) on a missing header/body, or when neither
	secret matches, so the caller can fail closed uniformly.
	"""
	if not provided_hmac_b64 or not raw_body:
		return False
	if secret and _hmac_matches(raw_body, secret, provided_hmac_b64):
		return True
	if previous_secret and _hmac_matches(raw_body, previous_secret, provided_hmac_b64):
		return True
	return False


def _hmac_matches(raw_body, secret, provided_hmac_b64):
	digest = hmac_lib.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
	computed = b64encode(digest).decode("utf-8")
	# compare_digest() on two str values raises TypeError for non-ASCII
	# input - provided_hmac_b64 is attacker-controlled (werkzeug decodes
	# headers as latin-1, so non-ASCII is reachable here), so both sides must
	# be encoded to bytes first to keep this function's "never raises"
	# promise. Mirrors api/talabat_webhooks.py's cstr(x).encode() pattern.
	return hmac_lib.compare_digest(cstr(computed).encode(), cstr(provided_hmac_b64).encode())


def sync_webhook_subscriptions(connection):
	"""Inspect this connection's current webhook subscriptions, repair any
	stale ones left at an old callback URL, and create any of
	SHOPIFY_WEBHOOK_TOPICS still missing at our current callback URL.

	A subscription for one of SHOPIFY_WEBHOOK_TOPICS registered at a
	DIFFERENT uri than our current callback URL (e.g. after a dev tunnel URL
	changed, or the site's host_name changed) is deleted via
	webhookSubscriptionDelete before the missing-topic pass runs - otherwise
	it stays invisible to the "already registered" check, a duplicate
	subscription gets created at the new URL, and the stale one keeps
	receiving deliveries forever (double-delivery, dead endpoint).

	Idempotent by construction: existing subscriptions (at any uri) are
	always read first, so calling this twice in a row only ever
	deletes/creates what's still stale/missing after the first call - a
	topic already registered at our current callback URL, with no stale
	sibling left over, is never touched again. A mutating call
	(webhookSubscriptionCreate/webhookSubscriptionDelete), so this must only
	ever run from an explicit user-triggered action (Shopify Connection's
	"Sync Webhook Subscriptions" button), never automatically - unlike
	#103's read-only live checks.
	"""
	client = ShopifyClient(connection.name)
	callback_url = get_webhook_callback_url()

	try:
		by_topic = _fetch_topic_subscriptions(client)
		errors = []

		for topic, subscriptions in by_topic.items():
			for subscription in subscriptions:
				if subscription.get("uri") == callback_url or not subscription.get("id"):
					continue
				user_errors = _delete_subscription(client, subscription["id"])
				if user_errors:
					errors.append(f"{topic} (stale subscription): {_format_user_errors(user_errors)}")

		current_topics = {
			topic
			for topic, subscriptions in by_topic.items()
			if any(sub.get("uri") == callback_url for sub in subscriptions)
		}
		missing = [topic for topic in SHOPIFY_WEBHOOK_TOPICS if topic not in current_topics]
		for topic in list(missing):
			user_errors = _create_subscription(client, topic, callback_url)
			if user_errors:
				errors.append(f"{topic}: {_format_user_errors(user_errors)}")
			else:
				missing.remove(topic)
	except Exception as e:
		error_message = truncate_response_text(str(e))
		# commit=True: db_set() defaults to commit=False, and Frappe's
		# request handler rolls back the whole transaction on any exception
		# escaping the request (the frappe.throw() below) - without an
		# explicit commit here, none of these health-status writes would
		# survive and the form would keep showing stale/previous status
		# after a real failure.
		connection.db_set("webhook_registration_status", "Error", update_modified=False, commit=True)
		connection.db_set("webhook_missing_topics", "", update_modified=False, commit=True)
		connection.db_set("webhook_last_checked_on", now_datetime(), update_modified=False, commit=True)
		connection.db_set("last_webhook_error", error_message, update_modified=False, commit=True)
		frappe.log_error(
			title="Shopify webhook subscription sync failed", message=frappe.get_traceback()
		)
		# escape_html(): error_message is an external string (Shopify/HTTP
		# error text) rendered into a msgprint, an HTML sink - see
		# docs/frappe-conventions.md's XSS-sink guidance.
		frappe.throw(
			frappe._("Failed to sync Shopify webhook subscriptions: {0}").format(
				escape_html(error_message)
			)
		)

	# errors must win regardless of whether anything is still "missing" - a
	# failed webhookSubscriptionDelete (stale-subscription cleanup) can leave
	# every topic's create-pass looking complete while a stale subscription
	# is still live and double-delivering; that must never read as "Complete".
	status = "Error" if errors else ("Complete" if not missing else "Incomplete")
	connection.db_set("webhook_registration_status", status, update_modified=False)
	connection.db_set("webhook_missing_topics", ", ".join(missing), update_modified=False)
	connection.db_set("webhook_last_checked_on", now_datetime(), update_modified=False)
	connection.db_set("last_webhook_error", "; ".join(errors), update_modified=False)
	return {"status": status, "missing_topics": missing, "errors": errors}


def _fetch_topic_subscriptions(client):
	"""Return ``{topic: [{"id": ..., "uri": ...}, ...]}`` for every currently
	registered subscription among SHOPIFY_WEBHOOK_TOPICS, at ANY uri - the
	caller partitions each topic's entries into "at our current callback
	URL" vs "stale, at some other URL". Bounded to a single page (first:
	50) - at most 7 topics can ever match the ``topics`` filter, so no
	pagination helper is needed here, same rationale as #103's single-page
	Location fetch."""
	data = client.execute(
		_WEBHOOK_SUBSCRIPTIONS_QUERY, {"first": 50, "topics": list(SHOPIFY_WEBHOOK_TOPICS)}
	)
	edges = ((data.get("webhookSubscriptions") or {}).get("edges")) or []
	by_topic = {}
	for edge in edges:
		node = edge.get("node") or {}
		topic = node.get("topic")
		if not topic:
			continue
		by_topic.setdefault(topic, []).append({"id": node.get("id"), "uri": node.get("uri")})
	return by_topic


def _create_subscription(client, topic, callback_url):
	data = client.execute(
		_WEBHOOK_SUBSCRIPTION_CREATE_MUTATION,
		{"topic": topic, "webhookSubscription": {"uri": callback_url, "format": "JSON"}},
	)
	result = data.get("webhookSubscriptionCreate") or {}
	return result.get("userErrors") or []


def _delete_subscription(client, subscription_id):
	data = client.execute(_WEBHOOK_SUBSCRIPTION_DELETE_MUTATION, {"id": subscription_id})
	result = data.get("webhookSubscriptionDelete") or {}
	return result.get("userErrors") or []


def _format_user_errors(user_errors):
	messages = [ue.get("message", "") for ue in user_errors if isinstance(ue, dict)]
	return "; ".join(m for m in messages if m) or "unknown error"


# A simple attempt-count cutoff, not a generic backoff/retry framework - see
# _process_delivery()'s docstring. Shared by both the duplicate-webhook_id
# reprocess path and the scheduled retry sweep below.
MAX_WEBHOOK_DELIVERY_ATTEMPTS = 5


def process_shopify_webhook(connection_name, topic, webhook_id, shop_domain):
	"""Idempotently persist and process one authenticated webhook delivery.

	The public webhook request (api/shopify_webhooks.py::webhook_event()) is
	handled as Guest and has already verified the HMAC signature before this
	job is enqueued. This worker switches to Frappe's system user because the
	trusted background worker must not inherit Guest's document permissions
	(same pattern as Talabat's process_order_event()).

	Deduplication is enforced by Shopify Webhook Delivery.webhook_id's DB-
	level unique index, not a check-then-insert read - see
	docs/frappe-conventions.md's concurrency guidance. A duplicate delivery
	(Shopify's at-least-once guarantee) hits UniqueValidationError on insert
	(the unique *field* constraint on webhook_id, not a duplicate *name* -
	that's DuplicateEntryError, a different exception): the unique index still
	guarantees only one Shopify Webhook Delivery ROW ever exists per
	webhook_id, but a duplicate is not always a harmless no-op. If the first
	attempt at that webhook_id already reached status="Processed", a later
	Shopify retry of the same event is genuinely redundant and is skipped. If
	the first attempt instead ended in "Error" (or never got past its initial
	"Received" default - e.g. a worker crash mid-processing), every one of
	Shopify's own retries of that webhook_id would otherwise be silently
	dropped forever - once one attempt errors, the event could never be
	reprocessed. So a duplicate whose existing row is not yet "Processed" is
	reprocessed in place, updating that same row rather than inserting a
	second one - via the same _process_delivery() the scheduled sweep
	(retry_errored_webhook_deliveries()) also uses, since a webhook whose only
	failure was e.g. a `webhook_event()` responding 200 before its ASYNC
	processing later failed (nothing left to trigger a Shopify redelivery)
	still needs a path back to "Processed".
	"""
	frappe.set_user("Administrator")

	delivery = frappe.new_doc("Shopify Webhook Delivery")
	delivery.connection = connection_name
	delivery.shop_domain = shop_domain
	delivery.topic = topic
	delivery.webhook_id = webhook_id
	delivery.received_at = now_datetime()
	try:
		delivery.insert(ignore_permissions=True)
	except frappe.UniqueValidationError:
		# show_unique_validation_message() (called from insert()'s validation
		# path before the exception is raised) appends a msgprint to
		# frappe.local.message_log - clear it so a background worker doesn't
		# leave a stray message behind for whichever request happens to read
		# the message log next. Mirrors sales_order_sync.py's frappe.throw()
		# / frappe.clear_messages() pattern for #85 (see
		# docs/marketplace-integrations/architecture.md).
		frappe.clear_messages()
		existing = frappe.get_doc("Shopify Webhook Delivery", {"webhook_id": webhook_id})
		if existing.status == "Processed":
			return
		delivery = existing

	connection = frappe.get_doc("Shopify Connection", connection_name)
	_process_delivery(delivery, connection)


def _process_delivery(delivery, connection):
	"""Process (or reprocess) one Shopify Webhook Delivery row end-to-end:
	record last-delivery bookkeeping on the connection, run the per-topic
	handler, and persist the outcome. This is the one shared implementation
	behind every caller that runs a delivery - a fresh delivery, a
	Shopify-initiated redelivery of a non-Processed row
	(process_shopify_webhook()'s UniqueValidationError handling above), and
	the scheduled retry sweep (retry_errored_webhook_deliveries()) - rather
	than duplicating this logic per caller.

	``last_webhook_delivery_at`` is always stamped with the CURRENT time
	(``now_datetime()``), not ``delivery.received_at`` - a reprocess/retry
	attempt must not make the connection's health fields look stale or move
	backwards. ``delivery.received_at`` itself is never touched here; it
	keeps reflecting the delivery's original first-received time.

	Bounded by MAX_WEBHOOK_DELIVERY_ATTEMPTS: a delivery whose
	``attempt_count`` has already reached the cap is left untouched (still
	"Error") rather than retried forever, whether the caller is Shopify's own
	redelivery or the sweep - a simple attempt-count cutoff, not a generic
	backoff/retry framework, consistent with this project's
	"don't over-engineer" rule.
	"""
	if delivery.attempt_count >= MAX_WEBHOOK_DELIVERY_ATTEMPTS:
		return

	topic = delivery.topic
	now = now_datetime()
	connection.db_set("last_webhook_delivery_at", now, update_modified=False)
	connection.db_set("last_webhook_delivery_topic", topic, update_modified=False)
	delivery.db_set("attempt_count", delivery.attempt_count + 1, update_modified=False)

	try:
		_handle_topic(connection, topic)
	except Exception as e:
		error_message = truncate_response_text(str(e))
		delivery.db_set("status", "Error", update_modified=False)
		delivery.db_set("error", error_message, update_modified=False)
		connection.db_set("last_webhook_error", error_message, update_modified=False)
		frappe.log_error(
			title=f"Shopify webhook processing failed ({topic})", message=frappe.get_traceback()
		)
		return

	processed_at = now_datetime()
	delivery.db_set("status", "Processed", update_modified=False)
	delivery.db_set("processed_at", processed_at, update_modified=False)
	connection.db_set("last_webhook_processed_at", processed_at, update_modified=False)
	connection.db_set("last_webhook_error", "", update_modified=False)


def retry_errored_webhook_deliveries():
	"""Scheduled sweep (hourly, see hooks.py's scheduler_events) for Shopify
	Webhook Delivery rows stuck at status="Error" with no Shopify-initiated
	redelivery ever going to retrigger them - e.g. an APP_UNINSTALLED
	delivery whose _disable_connection() save() failed on a lock timeout,
	leaving a connection that should have been disabled still enabled
	indefinitely, since `webhook_event()` already responded 200 before that
	ASYNC failure happened and Shopify has nothing telling it to redeliver.

	Reuses _process_delivery() - the exact same reprocess logic the
	duplicate-delivery path above uses - rather than a second copy of it
	here. The MAX_WEBHOOK_DELIVERY_ATTEMPTS cap is enforced inside
	_process_delivery() itself, so this sweep's own filter
	(``attempt_count < MAX_WEBHOOK_DELIVERY_ATTEMPTS``) is a query-level
	optimization to skip exhausted rows outright, not a second place the cap
	is decided.
	"""
	errored = frappe.get_all(
		"Shopify Webhook Delivery",
		filters={"status": "Error", "attempt_count": ["<", MAX_WEBHOOK_DELIVERY_ATTEMPTS]},
		fields=["name"],
	)
	for row in errored:
		delivery = frappe.get_doc("Shopify Webhook Delivery", row.name)
		connection = frappe.get_doc("Shopify Connection", delivery.connection)
		_process_delivery(delivery, connection)


def _handle_topic(connection, topic):
	"""Per-topic side effects. Every topic other than APP_UNINSTALLED is
	acknowledged and recorded only - canonical order retrieval and ERPNext
	Sales Order/document creation from webhook payloads are explicitly out of
	scope for #104."""
	if topic == "APP_UNINSTALLED":
		_disable_connection(connection)
	# ORDERS_CREATE / ORDERS_UPDATED / ORDERS_CANCELLED / ORDERS_PAID /
	# REFUNDS_CREATE / APP_SCOPES_UPDATE: no further action in this issue.


def _disable_connection(connection):
	"""Safely disable a connection on APP_UNINSTALLED: idempotent (a second
	uninstall delivery, or one arriving after an admin already disabled the
	connection manually, is a no-op), and reuses Shopify Connection.save()'s
	existing on_update() token-cache invalidation rather than duplicating
	it here."""
	if not connection.enabled:
		return
	connection.enabled = 0
	connection.save(ignore_permissions=True)
