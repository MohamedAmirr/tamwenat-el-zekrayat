# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bosta shipment created when a Delivery Note is submitted (epic #413, issue #408).

Delivery Note-first (``docs/marketplace-integrations/delivery-note-first.md``): the Delivery Note
with transporter "Bosta" is the shipment. Submit starts a background job (``request_create`` then
``run_create``) and the note submits only once the shipment is Created (``require_created``).

Native fields used: ``transporter`` (Supplier "Bosta", created by a patch), ``lr_no`` (the Bosta
tracking number) and ``lr_date``. The generic shipping fields (``custom_integration_status``,
``custom_shipping_status``, ``custom_carrier_delivery_id``, ``custom_carrier_reference``,
``custom_cod_amount``, ``custom_shipping_label``, ``custom_last_sync_at``,
``custom_last_integration_error``, ``custom_carrier_request_snapshot``) are carrier neutral; the
Bosta-only ones are ``custom_bosta_city`` and ``custom_bosta_city_name``.

The Integration Log is the native Integration Request (service "Bosta") with custom fields. It never
holds the API key, the webhook secret, or the receiver's name, phone or address.

COD: while the note is editable (Not Created / Creation Failed) the default COD is recomputed on every
save until the person confirms it (``custom_cod_confirmed``: ticked, or a changed COD); Submit refuses a
note whose COD was never confirmed. A typed 0 is a deliberate choice once confirmed.

Rules enforced here:

- Bosta has no idempotency key. The first attempt creates; **every later attempt looks the delivery up
  by its business reference first** (a note whose ``custom_carrier_reference`` is set was tried before,
  which survives Integration Request cleanup) and creates only when Bosta has none. There is no automatic retry
  of a create. A timeout, a connection error, a 5xx/429, a malformed 2xx or an ambiguous lookup is
  "Creation Unknown", never a failure.
- No lock is held across an HTTP call. Write-backs commit, take the row lock as the next statement
  and write only if the note is still Creating (``_finish``).
- The worker writes with ``db_set`` (no validate, no hooks): a system write-back.
"""

import html
import json
import uuid

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, cstr, flt, get_datetime, getdate, now_datetime
from frappe.utils.background_jobs import is_job_enqueued

from marketplace_integrations.integrations.bosta import city_match, locations
from marketplace_integrations.integrations.bosta.client import (
	SERVICE_NAME,
	BostaClient,
	is_success_response,
	response_body,
)
from marketplace_integrations.integrations.bosta.constants import BOSTA_EXCLUDED_PLATFORMS

#: ``supplier_name`` of the native Supplier used as the Delivery Note ``transporter``. The Supplier's
#: document name depends on Buying Settings, so it is looked up by this name, never assumed.
SUPPLIER_NAME = "Bosta"
#: The carrier name the Delivery Note shows its Shipping section for (``carrier_shipments.boot_session``).
SHIPPING_CARRIER = SUPPLIER_NAME

BOSTA_DELIVERY_TYPE = 10
BOSTA_COD_LIMIT = 30000
#: A Creating note with no queued job older than this counts as stuck (Check with Bosta can resolve it).
STALE_CREATING_MINUTES = 5
#: A Cancellation Pending note older than this lost its request (the call is synchronous, 10 s timeout):
#: it may be cancelled again, which is safe because a refused repeat is resolved by one GET.
STALE_CANCEL_MINUTES = 2

NOT_CREATED = "Not Created"
CREATING = "Creating"
UNKNOWN = "Creation Unknown"
CREATED = "Created"
FAILED = "Creation Failed"
CANCELLATION_PENDING = "Cancellation Pending"
CANCELLATION_UNKNOWN = "Cancellation Unknown"
CANCELLED = "Cancelled"
#: Statuses where the shipment is started or live: transporter, items, COD, city and AWB are locked and
#: the note cannot be cancelled or deleted.
LIVE_STATUSES = (CREATING, UNKNOWN, CREATED, CANCELLATION_PENDING, CANCELLATION_UNKNOWN)
#: Statuses where the person may still change the note and (re)start the create.
EDITABLE_STATUSES = ("", NOT_CREATED, FAILED)

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
)
#: Cleared on a new or amended note: Amend copies even ``no_copy`` fields (conventions).
CLEARED_ON_NEW = (*SYSTEM_FIELDS, "custom_bosta_city", "custom_bosta_city_name")
#: Fields a person edits that must stay put once the shipment is started.
LOCKED_FIELDS = ("transporter", "lr_no", "custom_cod_amount", "custom_bosta_city", "custom_bosta_city_name")

#: What Bosta received as the receiver phone and drop-off address (``build_payload``). After the start only
#: the Edit Bosta Shipment dialog changes them (``editing.edit_shipment``, which writes with ``db_set``).
RECEIVER_FIELDS = ("contact_mobile", "shipping_address_name", "shipping_address")

_CREATE_JOB = "bosta-create|{}"
_LABEL_JOB = "bosta-label|{}"
_RETRYABLE_HTTP = {408, 429}


class _Unknown(Exception):
	"""The outcome of a call is not known: the shipment may or may not exist at Bosta."""


class _Failed(Exception):
	"""Bosta definitively refused the request (a readable message)."""


# ---------------------------------------------------------------------------
# Which notes belong to Bosta
# ---------------------------------------------------------------------------


def bosta_supplier():
	"""The Supplier docname of "Bosta" (looked up by ``supplier_name``), or ``None``."""
	return frappe.db.get_value("Supplier", {"supplier_name": SUPPLIER_NAME}, "name")


def applies_to(doc):
	"""A non-return note whose transporter is the Bosta Supplier."""
	if doc.get("is_return") or not doc.get("transporter"):
		return False
	return frappe.db.get_value("Supplier", doc.transporter, "supplier_name") == SUPPLIER_NAME


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


def default_cod(doc):
	"""The note's share of what the customer still owes: its total minus the same share of the Sales
	Order's advance (proportional when the note ships part of the order), never below 0."""
	total = flt(doc.get("rounded_total")) or flt(doc.get("grand_total"))
	order = sales_order_of(doc)
	advance = 0
	values = (
		frappe.db.get_value("Sales Order", order, ["advance_paid", "rounded_total", "grand_total"], as_dict=True)
		if order
		else None
	)
	if values:
		order_total = flt(values.rounded_total) or flt(values.grand_total)
		if order_total:
			advance = flt(values.advance_paid) * min(total / order_total, 1)
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


def guard_changes(doc):
	"""``validate``: set up a new note; keep app-written fields and a started shipment untouched."""
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
		if ours:
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
		if ours:
			_apply_cod_rule(doc, before)
			_check_cod(doc.get("custom_cod_amount"))
		return
	if (
		any(cstr(before.get(f)) != cstr(doc.get(f)) for f in LOCKED_FIELDS)
		or flt(before.get("custom_cod_amount"), 2) != flt(doc.get("custom_cod_amount"), 2)
		or cint(before.get("custom_cod_confirmed")) != cint(doc.get("custom_cod_confirmed"))
	):
		frappe.throw(
			_("The carrier, tracking number, COD amount and city cannot be changed after the Bosta shipment was started."),
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
	if _rows(before) != _rows(doc):
		frappe.throw(
			_(
				"Items, quantities and rows cannot be changed after the Bosta shipment was started. "
				"They must match the shipment sent to Bosta."
			),
			title=_("Bosta shipment"),
		)


def require_created(doc):
	"""``before_submit``: a Bosta Delivery Note needs a Created shipment (judged by the stored status,
	not by the copy a client sent)."""
	if status_of(doc) == CREATED:
		return
	frappe.throw(
		_("The Bosta shipment of this Delivery Note is not Created yet, so it cannot be submitted."),
		title=_("Bosta shipment"),
	)


def guard_cancel_or_delete(doc):
	"""``before_cancel`` and ``on_trash``: refuse while a Bosta shipment is live. A note whose Integration
	Status is Cancelled (cancelled at Bosta, by this app or by Bosta) passes, without any call to Bosta."""
	if status_of(doc) in LIVE_STATUSES:
		frappe.throw(
			_(
				"This Delivery Note has a Bosta shipment ({0}). Cancel the shipment at Bosta first "
				"(Cancel Bosta Shipment, or Cancel on a submitted note)."
			).format(_(status_of(doc))),
			title=_("Bosta shipment"),
		)


def refuse_sales_order_cancel(order):
	"""Sales Order ``before_cancel``: refuse while a draft Delivery Note has a live Bosta shipment."""
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
		filters={"name": ["in", notes], "custom_integration_status": ["in", list(LIVE_STATUSES)]},
		pluck="name",
	)
	if live:
		frappe.throw(
			_("Sales Order {0} has a draft Delivery Note with a live Bosta shipment: {1}.").format(
				order.name, ", ".join(live)
			),
			title=_("Bosta shipment"),
		)


def queue(doc):
	"""Bosta never creates on insert (Submit starts it), so there is nothing to queue."""
	return None


def retry(delivery_note):
	"""The generic retry (``carrier_shipments.retry_shipment``, reached with write permission only) never
	starts a Bosta create: Submit does, with the submit permission and the stale-modified check
	(``api/bosta_shipments.request_shipment``)."""
	frappe.throw(
		_("A Bosta shipment is started by submitting the Delivery Note, not by a retry."), title=_("Bosta shipment")
	)


# ---------------------------------------------------------------------------
# Integration Log (native Integration Request + custom fields)
# ---------------------------------------------------------------------------


def _attempt_number(delivery_note):
	"""The next attempt number shown on the log (display only: Log Settings deletes old logs, so no
	decision may depend on it; ``request_create`` decides the lookup from ``custom_carrier_reference``)."""
	return (
		frappe.db.count(
			"Integration Request",
			{
				"integration_request_service": SERVICE_NAME,
				"reference_doctype": "Delivery Note",
				"reference_docname": delivery_note,
				"custom_operation_type": "Create",
			},
		)
		+ 1
	)


def _sanitized(payload):
	"""A payload without the receiver's name, phone and address, for the log."""
	if not isinstance(payload, dict):
		return {}
	clean = {k: v for k, v in payload.items() if k not in ("receiver", "dropOffAddress")}
	address = payload.get("dropOffAddress")
	if isinstance(address, dict) and address.get("city"):
		clean["dropOffAddress"] = {"city": address["city"]}
	return clean


def open_log(delivery_note, operation, attempt=None, correlation_id=None, reference=None):
	"""Insert the Integration Log row of one operation; returns its name. Inserted as the system after
	the caller's own permission check (Integration Request only grants System Manager)."""
	log = frappe.get_doc(
		{
			"doctype": "Integration Request",
			"integration_request_service": SERVICE_NAME,
			"reference_doctype": "Delivery Note",
			"reference_docname": delivery_note,
			"status": "Queued",
			"custom_operation_type": operation,
			"custom_attempt_no": attempt,
			"custom_correlation_id": correlation_id or uuid.uuid4().hex,
			"custom_external_reference": reference,
		}
	)
	log.insert(ignore_permissions=True)
	return log.name


def close_log(name, result, **values):
	"""Complete the log row. ``values`` may hold ``error``, ``data`` (sanitized), ``output``,
	``http_status``, ``error_code`` and ``delivery_id``."""
	if not name:
		return
	update = {
		"status": {"Success": "Completed"}.get(result, "Failed"),
		"custom_result": result,
		"custom_completed_at": now_datetime(),
	}
	for key, fieldname in (
		("error", "error"),
		("data", "data"),
		("output", "output"),
		("http_status", "custom_http_status"),
		("error_code", "custom_error_code"),
		("delivery_id", "custom_delivery_id"),
	):
		if values.get(key) not in (None, ""):
			value = values[key]
			update[fieldname] = value if isinstance(value, (int, float)) else cstr(value)[:2000]
	frappe.db.set_value("Integration Request", name, update, update_modified=False)


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


def _validate_ready(doc):
	"""Everything that can be refused before Bosta is contacted. A configuration problem is thrown here,
	so a note is never left Creating for it."""
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
	if not cstr(doc.get("custom_bosta_city")).strip():
		frappe.throw(_("Choose the Bosta city before sending the shipment."))
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


def request_create(delivery_note, modified=None):
	"""Start the Bosta create of a draft note (the API has checked the submit permission). Idempotent:
	a note that is already Creating or Created only returns its status. An empty Bosta city is first filled
	from the order's address (``autofill_city``); if it is still empty nothing is sent and the answer is
	``needs_city`` (the form opens the city picker)."""
	doc = frappe.get_doc("Delivery Note", delivery_note)
	if modified and cstr(doc.modified) != cstr(modified) and status_of(doc) in EDITABLE_STATUSES:
		frappe.throw(_("The Delivery Note was changed. Reload it and try again."), frappe.TimestampMismatchError)
	if status_of(doc) not in (CREATING, CREATED):
		if not autofill_city(doc):
			# Nothing to send yet: the form asks the person to pick the city, then submits again.
			return {"status": status_of(doc), "queued": False, "needs_city": True}
		_validate_ready(doc)
	# Commit, then lock as the very next statement (MariaDB snapshot isolation).
	frappe.db.commit()
	locked = frappe.db.get_value(
		"Delivery Note",
		delivery_note,
		["custom_integration_status", "docstatus", "modified", "custom_cod_confirmed"],
		as_dict=True,
		for_update=True,
	)
	if locked.docstatus != 0 or (cstr(locked.custom_integration_status) or NOT_CREATED) in (CREATING, CREATED):
		frappe.db.commit()
		return {"status": cstr(locked.custom_integration_status) or NOT_CREATED, "queued": False}
	# What was validated above may have changed before the lock (a save, a COD edit): judge the locked row.
	if (modified and cstr(locked.modified) != cstr(modified)) or not cint(locked.custom_cod_confirmed):
		frappe.db.commit()
		if not cint(locked.custom_cod_confirmed):
			frappe.throw(
				_("Confirm the COD amount before sending the shipment: tick COD confirmed, or change the COD. 0 is allowed.")
			)
		frappe.throw(_("The Delivery Note was changed. Reload it and try again."), frappe.TimestampMismatchError)
	# A reference on the note means a create was tried before: look the delivery up first (durable, unlike
	# the log rows). It is stored from the first attempt on and cleared on an amended note.
	previous = cstr(frappe.db.get_value("Delivery Note", delivery_note, "custom_carrier_reference"))
	reference = previous or doc.name
	frappe.db.set_value(
		"Delivery Note",
		delivery_note,
		{
			"custom_integration_status": CREATING,
			"custom_carrier_reference": reference,
			"custom_last_integration_error": None,
		},
	)
	log = open_log(delivery_note, "Create", attempt=_attempt_number(delivery_note), reference=reference)
	frappe.enqueue(
		"marketplace_integrations.integrations.bosta.delivery_note_shipment.run_create",
		queue="short",
		timeout=120,
		job_id=_CREATE_JOB.format(delivery_note),
		deduplicate=True,
		enqueue_after_commit=True,
		delivery_note=delivery_note,
		log=log,
		lookup_first=bool(previous),
	)
	frappe.db.commit()
	return {"status": CREATING, "queued": True}


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def build_payload(doc):
	"""The create body: only Bosta's required fields. The drop-off address is the city code and the first
	line; Bosta resolves the district."""
	order = sales_order_of(doc)
	settings = BostaClient().get_settings()
	reference = cstr(doc.custom_carrier_reference) or doc.name
	return {
		"type": BOSTA_DELIVERY_TYPE,
		"cod": flt(doc.custom_cod_amount, 2),
		"receiver": {"firstName": cstr(doc.customer_name).strip(), "phone": _receiver_phone(doc, order)},
		"dropOffAddress": {"city": cstr(doc.custom_bosta_city).strip(), "firstLine": _address_for(doc, order)["first_line"]},
		"businessLocationId": settings.business_location_id,
		# businessReference is the field Bosta documents for searching; it equals the unique reference so
		# a delivery whose response was lost can be found again.
		"businessReference": reference,
		"uniqueBusinessReference": reference,
	}


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


def run_create(delivery_note, log=None, lookup_first=True):
	"""Create (or find) the Bosta delivery of one Creating note and write the result back. ``lookup_first``
	is False only for the very first attempt of a note (``request_create`` decides it)."""
	frappe.set_user("Administrator")
	if status_of(delivery_note) != CREATING:
		frappe.db.commit()
		return
	try:
		doc = frappe.get_doc("Delivery Note", delivery_note)
		payload = build_payload(doc)
		frappe.db.commit()
		data = None
		if lookup_first:
			data = find_remote_delivery(payload["uniqueBusinessReference"])
		if data is None:
			data = _create_remote(payload)
		_finish_created(delivery_note, data, payload, log)
	except _Failed as failure:
		_finish(delivery_note, FAILED, {"custom_last_integration_error": cstr(failure)}, log, "Failed", error=cstr(failure))
	except _Unknown as unknown:
		_finish(delivery_note, UNKNOWN, {"custom_last_integration_error": cstr(unknown)}, log, "Unknown", error=cstr(unknown))
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Bosta shipment from Delivery Note failed", message=frappe.get_traceback())
		message = _("Unexpected error while sending the shipment to Bosta; see the Error Log. It may or may not have reached Bosta.")
		_finish(delivery_note, UNKNOWN, {"custom_last_integration_error": message}, log, "Unknown", error=message)


def _finish_created(delivery_note, data, payload, log):
	tracking = cstr(data.get("trackingNumber")).strip()
	values = {
		"custom_carrier_delivery_id": _delivery_id(data),
		"lr_no": tracking,
		"lr_date": getdate(),
		"custom_last_integration_error": None,
		"custom_last_sync_at": now_datetime(),
		"custom_carrier_request_snapshot": json.dumps(payload),
	}
	if _finish(
		delivery_note, CREATED, values, log, "Success", data=json.dumps(_sanitized(payload)), delivery_id=_delivery_id(data)
	):
		_after_created(delivery_note, _delivery_id(data))


def _after_created(delivery_note, delivery_id):
	"""Once a note is Created: the Sales Order summary, the label job, and the webhook events that arrived
	before the note could take them."""
	from marketplace_integrations.integrations.bosta import statuses

	_update_summary(delivery_note)
	frappe.enqueue(
		"marketplace_integrations.integrations.bosta.delivery_note_shipment.run_fetch_label",
		queue="short",
		timeout=120,
		job_id=_LABEL_JOB.format(delivery_note),
		deduplicate=True,
		enqueue_after_commit=True,
		delivery_note=delivery_note,
	)
	frappe.db.commit()
	try:
		statuses.replay_pending_events(delivery_id)
	except Exception:
		# The create is already logged Success: a replay problem must not turn it into Unknown. The
		# pending events stay Queued and the nightly reconciliation retries them.
		frappe.db.rollback()
		frappe.log_error(title="Bosta: pending events could not be replayed", message=frappe.get_traceback())


def _finish(delivery_note, status, values, log, result, expected=CREATING, **log_values):
	"""Close the log and write the note back, only if it is still ``expected`` (Creating for a create,
	Cancellation Pending for a cancel). Commit, lock as the next statement, write with ``db_set`` (system
	write-back, notifies open forms). Returns whether the note was written. The error text is stored raw: it
	is escaped where it is rendered."""
	close_log(log, result, **log_values)
	frappe.db.commit()
	locked = frappe.db.get_value("Delivery Note", delivery_note, "custom_integration_status", for_update=True)
	if cstr(locked) != expected:
		frappe.db.commit()
		return False
	doc = frappe.get_doc("Delivery Note", delivery_note)
	doc.db_set(dict(values, custom_integration_status=status), update_modified=True, notify=True, commit=True)
	return True


# ---------------------------------------------------------------------------
# Check with Bosta (lookup only, never creates)
# ---------------------------------------------------------------------------


def is_stale_creating(delivery_note):
	"""A Creating note with no queued job whose last change is older than ``STALE_CREATING_MINUTES``."""
	values = frappe.db.get_value("Delivery Note", delivery_note, ["custom_integration_status", "modified"], as_dict=True)
	if not values or cstr(values.custom_integration_status) != CREATING:
		return False
	if is_job_enqueued(_CREATE_JOB.format(delivery_note)):
		return False
	return get_datetime(values.modified) <= add_to_date(now_datetime(), minutes=-STALE_CREATING_MINUTES)


def check_with_bosta(delivery_note):
	"""Ask Bosta whether a Creation Unknown (or stuck Creating) note's delivery exists. Found: Created.
	Otherwise the note stays Creation Unknown: this never creates. The result is written only if the note
	is still as it was when the check started. The caller checked write access. The returned ``message``
	may hold Bosta's text: the caller escapes it where it is shown."""
	start = frappe.db.get_value(
		"Delivery Note", delivery_note, ["custom_integration_status", "modified", "custom_carrier_reference"], as_dict=True
	)
	status = cstr(start.custom_integration_status) or NOT_CREATED
	if status == CREATING and not is_stale_creating(delivery_note):
		frappe.throw(_("The shipment is still being sent to Bosta. Check again in a few minutes."))
	if status not in (CREATING, UNKNOWN):
		frappe.throw(_("Only a note with an unknown Bosta result can be checked."))
	reference = start.custom_carrier_reference or delivery_note
	BostaClient().ensure_ready()
	frappe.db.commit()
	log = open_log(delivery_note, "Refresh", reference=reference)
	frappe.db.commit()
	changed = {
		"status": status,
		"found": False,
		"message": _("The Delivery Note changed while checking with Bosta. Reload it and check again."),
	}
	try:
		data = find_remote_delivery(reference)
	except _Unknown as unknown:
		message = cstr(unknown)
		if not _resolve_unknown(delivery_note, start, UNKNOWN, {"custom_last_integration_error": message}, log, "Unknown", error=message):
			return changed
		return {"status": UNKNOWN, "found": False, "message": message}
	if data is None:
		message = _("Bosta has no delivery with this reference yet. The result is still unknown.")
		if not _resolve_unknown(delivery_note, start, UNKNOWN, {"custom_last_integration_error": message}, log, "Unknown", error=message):
			return changed
		return {"status": UNKNOWN, "found": False, "message": message}
	values = {
		"custom_carrier_delivery_id": _delivery_id(data),
		"lr_no": cstr(data.get("trackingNumber")).strip(),
		"lr_date": getdate(),
		"custom_last_integration_error": None,
		"custom_last_sync_at": now_datetime(),
	}
	if not _resolve_unknown(delivery_note, start, CREATED, values, log, "Success", delivery_id=_delivery_id(data)):
		return changed
	_after_created(delivery_note, _delivery_id(data))
	return {"status": CREATED, "found": True}


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


def _read_state(client, tracking):
	"""One GET of the delivery: ``("found", {"code", "name"})`` for a recognised state, ``("missing", None)``
	for a 404, else ``("failed", None)`` (any error, timeout or unrecognised answer)."""
	from marketplace_integrations.integrations.bosta import statuses

	try:
		response = client.get_delivery(tracking)
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


def cancel_at_bosta(delivery_note):
	"""Terminate the note's Bosta delivery (the API checked the cancel / write permission). The ERPNext
	cancel or delete comes after, natively, and is allowed by ``guard_cancel_or_delete`` only once the
	Integration Status is Cancelled.

	Synchronous (one terminate call with the client's timeout, at most one GET). Created / Cancellation
	Unknown -> Cancellation Pending (committed, no lock held across HTTP) -> Cancelled (a documented 200, or one
	GET showing 48/49 after a refusal or an unclear answer), Cancellation Unknown (timeout, 5xx, 429, or a 404 / 1066 /
	"already terminated" answer whose GET finds nothing or fails) or back to Created with the refusal in
	``custom_last_integration_error`` (a refusal, or an unclear answer whose GET shows a live state). A note that is already Cancelled is answered
	without calling Bosta: this is how a failed local cancel is retried.

	Returns ``{"status", "outcome", "message"}``; ``outcome`` is ``cancelled``, ``refused`` or ``unknown`` and
	``message`` is Bosta's raw text (the caller escapes it where it is shown)."""
	from marketplace_integrations.integrations.bosta import statuses

	BostaClient().ensure_ready()
	# Commit, then lock as the very next statement (MariaDB snapshot isolation).
	frappe.db.commit()
	locked = frappe.db.get_value(
		"Delivery Note",
		delivery_note,
		["custom_integration_status", "docstatus", "modified", "lr_no", "custom_carrier_reference"],
		as_dict=True,
		for_update=True,
	)
	status = cstr(locked.custom_integration_status) or NOT_CREATED
	if status == CANCELLED and locked.docstatus != 2:
		frappe.db.commit()
		return {"status": CANCELLED, "outcome": "cancelled", "message": ""}
	too_new = status == CANCELLATION_PENDING and get_datetime(locked.modified) > add_to_date(
		now_datetime(), minutes=-STALE_CANCEL_MINUTES
	)
	if (
		locked.docstatus == 2
		or too_new
		or status not in (CREATED, CANCELLATION_PENDING, CANCELLATION_UNKNOWN)
	):
		frappe.db.commit()
		if too_new:
			frappe.throw(_("Bosta is already being asked to cancel this shipment. Check again in a minute."))
		frappe.throw(_("Only a note with a created Bosta shipment can be cancelled at Bosta."))
	tracking = cstr(locked.lr_no).strip()
	if not tracking:
		frappe.db.commit()
		frappe.throw(_("This Delivery Note has no Bosta tracking number."))
	doc = frappe.get_doc("Delivery Note", delivery_note)
	doc.db_set(
		{"custom_integration_status": CANCELLATION_PENDING, "custom_last_integration_error": None},
		update_modified=True,
		notify=True,
	)
	log = open_log(delivery_note, "Cancel", reference=cstr(locked.custom_carrier_reference) or delivery_note)
	frappe.db.commit()

	client = BostaClient()
	try:
		response = client.terminate_delivery(tracking)
	except Exception as exc:
		outcome, message, http_status = (
			"unknown",
			_("The Bosta request failed ({0}). It may or may not have reached Bosta.").format(
				cstr(exc)[:200]
			),
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

	if outcome == "cancelled":
		values = {"custom_last_integration_error": None, "custom_last_sync_at": now_datetime()}
		if state:
			values["custom_shipping_status"] = state["name"]
		new_status, result = CANCELLED, "Success"
	elif outcome == "refused":
		values, new_status, result = {"custom_last_integration_error": message}, CREATED, "Failed"
	else:
		values, new_status, result = (
			{"custom_last_integration_error": message},
			CANCELLATION_UNKNOWN,
			"Unknown",
		)
	written = _finish(
		delivery_note,
		new_status,
		values,
		log,
		result,
		expected=CANCELLATION_PENDING,
		error=message if outcome != "cancelled" else None,
		http_status=http_status,
	)
	if not written:
		# A webhook or a refresh moved the note on while Bosta was asked; report what is stored now.
		return {"status": status_of(delivery_note), "outcome": outcome, "message": message}
	if new_status == CANCELLED:
		statuses.update_sales_order_summary_of(delivery_note)
	return {"status": new_status, "outcome": outcome, "message": message}


def _update_summary(delivery_note):
	from marketplace_integrations.integrations.bosta import statuses

	statuses.update_sales_order_summary_of(delivery_note)
	frappe.db.commit()


def _resolve_unknown(delivery_note, start, status, values, log, result, **log_values):
	"""Like ``_finish`` but for a note that is Creation Unknown or a stuck Creating: ``start`` is the
	status and ``modified`` read before the Bosta call; nothing is written if either changed since (a
	retry, another check or the worker may have moved the note on). Returns whether it was written."""
	close_log(log, result, **log_values)
	frappe.db.commit()
	locked = frappe.db.get_value(
		"Delivery Note", delivery_note, ["custom_integration_status", "modified"], as_dict=True, for_update=True
	)
	if (
		not locked
		or cstr(locked.custom_integration_status) != cstr(start.custom_integration_status)
		or cstr(locked.modified) != cstr(start.modified)
	):
		frappe.db.commit()
		return False
	frappe.get_doc("Delivery Note", delivery_note).db_set(
		dict(values, custom_integration_status=status), update_modified=True, notify=True, commit=True
	)
	return True


# ---------------------------------------------------------------------------
# Label
# ---------------------------------------------------------------------------


def run_fetch_label(delivery_note):
	"""Background job after Created: fetch the label as the system. A failure is logged and leaves the
	Integration Status alone; Fetch Label retries."""
	frappe.set_user("Administrator")
	return fetch_label(delivery_note)


def fetch_label(delivery_note, raise_on_error=False):
	"""Fetch the A4 Arabic label and attach it privately to the note (the one implementation behind the job
	and the Fetch Label button). Commits before the log row and before the HTTP call, so no transaction is
	open across it. A failure is logged Failed, never changes the Integration Status, and is raised again
	only when ``raise_on_error``."""
	from marketplace_integrations.integrations.bosta import labels

	frappe.db.commit()
	log = open_log(delivery_note, "Label Fetch")
	frappe.db.commit()
	try:
		result = labels.create_a4_arabic_label(delivery_note)
	except Exception as exc:
		frappe.db.rollback()
		# labels.py escapes Bosta's text for the screen; the log keeps it raw (escaped where it is rendered).
		close_log(log, "Failed", error=html.unescape(cstr(exc))[:500] or _("The label could not be fetched."))
		frappe.db.commit()
		if raise_on_error:
			raise
		return None
	close_log(log, "Success")
	frappe.db.commit()
	return result
