# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
import requests

from talabat_integration.api.auth import get_access_token, invalidate_access_token

REQUEST_TIMEOUT_SECONDS = 10


def talabat_request(method, path, **kwargs):
	"""Make an authenticated request to the Talabat Partner API.

	Single choke point for every Talabat endpoint (catalog, orders,
	promotions, ...) - do not call `requests` directly against Talabat
	elsewhere. Retries exactly once on a 401 with a freshly-fetched token.
	"""
	settings = frappe.get_single("Talabat Settings")
	url = f"{settings.get_base_url()}{path}"
	kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)

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

	try:
		return requests.request(method, url, headers=headers, **kwargs)
	except requests.exceptions.RequestException as e:
		frappe.throw(frappe._("Failed to reach Talabat: {0}").format(str(e)))
