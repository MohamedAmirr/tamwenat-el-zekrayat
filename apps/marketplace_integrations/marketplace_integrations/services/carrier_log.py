# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""What the neutral carrier service (``carrier_shipments.py``) and every carrier adapter share: the
Integration Status values, the two answers an adapter call can raise, and the Integration Log (native
Integration Request) rows. The log never holds an API key, a token or a receiver's name, phone or address.
The service name is the adapter's ``SERVICE_NAME``; nothing here knows a carrier."""

import uuid

import frappe
from frappe.utils import cstr, now_datetime

NOT_CREATED = "Not Created"
CREATING = "Creating"
UNKNOWN = "Creation Unknown"
CREATED = "Created"
FAILED = "Creation Failed"
CANCELLATION_PENDING = "Cancellation Pending"
CANCELLATION_UNKNOWN = "Cancellation Unknown"
CANCELLED = "Cancelled"
#: Statuses where the shipment is started or live: the note's shipment fields are locked and it cannot be
#: cancelled or deleted.
LIVE_STATUSES = (CREATING, UNKNOWN, CREATED, CANCELLATION_PENDING, CANCELLATION_UNKNOWN)
#: Statuses where the person may still change the note and (re)start the create.
EDITABLE_STATUSES = ("", NOT_CREATED, FAILED)


class Unknown(Exception):
	"""The outcome of a call is not known: the shipment may or may not exist at the carrier."""

	def __init__(self, message="", http_status=None):
		super().__init__(message)
		self.http_status = http_status


class Failed(Exception):
	"""The carrier definitively refused the request (a readable message)."""


class RateLimited(Exception):
	"""The carrier asked to slow down (HTTP 429); a reconciliation run stops early."""


def attempt_number(service, delivery_note, operation="Create"):
	"""The next attempt number shown on the log (display only: Log Settings deletes old logs, so no
	decision may depend on it)."""
	return (
		frappe.db.count(
			"Integration Request",
			{
				"integration_request_service": service,
				"reference_doctype": "Delivery Note",
				"reference_docname": delivery_note,
				"custom_operation_type": operation,
			},
		)
		+ 1
	)


def open_log(
	service, delivery_note, operation, attempt=None, correlation_id=None, reference=None, reference_doctype="Delivery Note"
):
	"""Insert the Integration Log row of one operation; returns its name. Inserted as the system after
	the caller's own permission check (Integration Request only grants System Manager). ``delivery_note`` is
	the name of the ``reference_doctype`` document (a Delivery Note unless the caller passes another, e.g. the
	Marketplace Account of a warehouse check)."""
	log = frappe.get_doc(
		{
			"doctype": "Integration Request",
			"integration_request_service": service,
			"reference_doctype": reference_doctype,
			"reference_docname": delivery_note,
			"status": "Queued",
			"custom_operation_type": operation,
			"custom_attempt_no": attempt,
			"custom_correlation_id": correlation_id or uuid.uuid4().hex,
			"custom_external_reference": reference,
		}
	)
	log.insert(ignore_permissions=True)
	return log.name


def close_log(name, result, **values):
	"""Complete the log row. ``values`` may hold ``error``, ``data`` (sanitized), ``output``,
	``http_status``, ``error_code`` and ``delivery_id``."""
	if not name:
		return
	update = {
		"status": {"Success": "Completed"}.get(result, "Failed"),
		"custom_result": result,
		"custom_completed_at": now_datetime(),
	}
	for key, fieldname in (
		("error", "error"),
		("data", "data"),
		("output", "output"),
		("http_status", "custom_http_status"),
		("error_code", "custom_error_code"),
		("delivery_id", "custom_delivery_id"),
	):
		if values.get(key) not in (None, ""):
			value = values[key]
			update[fieldname] = value if isinstance(value, (int, float)) else cstr(value)[:2000]
	frappe.db.set_value("Integration Request", name, update, update_modified=False)
