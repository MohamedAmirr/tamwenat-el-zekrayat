# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon adapter of the neutral carrier contract (``services/carrier_shipments.py``; epic #413, #415).

Delivery Note-first (``docs/marketplace-integrations/delivery-note-first.md``): the Delivery Note of a
Noon Sales Order is the shipment. Submit starts a background job (``carrier_shipments.request_create`` then
``run_create``); once the shipment is Created the job submits the note as the requester
(``require_shipment_created`` still refuses any submit before). This
module holds only the Noon parts: the contract methods and the Noon-only helpers (AWB step, unit picking).

Native fields used: ``transporter`` (Supplier "Noon", created by a patch), ``lr_no`` (the AWB) and
``lr_date``. The shipment number sent to Noon is the Delivery Note name, stored in
``custom_carrier_reference`` at the first attempt. ``custom_noon_generate_awb`` asks Noon for the AWB.
``custom_carrier_request_snapshot`` holds ``{"mp_item_nrs": [...]}``, the units the shipment was created with.

Rules enforced here:

- Noon has no idempotency key but ``integration_shipment_nr`` is unique, so **every attempt asks Noon first**
  (GetShipment, ``find_remote``) and creates only when Noon confirms there is none
  (``fulfillment._is_shipment_not_found``). No automatic retry of a create or a cancel. A timeout, a
  connection error, a 5xx/408/429, a malformed 2xx or an unrecognised "absent" answer is Unknown, never a
  failure. A definite 4xx, a configuration problem or a missing mapping is Failed.
- A Noon-generated AWB is requested once (``GetNoonLogisticsAWBs`` qty 1), written to ``lr_no`` and
  committed with an Integration Request row (operation "AWB") before CreateShipment. A retry reuses it. A
  typed AWB is sent with the courier ``fulfillment.NOON_COURIER`` ("noon"), always.
- An AWB held by another Delivery Note that is not cancelled is refused (case-insensitive).
- Units: ``_pick_units`` takes ``mp_item_nr`` by SKU and quantity from the order's latest payload; units that
  another live Noon note already started come from that note's snapshot, never from the old Marketplace Order
  shipment tables.
- No COD and no address: CreateShipment takes neither. A Noon note clears its Shipping Rule and the tax row
  that rule added (ERPNext only adds them, it never removes them).
- The worker writes with ``db_set`` (no validate, no hooks): a system write-back.

Status: ``refresh`` (GetShipment + GetFbpiOrder) derives the Shipping Status and the per-row Noon status from
the units' item statuses and never edits items; ``enqueue_note_refreshes`` (webhook) and ``run_reconcile``
(nightly) call it. The FBPI warehouse check of ``validate_ready`` is ``check_fbpi_warehouse`` (#416): it
refuses Submit-time creation unless ``catalog.fbpi_ready(account)`` says the stored warehouse is FBPI.
"""

import base64
import html
import json
import math
import re

import frappe
from frappe import _
from frappe.query_builder.functions import Lower
from frappe.utils import (
	add_days,
	cint,
	cstr,
	escape_html,
	flt,
	getdate,
	now_datetime,
	strip_html,
	strip_html_tags,
)

from marketplace_integrations.integrations.noon import catalog, fulfillment
from marketplace_integrations.integrations.noon.client import SERVICE_NAME
from marketplace_integrations.integrations.noon.constants import (
	NOON_LOGISTICS_AWBS_GET_PATH,
	SHIPMENT_CANCEL_PATH,
	SHIPMENT_CREATE_PATH,
	SHIPMENT_GET_PATH,
	SHIPMENT_INVOICE_UPLOAD_PATH,
)
from marketplace_integrations.services import carrier_log
from marketplace_integrations.services import delivery_note_workflow as lifecycle
from marketplace_integrations.services.carrier_log import (
	CANCELLATION_PENDING,
	CANCELLATION_UNKNOWN,
	CANCELLED,
	CREATED,
	CREATING,
	EDITABLE_STATUSES,
	FAILED,
	NOT_CREATED,
	UNKNOWN,
	Failed,
	Unknown,
)
from marketplace_integrations.services.mappings import resolve_item

#: ``supplier_name`` of the native Supplier used as the Delivery Note ``transporter`` (created by a patch).
#: The Supplier's document name depends on Buying Settings, so it is looked up by this name, never assumed.
SUPPLIER_NAME = "Noon"
#: The carrier name the Delivery Note shows its Shipping section for (``carrier_shipments.boot_session``).
SHIPPING_CARRIER = SUPPLIER_NAME
#: The job asks Noon (GetShipment) before every create, the first attempt included
#: (``carrier_shipments.request_create``).
LOOKUP_FIRST = True

#: Shipping Status values (Noon's own words, derived from the item statuses; written by ``refresh``).
SHIPPING_ACKNOWLEDGED = "Acknowledged"
SHIPPING_SHIPPED = "Shipped"
SHIPPING_OUT_OF_STOCK = "Out of Stock"
SHIPPING_CANCELLED = "Cancelled"
SHIPPING_MIXED = "Mixed"
#: Noon cancelled the shipment after a person confirmed the pickup (#527): the note stays submitted with the stock
#: out until Confirm Returned. Held = Integration Status Cancelled + this Shipping Status.
SHIPPING_AWAITING_RETURN = "Awaiting Return"
#: Set on the note by ``confirm_returned`` so ``guard_cancel`` lets that one native cancel through.
CONFIRM_RETURNED_FLAG = "noon_confirm_returned"
MP_STATUS_CANCELLED = "MP_ITEM_STATUS_CANCELLED"
#: GetFbpiOrder ``integration_status`` -> Shipping Status word.
_INTEGRATION_STATUS_NAMES = {
	"INTEGRATION_ITEM_STATUS_ACKNOWLEDGED": SHIPPING_ACKNOWLEDGED,
	"INTEGRATION_ITEM_STATUS_SHIPPED": SHIPPING_SHIPPED,
	"INTEGRATION_ITEM_STATUS_OUT_OF_STOCK": SHIPPING_OUT_OF_STOCK,
}
_REFRESH_JOB = "noon-refresh|{}"
_REFRESH_AGAIN = "noon-refresh-again|{}"
_REFRESH_PASSES = 3
RECONCILE_JOB = "noon-reconcile"
#: The nightly reconcile reads a Created note only for this long after its creation.
REFRESH_MAX_AGE_DAYS = 60

#: Written by the app only (workers and APIs); a person's save never changes them.
SYSTEM_FIELDS = (
	"custom_integration_status",
	"custom_shipping_status",
	"custom_carrier_delivery_id",
	"custom_carrier_reference",
	"custom_last_sync_at",
	"custom_last_integration_error",
	"custom_carrier_request_snapshot",
	# the shipment invoice record: an amended note has not had its invoice uploaded
	"custom_noon_invoice_uploaded_on",
)
#: Fields a person edits that must stay put once the shipment is started (and the Generate AWB checkbox).
LOCKED_FIELDS = ("transporter", "lr_no")
#: Integration Statuses whose snapshot holds units that are still claimed (the shipment may exist at Noon).
_CLAIMING_STATUSES = (CREATED, CANCELLATION_PENDING, CANCELLATION_UNKNOWN)
_RETRYABLE_HTTP = {408, 429}


class _Refused(Exception):
	"""A readable reason the shipment cannot be created from this note."""


# ---------------------------------------------------------------------------
# Which notes belong to Noon
# ---------------------------------------------------------------------------


def noon_order_name(doc):
	"""The Noon Marketplace Order of a Delivery Note whose rows all come from one submitted, Noon-linked Sales
	Order, else ``None`` (returns and every other note are not ours)."""
	if doc.get("is_return"):
		return None
	sales_orders = {row.get("against_sales_order") for row in doc.get("items") or []}
	if len(sales_orders) != 1 or not next(iter(sales_orders)):
		return None
	values = frappe.db.get_value(
		"Sales Order", next(iter(sales_orders)), ["docstatus", "custom_marketplace_order"], as_dict=True
	)
	if not values or values.docstatus != 1 or not values.custom_marketplace_order:
		return None
	if frappe.db.get_value("Marketplace Order", values.custom_marketplace_order, "platform") != "Noon":
		return None
	return values.custom_marketplace_order


def transporter_name():
	"""The Supplier docname of "Noon" (looked up by ``supplier_name``), or ``None`` when missing."""
	return frappe.db.get_value("Supplier", {"supplier_name": SUPPLIER_NAME}, "name")


def applies_to(doc):
	"""A note of a Noon Sales Order. A dict with only ``is_return`` and ``transporter`` (the Needs Attention
	collector and the amend cleanup have no rows to read) is judged by the transporter."""
	if doc.get("is_return"):
		return False
	if doc.get("items") is None:
		transporter = doc.get("transporter")
		return bool(transporter) and frappe.db.get_value("Supplier", transporter, "supplier_name") == SUPPLIER_NAME
	return bool(noon_order_name(doc))


def saved_state(doc):
	"""The note as stored (ownership of an existing note is judged by it), else ``doc`` itself."""
	return doc if doc.is_new() else (doc.get_doc_before_save() or doc)


def _rows(doc):
	return sorted((cstr(row.name), cstr(row.item_code), flt(row.qty, 6)) for row in doc.get("items") or [])


def _reference(doc):
	"""The shipment number sent to Noon: stored at the first attempt, else the note name."""
	return cstr(doc.get("custom_carrier_reference")).strip() or doc.name


def _order_values(doc):
	"""``marketplace_account``, ``external_order_id``, ``latest_payload`` and ``noon_pending_oos_units`` of
	the note's Noon Marketplace Order."""
	return frappe.db.get_value(
		"Marketplace Order",
		noon_order_name(doc),
		["marketplace_account", "external_order_id", "latest_payload", "noon_pending_oos_units"],
		as_dict=True,
	)


# ---------------------------------------------------------------------------
# Document rules (called from services/carrier_shipments.py)
# ---------------------------------------------------------------------------


def _awb_in_use(doc):
	"""Is the note's ``lr_no`` held by another Delivery Note that is not cancelled (any carrier, ignoring
	letter case)? Read without per-user permissions: only a yes/no leaves."""
	awb = cstr(doc.get("lr_no")).strip()
	if not awb:
		return False
	note = frappe.qb.DocType("Delivery Note")
	query = (
		frappe.qb.from_(note)
		.select(note.name)
		.where(Lower(note.lr_no) == awb.lower())
		.where(note.docstatus < 2)
	)
	if doc.get("name") and not doc.is_new():
		query = query.where(note.name != doc.name)
	return bool(query.limit(1).run())


def _refuse_used_awb(doc):
	frappe.throw(
		_("AWB {0} is already on another Delivery Note.").format(frappe.bold(frappe.utils.escape_html(cstr(doc.lr_no)))),
		title=_("Noon shipment"),
	)


def _clear_shipping_rule(doc):
	"""A Noon note has no Shipping Rule: ERPNext adds the rule's tax row on every calculation and removes it
	nowhere (``SellingController.remove_shipping_charge`` is never called), so the rule is cleared first
	(or the recalculation would add the row again), then its row is removed and the totals recalculated."""
	rule_name = doc.get("shipping_rule")
	if not rule_name:
		return
	rule = frappe.db.get_value("Shipping Rule", rule_name, ["account", "cost_center"], as_dict=True)
	doc.shipping_rule = None
	for row in list(doc.get("taxes") or []):
		if rule and row.charge_type == "Actual" and row.account_head == rule.account:
			if not rule.cost_center or cstr(row.cost_center) == cstr(rule.cost_center):
				doc.get("taxes").remove(row)
	doc.calculate_taxes_and_totals()


def _prepare(doc):
	"""An editable Noon note: the carrier is always the Noon Supplier, and no Shipping Rule."""
	supplier = transporter_name()
	if not supplier:
		frappe.throw(_("Supplier Noon (carrier) does not exist. Run bench migrate."), title=_("Noon shipment"))
	doc.transporter = supplier
	doc.transporter_name = SUPPLIER_NAME
	_clear_shipping_rule(doc)


def _clear_new(doc):
	"""Clear what a new or amended note must not carry over (Amend copies even ``no_copy`` fields)."""
	for fieldname in SYSTEM_FIELDS:
		doc.set(fieldname, None)
	for row in doc.get("items") or []:
		row.custom_noon_item_status = None
	doc.custom_integration_status = NOT_CREATED


def clear_inherited(doc):
	"""``validate`` of a new note amended from a Noon note that is no longer a Noon note (the carrier was
	changed): the old shipment's number, AWB, checkbox and status would sit on a note that has no Noon
	shipment."""
	_clear_new(doc)
	doc.custom_noon_generate_awb = 0
	doc.lr_no = None
	doc.lr_date = None


def _awb_was_generated(before):
	return bool(cint(before.get("custom_noon_generate_awb")) and cstr(before.get("lr_no")).strip())


def guard_changes(doc):
	"""``validate``: set up a new note; keep app-written fields and a started shipment untouched."""
	before = saved_state(doc)
	if not noon_order_name(before):
		return
	ours = bool(noon_order_name(doc))
	if doc.is_new():
		_clear_new(doc)
		if doc.get("amended_from"):
			# Amend copies the old note's AWB and checkbox, which belong to the shipment of that note. A
			# different AWB the person typed is kept.
			doc.custom_noon_generate_awb = 0
			source_awb = frappe.db.get_value("Delivery Note", doc.amended_from, "lr_no")
			if source_awb and cstr(doc.get("lr_no")) == cstr(source_awb):
				doc.lr_no = None
				doc.lr_date = None
		if ours:
			_prepare(doc)
			_check_awb_choice(doc, before)
		return
	# The app writes these with db_set (no validate): put the stored values back instead of refusing, so a
	# person's save never changes them and never loses the app's write.
	for fieldname in SYSTEM_FIELDS:
		doc.set(fieldname, before.get(fieldname))
	if cstr(before.get("custom_integration_status")) in EDITABLE_STATUSES:
		if ours:
			_prepare(doc)
			_check_awb_choice(doc, before)
		return
	# Normalized: the input sanitizer HTML-escapes a posted value that db_set stored raw, and None/"" are equal.
	meta = frappe.get_meta("Delivery Note")
	changed = [
		_(meta.get_label(f) or f)
		for f in LOCKED_FIELDS
		if html.unescape(cstr(before.get(f))).strip() != html.unescape(cstr(doc.get(f))).strip()
	]
	if cint(before.get("custom_noon_generate_awb")) != cint(doc.get("custom_noon_generate_awb")):
		changed.append(_(meta.get_label("custom_noon_generate_awb") or "custom_noon_generate_awb"))
	if changed:
		frappe.throw(
			_("The carrier, AWB and Generate AWB cannot be changed after the Noon shipment was started.")
			+ " "
			+ _("Changed: {0}").format(", ".join(changed)),
			title=_("Noon shipment"),
		)
	if _rows(before) != _rows(doc):
		frappe.throw(
			_(
				"Items, quantities and rows cannot be changed after the Noon shipment was started. "
				"They must match the units sent to Noon."
			),
			title=_("Noon shipment"),
		)


def _check_awb_choice(doc, before):
	"""An editable note: a generated AWB cannot be changed or unticked (the AWB would be wasted and a new one
	requested), a ticked box has no typed AWB, and a typed AWB is not on another live note."""
	if not doc.is_new() and _awb_was_generated(before):
		if cstr(doc.get("lr_no")) != cstr(before.get("lr_no")) or not cint(doc.get("custom_noon_generate_awb")):
			frappe.throw(
				_("The AWB was generated by Noon and cannot be changed. Cancel the shipment and amend the Delivery Note."),
				title=_("Noon shipment"),
			)
		return
	if cint(doc.get("custom_noon_generate_awb")) and cstr(doc.get("lr_no")).strip():
		frappe.throw(
			_("Untick Generate AWB through Noon, or clear the AWB: Noon cannot generate an AWB when one is typed."),
			title=_("Noon shipment"),
		)
	if _awb_in_use(doc):
		_refuse_used_awb(doc)


def refuse_sales_order_cancel(order):
	"""Sales Order ``before_cancel``: refuse while a draft Noon Delivery Note has a live shipment."""
	supplier = transporter_name()
	if not supplier:
		return
	notes = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": order.name, "docstatus": 0, "parenttype": "Delivery Note"},
		pluck="parent",
		distinct=True,
		parent_doctype="Delivery Note",
	)
	if not notes:
		return
	live = frappe.get_all(
		"Delivery Note",
		filters={
			"name": ["in", notes],
			"transporter": supplier,
			"custom_integration_status": ["in", list(carrier_log.LIVE_STATUSES)],
		},
		pluck="name",
	)
	if live:
		frappe.throw(
			_("Sales Order {0} has a draft Delivery Note with a live Noon shipment: {1}.").format(
				order.name, ", ".join(live)
			),
			title=_("Noon shipment"),
		)


# ---------------------------------------------------------------------------
# Validation before Noon is contacted
# ---------------------------------------------------------------------------


def check_fbpi_warehouse(doc, account):
	"""The FBPI warehouse check (sub-issue #416): refuse unless the stored Noon fulfillment system
	code is the FBPI code and the warehouse is active (``catalog.fbpi_ready``). Reads stored
	values only; nothing is sent to Noon."""
	ready, reason = catalog.fbpi_ready(account)
	if not ready:
		frappe.throw(reason, title=_("Noon shipment"))


def _started_units(doc):
	"""``mp_item_nr`` of the units other live Noon notes of the same Sales Order already started, read from
	their snapshot (a Cancelled note releases its units)."""
	orders = {row.get("against_sales_order") for row in doc.get("items") or [] if row.get("against_sales_order")}
	return _started_units_of_sales_orders(orders, exclude_note=doc.get("name"))


def _started_units_of_sales_orders(sales_orders, exclude_note=None):
	"""``mp_item_nr`` of the units the live Noon notes (Created, Cancellation Pending or Unknown) of these
	Sales Orders hold in their snapshot."""
	if not sales_orders:
		return set()
	parents = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": ["in", list(sales_orders)], "docstatus": ["<", 2], "parenttype": "Delivery Note"},
		pluck="parent",
		distinct=True,
		parent_doctype="Delivery Note",
	)
	if not parents:
		return set()
	notes = frappe.get_all(
		"Delivery Note",
		filters={
			"name": ["in", parents],
			"docstatus": ["<", 2],
			"custom_integration_status": ["in", list(_CLAIMING_STATUSES)],
		},
		fields=["name", "custom_carrier_request_snapshot"],
	)
	started = set()
	for note in notes:
		if exclude_note and note.name == exclude_note:
			continue
		started.update(_snapshot_units(note.custom_carrier_request_snapshot))
	return started


def started_units_of_order(marketplace_order_name):
	"""``mp_item_nr`` of the units a live Noon Delivery Note of this Marketplace Order started (the readers
	of order reconciliation, the Mark Out of Stock rule and the sync debounce use it)."""
	sales_orders = frappe.get_all(
		"Sales Order", filters={"custom_marketplace_order": marketplace_order_name}, pluck="name"
	)
	return _started_units_of_sales_orders(sales_orders)


def has_created_note(marketplace_order_name):
	"""Does a Delivery Note of this Marketplace Order (not cancelled) hold a Created Noon shipment? The order
	invoice upload needs one."""
	return _has_note_in(marketplace_order_name, CREATED)


def has_unresolved_create(marketplace_order_name):
	"""Does a Delivery Note of this Marketplace Order (not cancelled) have a create in flight (Creating) or with
	an unknown result (Creation Unknown)? Its units are not in a snapshot yet, but Noon may already hold the
	shipment: Mark Out of Stock is refused and order reconciliation treats the order as started."""
	return _has_note_in(marketplace_order_name, [CREATING, UNKNOWN])


def _has_note_in(marketplace_order_name, statuses):
	"""Is there a not-cancelled Delivery Note of this Marketplace Order whose Integration Status is (in) ``statuses``?"""
	statuses = [statuses] if isinstance(statuses, str) else list(statuses)
	sales_orders = frappe.get_all(
		"Sales Order", filters={"custom_marketplace_order": marketplace_order_name}, pluck="name"
	)
	if not sales_orders:
		return False
	parents = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": ["in", sales_orders], "docstatus": ["<", 2], "parenttype": "Delivery Note"},
		pluck="parent",
		distinct=True,
		parent_doctype="Delivery Note",
	)
	return bool(parents) and bool(
		frappe.get_all(
			"Delivery Note",
			filters={"name": ["in", parents], "docstatus": ["<", 2], "custom_integration_status": ["in", statuses]},
			limit=1,
			pluck="name",
		)
	)


def recent_awbs_of_orders(marketplace_order_names, modified_since):
	"""Distinct ``lr_no`` of the live Noon notes (Created, Cancellation Pending or Unknown) of these Marketplace
	Orders modified since ``modified_since`` (the daily return scan of ``returns.py``)."""
	sales_orders = frappe.get_all(
		"Sales Order", filters={"custom_marketplace_order": ["in", list(marketplace_order_names)]}, pluck="name"
	)
	if not sales_orders:
		return []
	parents = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": ["in", sales_orders], "docstatus": ["<", 2], "parenttype": "Delivery Note"},
		pluck="parent",
		distinct=True,
		parent_doctype="Delivery Note",
	)
	if not parents:
		return []
	awbs = frappe.get_all(
		"Delivery Note",
		filters={
			"name": ["in", parents],
			"docstatus": ["<", 2],
			"custom_integration_status": ["in", list(_CLAIMING_STATUSES)],
			"modified": [">=", modified_since],
		},
		pluck="lr_no",
	)
	return sorted({cstr(awb).strip() for awb in awbs if cstr(awb).strip()})


def _snapshot_units(snapshot):
	"""The ``mp_item_nr`` list of a ``custom_carrier_request_snapshot`` (empty when unreadable)."""
	try:
		data = frappe.parse_json(snapshot) if snapshot else {}
	except Exception:
		return []
	units = data.get("mp_item_nrs") if isinstance(data, dict) else None
	return [cstr(unit).strip() for unit in units or [] if cstr(unit).strip()]


def _snapshot_data(snapshot):
	"""The whole ``custom_carrier_request_snapshot`` as a dict (``mp_item_nrs`` and, once a refresh wrote an
	error, ``refresh_error``); empty when unreadable."""
	try:
		data = frappe.parse_json(snapshot) if snapshot else {}
	except Exception:
		return {}
	return dict(data) if isinstance(data, dict) else {}


def _pick_units(doc):
	"""``[{"mp_item_nr", "partner_sku"}]`` for the note's rows: per Item, the row quantity (sales UOM) is the
	number of units, taken from the units of the Item's Noon SKU that are Confirmed and Acknowledged and not
	already started by another live note or reserved as Out of Stock. Raises ``_Refused``."""
	wanted = {}
	for row in doc.get("items") or []:
		wanted[row.item_code] = wanted.get(row.item_code, 0) + flt(row.qty)
	order = _order_values(doc)
	payload_items = [
		item for item in fulfillment._latest_payload_items(order) if isinstance(item, dict)
	]
	skus_by_item = {}
	for sku in sorted({cstr(item.get("partner_sku")).strip() for item in payload_items} - {""}):
		item = resolve_item(order.marketplace_account, sku)
		if item:
			skus_by_item.setdefault(item, []).append(sku)

	claimed = _started_units(doc) | fulfillment._pending_oos_units(order)
	shippable_by_sku = {}
	seen = set()
	for item in payload_items:
		mp_item_nr = cstr(item.get("mp_item_nr")).strip()
		if not mp_item_nr or mp_item_nr in seen or mp_item_nr in claimed:
			continue
		if (
			item.get("mp_status") != fulfillment.MP_STATUS_CONFIRMED
			or item.get("integration_status") != fulfillment.INTEGRATION_STATUS_ACKNOWLEDGED
		):
			continue
		seen.add(mp_item_nr)
		shippable_by_sku.setdefault(cstr(item.get("partner_sku")).strip(), []).append(mp_item_nr)

	picked = []
	for item, qty in wanted.items():
		skus = skus_by_item.get(item, [])
		if not skus:
			raise _Refused(_("Item {0} has no enabled Noon product mapping for a SKU on this order.").format(item))
		if len(skus) > 1:
			raise _Refused(
				_("Item {0} is mapped to more than one Noon SKU on this order ({1}).").format(item, ", ".join(skus))
			)
		if qty <= 0 or qty != int(qty):
			raise _Refused(_("The quantity of Item {0} must be a whole number of units.").format(item))
		available = shippable_by_sku.get(skus[0], [])
		if len(available) < qty:
			raise _Refused(
				_("Item {0} needs {1} shippable unit(s) of SKU {2}, but only {3} are available.").format(
					item, int(qty), skus[0], len(available)
				)
			)
		picked += [{"mp_item_nr": nr, "partner_sku": skus[0]} for nr in available[: int(qty)]]
	return picked


def _account_and_client(doc):
	"""``(account, client)`` of the note's Noon Marketplace Account; a configuration problem raises
	``frappe.ValidationError`` with a readable message."""
	account, client = fulfillment._account_and_client(_order_values(doc))
	if not cstr(account.noon_warehouse_code).strip():
		frappe.throw(_("Marketplace Account {0} requires a Noon Warehouse Code.").format(account.name))
	return account, client


def _client_for(doc, error):
	"""Like ``_account_and_client`` but a configuration problem raises ``error`` (``Failed`` for a create,
	``Unknown`` for a lookup)."""
	try:
		return _account_and_client(doc)
	except frappe.ValidationError as exc:
		frappe.clear_messages()
		raise error(strip_html(cstr(exc))) from exc


def validate_ready(doc, action="create"):
	"""Everything that can be refused before Noon is contacted (the contract's ``validate_ready``). A
	configuration problem is thrown here, so a note is never left Creating for it. ``action`` ``"lookup"`` and
	``"cancel"`` only check the configuration (and, for a cancel, the shipment number)."""
	if not applies_to(doc):
		frappe.throw(_("This Delivery Note is not a Noon shipment."))
	account, _client = _account_and_client(doc)
	if action != "create":
		if action == "cancel" and not cstr(doc.get("custom_carrier_reference")).strip():
			frappe.throw(_("This Delivery Note has no Noon shipment number."))
		return None
	if doc.docstatus != 0:
		frappe.throw(_("Only a draft Delivery Note can be sent to Noon."))
	if not transporter_name() or cstr(doc.get("transporter")) != cstr(transporter_name()):
		frappe.throw(_("The carrier of this Delivery Note must be Noon."))
	if not doc.get("items"):
		frappe.throw(_("The Delivery Note has no items."))
	if not cstr(doc.get("lr_no")).strip() and not cint(doc.get("custom_noon_generate_awb")):
		frappe.throw(_("Type the AWB, or tick Generate AWB through Noon, before sending the shipment."))
	if _awb_in_use(doc):
		_refuse_used_awb(doc)
	check_fbpi_warehouse(doc, account)
	try:
		_pick_units(doc)
	except _Refused as refusal:
		frappe.throw(cstr(refusal))
	return None


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _json(response):
	try:
		return response.json()
	except (ValueError, AttributeError):
		return None


def _unreached(exc, what):
	return Unknown(_("The Noon {0} request failed ({1}). It may or may not have reached Noon.").format(what, cstr(exc)[:200]))


def _fail_or_unknown(client, response):
	"""A non-200 answer: Failed for a definite refusal (a 4xx other than 408/429), else Unknown."""
	message = client.error_message(response)
	if fulfillment._is_definite_refusal(response.status_code):
		return Failed(message)
	return Unknown(message, response.status_code)


def find_remote(doc):
	"""The contract's ``find_remote``: Noon's shipment (``{"awbs", "items"}``) when GetShipment has it,
	``None`` only when Noon says "No shipment found", else ``Unknown``."""
	account, client = _client_for(doc, Unknown)
	try:
		response = client.request(
			"POST",
			SHIPMENT_GET_PATH,
			json={"warehouse_code": account.noon_warehouse_code, "integration_shipment_nr": _reference(doc)},
		)
	except Exception as exc:
		raise _unreached(exc, "lookup") from exc
	if response.status_code == 200:
		body = _json(response)
		if not isinstance(body, dict) or not isinstance(body.get("items"), list) or not isinstance(body.get("awbs"), list):
			raise Unknown(_("Noon returned a shipment in an unrecognised format. Check the shipment in Noon."), 200)
		# The note name is not unique across sites on one Noon account: a shipment of another order (or of a
		# unit this order does not have) is not ours and is never adopted.
		order = _order_values(doc)
		units = {cstr(item.get("mp_item_nr")).strip() for item in body["items"] if isinstance(item, dict)}
		on_order = {
			cstr(item.get("mp_item_nr")).strip()
			for item in fulfillment._latest_payload_items(order)
			if isinstance(item, dict)
		}
		if cstr(body.get("fbpi_order_nr")).strip() != cstr(order.external_order_id).strip() or not units <= on_order:
			raise Unknown(
				_("Noon has a shipment with this number that belongs to another order. Check the shipment in Noon."), 200
			)
		return {
			"awbs": [awb for awb in body["awbs"] if isinstance(awb, dict)],
			"items": [item for item in body["items"] if isinstance(item, dict)],
		}
	if fulfillment._is_shipment_not_found(response):
		return None
	raise Unknown(client.error_message(response), response.status_code)


def _ensure_awb(doc, account, client):
	"""The AWB of the shipment: the stored ``lr_no`` (typed, or generated by an earlier attempt: never request
	another), else one Noon generates, written to the note and committed before CreateShipment."""
	awb = cstr(doc.get("lr_no")).strip()
	if awb:
		return awb
	if not cint(doc.get("custom_noon_generate_awb")):
		raise Failed(_("The Delivery Note has no AWB. Type the AWB or tick Generate AWB through Noon."))
	log = carrier_log.open_log(SERVICE_NAME, doc.name, "AWB", reference=_reference(doc))
	frappe.db.commit()
	try:
		response = client.request(
			"POST", NOON_LOGISTICS_AWBS_GET_PATH, json={"country_code": account.noon_country_code, "qty": 1}
		)
	except Exception as exc:
		error = _unreached(exc, "AWB")
		carrier_log.close_log(log, "Unknown", error=cstr(error))
		frappe.db.commit()
		raise error from exc
	if response.status_code != 200:
		error = _fail_or_unknown(client, response)
		carrier_log.close_log(
			log, "Failed" if isinstance(error, Failed) else "Unknown", error=cstr(error), http_status=response.status_code
		)
		frappe.db.commit()
		raise error
	body = _json(response)
	awbs = body.get("awb_nrs") if isinstance(body, dict) else None
	awb = cstr(awbs[0]).strip() if isinstance(awbs, list) and awbs else ""
	if not awb:
		error = Unknown(_("Noon returned no AWB number."), 200)
		carrier_log.close_log(log, "Unknown", error=cstr(error), http_status=200)
		frappe.db.commit()
		raise error
	# Stored at once: the form shows it, and a retry (even after CreateShipment failed) reuses it.
	doc.db_set({"lr_no": awb}, update_modified=False, notify=True, commit=True)
	carrier_log.close_log(log, "Success", output=awb, http_status=200)
	frappe.db.commit()
	return awb


def create_remote(doc):
	"""The contract's ``create_remote``: the AWB step, then CreateShipment. Returns ``{"awbs", "items"}`` as
	sent; raises ``Failed`` (a definite refusal, a configuration problem, a missing mapping) or ``Unknown``."""
	account, client = _client_for(doc, Failed)
	try:
		units = _pick_units(doc)
	except _Refused as refusal:
		raise Failed(cstr(refusal)) from refusal
	awb = _ensure_awb(doc, account, client)
	body = {
		"warehouse_code": account.noon_warehouse_code,
		"integration_shipment_nr": _reference(doc),
		"fbpi_order_nr": _order_values(doc).external_order_id,
		"awbs": [{"courier": fulfillment.NOON_COURIER, "awb_nr": awb}],
		"items": [{"mp_item_nr": unit["mp_item_nr"]} for unit in units],
	}
	frappe.db.commit()  # no transaction open across the call
	try:
		response = client.request("POST", SHIPMENT_CREATE_PATH, json=body)
	except Exception as exc:
		raise _unreached(exc, "create") from exc
	if response.status_code != 200:
		raise _fail_or_unknown(client, response)
	return {"awbs": body["awbs"], "items": body["items"]}


def created_values(doc, data):
	"""The Delivery Note fields written once Created (also by ``carrier_shipments.check``). The snapshot is
	the units Noon holds for the shipment; the Shipping Status starts as Acknowledged (the units were
	acknowledged when picked) until a refresh derives it."""
	units = sorted(
		{cstr(item.get("mp_item_nr")).strip() for item in data.get("items") or [] if cstr(item.get("mp_item_nr")).strip()}
	)
	awb = cstr(doc.get("lr_no")).strip() or next(
		(cstr(awb.get("awb_nr")).strip() for awb in data.get("awbs") or [] if cstr(awb.get("awb_nr")).strip()), ""
	)
	return {
		"lr_no": awb,
		"lr_date": getdate(),
		"custom_shipping_status": SHIPPING_ACKNOWLEDGED,
		"custom_last_integration_error": None,
		"custom_last_sync_at": now_datetime(),
		"custom_carrier_request_snapshot": json.dumps({"mp_item_nrs": units}),
	}


def log_data(values):
	"""The create data for the Integration Log: the units only (no personal data)."""
	return values.get("custom_carrier_request_snapshot")


def after_created(delivery_note, data):
	"""Once the note is Created: the Sales Order summary (no label)."""
	_update_summary(delivery_note)


def _update_summary(delivery_note):
	"""Refresh ``Sales Order.custom_shipping_status_summary`` (the shared Bosta/Noon counter).

	The summary is display only, so it never raises: a lock wait on the Sales Order row must not skip
	the automatic submit after create or the native cancel after a carrier cancel. A failure is logged and
	the next refresh recounts."""
	from marketplace_integrations.integrations.bosta import statuses

	try:
		statuses.update_sales_order_summary_of(delivery_note)
	except Exception:
		frappe.log_error(title="Noon shipping summary", message=frappe.get_traceback())


# ---------------------------------------------------------------------------
# Cancel (spec section 8)
# ---------------------------------------------------------------------------


def _all_units_cancelled(raw_order, units):
	"""Does GetFbpiOrder show every unit of the shipment as cancelled by Noon (``mp_status``)?"""
	items = raw_order.get("items") if isinstance(raw_order, dict) else None
	if not units or not isinstance(items, list):
		return False
	status = {
		cstr(item.get("mp_item_nr")).strip(): item.get("mp_status") for item in items if isinstance(item, dict)
	}
	return all(status.get(unit) == "MP_ITEM_STATUS_CANCELLED" for unit in units)


def cancel_remote(doc):
	"""The contract's ``cancel_remote`` (the neutral ``carrier_shipments.cancel`` holds the state machine, the
	lock and the log). Noon is asked first: GetFbpiOrder showing every unit of the shipment cancelled is
	success with no cancel call (``custom_shipping_status`` Cancelled). Otherwise CancelShipment: 200 is
	Cancelled, a definite 4xx is ``refused`` (Noon's text), anything else raises ``Unknown``. A failed
	GetFbpiOrder is Unknown too: a cancel is never sent blind, and Retry asks first again."""
	account, client = _client_for(doc, Unknown)
	values = {"custom_shipping_status": SHIPPING_CANCELLED}
	try:
		raw_order = fulfillment.get_fbpi_order(account, _order_values(doc).external_order_id)
	except Exception as exc:
		frappe.clear_messages()
		raise Unknown(_("Noon did not answer the order lookup before the cancel ({0}).").format(cstr(exc)[:200])) from exc
	if _all_units_cancelled(raw_order, _snapshot_units(doc.get("custom_carrier_request_snapshot"))):
		return frappe._dict(outcome="cancelled", message="", values=values, http_status=None)
	frappe.db.commit()  # no transaction open across the call
	try:
		response = client.request(
			"POST",
			SHIPMENT_CANCEL_PATH,
			json={"warehouse_code": account.noon_warehouse_code, "integration_shipment_nr": _reference(doc)},
		)
	except Exception as exc:
		raise _unreached(exc, "cancel") from exc
	if response.status_code == 200:
		return frappe._dict(outcome="cancelled", message="", values=values, http_status=200)
	message = client.error_message(response)
	if fulfillment._is_definite_refusal(response.status_code):
		return frappe._dict(outcome="refused", message=message, values={}, http_status=response.status_code)
	raise Unknown(message, response.status_code)


def picked_up(doc):
	"""The contract's optional ``picked_up``: a person confirmed the pickup, i.e. the note's workflow stage is Picked
	Up, In Transit or Delivered. Noon's own "Shipped" is NOT a pickup: Noon sets it right after the shipment is
	created, and the note is then Ready for Pickup, where the shipment can still be cancelled at Noon (#541). The
	states in which Noon itself refuses a cancel are not documented (business-spec, "Which states allow
	cancellation"); a refusal is shown when Noon gives one."""
	return cstr(doc.get(lifecycle.FIELD)) in lifecycle.NOON_MANUAL_STATES


def after_cancelled(delivery_note):
	"""Once the note is Cancelled: the Sales Order summary."""
	_update_summary(delivery_note)


# ---------------------------------------------------------------------------
# Held after a Noon cancel past pickup: Confirm Returned (#527)
# ---------------------------------------------------------------------------


def is_held(values):
	"""A submitted note Noon cancelled after pickup: Integration Status Cancelled + Shipping Status Awaiting Return.
	``values`` is a dict or document with ``docstatus``, ``custom_integration_status`` and ``custom_shipping_status``."""
	return (
		cint(values.get("docstatus")) == 1
		and cstr(values.get("custom_integration_status")) == CANCELLED
		and cstr(values.get("custom_shipping_status")) == SHIPPING_AWAITING_RETURN
	)


def guard_cancel(doc):
	"""The contract's optional ``guard_cancel`` (``carrier_shipments.guard_cancel_or_delete``, ``before_cancel``):
	the native cancel of a held note (form, workflow, list) is refused; only ``confirm_returned`` cancels it
	(it sets ``flags.noon_confirm_returned``). Judged by the stored values."""
	if doc.flags.get(CONFIRM_RETURNED_FLAG):
		return
	stored = frappe.db.get_value(
		"Delivery Note", doc.name, ["docstatus", "custom_integration_status", "custom_shipping_status"], as_dict=True
	)
	if stored and is_held(stored):
		frappe.throw(
			_("Noon cancelled this shipment after pickup. Use Confirm Returned when the goods are back."),
			title=_("Noon shipment"),
		)


def _stock_lines(doc):
	"""The posted stock lines of a note: a plain row, or for a Product Bundle row its Packed Items (what ERPNext's
	``SellingController.get_item_list`` posts). Rows of items that are not stock items post nothing and are
	left out. Each line: ``_dict(name, doctype, item_code, item_name, uom, conversion_factor, warehouse, qty,
	serial_batch, precision)``."""
	lines = []

	def add(row, doctype, warehouse):
		item = frappe.get_cached_value(
			"Item", row.item_code, ["is_stock_item", "has_serial_no", "has_batch_no"], as_dict=True
		)
		if not item or not item.is_stock_item:
			return
		lines.append(
			frappe._dict(
				name=row.name,
				doctype=doctype,
				item_code=row.item_code,
				item_name=row.get("item_name") or row.item_code,
				uom=row.get("uom"),
				conversion_factor=flt(row.get("conversion_factor")) or 1,
				warehouse=warehouse,
				qty=flt(row.qty, frappe.get_precision(doctype, "qty")),
				serial_batch=bool(
					item.has_serial_no
					or item.has_batch_no
					or row.get("serial_and_batch_bundle")
					or row.get("serial_no")
					or row.get("batch_no")
				),
				precision=frappe.get_precision(doctype, "qty"),
			)
		)

	for row in doc.get("items") or []:
		if row.get("delivered_by_supplier"):
			continue
		if doc.has_product_bundle(row.item_code):
			for packed in doc.get("packed_items") or []:
				if packed.parent_detail_docname == row.name and packed.parent_item == row.item_code:
					add(packed, "Packed Item", packed.warehouse or row.warehouse)
		else:
			add(row, "Delivery Note Item", row.warehouse)
	return lines


def _held_note(delivery_note, lock=False):
	"""The held note's stored values, else a readable refusal."""
	values = frappe.db.get_value(
		"Delivery Note",
		delivery_note,
		["docstatus", "custom_integration_status", "custom_shipping_status"],
		as_dict=True,
		for_update=lock,
	)
	if not values or not is_held(values):
		if lock:
			frappe.db.commit()
		frappe.throw(
			_("This Delivery Note is not waiting for its goods to come back from Noon."), title=_("Noon shipment")
		)


def get_return_lines(delivery_note):
	"""The lines of the Confirm Returned dialog of a held note: ``[{name, item_code, item_name, shipped_qty, uom}]``.
	The API authorized the caller (the note is read as the system)."""
	_held_note(delivery_note)
	doc = frappe.get_doc("Delivery Note", delivery_note)
	if not noon_order_name(doc):
		frappe.throw(_("This Delivery Note is not a Noon shipment."))
	return [
		{
			"name": line.name,
			"item_code": line.item_code,
			"item_name": line.item_name,
			"shipped_qty": line.qty,
			"uom": line.uom,
		}
		for line in _stock_lines(doc)
	]


def _check_returned(doc, lines, write_off):
	"""The write-off plan (nothing is written): ``[(line, shortfall)]`` for the lines with a shortfall. Refuses
	(``frappe.ValidationError``) a missing, unknown or repeated line, a returned quantity below 0 or above the shipped
	one, a shortfall without the write-off, a write-off of a serial / batch line and a Company without a Stock
	Adjustment Account or default cost center."""
	posted = {line.name: line for line in _stock_lines(doc)}
	sent = {}
	if isinstance(lines, str):
		try:
			lines = frappe.parse_json(lines)
		except Exception:
			lines = None
	if lines is None:
		lines = []
	if not isinstance(lines, (list, tuple)) or not all(isinstance(entry, dict) for entry in lines):
		frappe.throw(_("The returned quantities are not a list of lines."))
	for entry in lines:
		name = cstr(entry.get("name"))
		if name not in posted or name in sent:
			frappe.throw(_("The returned quantities do not match the lines of this Delivery Note."))
		sent[name] = entry.get("returned_qty")
	if set(sent) != set(posted):
		frappe.throw(_("The returned quantities do not match the lines of this Delivery Note."))
	plan = []
	for name, line in posted.items():
		try:
			if sent[name] is None or cstr(sent[name]).strip() == "":
				raise ValueError  # a blank is not "none came back"
			number = float(sent[name])
			if not math.isfinite(number):
				raise ValueError  # nan / inf
			returned = flt(number, line.precision)
		except (TypeError, ValueError):
			frappe.throw(_("Returned quantity of {0} is not a number.").format(frappe.bold(escape_html(line.item_code))))
		if returned < 0 or returned > line.qty:
			frappe.throw(
				_("Returned quantity of {0} must be between 0 and the shipped quantity ({1}).").format(
					frappe.bold(escape_html(line.item_code)), line.qty
				)
			)
		shortfall = flt(line.qty - returned, line.precision)
		if shortfall > 0:
			plan.append((line, shortfall))
	if plan and not write_off:
		frappe.throw(
			_("Some units did not come back. Tick Create Material Issue for broken/missing units, or enter the full shipped quantity.")
		)
	if write_off and plan:
		for line, _shortfall in plan:
			if line.serial_batch:
				frappe.throw(
					_("Item {0} has serial or batch numbers: write off its missing units by hand with a Stock Entry.").format(
						frappe.bold(escape_html(line.item_code))
					)
				)
		accounts = frappe.db.get_value("Company", doc.company, ["stock_adjustment_account", "cost_center"], as_dict=True)
		if not accounts or not accounts.stock_adjustment_account or not accounts.cost_center:
			frappe.throw(
				_("Company {0} needs a Stock Adjustment Account and a Default Cost Center to write off units.").format(
					frappe.bold(escape_html(doc.company))
				)
			)
	return plan


def _write_off(doc, plan):
	"""Insert and submit the Material Issue for the units that did not come back (``plan`` from
	``_check_returned``), on the Company's Stock Adjustment Account and default cost center whatever the Item says
	(ERPNext 16.51.1 would otherwise default them from the Item). Returns the Stock Entry."""
	accounts = frappe.db.get_value("Company", doc.company, ["stock_adjustment_account", "cost_center"], as_dict=True)
	entry = frappe.new_doc("Stock Entry")
	entry.stock_entry_type = "Material Issue"
	entry.purpose = "Material Issue"
	entry.company = doc.company
	entry.remarks = _("Units of Delivery Note {0} that did not come back after Noon cancelled the shipment.").format(doc.name)
	for line, shortfall in plan:
		entry.append(
			"items",
			{
				"item_code": line.item_code,
				"s_warehouse": line.warehouse,
				"qty": shortfall,
				"uom": line.uom,
				# a Packed Item's qty is in its stock UOM; the Delivery Note Item's is in its sales UOM
				"conversion_factor": 1 if line.doctype == "Packed Item" else line.conversion_factor,
				"expense_account": accounts.stock_adjustment_account,
				"cost_center": accounts.cost_center,
			},
		)
	# Permission bypass (narrow): ``api/noon_fulfillment.confirm_returned`` authorized the caller (Noon User or
	# System Manager, read on the note), who may hold no Stock Entry rights.
	entry.flags.ignore_permissions = True
	entry.insert()
	entry.submit()
	return entry


def confirm_returned(delivery_note, lines, write_off):
	"""Confirm Returned of a held note (the API authorized the caller). The goods are back: ONE transaction
	cancels the unpaid linked Sales Invoices and the note (stock back in), then, when ``write_off`` and some
	units did not come back, writes them off with a Material Issue. Any failure rolls it all back (the note stays
	held) and shows the escaped reason. After the commit: ``after_cancelled``. Returns ``{"stock_entry"}``.

	``lines`` is ``[{"name", "returned_qty"}]`` for every line of ``get_return_lines``. Commit, then lock the
	note as the next statement (MariaDB snapshot isolation); a second call finds it cancelled and is refused."""
	from marketplace_integrations.services import carrier_shipments

	write_off = bool(cint(write_off))
	frappe.db.commit()
	_held_note(delivery_note, lock=True)
	doc = frappe.get_doc("Delivery Note", delivery_note)
	try:
		if not noon_order_name(doc):
			frappe.throw(_("This Delivery Note is not a Noon shipment."))
		plan = _check_returned(doc, lines, write_off)
	except Exception:
		frappe.db.commit()
		raise
	try:
		# Permission bypass (narrow): the API authorized the caller; the invoice cancels and the note cancel are
		# the system's work (the person may hold no cancel right on them).
		cancelled_invoices = carrier_shipments._cancel_linked_sales_invoices(doc)
		if cancelled_invoices:
			doc.reload()  # the invoice cancel rewrote the note's billing status
		carrier_shipments._check_native_cancel_not_blocked(doc)
		doc.flags.ignore_permissions = True
		doc.flags[CONFIRM_RETURNED_FLAG] = True
		# The order cancel hook (order_cancel_cascade) reads it: Noon cancelled, a person did not.
		doc.flags.cancelled_by_carrier = True
		doc.cancel()
		entry = _write_off(doc, plan) if write_off and plan else None
		if entry:
			doc.add_comment(
				"Comment", _("Units that did not come back were written off with Stock Entry {0}.").format(entry.name)
			)
	except Exception as exc:
		frappe.db.rollback()
		if not isinstance(exc, frappe.ValidationError):
			frappe.log_error(title="Confirm Returned failed", message=f"{delivery_note}\n{frappe.get_traceback()}")
		frappe.clear_messages()
		reason = escape_html(strip_html_tags(cstr(exc)).strip()[:300]) or exc.__class__.__name__
		frappe.throw(
			_("The Delivery Note was not cancelled and nothing was written off: {0}").format(reason),
			title=_("Noon shipment"),
		)
	frappe.db.commit()
	after_cancelled(delivery_note)
	return {"stock_entry": entry.name if entry else None}


# ---------------------------------------------------------------------------
# Shipment invoice (spec section 9, #416)
# ---------------------------------------------------------------------------
# Invoice upload is a backup step: it never blocks Submit or Cancel (nothing here is called from a document
# event). The PDF is attached by a person; nothing is generated.

_KEY_PATTERN = re.compile(r"[A-Za-z0-9]{1,64}")
#: Cache marker "an invoice upload is queued or running for this note" (value: the press key), cleared by the
#: job; it expires on its own after the job timeout (300 s) plus a margin, so a killed job cannot block forever.
_INVOICE_INFLIGHT = "noon-shipment-invoice-inflight|{}"
_INVOICE_INFLIGHT_TTL = 600


def upload_invoice_for_note(delivery_note, file_url, invoice_nr, issued_at, idempotency_key=None):
	"""Request path of "Upload Shipment Invoice" (the API checked write on the note). Needs a submitted note
	whose Noon shipment is Created and an FBPI warehouse (clear refusal otherwise). The PDF becomes a private
	File attached to the note, then the job is queued. A new press makes a new idempotency key; Retry passes
	the key of its press back so Noon sees the same upload. Returns ``{"queued", "idempotency_key",
	"file_url"}`` (the attached copy, which a Retry sends)."""
	doc = frappe.get_doc("Delivery Note", delivery_note)
	if not noon_order_name(doc):
		frappe.throw(_("This Delivery Note is not a Noon shipment."))
	if doc.docstatus != 1 or cstr(doc.get("custom_integration_status")) != CREATED:
		frappe.throw(_("A shipment invoice can be uploaded only for a submitted Delivery Note whose Noon shipment is Created."))
	account, _client = _account_and_client(doc)
	check_fbpi_warehouse(doc, account)
	invoice_nr = cstr(invoice_nr).strip()
	if not invoice_nr or not issued_at:
		frappe.throw(_("Invoice Nr and Issued At are required."))
	if idempotency_key is not None and not _KEY_PATTERN.fullmatch(cstr(idempotency_key)):
		frappe.throw(_("Invalid upload key."))
	if idempotency_key is not None and not fulfillment.invoice_key_belongs_to(idempotency_key, "Delivery Note", doc.name):
		frappe.throw(_("Invalid upload key."))
	# One upload per note at a time: a double press must not send two (a Retry of the same key is allowed).
	marker_key = _INVOICE_INFLIGHT.format(doc.name)
	# Check and set the marker under the note's row lock (commit, then lock as the next statement): two presses
	# cannot both read "none running" and both set it. The lock is released by the commit below.
	frappe.db.commit()
	frappe.db.get_value("Delivery Note", doc.name, "name", for_update=True)
	running = frappe.cache().get_value(marker_key)
	if running and running != cstr(idempotency_key):
		frappe.throw(_("An invoice upload for this Delivery Note is already running. Wait for it to finish."))
	# the caller has write permission on this note (api/noon_fulfillment.py); the File is attached to it
	file_url = fulfillment.attach_invoice_file(file_url, "Delivery Note", doc.name)
	key = cstr(idempotency_key) or frappe.generate_hash(length=32)
	frappe.cache().set_value(marker_key, key, expires_in_sec=_INVOICE_INFLIGHT_TTL)
	fulfillment.set_invoice_result(key, "pending")
	fulfillment.bind_invoice_key(key, "Delivery Note", doc.name)
	frappe.db.commit()
	try:
		job = frappe.enqueue(
			"marketplace_integrations.integrations.noon.delivery_note_shipment.run_upload_invoice",
			queue="long",
			timeout=300,
			job_id=f"noon-shipment-invoice-{doc.name}-{key}",
			deduplicate=True,
			delivery_note=doc.name,
			file_url=file_url,
			invoice_nr=invoice_nr,
			issued_at=cstr(issued_at),
			idempotency_key=key,
		)
	except Exception:
		frappe.cache().delete_value(marker_key)
		raise
	return {"queued": job is not None, "idempotency_key": key, "file_url": file_url}


def run_upload_invoice(delivery_note, file_url, invoice_nr, issued_at, idempotency_key):
	"""Job of ``upload_invoice_for_note``: UploadShipmentInvoice with the note's shipment number
	(``custom_carrier_reference``), logged, never holding a lock across the call. Success records the upload
	time on the note (``db_set``: a system write-back on a submitted note); every
	failure only reaches the result the dialog reads (Retry) and never touches the note's state."""
	frappe.set_user("Administrator")
	try:
		doc = frappe.get_doc("Delivery Note", delivery_note)
		if doc.docstatus != 1 or cstr(doc.get("custom_integration_status")) != CREATED or not noon_order_name(doc):
			fulfillment.set_invoice_result(
				idempotency_key, "failed", _("The Delivery Note no longer has a Created Noon shipment.")
			)
			return
		account, client = _account_and_client(doc)
		reference = _reference(doc)
		date = getdate(issued_at).strftime("%Y-%m-%d")
		file_doc = frappe.get_doc("File", {"file_url": file_url})
		content = file_doc.get_content()
		if isinstance(content, str):
			content = content.encode()
		body = {
			"warehouse_code": account.noon_warehouse_code,
			"integration_shipment_nr": reference,
			"invoice": base64.b64encode(content).decode(),
			"invoice_nr": invoice_nr,
			"issued_at": date,
			"idempotency_key": idempotency_key,
		}
		frappe.db.commit()  # no transaction open across the call
		outcome, message = fulfillment.send_invoice(
			client,
			SHIPMENT_INVOICE_UPLOAD_PATH,
			body,
			"Delivery Note",
			doc.name,
			reference,
			{"integration_shipment_nr": reference, "invoice_nr": invoice_nr, "issued_at": date},
		)
		if outcome != "done":
			fulfillment.set_invoice_result(idempotency_key, "failed", message)
			return
		doc.db_set(
			{"custom_noon_invoice_uploaded_on": now_datetime()},
			update_modified=False,
			notify=True,
			commit=True,
		)
		fulfillment.set_invoice_result(idempotency_key, "done")
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Noon shipment invoice failed", message=frappe.get_traceback())
		fulfillment.set_invoice_result(
			idempotency_key, "failed", _("The invoice could not be sent. See the Error Log.")
		)
	finally:
		marker_key = _INVOICE_INFLIGHT.format(delivery_note)
		if frappe.cache().get_value(marker_key) == idempotency_key:
			frappe.cache().delete_value(marker_key)


# ---------------------------------------------------------------------------
# Status, nightly, Needs Attention
# ---------------------------------------------------------------------------


def _unit_status(item):
	"""Noon's word for one unit: Cancelled (``mp_status``) wins, else the ``integration_status``; ``None``
	when Noon sent a status this app does not know."""
	if item.get("mp_status") == MP_STATUS_CANCELLED:
		return SHIPPING_CANCELLED
	return _INTEGRATION_STATUS_NAMES.get(item.get("integration_status"))


def _combine(statuses):
	"""All agree = that status, they differ = Mixed, any unknown (or none at all) = ``None``."""
	statuses = list(statuses)
	if not statuses or None in statuses:
		return None
	return statuses[0] if len(set(statuses)) == 1 else SHIPPING_MIXED


def _unit_details(raw_order, units):
	"""``{mp_item_nr: (status, partner_sku)}`` of the shipment's units from GetFbpiOrder (a unit the order no
	longer lists has status ``None``)."""
	items = raw_order.get("items") if isinstance(raw_order, dict) else None
	by_nr = {cstr(item.get("mp_item_nr")).strip(): item for item in items or [] if isinstance(item, dict)}
	return {
		unit: (_unit_status(by_nr[unit]), cstr(by_nr[unit].get("partner_sku")).strip()) if unit in by_nr else (None, "")
		for unit in units
	}


def derive_status(doc, shipment, raw_order, marketplace_account):
	"""What a refresh writes (no writes here): ``_dict(shipping_status, rows, error)``.

	``shipping_status`` is derived from the units in the snapshot (all agree = that status, all cancelled =
	Cancelled, differ = Mixed), ``None`` when a unit is unknown or the snapshot is unreadable (nothing is
	changed then). ``rows`` is ``{Delivery Note Item name: status}``: the snapshot holds only ``mp_item_nr``,
	so a unit is placed on a row through its ``partner_sku`` (GetFbpiOrder) -> Item (the mapping): the units of
	an Item are pooled over every row of that Item. ``error`` is the readable problem (``None`` when the
	shipment is as sent): cancelled or differing units, or items that differ from the snapshot. Items are
	never edited."""
	units = _snapshot_units(doc.get("custom_carrier_request_snapshot"))
	result = frappe._dict(shipping_status=None, rows={}, error=None)
	if not units:
		return result
	details = _unit_details(raw_order, units)
	result.shipping_status = _combine(status for status, _sku in details.values())
	problems = []
	cancelled = [unit for unit, (status, _sku) in details.items() if status == SHIPPING_CANCELLED]
	if result.shipping_status is None:
		problems.append(_("Noon returned a unit status this app does not know or no longer lists a unit."))
	elif result.shipping_status == SHIPPING_CANCELLED:
		problems.append(_("Noon shows this shipment as cancelled. Cancel the Delivery Note here."))
	elif cancelled:
		problems.append(_("Noon cancelled {0} of {1} units of this shipment.").format(len(cancelled), len(units)))
	elif result.shipping_status == SHIPPING_MIXED:
		problems.append(_("The units of this shipment have different statuses at Noon."))
	if shipment is not None:
		shipped = {cstr(item.get("mp_item_nr")).strip() for item in shipment.get("items") or []}
		if shipped != set(units):
			problems.append(
				_("The items of the Noon shipment differ from the units sent. Items are never changed here: cancel the shipment and amend the Delivery Note.")
			)
	result.error = " ".join(problems) or None
	if result.shipping_status is not None:
		by_item = {}
		for status, sku in details.values():
			item = resolve_item(marketplace_account, sku) if sku else None
			by_item.setdefault(item, []).append(status)
		for row in doc.get("items") or []:
			status = _combine(by_item.get(row.item_code, []))
			if status:
				result.rows[row.name] = status
	return result


def _read_noon(doc, lookup_shipment=True):
	"""The HTTP part of a refresh (nothing locked, nothing written): ``(shipment, raw_order, account,
	marketplace_account)``. ``shipment`` is ``None`` when Noon confirms none. Raises ``Unknown``."""
	account, _client = _client_for(doc, Unknown)
	order = _order_values(doc)
	shipment = find_remote(doc) if lookup_shipment else None
	try:
		raw_order = fulfillment.get_fbpi_order(account, order.external_order_id)
	except Exception as exc:
		frappe.clear_messages()
		raise Unknown(_("Noon did not answer the order lookup ({0}).").format(cstr(exc)[:200])) from exc
	return shipment, raw_order, account, order.marketplace_account


def refresh(delivery_note):
	"""The contract's ``refresh``: GetShipment + GetFbpiOrder, then the derived Shipping Status, the item
	row statuses and Last Sync At are written (``derive_status``); returns the stored Shipping Status.

	HTTP first, then lock and write (nothing is held across the calls). Created: derive and write. Cancellation
	Unknown: only GetFbpiOrder, every unit cancelled resolves it to Cancelled (Noon asked first, never a
	cancel call). Other statuses, and a Cancelled note (terminal), only return the stored status. A Noon
	error is a readable ``frappe.ValidationError`` (the Refresh button shows it; jobs log it)."""
	doc = frappe.get_doc("Delivery Note", delivery_note)
	status = cstr(doc.get("custom_integration_status"))
	if (
		status == CANCELLED
		and doc.docstatus == 1
		and cstr(doc.get("custom_last_integration_error"))
		and cstr(doc.get("custom_shipping_status")) != SHIPPING_AWAITING_RETURN
	):
		# Cancelled with an error left by an earlier blocked automatic cancel: retry it (a note this app's
		# own Cancel just wrote Cancelled has no error and is left to the person about to cancel it). A held
		# note (Awaiting Return) is never retried: only Confirm Returned cancels it.
		_cancel_note_natively(delivery_note)
		return frappe.db.get_value("Delivery Note", delivery_note, "custom_shipping_status")
	if status not in (CREATED, CANCELLATION_UNKNOWN) or not noon_order_name(doc):
		return doc.get("custom_shipping_status")
	try:
		shipment, raw_order, _account, marketplace_account = _read_noon(doc, lookup_shipment=status == CREATED)
	except Unknown as unknown:
		frappe.throw(cstr(unknown), title=_("Noon shipment"))
	frappe.db.commit()  # lock as the next statement (MariaDB snapshot isolation)
	locked = frappe.db.get_value("Delivery Note", delivery_note, "custom_integration_status", for_update=True)
	if cstr(locked) != status:
		frappe.db.commit()
		return frappe.db.get_value("Delivery Note", delivery_note, "custom_shipping_status")
	note = frappe.get_doc("Delivery Note", delivery_note)
	values = {"custom_last_sync_at": now_datetime()}
	if status == CANCELLATION_UNKNOWN:
		if _all_units_cancelled(raw_order, _snapshot_units(note.get("custom_carrier_request_snapshot"))):
			values.update(
				custom_integration_status=CANCELLED, custom_shipping_status=SHIPPING_CANCELLED, custom_last_integration_error=None
			)
		note.db_set(lifecycle.with_state(delivery_note, values), update_modified=False, notify=True, commit=True)
		_update_summary(delivery_note)
		if values.get("custom_integration_status") == CANCELLED:
			_cancel_note_natively(delivery_note)
		return frappe.db.get_value("Delivery Note", delivery_note, "custom_shipping_status")
	held = False
	snapshot = _snapshot_data(note.get("custom_carrier_request_snapshot"))
	if shipment is None:
		found_error = _("Noon has no shipment with number {0}.").format(_reference(note))
		derived = None
	else:
		derived = derive_status(note, shipment, raw_order, marketplace_account)
		found_error = derived.error
	# The error field also holds other writers' text (a refused cancel): a clean refresh clears only the
	# text this refresh wrote itself (remembered in the snapshot).
	owned = cstr(snapshot.get("refresh_error"))
	if found_error:
		values["custom_last_integration_error"] = found_error
		snapshot["refresh_error"] = found_error
	elif not cstr(note.get("custom_last_integration_error")) or cstr(note.get("custom_last_integration_error")) == owned:
		values["custom_last_integration_error"] = None
		snapshot.pop("refresh_error", None)
	if snapshot != _snapshot_data(note.get("custom_carrier_request_snapshot")):
		values["custom_carrier_request_snapshot"] = json.dumps(snapshot)
	if derived is not None:
		if derived.shipping_status:
			values["custom_shipping_status"] = derived.shipping_status
		if derived.shipping_status == SHIPPING_CANCELLED and note.docstatus == 1:
			# Every unit of the shipment is cancelled at Noon (the proof ``cancel_remote`` accepts): the
			# shipment is Cancelled, so the native cancel of the note is allowed and done below.
			values.update(custom_integration_status=CANCELLED, custom_last_integration_error=None)
			snapshot.pop("refresh_error", None)
			if snapshot != _snapshot_data(note.get("custom_carrier_request_snapshot")):
				values["custom_carrier_request_snapshot"] = json.dumps(snapshot)
			if picked_up(note):
				# #527: a person confirmed the pickup (the stage, never Noon's Shipped), so the goods may be on
				# the road: the stock stays out until Confirm Returned. Held = Cancelled + Awaiting Return,
				# no error text, and no native cancel.
				values["custom_shipping_status"] = SHIPPING_AWAITING_RETURN
				held = True
		for row in note.get("items") or []:
			if derived.rows.get(row.name):
				row.db_set("custom_noon_item_status", derived.rows[row.name], update_modified=False)
	note.db_set(lifecycle.with_state(delivery_note, values), update_modified=False, notify=True, commit=True)
	_update_summary(delivery_note)
	if held:
		_notify_held(note)
	elif values.get("custom_integration_status") == CANCELLED:
		_cancel_note_natively(delivery_note)
	return frappe.db.get_value("Delivery Note", delivery_note, "custom_shipping_status")


def _notify_held(note):
	"""One Notification Log to the submitter and the Stock Managers when a note is held (the refresh that wrote it
	runs once: a second read finds the note no longer Created). Never raises."""
	from marketplace_integrations.services import carrier_shipments

	carrier_shipments._notify(
		note,
		_("Noon cancelled Delivery Note {0} after pickup. Stock stays out until you press Confirm Returned.").format(note.name),
	)
	frappe.db.commit()


def _cancel_note_natively(delivery_note):
	"""Noon cancelled the shipment: the neutral automatic native cancel (no Noon call, never raises)."""
	from marketplace_integrations.services import carrier_shipments

	carrier_shipments.cancel_after_carrier_cancelled(delivery_note)


def run_refresh(delivery_note):
	"""Background job of a webhook refresh (one per submitted Noon note, ``enqueue_note_refreshes``).

	A webhook that arrives while this job runs is dropped by the queue's deduplication, but it leaves a
	"again" flag: the job clears the flag before each read and reads once more when it was set again meanwhile
	(at most ``_REFRESH_PASSES`` reads)."""
	frappe.set_user("Administrator")
	key = _REFRESH_AGAIN.format(delivery_note)
	for _pass in range(_REFRESH_PASSES):
		frappe.cache().delete_value(key)
		try:
			refresh(delivery_note)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="Noon shipment refresh failed", message=frappe.get_traceback())
			return
		if not frappe.cache().get_value(key):
			return


def enqueue_note_refreshes(marketplace_order_name):
	"""Queue a refresh for every submitted Noon Delivery Note of the Marketplace Order (the
	``FBPI::ORDER_SYNC`` webhook path, ``orders.refresh_order``). One deduplicated job per note, queued after
	the caller's commit. Draft notes are ignored. Read as the system: the webhook is already authenticated."""
	supplier = transporter_name()
	orders = frappe.get_all(
		"Sales Order", filters={"custom_marketplace_order": marketplace_order_name, "docstatus": 1}, pluck="name"
	)
	if not supplier or not orders:
		return []
	parents = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": ["in", orders], "docstatus": 1, "parenttype": "Delivery Note"},
		pluck="parent",
		distinct=True,
		parent_doctype="Delivery Note",
	)
	notes = frappe.get_all(
		"Delivery Note",
		filters={
			"name": ["in", parents],
			"docstatus": 1,
			"is_return": 0,
			"transporter": supplier,
			"custom_integration_status": ["in", [CREATED, CANCELLATION_UNKNOWN]],
		},
		pluck="name",
	) if parents else []
	for note in notes:
		# Set before queueing: if the queue drops this job because one is running, that job reads again.
		frappe.cache().set_value(_REFRESH_AGAIN.format(note), 1, expires_in_sec=3600)
		frappe.enqueue(
			"marketplace_integrations.integrations.noon.delivery_note_shipment.run_refresh",
			queue="short",
			timeout=120,
			job_id=_REFRESH_JOB.format(note),
			deduplicate=True,
			enqueue_after_commit=True,
			delivery_note=note,
		)
	return notes


# ---------------------------------------------------------------------------
# Nightly reconcile
# ---------------------------------------------------------------------------


def reconcile():
	"""The contract's ``reconcile`` (called by ``carrier_shipments.reconcile_open_notes``, 23:59): queue the one
	long job ``run_reconcile`` (``job_id`` "noon-reconcile", deduplicated)."""
	frappe.enqueue(
		"marketplace_integrations.integrations.noon.delivery_note_shipment.run_reconcile",
		queue="long",
		timeout=3600,
		job_id=RECONCILE_JOB,
		deduplicate=True,
	)


def run_reconcile():
	"""Check every open Noon note, one at a time (committed per note, one note's failure never stops the rest):

	- Creating (stale) / Creation Unknown: ``carrier_shipments.check``, a lookup that never creates.
	- Cancellation Unknown: ``refresh`` asks Noon first and resolves a cancel Noon already did.
	- Draft with a shipment number (Not Created / Creation Failed): GetShipment found = the error
	  "draft holds a Noon shipment" for Needs Attention; the shipment is never adopted automatically.
	- Created (draft or submitted): ``refresh``; cancelled units / differing items set the error."""
	from marketplace_integrations.services import carrier_shipments

	frappe.set_user("Administrator")
	supplier = transporter_name()
	if not supplier:
		return
	notes = frappe.get_all(
		"Delivery Note",
		filters={
			"transporter": supplier,
			"is_return": 0,
			"docstatus": ["<", 2],
			"custom_integration_status": ["in", [CREATING, UNKNOWN, CANCELLATION_UNKNOWN, CREATED, NOT_CREATED, FAILED]],
		},
		fields=[
			"name",
			"docstatus",
			"custom_integration_status",
			"custom_carrier_reference",
			"custom_shipping_status",
			lifecycle.FIELD,
			"creation",
		],
		limit_page_length=0,
	)
	cutoff = add_days(now_datetime(), -REFRESH_MAX_AGE_DAYS)
	# A Created note stops being read once its stage is Delivered or Noon shows it Cancelled, or after 60 days (two
	# calls per note per night are not spent forever); the webhook and the Refresh button still work on it. Noon's
	# "Shipped" no longer stops it (#541): Noon sets it right after creation, and a Ready for Pickup note can still
	# be cancelled at Noon. A submitted Created note that shows Cancelled is kept: the refresh makes its
	# Integration Status Cancelled, which cancels the note natively.
	notes = [
		note
		for note in notes
		if cstr(note.custom_integration_status) != CREATED
		or (note.docstatus == 1 and cstr(note.custom_shipping_status) == SHIPPING_CANCELLED)
		or (
			cstr(note.custom_shipping_status) != SHIPPING_CANCELLED
			and cstr(note.get(lifecycle.FIELD)) != lifecycle.DELIVERED
			and note.creation >= cutoff
		)
	]
	for note in notes:
		try:
			_reconcile_note(note, carrier_shipments)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="Noon shipment reconcile failed", message=f"{note.name}\n{frappe.get_traceback()}")


def _reconcile_note(note, carrier_shipments):
	status = cstr(note.custom_integration_status) or NOT_CREATED
	if status == CREATING and not carrier_shipments.is_stale_creating(note.name):
		return
	if status in (CREATING, UNKNOWN):
		carrier_shipments.check(note.name)
	elif status in (CANCELLATION_UNKNOWN, CREATED):
		refresh(note.name)
	elif note.docstatus == 0 and cstr(note.custom_carrier_reference).strip():
		doc = frappe.get_doc("Delivery Note", note.name)
		if noon_order_name(doc) and find_remote(doc) is not None:
			frappe.db.set_value(
				"Delivery Note",
				note.name,
				"custom_last_integration_error",
				_("This draft holds a Noon shipment ({0}). Check it in Noon before sending or deleting the draft.").format(
					cstr(note.custom_carrier_reference)
				),
				update_modified=False,
			)


def problem_states():
	"""The contract's ``problem_states``: Needs Attention "Carrier Exception" rows for a note whose units differ
	at Noon (Mixed) or that Noon shows cancelled while the note is still open (the refresh wrote the reason
	to ``custom_last_integration_error``)."""
	from marketplace_integrations.services import marketplace_attention as attention

	return {attention.CARRIER_EXCEPTION: [SHIPPING_MIXED, SHIPPING_CANCELLED]}


def terminal_states(is_return=False):
	"""The contract's ``terminal_states``: Cancelled never changes again."""
	return [SHIPPING_CANCELLED]
