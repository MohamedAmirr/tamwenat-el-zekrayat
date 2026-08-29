# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe

from talabat_integration.api.auth import _request, get_access_token, invalidate_access_token


def talabat_request(method, path, **kwargs):
	"""Make an authenticated request to the Talabat Partner API.

	Single choke point for every Talabat endpoint (catalog, orders,
	promotions, ...) - do not call `requests` directly against Talabat
	elsewhere. Retries exactly once on a 401 with a freshly-fetched token.

	Returns the raw response for any non-401 status (200, 4xx, 5xx alike) -
	unlike auth.get_access_token(), this does NOT raise on a non-2xx
	response. Different Talabat endpoints use different success codes, so
	callers are responsible for checking `response.ok`/status themselves.
	"""
	settings = frappe.get_single("Talabat Settings")
	path = path if path.startswith("/") else f"/{path}"
	url = f"{settings.get_base_url()}{path}"

	response = _send(method, url, get_access_token(), **kwargs)

	if response.status_code == 401:
		invalidate_access_token()
		response = _send(method, url, get_access_token(), **kwargs)
		if response.status_code == 401:
			frappe.throw(
				frappe._(
					"Talabat rejected the request as unauthorized even after refreshing the access token."
				)
			)

	return response


def _send(method, url, access_token, **kwargs):
	headers = kwargs.pop("headers", {}) or {}
	headers["Authorization"] = f"Bearer {access_token}"
	return _request(method, url, headers=headers, **kwargs)
