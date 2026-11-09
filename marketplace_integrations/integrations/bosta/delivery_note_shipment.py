# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bosta adapter of the neutral carrier contract (``services/carrier_shipments.py``; epic #413, #408, #415).

Delivery Note-first (``docs/marketplace-integrations/delivery-note-first.md``): the Delivery Note
with transporter "Bosta" is the shipment. Submit starts a background job (``carrier_shipments.request_create``
then ``run_create``); once the shipment is Created the note STAYS A DRAFT (stage Shipment Created, #500) and
Bosta's pickup event submits it as the requester (``statuses._write_state``; ``require_shipment_created`` still
refuses any submit before Created).
This module holds only the Bosta parts: the contract methods and the Bosta-only helpers (city, COD, label).

Native fields used: ``transporter`` (Supplier "Bosta", created by a patch), ``lr_no`` (the Bosta
tracking number) and ``lr_date``. The generic shipping fields (``custom_integration_status``,
``custom_shipping_status``, ``custom_carrier_delivery_id``, ``custom_carrier_reference``,
``custom_cod_amount``, ``custom_shipping_label``, ``custom_last_sync_at``,
``custom_last_integration_error``, ``custom_carrier_request_snapshot``) are carrier neutral; the
Bosta-only ones are ``custom_bosta_city`` and ``custom_bosta_city_name``.

The Integration Log is the native Integration Request (service "Bosta", ``services/carrier_log.py``). It
never holds the API key, the webhook secret, or the receiver's name, phone or address.

COD: while the note is editable (Not Created / Creation Failed) the default COD is recomputed on every
save until the person confirms it (``custom_cod_confirmed``: ticked, or a changed COD); Submit refuses a
note whose COD was never confirmed. A typed 0 is a deliberate choice once confirmed.

Rules enforced here:

- Bosta has no idempotency key. The first attempt creates; **every later attempt looks the delivery up
  by its business reference first** (a note whose ``custom_carrier_reference`` is set was tried before,
  which survives Integration Request cleanup) and creates only when Bosta has none. There is no automatic retry
  of a create. A timeout, a connection error, a 5xx/429, a malformed 2xx or an ambiguous lookup is
  "Creation Unknown", never a failure.
- No lock is held across an HTTP call (the neutral ``_finish`` commits, takes the row lock as the next
  statement and writes only if the note is still Creating).
- The worker writes with ``db_set`` (no validate, no hooks): a system write-back.
"""

import html
import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, now_datetime

from marketplace_integrations.integrations.bosta import city_match, locations
from marketplace_integrations.integrations.bosta.client import (
	SERVICE_NAME,
	BostaClient,
	is_success_response,
	response_body,
)
from marketplace_integrations.integrations.bosta.constants import BOSTA_EXCLUDED_PLATFORMS
from marketplace_integrations.services import carrier_log
from marketplace_integrations.services.carrier_log import (
	CANCELLATION_PENDING,
	CANCELLATION_UNKNOWN,
	CANCELLED,
	CREATED,
	CREATING,
	EDITABLE_STATUSES,
	FAILED,
	LIVE_STATUSES,
	NOT_CREATED,
	UNKNOWN,
	Failed,
	Unknown,
)

#: ``supplier_name`` of the native Supplier used as the Delivery Note ``transporter``. The Supplier's
#: document name depends on Buying Settings, so it is looked up by this name, never assumed.
SUPPLIER_NAME = "Bosta"
#: The carrier name the Delivery Note shows its Shipping section for (``carrier_shipments.boot_session``).
SHIPPING_CARRIER = SUPPLIER_NAME

BOSTA_DELIVERY_TYPE = 10
#: Customer Return Pickup: a type 25 delivery whose COD is negative (the refund the courier hands over).
BOSTA_RETURN_PICKUP_TYPE = 25
#: ``custom_return_source`` of a return the staff want Bosta to collect from the customer.
RETURN_PICKUP = "Customer Return Pickup"
#: ``custom_return_source`` of the draft return the app makes when Bosta returns a delivery (automatic only).
RETURNED_BY_BOSTA = "Returned by Bosta"
# The adapter's optional "return source" that marks a return it created (read by the Needs Attention report).
RETURNED_SOURCE = RETURNED_BY_BOSTA
#: Set on the draft the app makes itself (``returns.create_returned_draft``); a person's save never has it.
AUTOMATIC_RETURN_FLAG = "bosta_automatic_return"
BOSTA_COD_LIMIT = 30000
#: #500: a Bosta note stays a draft (stage Shipment Created) until Bosta reports the pickup; the pickup event submits
#: it (``statuses._write_state`` -> ``carrier_shipments.run_submit_at_pickup``).
SUBMIT_AT_PICKUP = True
#: A NEW draft Bosta note starts its shipment by itself (``carrier_shipments.request_shipment_on_insert``); its
#: ``validate`` runs ``guard_new_note`` and the stock check first.
REQUEST_ON_INSERT = True
# The Integration Status values, LIVE_STATUSES and EDITABLE_STATUSES come from ``services/carrier_log.py``
# (shared with the neutral service).


#: Written by the app only (workers and APIs); a person's save never changes them.
SYSTEM_FIELDS = (
	"custom_integration_status",
	"custom_shipping_status",
	"custom_carrier_delivery_id",
	"custom_carrier_reference",
	"custom_shipping_label",
	"custom_last_sync_at",
	"custom_last_integration_error",
	"custom_carrier_request_snapshot",
	"custom_actual_shipping_cost",
	"custom_actual_cost_received_on",
	"custom_bosta_last_event_at",
	"custom_shipment_requested_by",
)
#: Cleared on a new or amended note: Amend copies even ``no_copy`` fields (conventions).
CLEARED_ON_NEW = (*SYSTEM_FIELDS, "custom_bosta_city", "custom_bosta_city_name")
#: Fields a person edits that must stay put once the shipment is started.
LOCKED_FIELDS = ("transporter", "lr_no", "custom_cod_amount", "custom_bosta_city", "custom_bosta_city_name")
#: Read-only on the form, set by the city dialog: restored from the stored note once the shipment is started.
CITY_FIELDS = ("custom_bosta_city", "custom_bosta_city_name")

#: What Bosta received as the receiver phone and drop-off address (``build_payload``). After the start only
#: the Edit Bosta Shipment dialog changes them (``editing.edit_shipment``, which writes with ``db_set``).
RECEIVER_FIELDS = ("contact_mobile", "shipping_address_name", "shipping_address")

_LABEL_JOB = "bosta-label|{}"
_RETRYABLE_HTTP = {408, 429}

# The answers an adapter call raises are the neutral ones (``services/carrier_log.py``).
_Unknown = Unknown
_Failed = Failed


# ---------------------------------------------------------------------------
# Which notes belong to Bosta
# ---------------------------------------------------------------------------


def bosta_supplier():
	"""The Supplier docname of "Bosta" (looked up by ``supplier_name``), or ``None``."""
	return frappe.db.get_value("Supplier", {"supplier_name": SUPPLIER_NAME}, "name")


def applies_to(doc):
	"""A note whose transporter is the Bosta Supplier: a normal note, or a return the staff marked
	"Customer Return Pickup" (a plain return or one returned by Bosta is not a Bosta shipment)."""
	if not doc.get("transporter"):
		return False
	if doc.get("is_return") and doc.get("custom_return_source") != RETURN_PICKUP:
		return False
	return frappe.db.get_value("Supplier", doc.get("transporter"), "supplier_name") == SUPPLIER_NAME


def status_of(doc_or_name):
	"""The stored Integration Status of a note ("" counts as Not Created)."""
	name = doc_or_name if isinstance(doc_or_name, str) else doc_or_name.name
	return cstr(frappe.db.get_value("Delivery Note", name, "custom_integration_status")) or NOT_CREATED


def sales_order_of(doc):
	"""The one Sales Order the note's rows come from, else ``None``."""
	orders = {row.get("against_sales_order") for row in doc.get("items") or [] if row.get("against_sales_order")}
	return next(iter(orders)) if len(orders) == 1 else None


def excluded_platform(sales_order):
	"""The marketplace platform of the Sales Order when it fulfils its own orders (no Bosta), else ``None``.
	Read from the Marketplace Order, the authority; ``custom_marketplace_platform`` is only fetched on save."""
	if not sales_order:
		return None
	values = frappe.db.get_value(
		"Sales Order", sales_order, ["custom_marketplace_order", "custom_marketplace_platform"], as_dict=True
	)
	if not values:
		return None
	platform = (
		frappe.db.get_value("Marketplace Order", values.custom_marketplace_order, "platform")
		if values.custom_marketplace_order
		else values.custom_marketplace_platform
	)
	return platform if platform in BOSTA_EXCLUDED_PLATFORMS else None


# ---------------------------------------------------------------------------
# Document rules (called from services/carrier_shipments.py)
# ---------------------------------------------------------------------------


def _rows(doc):
	return sorted((cstr(row.name), cstr(row.item_code), flt(row.qty, 6)) for row in doc.get("items") or [])


def _only_reduced(before, doc):
	"""A draft Customer Return Pickup may have the returned quantity of a row lowered (never raised, never a
	row added, removed or changed to another Item) before it is submitted: what the customer really hands
	back can be less than what the delivery had. Bosta's request does not carry the items."""
	if not (doc.get("is_return") and doc.docstatus == 0):
		return False
	old = {cstr(row.name): row for row in before.get("items") or []}
	new = {cstr(row.name): row for row in doc.get("items") or []}
	if set(old) != set(new):
		return False
	return all(
		cstr(new[name].item_code) == cstr(old[name].item_code)
		and flt(new[name].qty) * flt(old[name].qty) > 0  # same sign (returns are negative), not zero
		and abs(flt(new[name].qty, 6)) <= abs(flt(old[name].qty, 6))
		for name in old
	)


def default_cod(doc):
	"""The note's share of what the customer still owes: its GRAND total (Shopify's total, shipping included;
	never the rounded total, which can differ by up to 0.50) minus the same share of the Sales Order's
	advance (proportional when the note ships part of the order), never below 0. An order that is fully paid
	by ERPNext's own measure (nothing outstanding on ``rounded_total`` else ``grand_total`` minus
	``advance_paid``; ERPNext caps a recorded payment at the rounded total, so 494 paid on 494.21 is fully
	paid) has COD 0, not the 0.21 the grand total alone would leave."""
	order = sales_order_of(doc)
	values = (
		frappe.db.get_value(
			"Sales Order", order, ["advance_paid", "rounded_total", "grand_total"], as_dict=True
		)
		if order
		else None
	)
	return cod_from_totals(flt(doc.get("grand_total")), values)


def cod_from_totals(total, order_values=None):
	"""``default_cod`` from numbers: ``total`` is the note's grand total, ``order_values`` the Sales Order's
	``advance_paid``, ``rounded_total`` and ``grand_total`` (``None`` for a note with no Sales Order). The edit of
	a Shopify order (#501) calls it with the totals the order WILL have, before ERPNext is changed."""
	advance = 0
	if order_values:
		order_total = flt(order_values.get("grand_total"))
		if (
			flt(order_values.get("advance_paid")) > 0
			and flt((flt(order_values.get("rounded_total")) or order_total) - flt(order_values.get("advance_paid")), 2)
			<= 0
		):
			return 0
		if order_total:
			advance = flt(order_values.get("advance_paid")) * min(total / order_total, 1)
	return max(flt(total - advance, 2), 0)


def cod_hint(delivery_note):
	"""The computed COD when it differs from the merchant's confirmed amount on an editable draft note, else
	``{"default": None}``. A confirmed amount is never recomputed (it may be a deliberate choice); this only
	lets the form say that the items or advance changed since."""
	doc = frappe.get_doc("Delivery Note", delivery_note)
	if doc.docstatus != 0 or status_of(doc) not in EDITABLE_STATUSES or not cint(doc.get("custom_cod_confirmed")):
		return {"default": None}
	default = default_cod(doc)
	if flt(default, 2) == flt(doc.get("custom_cod_amount"), 2):
		return {"default": None}
	return {"default": default}


def _check_cod(cod):
	if flt(cod) < 0:
		frappe.throw(_("COD cannot be negative."), title=_("Bosta shipment"))
	if flt(cod) > BOSTA_COD_LIMIT:
		frappe.throw(
			_("The COD amount {0} exceeds Bosta's limit of {1}.").format(flt(cod, 2), BOSTA_COD_LIMIT),
			title=_("Bosta shipment"),
		)


def _check_returned_cod(value):
	"""The Returned COD is entered as zero or more; the request sends it negative. No other ceiling is
	defined (the spec's business-policy limit is open), so none is checked."""
	if flt(value) < 0:
		frappe.throw(_("Returned COD cannot be negative."), title=_("Bosta shipment"))


def _apply_cod_rule(doc, before):
	"""Editable Bosta note: until the person confirms the COD (ticks ``custom_cod_confirmed`` or changes the
	amount) the default is recomputed on every save, so a carrier chosen after the first save or removed
	rows never leave a stale amount. A new note keeps a typed amount only when it is marked confirmed (a
	form sends 0 for an untouched Currency, which is not a choice)."""
	if cint(doc.get("custom_cod_confirmed")) and doc.get("custom_cod_amount") is not None:
		return
	if before is not doc and flt(doc.get("custom_cod_amount"), 2) != flt(before.get("custom_cod_amount"), 2):
		doc.custom_cod_confirmed = 1
		return
	doc.custom_cod_amount = default_cod(doc)


def clear_inherited(doc):
	"""``validate`` of a new note amended from a Bosta note that is no longer a Bosta note (the carrier was
	changed): Amend copies even ``no_copy`` fields, so the old shipment's id, tracking number and status
	would sit on a note that has no Bosta shipment."""
	for fieldname in CLEARED_ON_NEW:
		doc.set(fieldname, None)
	doc.custom_cod_amount = None
	doc.custom_cod_confirmed = 0
	if doc.get("lr_no") and doc.lr_no == frappe.db.get_value("Delivery Note", doc.amended_from, "lr_no"):
		doc.lr_no = None
		doc.lr_date = None


def guard_always(doc):
	"""``validate`` of every Delivery Note (the optional contract hook): a person cannot set or clear "Returned
	by Bosta" on ``custom_return_source`` (only the app does, on the draft it makes for a returned delivery).
	An amended copy keeps what its source note had."""
	if doc.docstatus == 2 or doc.get("_action") == "cancel" or doc.flags.get(AUTOMATIC_RETURN_FLAG):
		return
	after = cstr(doc.get("custom_return_source"))
	if doc.is_new():
		before = ""
		if doc.get("amended_from"):
			before = cstr(frappe.db.get_value("Delivery Note", doc.amended_from, "custom_return_source"))
	else:
		before = cstr((doc.get_doc_before_save() or doc).get("custom_return_source"))
	if before != after and RETURNED_BY_BOSTA in (before, after):
		frappe.throw(
			_("The return source \"Returned by Bosta\" is set by the app only. It cannot be set or cleared by hand."),
			title=_("Bosta shipment"),
		)


def _text(value):
	"""A text value as compared by the guard: Frappe's input sanitizer HTML-escapes a posted Data value while
	``db_set`` stored the raw one, and None, "" and surrounding spaces are the same "empty"."""
	return html.unescape(cstr(value)).strip()


def changed_locked_fields(before, doc):
	"""The translated labels of the locked fields (``LOCKED_FIELDS``, the COD amount and its confirmation) whose
	value really differs between the stored note and ``doc`` (normalized, so None/""/0 and rounding do not count)."""
	meta = frappe.get_meta("Delivery Note")
	changed = []
	for fieldname in (*LOCKED_FIELDS, "custom_cod_confirmed"):
		if fieldname == "custom_cod_amount":
			different = flt(before.get(fieldname), 2) != flt(doc.get(fieldname), 2)
		elif fieldname == "custom_cod_confirmed":
			different = cint(before.get(fieldname)) != cint(doc.get(fieldname))
		else:
			different = _text(before.get(fieldname)) != _text(doc.get(fieldname))
		if different:
			changed.append(_(meta.get_label(fieldname) or fieldname))
	return changed


def guard_changes(doc):
	"""``validate``: set up a new note; keep app-written fields and a started shipment untouched. A cancel
	changes none of them and must never be blocked by this guard."""
	if doc.docstatus == 2 or doc.get("_action") == "cancel":
		return
	ours = applies_to(doc)
	platform = excluded_platform(sales_order_of(doc)) if ours else None
	if platform:
		frappe.throw(
			_("Bosta shipments cannot be created for {0} orders: {0} fulfils its own orders.").format(platform),
			title=_("Bosta shipment"),
		)
	if doc.is_new():
		for fieldname in CLEARED_ON_NEW if doc.get("amended_from") else SYSTEM_FIELDS:
			doc.set(fieldname, None)
		if doc.get("amended_from"):
			doc.custom_cod_amount = None
			doc.custom_cod_confirmed = 0
		doc.custom_integration_status = NOT_CREATED
		if doc.get("amended_from") and doc.get("lr_no"):
			# Amend copies the old note's tracking number, which belongs to that note's shipment.
			doc.lr_no = None
			doc.lr_date = None
		if ours and doc.get("is_return"):
			# make_sales_return copies the original delivery's tracking number: the pickup gets its own from
			# Bosta, and the original's must not look like it to the webhook match.
			doc.lr_no = None
			doc.lr_date = None
			_check_returned_cod(doc.get("custom_returned_cod"))
		elif ours:
			_apply_cod_rule(doc, doc)
			_check_cod(doc.custom_cod_amount)
		return
	before = doc.get_doc_before_save() or doc
	# The app writes these with db_set (no validate). A posted copy loaded before such a write (a form
	# open while the label or a status arrived) carries old values: put the stored ones back instead of
	# refusing, so a person's save never changes them and never loses the app's write.
	for fieldname in SYSTEM_FIELDS:
		doc.set(fieldname, before.get(fieldname))
	if cstr(before.get("custom_integration_status")) in EDITABLE_STATUSES:
		if ours and doc.get("is_return"):
			if cstr(before.get("custom_return_source")) != cstr(doc.get("custom_return_source")):
				# The return just became (or changed to) a pickup: the tracking number it copied from the
				# delivery belongs to that delivery, not to this pickup (same as on insert).
				doc.lr_no = None
				doc.lr_date = None
			_check_returned_cod(doc.get("custom_returned_cod"))
		elif ours:
			_apply_cod_rule(doc, before)
			_check_cod(doc.get("custom_cod_amount"))
		return
	if doc.get("is_return") and (
		flt(before.get("custom_returned_cod"), 2) != flt(doc.get("custom_returned_cod"), 2)
		or cstr(before.get("custom_return_source")) != cstr(doc.get("custom_return_source"))
	):
		frappe.throw(
			_("The returned COD and return source cannot be changed after the Bosta return pickup was started."),
			title=_("Bosta shipment"),
		)
	# The city is chosen by the Pick Bosta City dialog (read-only on the form), never typed: put the stored
	# value back like the app-written fields, so a sanitized or stale copy can never refuse the Submit.
	for fieldname in CITY_FIELDS:
		doc.set(fieldname, before.get(fieldname))
	changed = changed_locked_fields(before, doc)
	if changed:
		frappe.throw(
			_("The carrier, tracking number, COD amount and city cannot be changed after the Bosta shipment was started.")
			+ " "
			+ _("Changed: {0}").format(", ".join(changed)),
			title=_("Bosta shipment"),
		)
	if any(cstr(before.get(f)) != cstr(doc.get(f)) for f in RECEIVER_FIELDS):
		frappe.throw(
			_(
				"The receiver phone and shipping address cannot be changed after the Bosta shipment was "
				"started. Use Edit Bosta Shipment."
			),
			title=_("Bosta shipment"),
		)
	if _rows(before) != _rows(doc) and not _only_reduced(before, doc):
		frappe.throw(
			_(
				"Items, quantities and rows cannot be changed after the Bosta shipment was started. "
				"They must match the shipment sent to Bosta."
			),
			title=_("Bosta shipment"),
		)



def guard_new_note(doc):
	"""``validate`` of a NEW Bosta note (``REQUEST_ON_INSERT``): refuse when the order's state forbids a shipment now
	(Waiting for Stock, a change in flight or held, cancelled or refunded at the marketplace, a warehouse that is not
	the account's). The same rules as the one-step "Create Shipment" (``order_shipment.order_state_problem``)."""
	from marketplace_integrations.services import order_shipment

	problem = order_shipment.order_state_problem(sales_order_of(doc))
	if problem:
		frappe.throw(problem, title=_("Bosta shipment"))


def refuse_sales_order_cancel(order):
	"""Sales Order ``before_cancel``: refuse while a draft Delivery Note has a live Bosta shipment."""
	supplier = bosta_supplier()
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
			"custom_integration_status": ["in", list(LIVE_STATUSES)],
		},
		pluck="name",
	)
	if live:
		frappe.throw(
			_("Sales Order {0} has a draft Delivery Note with a live Bosta shipment: {1}.").format(
				order.name, ", ".join(live)
			),
			title=_("Bosta shipment"),
		)


# ---------------------------------------------------------------------------
# Request (user action) -> job
# ---------------------------------------------------------------------------


def _address_for(doc, order):
	"""Receiver address data: the note's shipping Address wins, the Sales Order's and the Marketplace
	Order's are fallbacks. Read without per-user permissions (the caller can read this note)."""
	address_name = doc.get("shipping_address_name") or (
		frappe.db.get_value("Sales Order", order, "shipping_address_name") if order else None
	)
	province = _shopify_province(order)
	if address_name:
		address = frappe.db.get_value(
			"Address", address_name, ["address_line1", "address_line2", "city", "state"], as_dict=True
		)
		if address:
			lines = [cstr(line).strip() for line in (address.address_line1, address.address_line2)]
			city = cstr(address.city).strip()
			state = cstr(address.state).strip()
			return {
				"first_line": ", ".join(line for line in lines if line),
				"city": city,
				"hints": [part for part in (city, state) if part],
				**province,
			}
	marketplace_order = frappe.db.get_value("Sales Order", order, "custom_marketplace_order") if order else None
	formatted = (
		frappe.db.get_value("Marketplace Order", marketplace_order, "customer_formatted_address")
		if marketplace_order
		else ""
	)
	return {"first_line": cstr(formatted).strip(), "city": "", "hints": [], **province}


def _shopify_province(order):
	"""``country_code`` / ``province_code`` from a Shopify Marketplace Order's ``shipping_address`` JSON."""
	marketplace_order = frappe.db.get_value("Sales Order", order, "custom_marketplace_order") if order else None
	if not marketplace_order:
		return {}
	row = frappe.db.get_value(
		"Marketplace Order", marketplace_order, ["platform", "shipping_address"], as_dict=True
	)
	if not row or row.get("platform") != "Shopify" or not row.get("shipping_address"):
		return {}
	try:
		data = frappe.parse_json(row.get("shipping_address"))
	except Exception:
		return {}
	if not isinstance(data, dict):
		return {}
	return {
		"country_code": cstr(data.get("countryCodeV2")).strip(),
		"province_code": cstr(data.get("provinceCode")).strip(),
	}


def _receiver_phone(doc, order):
	"""The note's contact phone, else the Sales Order's, else the Shopify buyer's phone (which platforms
	may use it is decided in ``sales_order_sync.buyer_phone_for_sales_order``)."""
	from marketplace_integrations.services import sales_order_sync

	phone = cstr(doc.get("contact_mobile")).strip()
	if phone or not order:
		return phone
	values = frappe.db.get_value(
		"Sales Order", order, ["contact_mobile", "contact_phone", "custom_marketplace_order"], as_dict=True
	)
	phone = cstr(values.contact_mobile or values.contact_phone).strip()
	if phone or not values.custom_marketplace_order:
		return phone
	buyer = frappe.db.get_value(
		"Marketplace Order", values.custom_marketplace_order, ["platform", "customer_phone_number"], as_dict=True
	)
	return sales_order_sync.buyer_phone_for_sales_order(buyer) if buyer else ""


def get_city_options(delivery_note):
	"""The Bosta cities and the city that matches the note's address (the caller checked read access).
	A failed Bosta lookup only loses the suggestion."""
	doc = frappe.get_doc("Delivery Note", delivery_note)
	suggested = {}
	try:
		suggested = city_match.resolve_city(_address_for(doc, sales_order_of(doc))) or {}
		if suggested.get("city_id"):
			suggested["city_name"] = locations.city_name_for(suggested["city_id"], suggested.get("city_name"))
	except locations.BostaLookupUnavailable:
		suggested = {}
	except Exception:
		frappe.clear_last_message()
		frappe.log_error(frappe.get_traceback(with_context=False), "Bosta city match failed")
		suggested = {}
	return {"cities": locations.get_cities(), "suggested": suggested}


def autofill_city(doc):
	"""Fill the Bosta city of an editable draft note from the address linked to the order, when it is empty
	(``city_match.resolve_city``). A city that is already set is never touched or asked again. Written with
	``update_modified=False`` so a form opened before it still submits. Returns whether the note now has a
	city; a failed or unmatched lookup only leaves it empty (the caller then asks the person)."""
	if cstr(doc.get("custom_bosta_city")).strip():
		return True
	if doc.docstatus != 0 or status_of(doc) not in EDITABLE_STATUSES:
		return False
	try:
		suggested = city_match.resolve_city(_address_for(doc, sales_order_of(doc))) or {}
		city = locations.get_city(suggested["city_id"]) if suggested.get("city_id") else None
	except Exception:
		frappe.clear_last_message()
		return False
	if not city:
		return False
	values = {"custom_bosta_city": city["code"], "custom_bosta_city_name": city["name"]}
	frappe.db.set_value("Delivery Note", doc.name, values, update_modified=False)
	doc.update(values)
	return True


def validate_ready(doc, action="create"):
	"""Everything that can be refused before Bosta is contacted (the contract's ``validate_ready``). A
	configuration problem is thrown here, so a note is never left Creating for it. ``action`` ``"lookup"`` and
	``"cancel"`` only check the configuration (and, for a cancel, the tracking number). For a create, an empty
	Bosta city is first filled from the order's address (``autofill_city``); if it is still empty nothing is
	sent and ``{"needs_city": True}`` is returned (the form opens the city picker)."""
	if action != "create":
		BostaClient().ensure_ready()
		if action == "cancel" and not cstr(doc.get("lr_no")).strip():
			frappe.throw(_("This Delivery Note has no Bosta tracking number."))
		return None
	if not autofill_city(doc):
		return {"needs_city": True}

	if doc.docstatus != 0:
		frappe.throw(_("Only a draft Delivery Note can be sent to Bosta."))
	if not applies_to(doc):
		frappe.throw(_("This Delivery Note is not a Bosta shipment. Choose the carrier Bosta."))
	order = sales_order_of(doc)
	platform = excluded_platform(order)
	if platform:
		frappe.throw(_("Bosta shipments cannot be created for {0} orders: {0} fulfils its own orders.").format(platform))
	if not doc.get("items"):
		frappe.throw(_("The Delivery Note has no items."))
	if doc.get("is_return"):
		_check_returned_cod(doc.get("custom_returned_cod"))
	else:
		_check_cod(doc.get("custom_cod_amount"))
		if not cint(doc.get("custom_cod_confirmed")):
			frappe.throw(
				_("Confirm the COD amount before sending the shipment: tick COD confirmed, or change the COD. 0 is allowed.")
			)
	if not cstr(doc.get("customer_name")).strip():
		frappe.throw(_("The receiver name is missing."))
	if not _receiver_phone(doc, order):
		frappe.throw(_("The receiver phone is missing. Add a contact phone to the Delivery Note."))
	if not _address_for(doc, order)["first_line"]:
		frappe.throw(_("The receiver address is missing. Add a shipping address to the Delivery Note."))
	company_currency = frappe.get_cached_value("Company", doc.company, "default_currency")
	if doc.get("currency") and doc.currency != company_currency:
		frappe.throw(
			_("Delivery Note {0} is in {1}; Bosta COD is only supported in the company currency {2}.").format(
				doc.name, doc.currency, company_currency
			)
		)
	BostaClient().ensure_ready()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def package_details(doc):
	"""What is in the parcel, for the Bosta AWB and the courier app: the piece count and the full
	``2x name, 1x name`` description of the note's items (no length cap; Bosta documents none). ``None`` when
	no row has a quantity above 0."""
	rows = [row for row in (doc.get("items") or []) if flt(row.qty) > 0]
	if not rows:
		return None
	parts = []
	for row in rows:
		qty_text = str(flt(row.qty, 3)).rstrip("0").rstrip(".")
		parts.append(f"{qty_text}x {cstr(row.item_name or row.item_code).strip()}")
	description = ", ".join(parts)
	return {"itemsCount": max(1, cint(round(sum(flt(row.qty) for row in rows)))), "description": description}


def _marketplace_order_row(sales_order):
	"""``platform`` and ``order_code`` of the Sales Order's Marketplace Order, else ``None``."""
	marketplace_order = (
		frappe.db.get_value("Sales Order", sales_order, "custom_marketplace_order") if sales_order else None
	)
	if not marketplace_order:
		return None
	return frappe.db.get_value("Marketplace Order", marketplace_order, ["platform", "order_code"], as_dict=True)


def shopify_notes(sales_order):
	"""The Bosta ``notes`` text: only the customer-facing Shopify order name, e.g. "#1234" (no platform word),
	or ``None`` when the Sales Order is not a Shopify order or the name is unknown. docs.yaml defines ``notes``
	but does not say it prints on the label."""
	row = _marketplace_order_row(sales_order)
	if not row or row.get("platform") != "Shopify" or not cstr(row.get("order_code")).strip():
		return None
	return cstr(row.order_code).strip()


def build_payload(doc):
	"""The create body: Bosta's required fields plus, for a delivery, the parcel contents
	(``specs.packageDetails``) and, for a Shopify delivery, ``notes``. The drop-off address is the city code
	and the first line; Bosta resolves the district."""
	order = sales_order_of(doc)
	settings = BostaClient().get_settings()
	reference = cstr(doc.custom_carrier_reference) or doc.name
	# A Customer Return Pickup (type 25) sends the customer's address as dropOffAddress (same builder as a
	# delivery) and a negative COD: the entered Returned COD, which is never negative. Bosta returns the
	# goods to ``businessLocationId`` (the pickup location of the settings, also used by a delivery).
	is_pickup = bool(doc.get("is_return"))
	returned_cod = flt(doc.get("custom_returned_cod"), 2)
	payload = {
		"type": BOSTA_RETURN_PICKUP_TYPE if is_pickup else BOSTA_DELIVERY_TYPE,
		"cod": (-returned_cod if returned_cod else 0) if is_pickup else flt(doc.custom_cod_amount, 2),
		"receiver": {"firstName": cstr(doc.customer_name).strip(), "phone": _receiver_phone(doc, order)},
		"dropOffAddress": {"city": cstr(doc.custom_bosta_city).strip(), "firstLine": _address_for(doc, order)["first_line"]},
		"businessLocationId": settings.business_location_id,
		# businessReference is the field Bosta documents for searching; it equals the unique reference so
		# a delivery whose response was lost can be found again.
		"businessReference": reference,
		"uniqueBusinessReference": reference,
	}
	details = None if is_pickup else package_details(doc)
	if details:
		# What is in the parcel (docs.yaml: ``specs.packageDetails.itemsCount`` / ``description``), shown on
		# the AWB and in the courier's app. A return pickup sends none (no ``returnSpecs`` either).
		payload["specs"] = {"packageDetails": details}
	notes = None if is_pickup else shopify_notes(order)
	if notes:
		payload["notes"] = notes
	return payload


def _delivery_data(body):
	return body.get("data") if isinstance(body.get("data"), dict) else body


def _delivery_id(data):
	return cstr(data.get("_id") or data.get("id")).strip()


def find_remote_delivery(reference):
	"""The Bosta delivery whose businessReference is ``reference``, or ``None`` when Bosta has none.

	Only recognised response shapes count as an answer; anything else raises ``_Unknown``, so an
	unexpected reply can never be mistaken for "not found" (which would allow a duplicate)."""
	client = BostaClient()
	try:
		response = client.search_by_business_reference(reference)
	except Exception as exc:
		raise _Unknown(_("The Bosta lookup failed ({0}).").format(cstr(exc)[:200])) from exc
	if not is_success_response(response):
		raise _Unknown(client.error_message(response))
	data = response_body(response).get("data")
	if not data:
		return None
	# Live shape (seen 2026-10-19): data = {count, deliveries, limit, page}. More matches than the page
	# holds cannot be checked, so it is ambiguous, never "none".
	if isinstance(data, dict) and isinstance(data.get("deliveries"), list):
		if cint(data.get("count")) > len(data["deliveries"]):
			raise _Unknown(_("Bosta returned an ambiguous search result. Check the delivery in Bosta."))
		data = data["deliveries"]
	if isinstance(data, dict) and data.get("_id"):
		data = [data]
	if not isinstance(data, list):
		raise _Unknown(_("Bosta returned a search result in an unrecognised format. Check the delivery in Bosta."))
	matches = [
		item
		for item in data
		if isinstance(item, dict)
		and cstr(item.get("businessReference")) == reference
		and _delivery_id(item)
		and cstr(item.get("trackingNumber")).strip()
	]
	if len(matches) > 1 or (data and not matches):
		raise _Unknown(_("Bosta returned an ambiguous search result. Check the delivery in Bosta."))
	return matches[0] if matches else None


def _create_remote(payload):
	"""POST the delivery. Returns Bosta's delivery data or raises ``_Failed`` (a definitive refusal) or
	``_Unknown`` (the outcome is not known)."""
	client = BostaClient()
	try:
		response = client.create_delivery(payload)
	except Exception as exc:
		raise _Unknown(
			_("The Bosta request failed ({0}). It may or may not have reached Bosta.").format(cstr(exc)[:200])
		) from exc
	status = response.status_code
	if not is_success_response(response):
		message = client.error_message(response)
		if 400 <= status < 500 and status not in _RETRYABLE_HTTP:
			raise _Failed(message)
		raise _Unknown(message)
	data = _delivery_data(response_body(response))
	if not _delivery_id(data) or not cstr(data.get("trackingNumber")).strip():
		raise _Unknown(_("Bosta accepted the request without a delivery ID and tracking number."))
	return data


def find_remote(doc):
	"""The contract's ``find_remote``: the delivery whose reference is the note's, else ``None``."""
	return find_remote_delivery(cstr(doc.get("custom_carrier_reference")) or doc.name)


def create_remote(doc):
	"""The contract's ``create_remote``: build the payload, release the transaction, POST."""
	payload = build_payload(doc)
	frappe.db.commit()
	return _create_remote(payload)


def created_values(doc, data):
	"""The Delivery Note fields written once Created (also by ``carrier_shipments.check``)."""
	payload = build_payload(doc)
	return {
		"custom_carrier_delivery_id": _delivery_id(data),
		"lr_no": cstr(data.get("trackingNumber")).strip(),
		"lr_date": getdate(),
		"custom_last_integration_error": None,
		"custom_last_sync_at": now_datetime(),
		"custom_carrier_request_snapshot": json.dumps(payload),
	}


def _sanitized(payload):
	"""A payload without the receiver's name, phone and address, for the log."""
	if not isinstance(payload, dict):
		return {}
	clean = {k: v for k, v in payload.items() if k not in ("receiver", "dropOffAddress")}
	address = payload.get("dropOffAddress")
	if isinstance(address, dict) and address.get("city"):
		clean["dropOffAddress"] = {"city": address["city"]}
	return clean


def log_data(values):
	"""The create payload for the Integration Log, without the receiver's name, phone and address."""
	snapshot = values.get("custom_carrier_request_snapshot")
	return json.dumps(_sanitized(json.loads(snapshot))) if snapshot else None


def after_created(delivery_note, data):
	"""Once a note is Created: the Sales Order summary, the label job (#500: the note stays a draft and nothing
	submits it right after Created, so the old race of the label job with the create job's submit is gone) and the
	webhook events that arrived before the note could take them."""
	from marketplace_integrations.integrations.bosta import statuses

	if frappe.db.get_value("Delivery Note", delivery_note, "is_return"):
		# A return pickup: not part of the Sales Order's shipping summary, and no label job.
		return
	delivery_id = _delivery_id(data)
	_update_summary(delivery_note)
	if not frappe.db.get_value("Delivery Note", delivery_note, "custom_shipping_label"):
		_queue_label(delivery_note)
	frappe.db.commit()
	try:
		statuses.replay_pending_events(delivery_id)
	except Exception:
		# The create is already logged Success: a replay problem must not turn it into Unknown. The
		# pending events stay Queued and the nightly reconciliation retries them.
		frappe.db.rollback()
		frappe.log_error(title="Bosta: pending events could not be replayed", message=frappe.get_traceback())


def _queue_label(delivery_note):
	"""The label job, queued once the shipment is Created (#500: nothing is submitted after Created any more, so
	the old race of the label job with the create job's submit is gone). Runs after the caller's commit."""
	frappe.enqueue(
		"marketplace_integrations.integrations.bosta.delivery_note_shipment.run_fetch_label",
		queue="short",
		timeout=120,
		job_id=_LABEL_JOB.format(delivery_note),
		deduplicate=True,
		enqueue_after_commit=True,
		delivery_note=delivery_note,
	)


def after_pickup_submit(delivery_note):
	"""A draft the pickup event just submitted: when Bosta already reported the goods returned (46/60), queue the
	draft return as a note that was submitted before would (idempotent)."""
	from marketplace_integrations.integrations.bosta import returns, statuses

	values = frappe.db.get_value("Delivery Note", delivery_note, ["custom_shipping_status", "is_return"], as_dict=True)
	if values and not values.is_return and values.custom_shipping_status in [
		statuses.BOSTA_STATE_NAMES[code] for code in statuses.RETURNED_STATE_CODES
	]:
		returns.queue_returned_draft(delivery_note)


def refresh(delivery_note):
	"""The contract's ``refresh``: read Bosta's state and apply it; returns the Bosta state name."""
	from marketplace_integrations.integrations.bosta import statuses

	state = statuses.refresh_note(delivery_note)
	if state is None:
		# Without ``held`` the only silent ``None`` is a note with no tracking number.
		frappe.throw(_("This Delivery Note has no Bosta tracking number to refresh."))
	return state


def reconcile():
	"""The contract's ``reconcile``: queue the nightly reconciliation job."""
	from marketplace_integrations.integrations.bosta import statuses

	return statuses.reconcile_open_notes()


def problem_states():
	"""The contract's ``problem_states``: the neutral Needs Attention problem types and the Bosta shipping
	statuses that raise them."""
	from marketplace_integrations.integrations.bosta import statuses
	from marketplace_integrations.services import marketplace_attention as attention

	return {
		attention.CARRIER_EXCEPTION: [statuses.BOSTA_STATE_NAMES[c] for c in statuses.PROBLEM_STATE_CODES],
		attention.CARRIER_RETURN: [statuses.BOSTA_STATE_NAMES[c] for c in statuses.RETURNED_STATE_CODES],
		# #500: the statuses that submit the draft; a draft still in one of them was not submitted.
		attention.PICKED_UP_NOT_SUBMITTED: [statuses.BOSTA_STATE_NAMES[c] for c in statuses.PICKUP_CODES],
	}


def terminal_states(is_return=False):
	"""The contract's ``terminal_states``: the shipping statuses that never change again, so a note in one
	is not a stale status. A Customer Return Pickup (``is_return``) also ends at the returned states."""
	from marketplace_integrations.integrations.bosta import statuses

	return statuses.terminal_state_names(is_return)


# ---------------------------------------------------------------------------
# Cancel at Bosta (#410)
# ---------------------------------------------------------------------------


def _terminate_outcome(client, response):
	"""``(outcome, message)`` of a terminate answer: ``cancelled`` (a documented 200 with ``success``),
	``unclear`` (a 404, errorCode 1066 or an "already terminated" text: the answer does not say whether the
	delivery is cancelled, so ``cancel_at_bosta`` asks Bosta with one read), ``refused`` (any other 4xx that is
	not 408 or 429: Bosta definitively says no) or ``unknown`` (everything else). A generic error is never read
	as "not found"."""
	status = response.status_code
	body = response_body(response)
	if 200 <= status < 300:
		if body.get("success") is True:
			return "cancelled", ""
		return "unknown", client.error_message(response)
	message = client.error_message(response)
	if status == 404 or cstr(body.get("errorCode")) == "1066" or "already terminated" in message.lower():
		return "unclear", message
	if 400 <= status < 500 and status not in _RETRYABLE_HTTP:
		return "refused", message
	return "unknown", message


#: Bosta answers a read of a terminated delivery with HTTP 400 ``{"message": "Delivery not found.",
#: "errorCode": 1066}`` (seen on a real terminated delivery, 2026-11-01).
DELIVERY_NOT_FOUND_ERROR_CODE = "1066"
#: The Bosta state a terminated delivery reads back as (``statuses.BOSTA_STATE_NAMES[48]``).
TERMINATED_STATE_CODE = 48


def is_terminated_answer(response):
	"""Is this GET answer Bosta's "Delivery not found" (HTTP 400, errorCode 1066) for a terminated delivery."""
	return (
		response.status_code == 400
		and cstr(response_body(response).get("errorCode")) == DELIVERY_NOT_FOUND_ERROR_CODE
	)


def _read_state(client, tracking):
	"""One GET of the delivery: ``("found", {"code", "name"})`` for a recognised state (a terminated delivery's
	"Delivery not found" answer reads as Terminated, 48), ``("missing", None)`` for a 404, else
	``("failed", None)`` (any error, timeout or unrecognised answer)."""
	from marketplace_integrations.integrations.bosta import statuses

	try:
		response = client.get_delivery(tracking)
		if is_terminated_answer(response):
			return "found", {
				"code": TERMINATED_STATE_CODE,
				"name": statuses.BOSTA_STATE_NAMES[TERMINATED_STATE_CODE],
			}
		if response.status_code == 404:
			return "missing", None
		if not is_success_response(response):
			return "failed", None
		state = statuses._polled_state(response_body(response))
	except Exception:
		return "failed", None
	if state and state["code"] in statuses.BOSTA_STATE_NAMES:
		return "found", {"code": state["code"], "name": statuses.BOSTA_STATE_NAMES[state["code"]]}
	return "failed", None


def cancel_remote(doc):
	"""The contract's ``cancel_remote``: terminate the note's Bosta delivery (the neutral
	``carrier_shipments.cancel`` holds the state machine, the lock and the log).

	One terminate call with the client's timeout, at most one GET. Returns ``_dict(outcome, message, values,
	http_status)``: ``cancelled`` (a documented 200, or one GET showing 48/49 after a refusal or an unclear
	answer) or ``refused`` (a refusal, or an unclear answer whose GET shows a live state). Raises ``Unknown``
	(timeout, 5xx, 429, or a 404 / 1066 / "already terminated" answer whose GET finds nothing or fails). The
	message is Bosta's raw text (the caller escapes it where it is shown)."""
	from marketplace_integrations.integrations.bosta import statuses

	tracking = cstr(doc.get("lr_no")).strip()
	client = BostaClient()
	try:
		response = client.terminate_delivery(tracking)
	except Exception as exc:
		outcome, message, http_status = (
			"unknown",
			_("The Bosta request failed ({0}). It may or may not have reached Bosta.").format(cstr(exc)[:200]),
			None,
		)
	else:
		outcome, message = _terminate_outcome(client, response)
		http_status = response.status_code
	state = None
	if outcome in ("refused", "unclear"):
		# One read settles it. Cancelled (48/49) is Cancelled. For a plain refusal anything else keeps the
		# shipment (back to Created). For an unclear answer a live state is also back to Created with the raw
		# refusal, while "not found" and a failed read stay Cancellation Unknown.
		kind, found = _read_state(client, tracking)
		if kind == "found" and found["code"] in statuses.CANCELLED_STATE_CODES:
			state, outcome = found, "cancelled"
		elif outcome == "unclear":
			outcome = "refused" if kind == "found" else "unknown"
	if outcome == "unknown":
		raise Unknown(message, http_status)
	if state:
		values = {"custom_shipping_status": state["name"]}
	elif outcome == "cancelled":
		# Bosta confirmed the terminate with a 200: the shipping status is the terminated label too
		values = {"custom_shipping_status": statuses.BOSTA_STATE_NAMES[TERMINATED_STATE_CODE]}
	else:
		values = {}
	return frappe._dict(outcome=outcome, message=message, values=values, http_status=http_status)


def after_cancelled(delivery_note):
	"""Once the note is Cancelled: the Sales Order summary."""
	from marketplace_integrations.integrations.bosta import statuses

	statuses.update_sales_order_summary_of(delivery_note)



def _update_summary(delivery_note):
	from marketplace_integrations.integrations.bosta import statuses

	statuses.update_sales_order_summary_of(delivery_note)
	frappe.db.commit()


# ---------------------------------------------------------------------------
# Label
# ---------------------------------------------------------------------------


def run_fetch_label(delivery_note, replace=False, cod=None):
	"""Background job after Created: fetch the label as the system. A failure is logged and leaves the
	Integration Status alone; Fetch Label retries. ``replace``: the COD changed (#501), request a new label.
	``cod``: the COD that update sent; when the note's COD has moved on since, a newer update queued its own refresh
	and this one does nothing (the label would show a COD Bosta no longer has)."""
	frappe.set_user("Administrator")
	if replace and cod is not None and _snapshot_cod(delivery_note) != flt(cod, 2):
		return None
	return fetch_label(delivery_note, replace=replace, cod=cod)


def _snapshot_cod(delivery_note):
	"""The COD in the note's request snapshot (what Bosta was last sent), else ``None``."""
	raw = frappe.db.get_value("Delivery Note", delivery_note, "custom_carrier_request_snapshot")
	try:
		data = frappe.parse_json(raw) if raw else {}
	except Exception:
		return None
	if not isinstance(data, dict) or data.get("cod") is None:
		return None
	return flt(data["cod"], 2)


def queue_label_refresh(delivery_note):
	"""After a COD update (#501): queue a new label when one is stored (the stored one shows the old COD). Runs
	after the caller's commit. One job per note AND COD: a second update while a refresh runs queues its own, and the
	older one skips itself (``run_fetch_label``), so the last COD always gets its label."""
	if not frappe.db.get_value("Delivery Note", delivery_note, "custom_shipping_label"):
		return
	cod = _snapshot_cod(delivery_note)
	frappe.enqueue(
		"marketplace_integrations.integrations.bosta.delivery_note_shipment.run_fetch_label",
		queue="short",
		timeout=120,
		job_id=f"{_LABEL_JOB.format(delivery_note)}|refresh|{cod}",
		deduplicate=True,
		enqueue_after_commit=True,
		delivery_note=delivery_note,
		replace=True,
		cod=cod,
	)


def fetch_label(delivery_note, raise_on_error=False, replace=False, cod=None):
	"""Fetch the A4 Arabic label and attach it privately to the note (the one implementation behind the job
	and the Fetch Label button). Commits before the log row and before the HTTP call, so no transaction is
	open across it. A failure is logged Failed, never changes the Integration Status, and is raised again
	only when ``raise_on_error``. ``replace`` (#501): fetch even when a label is stored; when that fails the stored
	label is dropped, because it shows a COD Bosta no longer has (Fetch Bosta Label gets it again)."""
	from marketplace_integrations.integrations.bosta import labels

	frappe.db.commit()
	log = carrier_log.open_log(SERVICE_NAME, delivery_note, "Label Fetch")
	frappe.db.commit()
	try:
		result = labels.create_a4_arabic_label(delivery_note, replace=replace, cod=cod)
	except Exception as exc:
		frappe.db.rollback()
		# A job of an older COD must not drop the label a newer COD's job stored.
		if replace and (cod is None or _snapshot_cod(delivery_note) == flt(cod, 2)):
			frappe.db.set_value("Delivery Note", delivery_note, "custom_shipping_label", None, update_modified=False)
		# labels.py escapes Bosta's text for the screen; the log keeps it raw (escaped where it is rendered).
		carrier_log.close_log(log, "Failed", error=html.unescape(cstr(exc))[:500] or _("The label could not be fetched."))
		frappe.db.commit()
		if raise_on_error:
			raise
		return None
	carrier_log.close_log(log, "Success")
	frappe.db.commit()
	return result
