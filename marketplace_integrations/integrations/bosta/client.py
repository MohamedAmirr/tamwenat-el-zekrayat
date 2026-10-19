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
from frappe.utils import cint, escape_html

from marketplace_integrations.integrations.bosta.constants import (
	BOSTA_ALL_DISTRICTS_PATH,
	BOSTA_API_BASE_URL,
	BOSTA_AWB_MAX_TRACKING_NUMBERS,
	BOSTA_CITIES_PATH,
	BOSTA_CREATE_DELIVERY_PATH,
	BOSTA_DISTRICTS_PATH,
	BOSTA_GET_DELIVERY_PATH,
	BOSTA_MASS_AWB_PATH,
	BOSTA_PICKUP_LOCATIONS_PATH,
	BOSTA_PIN_LOCATION_PATH,
	BOSTA_SEARCH_DELIVERIES_PATH,
	BOSTA_TERMINATE_DELIVERY_PATH,
	BOSTA_UPDATE_DELIVERY_PATH,
)
from marketplace_integrations.integrations.http import error_message_from_response, http_request

SERVICE_NAME = "Bosta"
# Safety bound on pickup-location paging; a business has a handful of locations.
MAX_PICKUP_LOCATION_PAGES = 20


class BostaClient:
	"""Authenticated transport for the site's single Bosta carrier setup."""

	def __init__(self, settings=None, allow_disabled=False):
		self._settings = settings
		# Picking the business location happens before Bosta can be enabled (enabling
		# requires that location), so that one call may run while disabled.
		self._allow_disabled = allow_disabled

	def get_settings(self):
		return self._settings or frappe.get_single("Bosta Settings")

	def ensure_ready(self):
		"""Raise before anything is sent if Bosta is disabled or has no API key; return the key.

		Callers use this to fail on configuration problems before they persist any state
		that would otherwise look like an ambiguous (maybe-sent) request."""
		settings = self.get_settings()
		if not settings.enabled and not self._allow_disabled:
			frappe.throw(_("Bosta Settings is disabled."))
		api_key = settings.get_password("api_key", raise_exception=False)
		if not api_key:
			frappe.throw(_("Bosta Settings has no API key."))
		return api_key

	def request(self, method, path, **kwargs):
		"""Make one Bosta request and return its raw response.

		Endpoint callers determine their own success code because Bosta endpoints
		do not all document the same success status. Network errors are sanitized
		so credentials cannot be leaked through exception text.
		"""
		api_key = self.ensure_ready()
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
		"""Request Bosta's documented A4 Arabic AWB PDF response.

		Bosta documents a different, unimplemented response once a request has
		more than 50 labels.  Enforce the ERPNext MVP limit before any network
		call so an operator never receives a response we cannot safely handle.
		"""
		tracking_numbers = _normalise_awb_tracking_numbers(tracking_numbers)
		if not tracking_numbers:
			frappe.throw(_("At least one Bosta tracking number is required."))
		if len(tracking_numbers) > BOSTA_AWB_MAX_TRACKING_NUMBERS:
			frappe.throw(
				_("Bosta label printing supports at most {0} tracking numbers at a time.").format(
					BOSTA_AWB_MAX_TRACKING_NUMBERS
				)
			)

		return self.request(
			"POST",
			BOSTA_MASS_AWB_PATH,
			json={
				"trackingNumbers": ",".join(tracking_numbers),
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

	def update_delivery(self, tracking_number, payload):
		"""Bosta's documented ``PUT /deliveries/business/{trackingNumber}`` with an opaque body."""
		tracking_number = str(tracking_number or "").strip()
		if not tracking_number:
			frappe.throw(_("A Bosta tracking number is required."))
		if not isinstance(payload, dict):
			frappe.throw(_("Bosta delivery update payload must be an object."))
		return self.request(
			"PUT",
			BOSTA_UPDATE_DELIVERY_PATH.format(tracking_number=quote(tracking_number, safe="")),
			json=payload,
		)

	def terminate_delivery(self, tracking_number):
		"""Bosta's documented ``DELETE /deliveries/business/{trackingNumber}/terminate`` (no body)."""
		tracking_number = str(tracking_number or "").strip()
		if not tracking_number:
			frappe.throw(_("A Bosta tracking number is required."))
		return self.request(
			"DELETE",
			BOSTA_TERMINATE_DELIVERY_PATH.format(tracking_number=quote(tracking_number, safe="")),
		)

	def search_by_business_reference(self, business_reference):
		"""Search Bosta's documented ``businessReference`` filter (the OpenAPI does not
		document searching by ``uniqueBusinessReference``)."""
		business_reference = str(business_reference or "").strip()
		if not business_reference:
			frappe.throw(_("A Bosta business reference is required."))
		return self.request(
			"POST", BOSTA_SEARCH_DELIVERIES_PATH, json={"businessReference": business_reference}
		)

	def get_cities(self):
		"""Bosta's documented ``GET /cities`` (public endpoint; the key is sent anyway)."""
		return self.request("GET", BOSTA_CITIES_PATH)

	def get_districts(self, city_id):
		"""Bosta's documented ``GET /cities/{cityId}/districts``."""
		city_id = str(city_id or "").strip()
		if not city_id:
			frappe.throw(_("A Bosta city ID is required."))
		return self.request("GET", BOSTA_DISTRICTS_PATH.format(city_id=quote(city_id, safe="")))

	def get_all_districts(self):
		"""Bosta's documented ``GET /cities/getAllDistricts`` (every city with its districts)."""
		return self.request("GET", BOSTA_ALL_DISTRICTS_PATH)

	def pin_location(self, lat, lng):
		"""``GET /cities/pinLocation?lat=&lng=``: the Bosta districts at a coordinate. Not
		documented by Bosta (verified live 2026-10-19, may change)."""
		return self.request("GET", BOSTA_PIN_LOCATION_PATH, params={"lat": lat, "lng": lng})

	def get_pickup_locations(self):
		"""Every raw pickup location of the business from ``GET /pickup-locations``.

		The documented response is ``data: {list, page, pages, ...}`` (field shapes from
		the OpenAPI example; ``data.list[]._id/locationName/address.firstLine/address.city.name/
		isDefault`` and ``pages/page/limit/total`` were seen in a live response on
		2026-10-16). The ``page`` query parameter is honored and the default page size is
		200 (verified live 2026-10-16). A Bosta failure is raised escaped."""
		locations = []
		page = 1
		while True:
			response = self.request("GET", BOSTA_PICKUP_LOCATIONS_PATH, params={"page": page})
			if not is_success_response(response):
				frappe.throw(escape_html(self.error_message(response)))
			data = response_body(response).get("data")
			items = data.get("list") if isinstance(data, dict) else None
			if not isinstance(items, list):
				frappe.throw(_("Bosta returned the pickup locations in an unrecognised format."))
			if not items:
				return locations
			locations.extend(items)
			pages = min(cint(data.get("pages")), MAX_PICKUP_LOCATION_PAGES)
			if page >= pages:
				return locations
			page += 1

	def error_message(self, response):
		"""Return a bounded Bosta error suitable for an operator-facing message."""
		return _("Bosta request failed (HTTP {0}): {1}").format(
			response.status_code, error_message_from_response(response)
		)


def _normalise_awb_tracking_numbers(tracking_numbers):
	"""Return non-empty tracking numbers without changing their values."""
	if isinstance(tracking_numbers, str):
		tracking_numbers = tracking_numbers.split(",")
	if not isinstance(tracking_numbers, (list, tuple)):
		return []

	if any(number is None for number in tracking_numbers):
		return []
	numbers = [str(number).strip() for number in tracking_numbers]
	if not all(numbers):
		return []
	return numbers


def response_body(response):
	"""The JSON object body of a Bosta response, or ``{}`` when it is not one."""
	try:
		body = response.json()
	except ValueError:
		return {}
	return body if isinstance(body, dict) else {}


def is_success_response(response):
	return 200 <= response.status_code < 300 and response_body(response).get("success") is not False
