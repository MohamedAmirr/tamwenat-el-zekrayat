# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Public Noon FBPI event-notification webhook receiver (#163).

Auth and envelope-shape validation finish synchronously so an invalid
request never reaches a worker - no Noon HTTP call happens in the request
itself. ``FBPI::ORDER_SYNC`` only queues the existing order import: its
payload is checked to be an object but never parsed, stored or logged.
``RETURNS::REFERENCE_UPDATE`` (#263) reads ``payload.barcode``, queues a
deduplicated return-reference lookup and stores the raw payload (capped) on
a ``Return Lookup`` sync log row; every other event type is acknowledged
with 200 and takes no action, but is still recorded as an Order Import /
Success / ``Noon Webhook`` log row (it counts as a received event). The secret header value is never logged.

Unlike Talabat's webhook pair (``api/talabat_webhooks.py``), Noon's Event
Notifications have no HMAC/signature scheme - only a static per-connection
credential header, defined and checked entirely by this receiver (see
``docs/noon-integration/event-notifications.md``, sourced from Noon's
public docs and explicitly user-approved for this work).
"""

import hmac

import frappe
from frappe import _
from frappe.utils import cstr
from frappe.utils.password import get_decrypted_password

from marketplace_integrations.integrations.noon.constants import (
	NOON_FBPI_ORDER_SYNC_EVENT_TYPE,
	NOON_RETURN_BARCODE_MAX,
	NOON_RETURN_EVENT_PAYLOAD_LOG_MAX,
	NOON_RETURN_REFERENCE_UPDATE_EVENT_TYPE,
	NOON_WEBHOOK_SECRET_HEADER,
)
from marketplace_integrations.integrations.noon.orders import enqueue_order_import
from marketplace_integrations.integrations.noon.returns import queue_webhook_lookup


@frappe.whitelist(allow_guest=True, methods=["POST"])
def event_notification():
	"""Authenticate one Noon FBPI event-notification envelope and, for a
	handled ``event_type``, enqueue the existing per-account deduplicated
	order-import job. Any other ``event_type`` is acknowledged (200) without
	enqueuing anything, so Noon stops retrying a type this app doesn't act
	on."""
	body = frappe.request.get_json(silent=True)
	if not isinstance(body, dict):
		_invalid_webhook()

	if _is_bare_order_body(body):
		# Noon sent a bare {"order_nr": ...} body (observed once, cause
		# unconfirmed). There is no project_code to resolve a connection, so
		# authenticate the key against every enabled connection, then ack.
		if not _header_matches_any_enabled_connection():
			_invalid_webhook()
		return {"accepted": True}

	event_type = body.get("event_type")
	metadata = body.get("metadata")
	payload = body.get("payload")
	if not isinstance(event_type, str) or not event_type:
		_invalid_webhook()
	if not isinstance(metadata, dict):
		_invalid_webhook()
	if not isinstance(payload, dict):
		_invalid_webhook()

	message_id = metadata.get("message_id")
	project_code = metadata.get("project_code")
	if not isinstance(message_id, str) or not message_id:
		_invalid_webhook()
	if not isinstance(project_code, str) or not project_code:
		_invalid_webhook()

	account, connection = _resolve_account_and_connection(project_code)

	secret = connection.get_password("webhook_secret", raise_exception=False)
	header_value = frappe.get_request_header(NOON_WEBHOOK_SECRET_HEADER, "")
	if not secret or not hmac.compare_digest(cstr(header_value).encode(), cstr(secret).encode()):
		_invalid_webhook()

	if event_type == NOON_RETURN_REFERENCE_UPDATE_EVENT_TYPE:
		_handle_return_reference_update(account, message_id, payload)
		return {"accepted": True}

	import_queued = False
	if event_type == NOON_FBPI_ORDER_SYNC_EVENT_TYPE:
		job = enqueue_order_import(account.name, trigger_source="Noon Webhook")
		import_queued = job is not None

	_log_accepted_event(account, message_id, event_type, import_queued)

	return {"accepted": True}


def _handle_return_reference_update(account, message_id, payload):
	"""#263: queue a lookup for the payload's ``barcode`` and keep the raw payload.

	The ``barcode`` field name is an unverified assumption (AI-written summary),
	so the capped raw payload goes on the log row to let the first real delivery
	confirm or correct it. A payload without a usable string ``barcode`` is
	logged and ignored; the caller still answers 200 so Noon stops retrying."""
	barcode = payload.get("barcode")
	barcode = barcode.strip() if isinstance(barcode, str) else ""
	lookup_queued = False
	if barcode and len(barcode) <= NOON_RETURN_BARCODE_MAX:
		lookup_queued = queue_webhook_lookup(account.name, barcode)
	_log_return_reference_event(account, message_id, payload, lookup_queued)


def _capped_payload(payload):
	raw = frappe.as_json(payload)
	if len(raw) <= NOON_RETURN_EVENT_PAYLOAD_LOG_MAX:
		return payload
	return {"truncated_payload": raw[:NOON_RETURN_EVENT_PAYLOAD_LOG_MAX]}


def _log_return_reference_event(account, message_id, payload, lookup_queued):
	"""One committed ``Return Lookup`` log row per accepted return-reference
	event. Never raises outward, like ``_log_accepted_event``. Headers and the
	key are never stored."""
	try:
		now = frappe.utils.now_datetime()
		frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Return Lookup",
				"marketplace_account": account.name,
				"status": "Success",
				"trigger_source": "Noon Webhook",
				"attempted_at": now,
				"completed_at": now,
				"request_data": frappe.as_json(
					{
						"message_id": message_id,
						"event_type": NOON_RETURN_REFERENCE_UPDATE_EVENT_TYPE,
						"lookup_queued": lookup_queued,
						"payload": _capped_payload(payload),
					}
				),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="Noon webhook: failed to write sync log", message=frappe.get_traceback())


def _is_bare_order_body(body):
	return "order_nr" in body and not any(key in body for key in ("event_type", "metadata", "payload"))


def _header_matches_any_enabled_connection():
	"""Constant-time compare of the X-Api-Key header against every enabled
	Noon Connection's secret. Key uniqueness across connections is not enforced."""
	header_value = cstr(frappe.get_request_header(NOON_WEBHOOK_SECRET_HEADER, "")).encode()
	if not header_value:
		return False
	matched = False
	for name in frappe.get_all("Noon Connection", filters={"enabled": 1}, pluck="name"):
		secret = get_decrypted_password("Noon Connection", name, "webhook_secret", raise_exception=False)
		if secret and hmac.compare_digest(header_value, cstr(secret).encode()):
			matched = True
	return matched


def _resolve_account_and_connection(project_code):
	"""Resolve exactly one enabled Noon Connection for ``project_code``, and
	that connection to exactly one enabled Noon Marketplace Account. Either
	zero or more than one match at either step is rejected."""
	connections = frappe.get_all(
		"Noon Connection",
		filters={"project_code": project_code, "enabled": 1},
		fields=["name"],
	)
	if len(connections) != 1:
		_invalid_webhook()
	connection = frappe.get_doc("Noon Connection", connections[0].name)

	accounts = frappe.get_all(
		"Marketplace Account",
		filters={
			"platform": "Noon",
			"enabled": 1,
			"connection_doctype": "Noon Connection",
			"connection": connection.name,
		},
		fields=["name"],
	)
	if len(accounts) != 1:
		_invalid_webhook()

	account = frappe.get_doc("Marketplace Account", accounts[0].name)
	return account, connection


def _log_accepted_event(account, message_id, event_type, import_queued):
	"""Write one committed ``Marketplace Sync Log`` row for an accepted event
	(decision: relies on #145's existing per-account job_id dedup rather than
	a new dedup doctype). Never raises outward - a logging failure must not
	fail the webhook response Noon is waiting on.

	Always called with the already-known real outcome of the enqueue
	attempt - ``import_queued`` is ``True`` only when ``enqueue_order_import()``
	actually returned a queued job, and ``False`` for both an unhandled
	``event_type`` and a deduplicated delivery (enqueue returned ``None``
	because an identical job_id was already queued/running). If
	``enqueue_order_import()`` itself raises, this function is never reached
	at all (see ``event_notification()``), so no row is ever committed that
	misrepresents a raised enqueue failure as a Success."""
	try:
		now = frappe.utils.now_datetime()
		frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"marketplace_account": account.name,
				"status": "Success",
				"trigger_source": "Noon Webhook",
				"attempted_at": now,
				"completed_at": now,
				"request_data": frappe.as_json(
					{
						"message_id": message_id,
						"event_type": event_type,
						"import_queued": import_queued,
					}
				),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="Noon webhook: failed to write sync log", message=frappe.get_traceback())


def _invalid_webhook():
	frappe.throw(_("Invalid Noon event-notification webhook."), frappe.PermissionError)
