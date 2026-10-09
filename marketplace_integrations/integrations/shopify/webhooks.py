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
``retry_errored_webhook_deliveries()``, gives both an errored delivery and a
delivery stuck at "Received" (a worker crash before it ever reached a
terminal status) an automatic path back to "Processed" without waiting on a
Shopify-initiated redelivery; see its own docstring and
``_process_delivery()``'s.

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
from frappe.utils import add_to_date, cstr, escape_html, now_datetime

from marketplace_integrations.integrations.http import truncate_response_text
from marketplace_integrations.integrations.shopify.client import ShopifyClient
from marketplace_integrations.integrations.shopify.constants import (
	SHOPIFY_ORDER_WEBHOOK_TOPICS,
	SHOPIFY_WEBHOOK_PATH,
	SHOPIFY_WEBHOOK_TOPICS,
)
from marketplace_integrations.integrations.shopify.orders import account_for_connection, import_order
from marketplace_integrations.services.sales_order_sync_debounce import bump_sync_debounce

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

# The retry sweep also recovers a delivery stuck at status="Received" once
# it's older than this - e.g. a worker crash between delivery.insert() and
# the first status write, the one failure mode with no other recovery path
# at all (nothing else would ever move that row off "Received"). Long enough
# that the sweep doesn't race a delivery a worker is actively processing
# right now. A single constant, not configurable - see this project's
# "don't over-engineer" rule.
STUCK_RECEIVED_THRESHOLD_MINUTES = 15

# Upper bound on how many rows one sweep run processes - keeps a single
# scheduled job run bounded even against a large backlog; any remainder
# drains over subsequent hourly runs (see retry_errored_webhook_deliveries()'s
# deterministic creation-ascending order below).
SWEEP_BATCH_LIMIT = 200


def process_shopify_webhook(connection_name, topic, webhook_id, shop_domain, order_gid=None):
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
	delivery.order_gid = order_gid
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

	``last_webhook_delivery_at``/``last_webhook_delivery_topic`` are only
	ever stamped from ``delivery.received_at``/``delivery.topic`` on this
	delivery's ORIGINAL processing attempt (``attempt_count == 0`` going
	in) - never on a reprocess/retry (a Shopify redelivery of a non-Processed
	row, or the scheduled sweep). Those fields mean "has Shopify stopped
	delivering to us"; stamping them with the CURRENT time on every retry of
	an old errored/stuck delivery would make a stale delivery falsely look
	fresh and defeat that purpose. ``last_webhook_processed_at``/
	``last_webhook_error`` still update on every attempt, original or
	retried - those track "last processing attempt/outcome", not "last
	delivery received".

	The atomic ``UPDATE ... WHERE attempt_count < %s`` below is both the
	MAX_WEBHOOK_DELIVERY_ATTEMPTS cap check AND the increment in one
	statement - see docs/frappe-conventions.md's concurrency guidance on
	check-then-write races. A separate ``if delivery.attempt_count >=
	MAX...: return`` followed later by a plain ``db_set("attempt_count",
	delivery.attempt_count + 1)`` would let two concurrent callers (e.g. the
	sweep and a simultaneous Shopify redelivery hitting the same row) both
	pass the check against the same stale in-memory count and then both
	write, either double-counting or exceeding the cap. 0 rows updated means
	either the cap was already reached, or a concurrent caller won this
	exact increment first - either way, this call bails out cleanly without
	reprocessing.
	"""
	is_original_attempt = delivery.attempt_count == 0

	frappe.db.sql(
		"""
		UPDATE `tabShopify Webhook Delivery`
		SET attempt_count = attempt_count + 1
		WHERE name = %s AND attempt_count < %s
		""",
		(delivery.name, MAX_WEBHOOK_DELIVERY_ATTEMPTS),
	)
	if frappe.db._cursor.rowcount != 1:
		return
	# The raw SQL UPDATE above bypasses Document's own cache invalidation -
	# see docs/frappe-conventions.md's concurrency guidance.
	frappe.clear_document_cache(delivery.doctype, delivery.name)
	delivery.attempt_count += 1

	# One webhook_id is always exactly one event, so it can never legitimately
	# have two different topics across original processing and a later
	# reprocess - reading it from the persisted delivery row (rather than a
	# caller-supplied argument) is always correct and keeps every caller
	# (fresh delivery, duplicate-id reprocess, sweep) using one source of
	# truth for "what event is this".
	topic = delivery.topic

	if is_original_attempt:
		connection.db_set("last_webhook_delivery_at", delivery.received_at, update_modified=False)
		connection.db_set("last_webhook_delivery_topic", topic, update_modified=False)

	try:
		if topic in SHOPIFY_ORDER_WEBHOOK_TOPICS:
			note = _handle_topic(connection, topic, delivery.order_gid)
		else:
			note = _handle_topic(connection, topic)
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
	# `note` (decision 5's third edge case: order(id:) returned null - a
	# genuinely deleted order, or one outside the 60-day read_orders window)
	# reuses the same `error` field the Error status uses, but WITHOUT
	# changing status away from "Processed" - this is what makes the outcome
	# distinguishable from both a genuine success (blank error) and a real
	# Error status, without adding a new field. Blank clears any stale note
	# from an earlier attempt on the same delivery row.
	delivery.db_set("error", note or "", update_modified=False)
	connection.db_set("last_webhook_processed_at", processed_at, update_modified=False)
	connection.db_set("last_webhook_error", "", update_modified=False)


def retry_errored_webhook_deliveries():
	"""Scheduled sweep (hourly, see hooks.py's scheduler_events) that gives
	two kinds of stuck Shopify Webhook Delivery rows an automatic path back
	to "Processed" with no Shopify-initiated redelivery to rely on:

	- ``status="Error"`` rows - e.g. an APP_UNINSTALLED delivery whose
	  _disable_connection() save() failed on a lock timeout, leaving a
	  connection that should have been disabled still enabled indefinitely,
	  since `webhook_event()` already responded 200 before that ASYNC
	  failure happened and Shopify has nothing telling it to redeliver.
	- ``status="Received"`` rows older than STUCK_RECEIVED_THRESHOLD_MINUTES
	  - a worker crash between ``delivery.insert()`` and the first status
	    write. This is the one failure mode with literally no other recovery
	    path (nothing else would ever move such a row off "Received"). The
	    age threshold avoids racing a row a worker is actively processing
	    right now.

	Reuses _process_delivery() - the exact same reprocess logic the
	duplicate-delivery path above uses - rather than a second copy of it
	here. The MAX_WEBHOOK_DELIVERY_ATTEMPTS cap is enforced inside
	_process_delivery() itself, so this sweep's own filter
	(``attempt_count < MAX_WEBHOOK_DELIVERY_ATTEMPTS``) is a query-level
	optimization to skip exhausted rows outright, not a second place the cap
	is decided.

	The two status branches can't both be expressed as plain ANDed
	frappe.get_all() filters (the age condition applies only to the
	"Received" branch, not "Error"), so this uses one parameterized raw SQL
	SELECT rather than two separate queries merged in Python - see
	docs/frappe-conventions.md's "prefer the ORM, raw SQL only when it
	genuinely can't express the query" guidance. Rows are read oldest-first
	(``ORDER BY creation ASC``) and bounded to SWEEP_BATCH_LIMIT per run, so
	one run is never unbounded against a large backlog and any remainder
	drains deterministically over subsequent hourly runs.

	Each row is processed under its own savepoint: one bad row (an
	unresolvable connection, a lock-wait timeout on a concurrent save) must
	not roll back every row already successfully processed earlier in the
	same sweep run - see docs/frappe-conventions.md's savepoint-per-row
	guidance (same pattern as api/talabat_orders.py's vendor-order import).
	Without this, one poison row would roll back the whole sweep on every
	hourly run forever, including any real side effect (e.g. disabling a
	connection) that happened to run before it in that same pass.
	Successfully processed rows are committed per-row so that progress
	survives a later row's failure.
	"""
	stuck_received_before = add_to_date(now_datetime(), minutes=-STUCK_RECEIVED_THRESHOLD_MINUTES)
	rows = frappe.db.sql(
		"""
		SELECT name
		FROM `tabShopify Webhook Delivery`
		WHERE attempt_count < %(max_attempts)s
		  AND (
		    status = 'Error'
		    OR (status = 'Received' AND creation < %(stuck_received_before)s)
		  )
		ORDER BY creation ASC
		LIMIT %(limit)s
		""",
		{
			"max_attempts": MAX_WEBHOOK_DELIVERY_ATTEMPTS,
			"stuck_received_before": stuck_received_before,
			"limit": SWEEP_BATCH_LIMIT,
		},
		as_dict=True,
	)

	connections = {}
	for row in rows:
		savepoint = "shopify_webhook_sweep_" + frappe.generate_hash(length=10)
		frappe.db.savepoint(savepoint)
		delivery = None
		try:
			delivery = frappe.get_doc("Shopify Webhook Delivery", row.name)
			connection = connections.get(delivery.connection)
			if connection is None:
				connection = frappe.get_doc("Shopify Connection", delivery.connection)
				connections[delivery.connection] = connection
			_process_delivery(delivery, connection)
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			if delivery is not None:
				# The connection's cached in-memory state may reflect writes
				# this row's failure just rolled back - drop it so the next
				# row for the same connection re-fetches a clean copy rather
				# than reasoning from a now-stale cached object.
				connections.pop(delivery.connection, None)
			frappe.log_error(
				title="Shopify webhook retry sweep: row failed",
				message=frappe.get_traceback(),
			)
		else:
			frappe.db.release_savepoint(savepoint)
			frappe.db.commit()


def _handle_topic(connection, topic, order_gid=None):
	"""Per-topic side effects. Every topic other than APP_UNINSTALLED is
	acknowledged and recorded only - canonical order retrieval and ERPNext
	Sales Order/document creation from webhook payloads are explicitly out of
	scope for #104.

	Returns an optional note string for the caller to persist alongside a
	"Processed" status (see _process_delivery()) - used only for decision 5's
	null-order edge case below. Every other path returns None (a plain,
	silent "Processed")."""
	if topic == "APP_UNINSTALLED":
		_disable_connection(connection)
		return None
	if topic in SHOPIFY_ORDER_WEBHOOK_TOPICS:
		if not order_gid:
			frappe.throw(frappe._("Shopify webhook is missing order id."))
		account = account_for_connection(connection.name)
		if not account:
			# #109 decision 5, edge case 2: no enabled account/connection
			# resolvable for the shop - a silent Processed no-op. An Error
			# here would just burn through the 5-attempt retry budget for
			# nothing actionable.
			return None
		order = import_order(account, order_gid)
		if order is None:
			# #109 decision 5, edge case 3: order(id:) returned null (older
			# than the 60-day read_orders window, or genuinely deleted).
			# Retrying cannot succeed, so this stays Processed - but WITH a
			# visible note, unlike the silent no-op above - see
			# _process_delivery()'s use of this return value.
			return frappe._("Shopify order {0} could not be refetched (not found or outside the read_orders window).").format(order_gid)
		# #133: a successful import schedules the eventual Sales Order sync via
		# the debounce window (#132) rather than enqueueing a sync job
		# directly - a burst of webhooks for the same order within the
		# debounce window collapses to exactly one eventual sync_sales_order()
		# call. bump_sync_debounce() records trigger_source="Shopify Webhook"
		# on the order itself (#135); _run_debounced_sync() reads it back and
		# threads it into sync_sales_order() so the Marketplace Sync Log row
		# this eventually produces records the real origin, not "Manual".
		bump_sync_debounce(order.name, trigger_source="Shopify Webhook")
		return None
	# APP_SCOPES_UPDATE remains an acknowledged no-op.
	return None


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
