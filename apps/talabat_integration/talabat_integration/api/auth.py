# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import time

import frappe
import requests

CACHE_KEY_PREFIX = "talabat_integration:access_token"
# Refresh a bit before actual expiry so a request never races a token that
# expires mid-flight.
EXPIRY_BUFFER_SECONDS = 60


def get_access_token():
	"""Return a valid Talabat Partner API access token, using the cache when possible."""
	settings = frappe.get_single("Talabat Settings")
	cache_key = _cache_key(settings.environment)

	cached = frappe.cache().get_value(cache_key)
	if cached and cached["expires_at"] > time.time():
		return cached["access_token"]

	token = _fetch_access_token(settings)
	frappe.cache().set_value(
		cache_key,
		token,
		expires_in_sec=max(token["expires_in"] - EXPIRY_BUFFER_SECONDS, 1),
	)
	return token["access_token"]


def _fetch_access_token(settings):
	response = requests.post(
		f"{settings.get_base_url()}/v2/oauth/token",
		data={
			"grant_type": "client_credentials",
			"client_id": settings.client_id,
			"client_secret": settings.get_password("client_secret"),
		},
	)

	if not response.ok:
		frappe.throw(
			frappe._("Failed to authenticate with Talabat: {0}").format(_error_message(response))
		)

	body = response.json()
	expires_in = body.get("expires_in", 7200)
	return {
		"access_token": body["access_token"],
		"expires_in": expires_in,
		"expires_at": time.time() + expires_in - EXPIRY_BUFFER_SECONDS,
	}


def _error_message(response):
	try:
		body = response.json()
	except ValueError:
		return response.text
	return body.get("error_description") or body.get("error") or response.text


def _cache_key(environment):
	return f"{CACHE_KEY_PREFIX}:{environment}"
