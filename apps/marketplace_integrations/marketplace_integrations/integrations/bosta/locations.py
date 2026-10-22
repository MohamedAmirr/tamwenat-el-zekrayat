# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bosta's cities for the Create / Edit Bosta Shipment dialogs (#245) and their city codes (#318).

The city list comes from Bosta's public ``GET /cities`` (``data.list[]`` of ``{_id, name,
nameAr, ...}``, per docs/bosta-integration/docs.yaml) and the city codes from
``GET /cities/getAllDistricts``. Both are cached for 24 hours in the Frappe cache. Nothing
is cached on failure, so a Bosta outage never poisons the cache and a refresh that fails
keeps the previous lists.
"""

import frappe
from frappe import _
from frappe.utils import cstr, escape_html

from marketplace_integrations.integrations.bosta.client import (
	BostaClient,
	is_success_response,
	response_body,
)

CACHE_TTL_SECONDS = 24 * 60 * 60
CITIES_CACHE_KEY = "marketplace_integrations:bosta:cities"
DIRECTORY_CACHE_KEY = "marketplace_integrations:bosta:directory"
LOOKUP_FAILED_CACHE_KEY = "marketplace_integrations:bosta:lookup_failed"
LOOKUP_FAILURE_TTL_SECONDS = 60


def get_cities(refresh=False):
	"""Bosta cities as ``[{id, name, name_ar}]``. ``refresh`` refetches the cities and drops
	the cached city codes too."""
	cache = frappe.cache()
	if not refresh:
		cached = cache.get_value(CITIES_CACHE_KEY)
		if cached:
			return cached

	client = BostaClient()
	response = client.get_cities()
	cities = _parse_cities(_data_or_throw(client, response))
	if refresh:
		cache.delete_value(DIRECTORY_CACHE_KEY)
		frappe.local.bosta_directory = None
	cache.set_value(CITIES_CACHE_KEY, cities, expires_in_sec=CACHE_TTL_SECONDS)
	return cities


def get_city(city_id):
	"""``{id, name, code}`` of a Bosta city: the name ``GET /cities`` gives it (what the
	shipment shows) and its code from the directory (what Bosta is sent). Throws for a city
	that is not in Bosta's list or has no code, so a name is never sent in place of a code."""
	city_id = cstr(city_id).strip()
	if not city_id:
		frappe.throw(_("Choose a Bosta city."))
	city = next((city for city in get_cities() if city["id"] == city_id), None)
	if not city:
		frappe.throw(_("Unknown Bosta city. Refresh the Bosta lists and choose the city again."))
	try:
		directory = get_directory()
	except BostaLookupUnavailable:
		frappe.throw(_("Could not load the Bosta city codes. Try again in a minute."))
	code = next((entry["code"] for entry in directory if entry["id"] == city_id), "")
	if not code:
		frappe.throw(
			_("Bosta has no city code for {0}. Refresh the Bosta lists and try again.").format(
				escape_html(city["name"])
			)
		)
	return {"id": city_id, "name": city["name"], "code": code}


class BostaLookupUnavailable(Exception):
	"""A Bosta directory lookup failed a moment ago, so no further lookup is tried (fail fast)."""


def reset_request_state():
	"""Forget this request's memoized directory and fail-fast flag, and the shared failure marker."""
	frappe.local.bosta_directory = None
	frappe.local.bosta_lookup_failed = False
	frappe.cache().delete_value(LOOKUP_FAILED_CACHE_KEY)


def lookups_blocked():
	"""True after a failed directory lookup in this request, or within the last minute in any
	request: orders then simply fall back to review (#305)."""
	return bool(
		getattr(frappe.local, "bosta_lookup_failed", False)
		or frappe.cache().get_value(LOOKUP_FAILED_CACHE_KEY)
	)


def _mark_lookup_failed():
	frappe.local.bosta_lookup_failed = True
	frappe.cache().set_value(LOOKUP_FAILED_CACHE_KEY, 1, expires_in_sec=LOOKUP_FAILURE_TTL_SECONDS)


def get_directory(refresh=False):
	"""Every Bosta city with its code: ``[{id, name, name_ar, code}]``. Cached 24 hours (and
	kept for the rest of the request); nothing is cached on failure. A failed fetch blocks
	further lookups (``BostaLookupUnavailable``) for this request and for a minute."""
	if not refresh and getattr(frappe.local, "bosta_directory", None):
		return frappe.local.bosta_directory
	cache = frappe.cache()
	if not refresh:
		cached = cache.get_value(DIRECTORY_CACHE_KEY)
		if cached:
			frappe.local.bosta_directory = cached
			return cached
		if lookups_blocked():
			raise BostaLookupUnavailable
	try:
		client = BostaClient()
		response = client.get_all_districts()
		directory = _parse_directory(_data_or_throw(client, response))
	except Exception:
		_mark_lookup_failed()
		raise
	cache.set_value(DIRECTORY_CACHE_KEY, directory, expires_in_sec=CACHE_TTL_SECONDS)
	frappe.local.bosta_directory = directory
	return directory


def city_name_for(city_id, default):
	"""The name ``GET /cities`` gives this city, else ``default`` (the directory's name), so the
	automatic path sends the same city name as the dialog."""
	try:
		for city in get_cities():
			if city["id"] == city_id:
				return city["name"]
	except Exception:
		frappe.clear_last_message()
	return default


def _data_or_throw(client, response):
	if not is_success_response(response):
		frappe.throw(escape_html(client.error_message(response)))
	body = response_body(response)
	if "data" not in body:
		frappe.throw(_("Bosta returned a list in an unrecognised format."))
	return body["data"]


def _parse_cities(data):
	# Documented as data.list[]; a bare list is accepted too.
	items = data.get("list") if isinstance(data, dict) else data
	if not isinstance(items, list):
		frappe.throw(_("Bosta returned the city list in an unrecognised format."))
	cities = []
	for item in items:
		if not isinstance(item, dict):
			continue
		city_id = cstr(item.get("_id") or item.get("id")).strip()
		name = cstr(item.get("name")).strip()
		if city_id and name:
			cities.append({"id": city_id, "name": name, "name_ar": cstr(item.get("nameAr")).strip()})
	if not cities:
		frappe.throw(_("Bosta returned no usable cities."))
	return cities


def _parse_directory(data):
	"""``GET /cities/getAllDistricts``: ``data[]`` of ``{cityId, cityName, cityOtherName,
	cityCode, districts: [...]}``. Only the city fields are kept."""
	items = data.get("list") if isinstance(data, dict) else data
	if not isinstance(items, list):
		frappe.throw(_("Bosta returned the district directory in an unrecognised format."))
	directory = []
	for item in items:
		if not isinstance(item, dict):
			continue
		city_id = cstr(item.get("cityId")).strip()
		city_name = cstr(item.get("cityName")).strip()
		if city_id and city_name:
			directory.append(
				{
					"id": city_id,
					"name": city_name,
					"name_ar": cstr(item.get("cityOtherName")).strip(),
					"code": cstr(item.get("cityCode")).strip(),
				}
			)
	if not directory:
		frappe.throw(_("Bosta returned no usable cities."))
	return directory


def get_pickup_locations():
	"""The business's Bosta pickup locations as ``[{id, name, address, city, is_default}]``
	for the Bosta Settings picker (#276). Uses the saved API key, works while Bosta is
	still disabled, and caches/stores nothing.

	``GET /pickup-locations`` is documented as ``data.list[]`` of ``{_id, locationName,
	address: {firstLine, city: {_id, name}}, isDefault}`` (field shapes from the docs.yaml
	example; ``_id``, ``locationName``, ``address.firstLine``, ``address.city.name`` and
	``isDefault`` were seen in a live response on 2026-10-16). The ``page`` query parameter
	is honored and the default page size is 200 (verified live 2026-10-16). A location
	repeated across pages is returned once."""
	client = BostaClient(allow_disabled=True)
	locations = []
	seen = set()
	for item in client.get_pickup_locations():
		if not isinstance(item, dict):
			continue
		location_id = cstr(item.get("_id")).strip()
		if not location_id or location_id in seen:
			continue
		seen.add(location_id)
		address = item.get("address") if isinstance(item.get("address"), dict) else {}
		city = address.get("city") if isinstance(address.get("city"), dict) else {}
		locations.append(
			{
				"id": location_id,
				"name": cstr(item.get("locationName")).strip(),
				"address": cstr(address.get("firstLine")).strip(),
				"city": cstr(city.get("name")).strip(),
				"is_default": bool(item.get("isDefault")),
			}
		)
	return locations
