# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Talabat reconciliation evidence for a Marketplace Order (#178).

``build_evidence()`` is pure: it reads only the Marketplace Order document and
its child rows (no Talabat call, no write) and translates Talabat's documented
``order.Order.status`` / ``cancellation.post_picked_up`` into the
platform-neutral evidence ``services/sales_order_reconciliation.plan()``
consumes (see that module for the key meanings).

Status spellings are exactly as in ``docs/talabat-integration/openapi.json``'s
``order.Order.status`` enum.  The documented prose once spells it ``CANCELED``;
that variant is deliberately not accepted and reads as an unknown status.

Line-level changes (``IN_CART``/``ADDITION`` quantities, ``NOT_FOUND``/
``REPLACED`` removals) are not evidence: they surface through the commercial
snapshot diff.  A promotion-only refresh changes no snapshot field and so is not
an edit.
"""

from frappe import _
from frappe.utils import cstr

# Talabat has not started picking/packing handover to a rider: edits are still allowed.
_EDITABLE_STATUSES = {"RECEIVED"}
# Picking and packing is finished (ready for rider, dispatched or delivered).
_FULFILLED_STATUSES = {"READY_FOR_PICKUP", "DISPATCHED", "DELIVERED"}
_CANCELLED_STATUS = "CANCELLED"


def build_evidence(order):
	status = cstr(order.external_status).strip()
	evidence = {"source": "marketplace"}
	line_ids = [cstr(row.external_item_id).strip() for row in order.items or []]

	if status == _CANCELLED_STATUS:
		evidence["cancellation"] = "full"
		if order.post_picked_up:
			evidence["cancelled_after_fulfillment"] = True
	elif status in _EDITABLE_STATUSES:
		evidence["lifecycle_permits_change"] = True
		evidence["fulfillment_started"] = dict.fromkeys(line_ids, False)
	elif status in _FULFILLED_STATUSES:
		evidence["lifecycle_permits_change"] = False
		evidence["lifecycle_reason"] = _(
			"The Talabat order is already {0}; a later change to it cannot be applied to the "
			"ERPNext Sales Order automatically."
		).format(status)
		evidence["fulfillment_started"] = dict.fromkeys(line_ids, True)
	else:
		evidence["lifecycle_permits_change"] = None
		evidence["lifecycle_reason"] = _(
			"The Talabat order status ({0}) is not recognized, so it is unknown whether the order can "
			"still change."
		).format(status or _("blank"))
	return evidence
