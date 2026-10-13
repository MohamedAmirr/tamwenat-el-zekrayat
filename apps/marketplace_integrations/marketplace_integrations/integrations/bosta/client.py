# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authenticated Bosta business API client.

The client deliberately does not construct a Create Delivery payload. The
Sales Order workflow owns the verified payload mapping; this module owns Bosta
authentication and HTTP semantics, plus the A4 Arabic AWB request.
"""

from urllib.parse import quote

import frappe
from frappe import _

from marketplace_integrations.integrations.bosta.constants import (
	BOSTA_API_BASE_URL,
	BOSTA_CREATE_DELIVERY_PATH,
	BOSTA_GET_DELIVERY_PATH,
	BOSTA_MASS_AWB_PATH,
)
from marketplace_integrations.integrations.http import error_message_from_response, http_request

SERVICE_NAME = "Bosta"


class BostaClient:
	"""Authenticated transport for the site's single Bosta carrier setup."""

	def __init__(self, settings=None):
		self._settings = settings

	def get_settings(self):
		return self._settings or frappe.get_single("Bosta Settings")

	def request(self, method, path, **kwargs):
		"""Make one Bosta request and return its raw response.

		Endpoint callers determine their own success code because Bosta endpoints
		do not all document the same success status. Network errors are sanitized
		so credentials cannot be leaked through exception text.
		"""
		settings = self.get_settings()
		if not settings.enabled:
			frappe.throw(_("Bosta Settings is disabled."))

		api_key = settings.get_password("api_key", raise_exception=False)
		if not api_key:
			frappe.throw(_("Bosta Settings has no API key."))

		path = path if path.startswith("/") else f"/{path}"
		headers = kwargs.pop("headers", {}) or {}
		headers.update({"Authorization": api_key, "Content-Type": "application/json"})
		return http_request(
			method,
			f"{BOSTA_API_BASE_URL}{path}",
			service_name=SERVICE_NAME,
			headers=headers,
			include_exception_details=False,
			**kwargs,
		)

	def create_delivery(self, payload):
		"""Send a verified delivery payload without adding or transforming fields."""
		if not isinstance(payload, dict):
			frappe.throw(_("Bosta delivery payload must be an object."))
		return self.request("POST", BOSTA_CREATE_DELIVERY_PATH, json=payload)

	def get_a4_arabic_awb(self, tracking_numbers):
		"""Request Bosta's documented A4 Arabic AWB for one or more tracking numbers."""
		if isinstance(tracking_numbers, (list, tuple)):
			tracking_numbers = ",".join(str(number) for number in tracking_numbers if number)
		if not isinstance(tracking_numbers, str) or not tracking_numbers.strip():
			frappe.throw(_("At least one Bosta tracking number is required."))

		return self.request(
			"POST",
			BOSTA_MASS_AWB_PATH,
			json={
				"trackingNumbers": tracking_numbers,
				"requestedAwbType": "A4",
				"lang": "ar",
			},
		)

	def get_delivery(self, tracking_number):
		"""Fetch one delivery using Bosta's documented tracking-number endpoint."""
		tracking_number = str(tracking_number or "").strip()
		if not tracking_number:
			frappe.throw(_("A Bosta tracking number is required."))
		return self.request(
			"GET", BOSTA_GET_DELIVERY_PATH.format(tracking_number=quote(tracking_number, safe=""))
		)

	def error_message(self, response):
		"""Return a bounded Bosta error suitable for an operator-facing message."""
		return _("Bosta request failed (HTTP {0}): {1}").format(
			response.status_code, error_message_from_response(response)
		)
