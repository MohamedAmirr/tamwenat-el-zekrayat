# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Talabat outlet availability retrieval and updates for one Marketplace Account."""

from datetime import UTC
from urllib.parse import quote
from zoneinfo import ZoneInfo

import frappe
import frappe.utils.data as frappe_data
from frappe import _
from frappe.utils import cstr, get_datetime, get_datetime_str, now_datetime

from marketplace_integrations.integrations.talabat.client import TalabatClient
from marketplace_integrations.integrations.talabat.orders import get_account

GET_STATUSES = {"OPEN", "CLOSED_TODAY", "CLOSED_UNTIL", "CLOSED"}
PUT_STATUSES = {"OPEN", "CLOSED_TODAY", "CLOSED_UNTIL"}
CLOSE_REASONS = {
	"TOO_BUSY_NO_DRIVERS",
	"TOO_BUSY_KITCHEN",
	"UPDATES_IN_MENU",
	"TECHNICAL_PROBLEM",
	"CLOSED",
	"OTHER",
	"BAD_WEATHER",
	"HOLIDAY_SPECIAL_DAY",
}
REMOTE_ERROR = _("Talabat did not return a successful outlet status response.")


def get_outlet_status(account):
	"""Fetch and persist Talabat's current confirmed outlet availability."""
	account_doc, connection = get_account(account)
	response = _request_status(
		account_doc,
		connection.name,
		"GET",
		_status_path(connection.chain_id, account_doc.external_account_id),
	)
	if not response.ok:
		_persist_error(account_doc, REMOTE_ERROR)
		frappe.throw(REMOTE_ERROR)
	return _persist_status(account_doc, _response_body(account_doc, response))


def update_outlet_status(account, status, closed_reason=None, closed_until=None):
	"""Send one supported Talabat outlet update and persist its confirmed response."""
	account_doc, connection = get_account(account)
	payload = _build_update_payload(status, closed_reason, closed_until)
	response = _request_status(
		account_doc,
		connection.name,
		"PUT",
		_status_path(connection.chain_id, account_doc.external_account_id),
		json=payload,
	)
	if not response.ok:
		_persist_error(account_doc, REMOTE_ERROR)
		frappe.throw(REMOTE_ERROR)
	return _persist_status(account_doc, _response_body(account_doc, response))


def _status_path(chain_id, vendor_id):
	return f"/v2/chains/{quote(cstr(chain_id), safe='')}/vendors/{quote(cstr(vendor_id), safe='')}/status"


def _request_status(account, connection_name, method, path, **kwargs):
	try:
		return TalabatClient(connection_name).request(method, path, **kwargs)
	except frappe.ValidationError:
		_persist_error(account, REMOTE_ERROR)
		frappe.throw(REMOTE_ERROR)


def _build_update_payload(status, closed_reason, closed_until):
	status = cstr(status).strip()
	if status not in PUT_STATUSES:
		frappe.throw(_("Unsupported Talabat outlet status update."))
	if status == "OPEN":
		return {"status": status}

	closed_reason = cstr(closed_reason).strip()
	if closed_reason not in CLOSE_REASONS:
		frappe.throw(_("A supported Talabat close reason is required."))
	payload = {"status": status, "closed_reason": closed_reason}
	if status == "CLOSED_UNTIL":
		payload["closed_until"] = _to_utc_rfc3339(closed_until)
	return payload


def _to_utc_rfc3339(value):
	value = cstr(value).strip()
	if not value:
		frappe.throw(_("Closed Until is required when closing a Talabat outlet until a date and time."))
	try:
		local_datetime = get_datetime(value)
		if local_datetime.tzinfo is None:
			local_datetime = local_datetime.replace(tzinfo=ZoneInfo(frappe_data.get_system_timezone()))
		utc_datetime = local_datetime.astimezone(UTC)
	except (TypeError, ValueError):
		frappe.throw(_("Closed Until must be a valid date and time."))
	return utc_datetime.isoformat(timespec="seconds").replace("+00:00", "Z")


def _response_body(account, response):
	try:
		body = response.json()
	except ValueError:
		_persist_error(account, REMOTE_ERROR)
		frappe.throw(REMOTE_ERROR)
	if not isinstance(body, dict):
		_persist_error(account, REMOTE_ERROR)
		frappe.throw(REMOTE_ERROR)
	return body


def _persist_status(account, body):
	status = cstr(body.get("status")).strip()
	if status not in GET_STATUSES:
		_persist_error(account, REMOTE_ERROR)
		frappe.throw(REMOTE_ERROR)
	account.external_status = status
	account.external_status_reason = cstr(body.get("closed_reason")).strip() or None
	account.external_status_until = _to_system_datetime(body.get("closed_until"))
	account.external_status_checked_on = now_datetime()
	account.external_status_error = None
	account.external_status_payload = body
	account.save()
	return account


def _persist_error(account, message):
	account.external_status_error = cstr(message).strip()
	account.save()


def _to_system_datetime(value):
	value = cstr(value).strip()
	if not value:
		return None
	try:
		return get_datetime_str(frappe_data.convert_utc_to_system_timezone(get_datetime(value)))
	except (TypeError, ValueError):
		return None
