# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bosta's city and district lists for the Create Bosta Shipment dialog (#245).

Both lists come from Bosta's public ``GET /cities`` and ``GET /cities/{cityId}/districts``
and are cached for 24 hours in the Frappe cache. Nothing is cached on failure, so a
Bosta outage never poisons the cache and a refresh that fails keeps the previous lists.

Response shapes follow docs/bosta-integration/docs.yaml (checked against its examples,
not a live call): cities are ``data.list[]`` of ``{_id, name, nameAr, ...}``; districts
are ``data[]`` of ``{zoneName, districtId, districtName, districtOtherName,
dropOffAvailability, ...}``.
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
DISTRICTS_CACHE_KEY = "marketplace_integrations:bosta:districts:{city_id}"
DIRECTORY_CACHE_KEY = "marketplace_integrations:bosta:directory"
LOOKUP_FAILED_CACHE_KEY = "marketplace_integrations:bosta:lookup_failed"
LOOKUP_FAILURE_TTL_SECONDS = 60
PIN_CACHE_KEY = "marketplace_integrations:bosta:pin:{lat}:{lng}"


def get_cities(refresh=False):
	"""Bosta cities as ``[{id, name, name_ar}]``. ``refresh`` refetches the cities and
	drops every cached district list (they hang off the cities)."""
	cache = frappe.cache()
	if not refresh:
		cached = cache.get_value(CITIES_CACHE_KEY)
		if cached:
			return cached

	client = BostaClient()
	response = client.get_cities()
	cities = _parse_cities(_data_or_throw(client, response))
	if refresh:
		stale_ids = {city["id"] for city in cache.get_value(CITIES_CACHE_KEY) or []}
		for city_id in stale_ids | {city["id"] for city in cities}:
			cache.delete_value(DISTRICTS_CACHE_KEY.format(city_id=city_id))
	if refresh:
		cache.delete_value(DIRECTORY_CACHE_KEY)
		frappe.local.bosta_directory = None
	cache.set_value(CITIES_CACHE_KEY, cities, expires_in_sec=CACHE_TTL_SECONDS)
	return cities


def get_districts(city_id, refresh=False):
	"""The city's districts that accept drop-offs, as ``[{id, name, name_ar, zone}]``."""
	city_id = cstr(city_id).strip()
	if not city_id:
		frappe.throw(_("A Bosta city ID is required."))
	# Only ids from Bosta's own city list reach the HTTP path (and the cache).
	if city_id not in {city["id"] for city in get_cities()}:
		frappe.throw(_("Unknown Bosta city. Refresh the Bosta lists and choose the city again."))
	cache = frappe.cache()
	key = DISTRICTS_CACHE_KEY.format(city_id=city_id)
	if not refresh:
		cached = cache.get_value(key)
		if cached is not None:
			return cached

	client = BostaClient()
	response = client.get_districts(city_id)
	districts = _parse_districts(_data_or_throw(client, response))
	cache.set_value(key, districts, expires_in_sec=CACHE_TTL_SECONDS)
	return districts


class BostaLookupUnavailable(Exception):
	"""A Bosta district lookup failed a moment ago, so no further lookup is tried (fail fast)."""


def reset_request_state():
	"""Forget this request's memoized directory and fail-fast flag, and the shared failure marker."""
	frappe.local.bosta_directory = None
	frappe.local.bosta_name_index = None
	frappe.local.bosta_lookup_failed = False
	frappe.cache().delete_value(LOOKUP_FAILED_CACHE_KEY)


def lookups_blocked():
	"""True after a failed directory/pinLocation lookup in this request, or within the last
	minute in any request: orders then simply fall back to review (#305)."""
	return bool(
		getattr(frappe.local, "bosta_lookup_failed", False)
		or frappe.cache().get_value(LOOKUP_FAILED_CACHE_KEY)
	)


def _mark_lookup_failed():
	frappe.local.bosta_lookup_failed = True
	frappe.cache().set_value(LOOKUP_FAILED_CACHE_KEY, 1, expires_in_sec=LOOKUP_FAILURE_TTL_SECONDS)


def get_directory(refresh=False):
	"""Every Bosta city with its drop-off-available districts, for district matching:
	``[{id, name, name_ar, code, districts: [{id, name, name_ar}]}]``. Cached 24 hours (and
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


def get_pin_districts(lat, lng):
	"""Bosta's districts at a coordinate, as ``[{city_id, district_id, name}]`` (drop-off-capable
	only), cached 24 hours per coordinate rounded to 4 decimals. Any failure logs a short Error
	Log entry and returns ``[]``: a pin lookup only ever helps and never blocks (#305)."""
	try:
		lat, lng = round(float(lat), 4), round(float(lng), 4)
	except (TypeError, ValueError):
		return []
	cache = frappe.cache()
	key = PIN_CACHE_KEY.format(lat=lat, lng=lng)
	cached = cache.get_value(key)
	if cached is not None:
		return cached
	if lookups_blocked():
		return []
	try:
		client = BostaClient()
		data = _data_or_throw(client, client.pin_location(lat, lng))
		items = data.get("list") if isinstance(data, dict) else data
		if not isinstance(items, list):
			raise ValueError("unrecognised pinLocation response")
	except Exception:
		_mark_lookup_failed()
		frappe.clear_last_message()
		frappe.log_error(frappe.get_traceback(with_context=False)[-500:], "Bosta pin location failed")
		return []
	rows = []
	for item in items:
		if not isinstance(item, dict) or item.get("dropOffAvailability") is False:
			continue
		city_id = cstr(item.get("cityId")).strip()
		district_id = cstr(item.get("districtId")).strip()
		if city_id and district_id:
			rows.append(
				{
					"city_id": city_id,
					"district_id": district_id,
					"name": cstr(item.get("districtName")).strip(),
				}
			)
	cache.set_value(key, rows, expires_in_sec=CACHE_TTL_SECONDS)
	return rows


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


def _parse_districts(data):
	items = data.get("list") if isinstance(data, dict) else data
	if not isinstance(items, list):
		frappe.throw(_("Bosta returned the district list in an unrecognised format."))
	districts = []
	for item in items:
		if not isinstance(item, dict) or item.get("dropOffAvailability") is False:
			continue
		district_id = cstr(item.get("districtId") or item.get("_id") or item.get("id")).strip()
		name = cstr(item.get("districtName") or item.get("name")).strip()
		if district_id and name:
			districts.append(
				{
					"id": district_id,
					"name": name,
					"name_ar": cstr(item.get("districtOtherName") or item.get("nameAr")).strip(),
					"zone": cstr(item.get("zoneName")).strip(),
				}
			)
	return districts


def _parse_directory(data):
	"""``GET /cities/getAllDistricts``: ``data[]`` of ``{cityId, cityName, cityOtherName,
	cityCode, districts: [{districtId, districtName, districtOtherName,
	dropOffAvailability}]}``. Districts that do not accept drop-offs are left out."""
	items = data.get("list") if isinstance(data, dict) else data
	if not isinstance(items, list):
		frappe.throw(_("Bosta returned the district directory in an unrecognised format."))
	directory = []
	for item in items:
		if not isinstance(item, dict):
			continue
		city_id = cstr(item.get("cityId")).strip()
		city_name = cstr(item.get("cityName")).strip()
		if not city_id or not city_name:
			continue
		districts = []
		for district in item.get("districts") or []:
			if not isinstance(district, dict) or district.get("dropOffAvailability") is False:
				continue
			district_id = cstr(district.get("districtId")).strip()
			district_name = cstr(district.get("districtName")).strip()
			if district_id and district_name:
				districts.append(
					{
						"id": district_id,
						"name": district_name,
						"name_ar": cstr(district.get("districtOtherName")).strip(),
					}
				)
		directory.append(
			{
				"id": city_id,
				"name": city_name,
				"name_ar": cstr(item.get("cityOtherName")).strip(),
				"code": cstr(item.get("cityCode")).strip(),
				"districts": districts,
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
