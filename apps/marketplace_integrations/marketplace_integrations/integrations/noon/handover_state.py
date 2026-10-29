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


def shipment_notes(shipment):
	"""Names of every Delivery Note recorded for the shipment: the ``delivery_note`` link (the
	first one) and the ``delivery_notes`` JSON list (all of them)."""
	names = []
	raw = cstr(shipment.get("delivery_notes")).strip()
	if raw:
		try:
			listed = frappe.parse_json(raw)
		except ValueError:
			listed = []
		names = [n for n in listed if isinstance(n, str)] if isinstance(listed, list) else []
	first = cstr(shipment.get("delivery_note")).strip()
	if first and first not in names:
		names.insert(0, first)
	return names


def record_note(shipment, name):
	"""Record ``name`` on the shipment row (in memory); the first note is also the Link field."""
	names = shipment_notes(shipment)
	if name not in names:
		names.append(name)
	shipment.delivery_notes = frappe.as_json(names)
	if not shipment.get("delivery_note"):
		shipment.delivery_note = name


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


def live_notes(shipment):
	"""Live Delivery Notes of the shipment (see ``live_note``), first note first."""
	live = []
	for name in shipment_notes(shipment):
		found = live_note(name)
		if found and found not in live:
			live.append(found)
	return live


def reset_delivery_note_tracking(shipment):
	"""Forget the shipment's Delivery Notes (in memory)."""
	shipment.delivery_note = None
	shipment.delivery_notes = None
	shipment.handed_over_on = None


def clear_deleted_delivery_note(doc, method=None):
	"""``doc_events`` handler (Delivery Note ``on_trash``): forget the notes of the shipment rows
	that track the deleted one.  Never raises.

	``Marketplace Order Shipment.delivery_note`` is a Link, and Frappe refuses to delete a
	document that is still linked, so the link must be cleared before that check.  The write is
	a direct ``set_value`` inside the deleter's transaction on purpose (a queued write would come
	too late) and it rolls back with that transaction.  The parent Marketplace Order row is
	locked first and its ``modified`` is bumped, so a copy loaded earlier cannot save over the
	change unnoticed.  Tracking is reset for the whole shipment (link, list and handed-over time):
	the next Mark Handed Over drafts the shipment again, and units that another live Delivery
	Note still covers count as resolved, so nothing is delivered twice.  A cancelled note keeps
	its link on purpose (the amend flow re-links through ``amended_from``, see ``live_note``).
	The unresolved record is left as it is.
	"""
	try:
		# No Delivery Note lock here on purpose. A note created from a Delivery Note-first Noon
		# shipment is locked (note, then order) by delivery_note_shipment.guard_cancel_or_delete, which
		# runs before this handler. An old-flow note keeps the order-then-note order that
		# fulfillment.retry_unresolved_handovers uses (it holds the order lock, then saves the draft
		# note), so locking the note first here would invert that order and could deadlock.
		names = [doc.name] + ([doc.amended_from] if doc.get("amended_from") else [])
		rows = {}
		for name in names:
			for filters in (
				{"delivery_note": name},
				[["delivery_notes", "like", f'%"{name}"%']],
			):
				for row in frappe.get_all(
					SHIPMENT_DOCTYPE,
					filters=filters,
					fields=["name", "parent"],
					parent_doctype="Marketplace Order",
				):
					rows[row.name] = row.parent
		for row_name, parent in rows.items():
			frappe.db.get_value("Marketplace Order", parent, "name", for_update=True)
			frappe.db.set_value(
				SHIPMENT_DOCTYPE,
				row_name,
				{"delivery_note": None, "delivery_notes": None, "handed_over_on": None},
				update_modified=False,
			)
			frappe.db.set_value("Marketplace Order", parent, "modified", now_datetime(), update_modified=False)
	except Exception:
		frappe.log_error(title="Noon handover: could not clear a Delivery Note link", message=frappe.get_traceback())
