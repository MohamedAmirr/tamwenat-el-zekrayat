# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon FBPI order -> normalized Marketplace Order translation (#145).

This is the single shared translation path between ``ListFbpiOrders`` rows
and a ``GetFbpiOrder`` body - both share the identical documented response
shape (docs/noon-integration/noon_api_full.md), so ``normalize_order()``
accepts either one. ``latest_payload`` is always whichever raw object was
actually passed in - the canonical ``GetFbpiOrder`` body when a full GET was
performed for that order, or the ``ListFbpiOrders`` element otherwise
(decision 3); #145 itself never performs a per-order GetFbpiOrder call (see
``integrations/noon/orders.py``'s module docstring for why).

Unlike Talabat's/Shopify's normalizer, this one is not pure translation: it
also resolves each line's ``partner_sku`` through
``services/mappings.py::resolve_mapping_and_item()`` (decision 7) so it can
aggregate Noon's one-entry-per-physical-unit ``items[]`` into one
Marketplace Order Item row per ``(partner_sku, mp_status, integration_status)``
combination (decision 13, review finding A). The resolution result itself is
never stored on the row - only used to decide whether the SKU is currently
mapped, for the unmapped-SKU signal (decision 19) - so a later mapping fix
still takes effect on presentation/sync without re-import, exactly as
decision 7 requires. Grouping (and the row's identity, see
``_aggregate_items()``) is always by ``partner_sku`` plus the unit's own raw
status fields, never by the resolved Item, so a SKU getting mapped between
two polls can never change that row's identity - see
``_aggregate_items()``'s docstring.
"""

import frappe
from frappe import _
from frappe.utils import convert_utc_to_system_timezone, cstr, flt, get_datetime, get_datetime_str

from marketplace_integrations.services.mappings import resolve_mapping_and_item

# Noon's item-level enums are documented with a verbose prefix
# (docs/noon-integration/noon_api_full.md); #145 decision 14 names the
# Marketplace Order Item Select options using only the short suffix. This
# map is where that prefix is stripped - an unrecognized enum value (a
# future Noon addition, or MP_ITEM_STATUS_UNSPECIFIED /
# INTEGRATION_ITEM_STATUS_UNSPECIFIED) is preserved by falling back to the
# raw value rather than silently dropping it.
MP_STATUS_MAP = {
	"MP_ITEM_STATUS_CONFIRMED": "CONFIRMED",
	"MP_ITEM_STATUS_CANCELLED": "CANCELLED",
}
INTEGRATION_STATUS_MAP = {
	"INTEGRATION_ITEM_STATUS_ACKNOWLEDGED": "ACKNOWLEDGED",
	"INTEGRATION_ITEM_STATUS_OUT_OF_STOCK": "OUT_OF_STOCK",
	"INTEGRATION_ITEM_STATUS_SHIPPED": "SHIPPED",
}

REQUIRED_ORDER_FIELDS = (
	"fbpi_order_nr",
	"mp_code",
	"mp_order_nr",
	"mp_country_code",
	"customer_country_code",
	"merchant_code",
	"currency_code",
	"warehouse_code",
	"order_created_at",
)
REQUIRED_ITEM_FIELDS = ("mp_item_nr", "partner_sku", "mp_status", "integration_status")


def normalize_order(account, raw_order):
	"""Translate one raw Noon FBPI order object into the normalized
	structure ``services/marketplace_orders.py``'s ``upsert_marketplace_order()``
	expects.

	``account`` is the resolved Noon ``Marketplace Account`` document (not
	just its name) - both the account-scope filters below and the per-line
	mapping resolution need more than the account's name.

	Raises ``frappe.ValidationError`` and normalizes nothing (decision 4)
	when:
	- ``raw_order`` isn't an object, or is missing/blank any of
	  ``REQUIRED_ORDER_FIELDS``, or ``items`` isn't a list, or
	  ``order_created_at`` is present but unparseable;
	- any item entry isn't an object, or is missing/blank any of
	  ``REQUIRED_ITEM_FIELDS``, or has a missing/non-numeric
	  ``delivered_invoice_price`` (also a required field, just not a blank-
	  string-checkable one);
	- the order's ``warehouse_code``/``mp_country_code`` doesn't match this
	  account's ``noon_warehouse_code``/``noon_country_code`` (decision 20 -
	  a scope violation is a visible rejection, not a silent skip).

	Skipping an order whose ``mp_code`` isn't ``"noon"`` (decision 20) is
	deliberately NOT this function's job - that's not a malformed/rejected
	order, it's a different marketplace's order that should never reach
	here at all; ``integrations/noon/orders.py`` filters those out before
	ever calling this function.
	"""
	if not isinstance(raw_order, dict):
		frappe.throw(_("Noon FBPI order payload must be an object."))
	_require_fields(raw_order, REQUIRED_ORDER_FIELDS, _("Noon FBPI order"))

	fbpi_order_nr = cstr(raw_order.get("fbpi_order_nr")).strip()
	warehouse_code = cstr(raw_order.get("warehouse_code")).strip()
	mp_country_code = cstr(raw_order.get("mp_country_code")).strip()
	account_warehouse_code = cstr(account.noon_warehouse_code).strip()
	account_country_code = cstr(account.noon_country_code).strip()
	if not account_warehouse_code or warehouse_code != account_warehouse_code:
		frappe.throw(
			_(
				"Noon order {0} warehouse {1} does not match Marketplace Account {2}'s Noon Warehouse Code {3}."
			).format(fbpi_order_nr, warehouse_code, account.name, account_warehouse_code)
		)
	if not account_country_code or mp_country_code != account_country_code:
		frappe.throw(
			_(
				"Noon order {0} country {1} does not match Marketplace Account {2}'s Noon Country Code {3}."
			).format(fbpi_order_nr, mp_country_code, account.name, account_country_code)
		)

	raw_items = raw_order.get("items")
	if not isinstance(raw_items, list):
		frappe.throw(_("Noon FBPI order {0} items must be a list.").format(fbpi_order_nr))
	for item in raw_items:
		if not isinstance(item, dict):
			frappe.throw(_("Noon FBPI order {0} item entries must be objects.").format(fbpi_order_nr))
		_require_fields(item, REQUIRED_ITEM_FIELDS, _("Noon FBPI order {0} item").format(fbpi_order_nr))
		if _parse_price(item.get("delivered_invoice_price")) is None:
			frappe.throw(
				_("Noon FBPI order {0} item is missing a valid delivered_invoice_price.").format(
					fbpi_order_nr
				)
			)

	items, unmapped_skus = _aggregate_items(account, raw_items)

	return {
		"external_order_id": fbpi_order_nr,
		"platform_order_reference": raw_order.get("mp_order_nr"),
		"order_code": raw_order.get("mp_order_nr"),
		"received_on": _parse_datetime(raw_order.get("order_created_at"), fbpi_order_nr),
		"presentment_currency": raw_order.get("currency_code"),
		"items": items,
		"latest_payload": raw_order,
		# Not one of upsert_marketplace_order()'s known ORDER_FIELDS/
		# JSON_ORDER_FIELDS, so it's silently ignored by persistence -
		# carried here only so integrations/noon/orders.py can write
		# decision 19's per-run unmapped-SKU Marketplace Sync Log row
		# without a second mapping-resolution pass over the same items.
		"_unmapped_skus": unmapped_skus,
	}


def _require_fields(payload, fields, subject):
	for field in fields:
		value = payload.get(field)
		if value is None or (isinstance(value, str) and not value.strip()):
			frappe.throw(_("{0} is missing required field {1}.").format(subject, field))


def _parse_price(value):
	"""Return ``value`` as a float, or ``None`` if it's missing/non-numeric.

	``delivered_invoice_price`` is a required field (decision 4): a missing
	or malformed value must reject the whole order rather than silently
	coercing to 0.0, which is what ``frappe.utils.flt()`` alone would do.
	"""
	if value is None or isinstance(value, bool):
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def _aggregate_items(account, raw_items):
	"""Group Noon's one-entry-per-physical-unit ``items[]`` into one row per
	``(partner_sku, mp_status, integration_status)`` combination, summing
	quantity/amount within each group (decision 13, review finding A).

	Noon documents no merge rule for two entries of the same SKU whose
	``mp_status``/``integration_status`` differ (e.g. some physical units
	of a SKU already cancelled, others still confirmed) - an earlier version
	of this function grouped by ``partner_sku`` alone and let the last
	entry encountered silently overwrite the row's status, which discarded
	the cancelled units' own status and folded their quantity/amount into
	the confirmed row. Grouping on status too fixes that: the common case
	(every unit of a SKU shares one status) still yields exactly one row per
	SKU, while a genuinely mixed-status order yields one row per distinct
	status combination, each with its own correct summed quantity/amount for
	just the units in that group. The full per-unit detail always remains
	available in ``latest_payload`` regardless.

	An unmapped ``partner_sku`` is grouped the same way - by
	``(partner_sku, mp_status, integration_status)``, never by the resolved
	Item, so there's nothing else to key on and the order still fully
	imports uniformly whether or not a mapping exists (decision 7); only a
	later Sales Order sync (#146, not this issue) is expected to fail closed
	on it.

	The aggregated row's ``external_item_id`` (the identity
	``upsert_marketplace_order()`` upserts child rows by) is derived as
	``"sku:<partner_sku>:<mp_status>:<integration_status>"`` using Noon's own
	raw enum values - unaffected by whether a Marketplace Product Mapping
	exists for it yet, for the same reason as before: a mapping getting
	created between two polls must never change a row's identity, or
	``upsert_marketplace_order()`` (which matches child rows by this key)
	would insert a *second* row for the same physical units instead of
	updating the first, duplicating the reported quantity (see
	``test_import_order_sku_mapped_between_polls_updates_same_row``).

	Including status in the identity means a genuine status transition
	between two polls (e.g. a unit moving from
	``MP_ITEM_STATUS_CONFIRMED``/``INTEGRATION_ITEM_STATUS_ACKNOWLEDGED`` to
	.../``INTEGRATION_ITEM_STATUS_SHIPPED``) changes that unit's group key -
	it's no longer reported under its old key on the next poll.
	``upsert_marketplace_order()``'s own stated policy is that a child row
	absent from a later payload is retained, not deleted, so the old
	status-keyed row stays as a record of "this many units had this status
	as of the last poll that saw them there", and a new row appears for the
	new status. This is bounded, not runaway: Noon's status enums are a
	small fixed set, so re-polling the same order - whether the status is
	unchanged or has moved on - reuses the same finite set of group keys
	rather than growing a new row on every poll; a row is only ever added
	the first time a given status combination is seen for that SKU on that
	order (see ``test_import_order_status_transition_does_not_duplicate_rows``).
	"""
	groups = {}
	group_order = []
	mapping_cache = {}
	unmapped_skus = []

	for item in raw_items:
		partner_sku = cstr(item.get("partner_sku")).strip()
		if partner_sku not in mapping_cache:
			mapping_cache[partner_sku] = resolve_mapping_and_item(account.name, partner_sku)
		resolved = mapping_cache[partner_sku]
		if not resolved and partner_sku not in unmapped_skus:
			unmapped_skus.append(partner_sku)

		raw_mp_status = cstr(item.get("mp_status")).strip()
		raw_integration_status = cstr(item.get("integration_status")).strip()
		group_key = f"sku:{partner_sku}:{raw_mp_status}:{raw_integration_status}"

		row = groups.get(group_key)
		if not row:
			row = {
				"external_item_id": group_key,
				"external_sku": partner_sku,
				"pricing_type": "UNIT",
				"original_quantity": 0,
				"fulfilled_quantity": 0,
				"current_quantity": 0,
				"transaction_amount": 0.0,
				"transaction_amount_missing": 0,
				"external_status": MP_STATUS_MAP.get(raw_mp_status, raw_mp_status),
				"integration_status": INTEGRATION_STATUS_MAP.get(raw_integration_status, raw_integration_status),
			}
			groups[group_key] = row
			group_order.append(group_key)

		row["original_quantity"] += 1
		row["fulfilled_quantity"] += 1
		row["current_quantity"] += 1
		row["transaction_amount"] += flt(item.get("delivered_invoice_price"))

	return [groups[key] for key in group_order], unmapped_skus


def _parse_datetime(value, fbpi_order_nr):
	"""Convert Noon's ``order_created_at`` UTC instant into Frappe's
	canonical Datetime string, in the site's system timezone - same
	conversion Talabat's ``order_mapper.py`` uses and for the same reason
	(``get_datetime_str()`` alone only formats a datetime's own fields, it
	never converts timezones; see docs/frappe-conventions.md).

	``order_created_at`` is one of ``REQUIRED_ORDER_FIELDS`` (already
	validated non-blank by ``_require_fields()`` before this is called), so
	a value that still fails to parse here is malformed, not missing -
	decision 4 treats a malformed required field the same as a missing one:
	the whole order is rejected (``frappe.throw()``), never silently stored
	with a blank ``received_on``.
	"""
	raw_value = cstr(value).strip()
	try:
		parsed = get_datetime(raw_value)
		return get_datetime_str(convert_utc_to_system_timezone(parsed))
	except Exception:
		frappe.throw(
			_("Noon FBPI order {0} has a malformed order_created_at value {1}.").format(
				fbpi_order_nr, raw_value
			)
		)
