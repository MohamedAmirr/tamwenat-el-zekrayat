# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon reconciliation evidence for a Marketplace Order (#178 part 3).

``build_evidence()`` is pure: it reads only the Marketplace Order document, its
``noon_shipments``/``noon_shipment_units`` child tables and the per-unit
``latest_payload.items[]`` (the only place Noon's per-physical-unit ``mp_status``
/ ``integration_status`` / ``mp_item_nr`` live, #145).  No Noon call, no write.

Units are judged one by one and aggregated per Sales Order line key
``sku:<partner_sku>`` (the planner snapshot's ``external_item_id`` - not the
status-grouped Marketplace Order Item id):

- a unit is Sales-Order-eligible exactly as the snapshot builder decides
  (``NOON_ELIGIBLE_EXTERNAL_STATUS``, not ``NOON_INELIGIBLE_INTEGRATION_STATUS``);
- a line has started fulfillment when ANY of its units is ``SHIPPED`` OR sits in a
  shipment whose status is in ``_STARTED_SHIPMENT_STATUSES``; either source is
  enough (conservative: it blocks a change rather than letting one through).  Only a
  ``Created`` shipment exists at Noon; ``Pending`` (CreateShipment not yet sent) and
  ``Error`` (never created) shipments never left, so they do not count.  OPEN QUESTION:
  Noon documents no outcome for ``Cancel Pending`` (a CancelShipment call is in
  flight for a shipment that was ``Created``); it is counted as started until the
  cancellation is confirmed;
- full cancellation = no eligible unit remains;
- an unrecognized status (``UNSPECIFIED`` or a future value; Noon documents no
  meaning for it) makes the stage unknown instead of being guessed.

``Noon Return`` rows are deliberately not consumed yet: ``return`` evidence is
never produced here (deferred).  Own ``UpdateOrder`` out-of-stock marks cannot be
told apart from a customer cancellation with the data stored today.
"""

from frappe import _
from frappe.utils import cstr

from marketplace_integrations.integrations.noon.fulfillment import _latest_payload_items
from marketplace_integrations.integrations.noon.order_mapper import INTEGRATION_STATUS_MAP, MP_STATUS_MAP
from marketplace_integrations.services.sales_order_sync import (
	NOON_ELIGIBLE_EXTERNAL_STATUS,
	NOON_INELIGIBLE_INTEGRATION_STATUS,
)

_KNOWN_MP_STATUSES = set(MP_STATUS_MAP.values())
_KNOWN_INTEGRATION_STATUSES = set(INTEGRATION_STATUS_MAP.values())
_SHIPPED = INTEGRATION_STATUS_MAP["INTEGRATION_ITEM_STATUS_SHIPPED"]
# Deliberately separate from ``fulfillment._ACTIVE_SHIPMENT_STATUSES`` (which decides
# which units are still claimed by a shipment): only these shipments may have left.
_STARTED_SHIPMENT_STATUSES = {"Created", "Cancel Pending"}


def _started_shipment_mp_item_nrs(order):
	started_shipment_nrs = {
		row.integration_shipment_nr
		for row in order.noon_shipments or []
		if row.status in _STARTED_SHIPMENT_STATUSES
	}
	return {
		cstr(row.mp_item_nr).strip()
		for row in order.noon_shipment_units or []
		if row.integration_shipment_nr in started_shipment_nrs
	}


def build_evidence(order):
	units = [unit for unit in _latest_payload_items(order) if isinstance(unit, dict)]
	if not units:
		return {
			"source": "marketplace",
			"lifecycle_permits_change": None,
			"lifecycle_reason": _(
				"The Noon order has no per-unit item data, so it is unknown whether the order can still change."
			),
		}

	in_started_shipment = _started_shipment_mp_item_nrs(order)
	started = {}
	has_eligible = False
	has_unknown = False
	any_started = False
	for unit in units:
		mp_status = MP_STATUS_MAP.get(cstr(unit.get("mp_status")), cstr(unit.get("mp_status")))
		integration_status = INTEGRATION_STATUS_MAP.get(
			cstr(unit.get("integration_status")), cstr(unit.get("integration_status"))
		)
		if mp_status not in _KNOWN_MP_STATUSES or integration_status not in _KNOWN_INTEGRATION_STATUSES:
			has_unknown = True
		if (
			mp_status == NOON_ELIGIBLE_EXTERNAL_STATUS
			and integration_status != NOON_INELIGIBLE_INTEGRATION_STATUS
		):
			has_eligible = True
		unit_started = (
			integration_status == _SHIPPED or cstr(unit.get("mp_item_nr")).strip() in in_started_shipment
		)
		any_started = any_started or unit_started
		line_key = f"sku:{cstr(unit.get('partner_sku')).strip()}"
		started[line_key] = started.get(line_key, False) or unit_started

	evidence = {"source": "marketplace", "fulfillment_started": started}
	if has_unknown:
		evidence["lifecycle_permits_change"] = None
		evidence["lifecycle_reason"] = _(
			"A Noon unit has a status this app does not recognize, so it is unknown whether the order "
			"can still change."
		)
		return evidence
	if not has_eligible:
		evidence["cancellation"] = "full"
		if any_started:
			evidence["cancelled_after_fulfillment"] = True
		return evidence
	if all(started.values()):
		evidence["lifecycle_permits_change"] = False
		evidence["lifecycle_reason"] = _(
			"Noon fulfillment has already started for every line, so the change was not applied."
		)
	else:
		# Before or partway through fulfillment: a changed line is judged on its own flag.
		evidence["lifecycle_permits_change"] = True
	return evidence
