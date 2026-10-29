# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Persistent record of handed-over Noon units that could not be drafted (#368).

``services/delivery_note_drafts.py`` returns the units it could not put on a draft Delivery
Note.  Noon's Mark Handed Over stores them on the shipment row (``skipped_units``, a JSON list,
and ``skipped_units_on``).  That record, not a one-time status write, is authoritative: while a
shipment that is not Cancelled or Discarded has a *final* entry, the order is kept in Needs
Action with ``DELIVERY_NOTE_UNITS_SKIPPED`` (``reassert()``, called at the end of
``Marketplace Order.before_save`` so no save, sync or recheck can clear it).  The record is
removed only when the units are drafted (a later Mark Handed Over or the automatic retry) or the
shipment is Cancelled or Discarded.

An entry is ``{"external_sku", "item", "qty", "reason", "final"}``.  ``reason`` is ``unmapped``
(no enabled product mapping), ``not_on_order`` (the Item is not on the Sales Order) or
``excess`` (more units than the Sales Order row still has undelivered).  The last two mean the
Sales Order is behind the platform order, so a sync is queued first; they only become ``final``
after a sync has run and the retry still cannot place them.  Until then the order shows Syncing,
which ``reassert()`` leaves alone.

#390: the Delivery Note of a shipment is recorded per unit (``Marketplace Order Shipment Unit.
delivery_note``), so a unit is covered while that note is live.  The shipment fields
``delivery_note`` / ``delivery_notes`` are no longer written.

Conflicts: ``reassert()`` never overwrites a different Sync Error or Needs Action reason.  When
that reason clears the order is Synced and the next save re-applies this one.  Sync Error is
therefore shown before this Needs Action.
"""

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from marketplace_integrations.services import sync_reason_codes as reasons
from marketplace_integrations.services.delivery_note_drafts import (
	NOT_ON_ORDER,
	RESYNC_TRIGGER,
	UNMAPPED,
)
from marketplace_integrations.services.sync_status import SYNCED

INACTIVE_SHIPMENT_STATUSES = ("Cancelled", "Discarded")
SHIPMENT_DOCTYPE = "Marketplace Order Shipment"
UNIT_DOCTYPE = "Marketplace Order Shipment Unit"


def load_entries(shipment):
	"""The shipment's recorded unresolved entries ([] when none or unreadable)."""
	raw = cstr(shipment.get("skipped_units")).strip()
	if not raw:
		return []
	try:
		entries = frappe.parse_json(raw)
	except ValueError:
		return []
	return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def store_entries(shipment, entries):
	"""Replace the record on the shipment row (in memory; the caller saves the order)."""
	if not entries:
		shipment.skipped_units = None
		shipment.skipped_units_on = None
		return
	shipment.skipped_units = frappe.as_json(entries)
	if not shipment.get("skipped_units_on"):
		shipment.skipped_units_on = now_datetime()


def clear_entries(shipment):
	store_entries(shipment, [])


def entry_label(entry):
	return cstr(entry.get("external_sku")).strip() or cstr(entry.get("item")).strip() or _("blank")


def entry_text(entry):
	label = entry_label(entry)
	reason = entry.get("reason")
	if reason == UNMAPPED:
		return _("{0}: no enabled product mapping").format(label)
	if reason == NOT_ON_ORDER:
		return _("{0}: not on the Sales Order").format(label)
	return _("{0}: {1} more than the Sales Order has").format(label, entry.get("qty"))


def active_shipments(order):
	return [row for row in order.get("noon_shipments") or [] if row.status not in INACTIVE_SHIPMENT_STATUSES]


def final_entries(order):
	return [e for row in active_shipments(order) for e in load_entries(row) if e.get("final")]


def has_unresolved(order_name):
	"""True when any shipment row of ``order_name`` still has a record (cheap pre-check)."""
	return bool(
		frappe.db.exists(
			"Marketplace Order Shipment",
			{"parent": order_name, "parenttype": "Marketplace Order", "skipped_units": ["!=", ""]},
		)
	)


def reassert(order):
	"""Keep ``order`` in Needs Action while a final unresolved record exists (in memory).

	Called at the end of ``Marketplace Order.before_save``.  Leaves Syncing, Cancelled and a
	different Sync Error or Needs Action reason alone.  When no final record is left and this
	reason is still on the order, the order is queued for a normal re-sync so the planner
	recomputes the real status.
	"""
	if order.get("platform") != "Noon":
		return
	entries = final_entries(order)
	if entries:
		same_reason = order.get("sync_reason_code") == reasons.DELIVERY_NOTE_UNITS_SKIPPED
		if order.erpnext_sync_status == SYNCED or same_reason:
			message = _("Not added to the Delivery Note: {0}").format("; ".join(entry_text(e) for e in entries))
			reasons.apply_sync_state(order, reasons.DELIVERY_NOTE_UNITS_SKIPPED, message)
		return
	if order.get("sync_reason_code") == reasons.DELIVERY_NOTE_UNITS_SKIPPED:
		from marketplace_integrations.services.sales_order_sync import queue_resync_in_memory

		reasons.clear_sync_state(order)
		queue_resync_in_memory(order, RESYNC_TRIGGER)


def unit_rows(units, integration_shipment_nr):
	"""The unit rows (``Marketplace Order Shipment Unit``) of one shipment."""
	return [row for row in units or [] if row.get("integration_shipment_nr") == integration_shipment_nr]


def shipment_notes(units, integration_shipment_nr):
	"""Names of the Delivery Notes recorded on the shipment's unit rows (#390), first-seen order.
	``units`` is any iterable of rows with ``integration_shipment_nr`` and ``delivery_note``."""
	names = []
	for row in unit_rows(units, integration_shipment_nr):
		name = cstr(row.get("delivery_note")).strip()
		if name and name not in names:
			names.append(name)
	return names


def record_note(rows, name):
	"""Record ``name`` on the given unit rows (in memory); the caller saves the order."""
	for row in rows:
		row.delivery_note = name


def live_note(name):
	"""``name`` while it is a draft or submitted Delivery Note.  A cancelled note is followed to
	the note that amends it (``amended_from``), so an amended draft counts as the shipment's."""
	for _hop in range(3):
		if not name:
			return None
		docstatus = frappe.db.get_value("Delivery Note", name, "docstatus")
		if docstatus is None:
			return None
		if docstatus != 2:
			return name
		name = frappe.db.get_value("Delivery Note", {"amended_from": name, "docstatus": ["!=", 2]}, "name")
	return None


def live_notes(units, integration_shipment_nr):
	"""Live Delivery Notes of the shipment (see ``live_note``), first note first."""
	live = []
	for name in shipment_notes(units, integration_shipment_nr):
		found = live_note(name)
		if found and found not in live:
			live.append(found)
	return live


def is_covered(row):
	"""True while the unit row's Delivery Note is live (draft, submitted or its amendment)."""
	return bool(live_note(cstr(row.get("delivery_note")).strip()))


def draft_note(units, integration_shipment_nr):
	"""The shipment's live Delivery Note that is still a draft, else None."""
	for name in live_notes(units, integration_shipment_nr):
		if frappe.db.get_value("Delivery Note", name, "docstatus") == 0:
			return name
	return None


def submitted_note(units, integration_shipment_nr):
	"""The shipment's live submitted Delivery Note, else None."""
	for name in live_notes(units, integration_shipment_nr):
		if frappe.db.get_value("Delivery Note", name, "docstatus") == 1:
			return name
	return None


# ---------------------------------------------------------------------------
# SHIPPED units (#390)
# ---------------------------------------------------------------------------

STATUS_SHIPPED = "INTEGRATION_ITEM_STATUS_SHIPPED"
STATUS_OUT_OF_STOCK = "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"
MP_STATUS_CANCELLED = "MP_ITEM_STATUS_CANCELLED"


def payload_items(latest_payload):
	"""``items`` of a stored ``latest_payload`` (dict or JSON text); [] when unreadable."""
	if isinstance(latest_payload, str):
		try:
			latest_payload = frappe.parse_json(latest_payload)
		except ValueError:
			return []
	items = latest_payload.get("items") if isinstance(latest_payload, dict) else None
	return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _is_active(item):
	"""A unit that still has to ship: not cancelled and not out of stock."""
	return item.get("mp_status") != MP_STATUS_CANCELLED and item.get("integration_status") != STATUS_OUT_OF_STOCK


def shipment_plan(payload, shipment_units):
	"""``{"shipped": [unit rows SHIPPED on Noon], "all_shipped": bool}`` for one shipment.

	``payload`` is ``payload_items()``; ``shipment_units`` the shipment's unit rows.  A unit absent
	from the payload counts as active and not shipped.  ``all_shipped`` needs at least one active unit
	and every active unit SHIPPED.
	"""
	by_nr = {cstr(i.get("mp_item_nr")).strip(): i for i in payload}
	shipped = []
	active = 0
	active_shipped = 0
	for row in shipment_units:
		item = by_nr.get(cstr(row.get("mp_item_nr")).strip())
		is_shipped = bool(item) and item.get("integration_status") == STATUS_SHIPPED
		if is_shipped:
			shipped.append(row)
		if not item or _is_active(item):
			active += 1
			active_shipped += 1 if is_shipped else 0
	return {"shipped": shipped, "all_shipped": bool(active) and active == active_shipped}


def shipped_work_pending(payload, shipments, units):
	"""True when a Created shipment has a SHIPPED unit no live Delivery Note covers, or has all its
	active units SHIPPED while its note is still a draft (the trigger of ``process_shipped_units``)."""
	for shipment in shipments:
		if shipment.get("status") != "Created":
			continue
		nr = shipment.get("integration_shipment_nr")
		rows = unit_rows(units, nr)
		plan = shipment_plan(payload, rows)
		if any(not is_covered(row) for row in plan["shipped"]):
			return True
		if plan["all_shipped"] and draft_note(units, nr):
			return True
	return False


def shipped_problems(payload, shipments, units):
	"""Shipped units that get no Delivery Note: ``{"outside": [item, ...], "after_submit":
	[(item, note), ...]}`` (``item`` is the payload dict).

	``outside``: SHIPPED but not a unit of a Created shipment.  ``after_submit``: SHIPPED, uncovered,
	and the shipment's note is already submitted with no draft left to add to.
	"""
	created = {s.get("integration_shipment_nr") for s in shipments if s.get("status") == "Created"}
	created_rows = {}
	for row in units or []:
		if row.get("integration_shipment_nr") in created:
			created_rows.setdefault(cstr(row.get("mp_item_nr")).strip(), row)
	outside = []
	after_submit = []
	for item in payload:
		if item.get("integration_status") != STATUS_SHIPPED:
			continue
		row = created_rows.get(cstr(item.get("mp_item_nr")).strip())
		if row is None:
			outside.append(item)
			continue
		if is_covered(row):
			continue
		nr = row.get("integration_shipment_nr")
		submitted = submitted_note(units, nr)
		if submitted and not draft_note(units, nr):
			after_submit.append((item, submitted))
	return {"outside": outside, "after_submit": after_submit}


def clear_deleted_delivery_note(doc, method=None):
	"""``doc_events`` handler (Delivery Note ``on_trash``): clear the deleted note from the unit rows
	(and the legacy shipment fields) that track it.  Never raises.

	``Marketplace Order Shipment Unit.delivery_note`` is a Link, and Frappe refuses to delete a
	document that is still linked, so the link must be cleared before that check.  The write is
	a direct ``set_value`` inside the deleter's transaction on purpose (a queued write would come
	too late) and it rolls back with that transaction.  The parent Marketplace Order row is
	locked first and its ``modified`` is bumped, so a copy loaded earlier cannot save over the
	change unnoticed.  The shipment's handed-over time is reset when no unit of it is left on a
	Delivery Note: the units are uncovered again and the next import drafts the shipped ones (or
	Mark Handed Over drafts them all), while units that another live Delivery Note covers keep
	their link, so nothing is delivered twice.  A cancelled note keeps its link on purpose (the
	amend flow re-links through ``amended_from``, see ``live_note``).  The unresolved record is
	left as it is.  The legacy shipment fields ``delivery_note`` / ``delivery_notes`` (no longer
	written, #390) are cleared too, so an older link never blocks the deletion.
	"""
	try:
		names = [doc.name] + ([doc.amended_from] if doc.get("amended_from") else [])
		parents = {}
		for name in names:
			for row in frappe.get_all(
				UNIT_DOCTYPE,
				filters={"delivery_note": name},
				fields=["name", "parent", "integration_shipment_nr"],
				parent_doctype="Marketplace Order",
			):
				parents.setdefault(row.parent, {"units": [], "legacy": {}})["units"].append(row)
			for filters in ({"delivery_note": name}, [["delivery_notes", "like", f'%"{name}"%']]):
				for row in frappe.get_all(
					SHIPMENT_DOCTYPE, filters=filters, fields=["name", "parent"], parent_doctype="Marketplace Order"
				):
					parents.setdefault(row.parent, {"units": [], "legacy": {}})["legacy"][row.name] = row.parent
		for parent, found in parents.items():
			frappe.db.get_value("Marketplace Order", parent, "name", for_update=True)
			shipments = set()
			for row in found["units"]:
				frappe.db.set_value(UNIT_DOCTYPE, row.name, "delivery_note", None, update_modified=False)
				shipments.add(row.integration_shipment_nr)
			for shipment_row in found["legacy"]:
				frappe.db.set_value(
					SHIPMENT_DOCTYPE,
					shipment_row,
					{"delivery_note": None, "delivery_notes": None},
					update_modified=False,
				)
			for nr in shipments:
				if not frappe.db.exists(
					UNIT_DOCTYPE, {"parent": parent, "integration_shipment_nr": nr, "delivery_note": ["!=", ""]}
				):
					frappe.db.set_value(
						SHIPMENT_DOCTYPE,
						{"parent": parent, "integration_shipment_nr": nr},
						{"handed_over_on": None, "delivery_note_error": None},
						update_modified=False,
					)
			frappe.db.set_value("Marketplace Order", parent, "modified", now_datetime(), update_modified=False)
	except Exception:
		frappe.log_error(title="Noon handover: could not clear a Delivery Note link", message=frappe.get_traceback())
