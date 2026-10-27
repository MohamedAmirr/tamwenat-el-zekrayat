# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Expected-change marker for Noon Mark Out of Stock (#364): what is hashed and compared.

The marker mechanics (store, compare, clear, the echo path) are in ``services/echo_guard.py``.
The sent state is the ``mp_item_nr`` values each expected ``OUT_OF_STOCK``; the incoming state
is the ``integration_status`` of exactly those units in the incoming order.  Cancel Shipment is
not covered.
"""

import json

import frappe
from frappe.utils import cstr

from marketplace_integrations.integrations.noon.order_mapper import INTEGRATION_STATUS_MAP
from marketplace_integrations.services.echo_guard import digest, record_expected_change

NOON_OUT_OF_STOCK = INTEGRATION_STATUS_MAP["INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"]
_NOON_MISSING = "MISSING"


def sent_hash(mp_item_nrs):
	return digest([[nr, NOON_OUT_OF_STOCK] for nr in sorted(mp_item_nrs)])


def incoming_hash(raw_order, mp_item_nrs):
	statuses = {}
	for item in (raw_order or {}).get("items") or []:
		if isinstance(item, dict):
			raw = cstr(item.get("integration_status")).strip()
			statuses[cstr(item.get("mp_item_nr")).strip()] = INTEGRATION_STATUS_MAP.get(raw, raw)
	return digest([[nr, statuses.get(nr, _NOON_MISSING)] for nr in sorted(mp_item_nrs)])


def record_out_of_stock(marketplace_order, mp_item_nrs):
	"""Store the marker for a Noon Mark Out of Stock call the platform accepted.

	Does not commit: the worker commits right after, before its next HTTP call.
	"""
	nrs = sorted(set(mp_item_nrs))
	record_expected_change(marketplace_order, sent_hash(nrs), frappe.as_json(nrs))


def incoming_matcher(raw_order):
	"""``incoming_hash_fn`` for ``consume_expected_change`` over a raw Noon order."""

	def hash_for_scope(scope):
		try:
			nrs = json.loads(scope or "[]")
		except ValueError:
			return None
		return incoming_hash(raw_order, nrs) if nrs else None

	return hash_for_scope
