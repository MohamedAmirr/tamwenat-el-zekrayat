# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Persist Bosta's documented delivery-state response without ERP side effects.

The Bosta contract supplied for this MVP documents the delivery read endpoint
and its ``state.code``/``state.value`` response fields. It does not document a
webhook event body or standard-Deliver return/cancellation semantics, so this
module neither interprets unknown values nor changes Delivery Notes.
"""

import frappe
from frappe.utils import cstr, now_datetime

from marketplace_integrations.integrations.bosta.client import BostaClient

# ``Delivered`` is the only terminal delivery state explicitly shown in the
# supplied Bosta read-response contract. All other (including unknown) states
# remain eligible for polling rather than being guessed as terminal.
DOCUMENTED_COMPLETE_STATE_VALUES = {"Delivered"}


def poll_incomplete_shipments():
	"""Poll Bosta deliveries that do not yet have its documented Delivered state."""
	settings = frappe.get_single("Bosta Settings")
	if not settings.enabled:
		return 0

	shipment_names = frappe.get_all(
		"Bosta Shipment",
		filters={"tracking_number": ["!=", ""]},
		pluck="name",
		order_by="name asc",
	)
	updated = 0
	client = BostaClient(settings=settings)
	for shipment_name in shipment_names:
		shipment = frappe.get_doc("Bosta Shipment", shipment_name)
		if is_bosta_delivery_complete(shipment.bosta_state_value):
			continue
		try:
			response = client.get_delivery(shipment.tracking_number)
			if not _is_success_response(response):
				raise BostaStatusError(client.error_message(response))
			if update_shipment_status(shipment, _response_body(response)):
				updated += 1
		except Exception:
			frappe.log_error(
				frappe.get_traceback(with_context=False),
				f"Bosta status polling failed for {shipment.name}",
			)
	return updated


def update_shipment_status(shipment, response_body):
	"""Persist only the documented ``state`` snapshot from a Bosta response.

	Malformed or schema-incomplete responses are intentionally ignored. This
	keeps an undocumented payload from becoming a business-state transition.
	"""
	data = response_body.get("data") if isinstance(response_body, dict) else None
	state = data.get("state") if isinstance(data, dict) else None
	if not isinstance(state, dict):
		return False

	code = state.get("code")
	value = state.get("value")
	if code is None and value is None:
		return False

	snapshot = {}
	if code is not None:
		snapshot["code"] = code
	if value is not None:
		snapshot["value"] = value
	shipment.bosta_state_code = cstr(code) if code is not None else ""
	shipment.bosta_state_value = cstr(value) if value is not None else ""
	shipment.bosta_status_snapshot = frappe.as_json(snapshot)
	shipment.last_bosta_status_on = now_datetime()
	shipment.save(ignore_permissions=True)
	return True


def is_bosta_delivery_complete(state_value):
	return cstr(state_value).strip() in DOCUMENTED_COMPLETE_STATE_VALUES


class BostaStatusError(Exception):
	"""Expected remote status failure isolated to one scheduled shipment."""


def _is_success_response(response):
	return 200 <= response.status_code < 300 and _response_body(response).get("success") is not False


def _response_body(response):
	try:
		body = response.json()
	except ValueError:
		return {}
	return body if isinstance(body, dict) else {}
