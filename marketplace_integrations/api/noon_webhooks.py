# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Public Noon FBPI event-notification webhook receiver (#163).

Auth and envelope-shape validation finish synchronously so an invalid
request never reaches a worker - no Noon HTTP call happens in the request
itself. ``FBPI::ORDER_SYNC`` (#400) reads ``payload.order_nr`` as a lookup key
only (never stored): a known order queues the deduplicated per-order
``GetFbpiOrder`` refresh, an unknown one the existing account import. After
authentication, an envelope whose content is unusable (wrong or missing
``event_schema_version``, missing ``event_type``, non-object or order-less
``payload``) is answered 200, so Noon stops retrying, and logged as an
``Invalid`` row with the raw payload capped like #263's. A missing or wrong
``X-Api-Key`` and an unresolvable ``project_code`` stay 403, so
unauthenticated traffic is never stored.
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
	NOON_ORDER_SYNC_SCHEMA_VERSION,
	NOON_RETURN_BARCODE_MAX,
	NOON_RETURN_EVENT_PAYLOAD_LOG_MAX,
	NOON_RETURN_REFERENCE_UPDATE_EVENT_TYPE,
	NOON_WEBHOOK_SECRET_HEADER,
)
from marketplace_integrations.integrations.noon.orders import enqueue_order_import, enqueue_order_refresh
from marketplace_integrations.integrations.noon.returns import queue_webhook_lookup
from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def event_notification():
	"""Authenticate one Noon FBPI event-notification envelope and, for
	``FBPI::ORDER_SYNC``, enqueue either the deduplicated per-order refresh (order already
	imported) or the deduplicated per-account order import (#400); ``RETURNS::REFERENCE_UPDATE``
	queues a return lookup. Any other ``event_type`` is acknowledged (200) without enqueuing
	anything, so Noon stops retrying a type this app doesn't act on."""
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
	if not isinstance(metadata, dict):
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

	# Authenticated from here on: an unusable envelope is answered 200 (so Noon stops retrying)
	# and logged, never 403.
	if not isinstance(event_type, str) or not event_type:
		_log_invalid_event(account, message_id, event_type, payload, "event_type is missing or not a string")
		return {"accepted": True}
	if not isinstance(payload, dict):
		_log_invalid_event(account, message_id, event_type, payload, "payload is missing or not an object")
		return {"accepted": True}

	if event_type == NOON_RETURN_REFERENCE_UPDATE_EVENT_TYPE:
		_handle_return_reference_update(account, message_id, payload)
		return {"accepted": True}

	import_queued = False
	refresh_queued = None
	if event_type == NOON_FBPI_ORDER_SYNC_EVENT_TYPE:
		if not _is_supported_schema_version(body.get("event_schema_version")):
			_log_invalid_event(
				account,
				message_id,
				event_type,
				payload,
				"event_schema_version is missing or not 1",
				extra=lambda: _schema_version_diagnostics(body, metadata),
			)
			return {"accepted": True}
		order_nr = _order_nr(payload)
		if not order_nr:
			_log_invalid_event(
				account, message_id, event_type, payload, "payload.order_nr is missing or empty"
			)
			return {"accepted": True}
		known_order = _imported_order(account, order_nr)
		if known_order:
			job = enqueue_order_refresh(known_order, trigger_source="Noon Webhook")
			refresh_queued = job is not None
		else:
			job = enqueue_order_import(account.name, trigger_source="Noon Webhook")
			import_queued = job is not None

	_log_accepted_event(account, message_id, event_type, import_queued, refresh_queued)

	return {"accepted": True}


def _is_supported_schema_version(value):
	"""Noon documents ``event_schema_version`` ``1``; accept the integer or the string ``"1"``."""
	if isinstance(value, bool):
		return False
	if isinstance(value, int):
		return value == NOON_ORDER_SYNC_SCHEMA_VERSION
	return isinstance(value, str) and value.strip() == str(NOON_ORDER_SYNC_SCHEMA_VERSION)


def _order_nr(payload):
	order_nr = payload.get("order_nr")
	return order_nr.strip() if isinstance(order_nr, str) else ""


def _imported_order(account, order_nr):
	"""Name of the Marketplace Order already imported for ``order_nr``, else ``None``.

	``order_nr`` is only a lookup key (via the same identity key the import uses): it is never
	stored or logged. An unknown value means the caller falls back to the account import."""
	return frappe.db.get_value(
		"Marketplace Order",
		{"order_identity_key": compute_order_identity_key(account.name, order_nr)},
		"name",
	)


def _schema_version_diagnostics(body, metadata):
	"""Extra log data for a schema-version rejection: what Noon sent in ``event_schema_version``
	(repr capped at 100 characters, plus its JSON type name) and the envelope/``metadata`` key
	names. Names only, never values, so no secret or metadata value is stored."""
	value = body.get("event_schema_version") if "event_schema_version" in body else None
	if value is None:
		type_name = "null" if "event_schema_version" in body else "missing"
	elif isinstance(value, bool):
		type_name = "boolean"
	elif isinstance(value, (int, float)):
		type_name = "number"
	elif isinstance(value, str):
		type_name = "string"
	elif isinstance(value, list):
		type_name = "array"
	elif isinstance(value, dict):
		type_name = "object"
	else:
		type_name = type(value).__name__
	return {
		"received_event_schema_version": {
			"repr": repr(value)[:100] if type_name != "missing" else None,
			"type": type_name,
		},
		"envelope_keys": _capped_key_names(body),
		"metadata_keys": _capped_key_names(metadata),
	}


def _capped_key_names(mapping):
	return [cstr(key)[:100] for key in list(mapping)[:50]]


def _log_invalid_event(account, message_id, event_type, payload, reason, extra=None):
	"""One committed ``Invalid`` ``Marketplace Sync Log`` row for an authenticated envelope whose
	content is unusable, with the raw payload, ``message_id`` and ``event_type`` each capped at
	4096 characters (the #263 pattern). ``extra`` is an optional callable returning diagnostic keys for
	the stored data; it runs inside the guard below, so a failure in it cannot turn the 200 into a 500.
	Never raises outward: the caller still answers 200."""
	try:
		now = frappe.utils.now_datetime()
		frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"marketplace_account": account.name,
				"status": "Invalid",
				"trigger_source": "Noon Webhook",
				"attempted_at": now,
				"completed_at": now,
				"request_data": frappe.as_json(
					{
						"message_id": _capped_text(message_id),
						"event_type": _capped_text(event_type),
						"reason": reason,
						"payload": _capped_payload(payload),
						**(extra() if extra else {}),
					}
				),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="Noon webhook: failed to write sync log", message=frappe.get_traceback())


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


def _capped_text(value):
	"""A string cap for attacker-controlled scalars (``message_id``, ``event_type``) in log rows;
	``None`` stays ``None``, any other non-string is stringified first."""
	if value is None:
		return None
	return cstr(value)[:NOON_RETURN_EVENT_PAYLOAD_LOG_MAX]


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


def _log_accepted_event(account, message_id, event_type, import_queued, refresh_queued=None):
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
	misrepresents a raised enqueue failure as a Success. ``refresh_queued`` is
	``None`` unless the event named an already imported order (#400): then the
	per-order refresh was queued instead of the import, and the row records whether
	that job was new (``False``: a deduplicated redelivery)."""
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
						**({} if refresh_queued is None else {"refresh_queued": refresh_queued}),
					}
				),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="Noon webhook: failed to write sync log", message=frappe.get_traceback())


def _invalid_webhook():
	frappe.throw(_("Invalid Noon event-notification webhook."), frappe.PermissionError)
