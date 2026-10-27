# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bosta delivery creation and the corresponding ERPNext Delivery Note."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from frappe import _
from frappe.utils import add_to_date, cint, cstr, escape_html, flt, get_datetime, now_datetime

from marketplace_integrations.integrations.bosta import city_match, locations
from marketplace_integrations.integrations.bosta.client import (
	BostaClient,
	is_success_response,
	response_body,
)
from marketplace_integrations.integrations.bosta.constants import BOSTA_EXCLUDED_PLATFORMS
from marketplace_integrations.marketplace_integrations.doctype.bosta_shipment.bosta_shipment import (
	save_as_system,
)
from marketplace_integrations.services import sales_order_sync

BOSTA_DELIVERY_TYPE = 10

# Roles that may create/check Bosta shipments (D3). Sales User is read-only.
CREATOR_ROLES = ("Sales Manager", "Stock User", "System Manager", "Bosta User")
HANDOVER_ROLES = ("System Manager", "Stock Manager")

# Bosta's documented COD ceiling for a delivery (OpenAPI ``cod`` description).
BOSTA_COD_LIMIT = 30000

# A "Pending Bosta" row younger than this may still have its create call in flight, so
# "Check Bosta" refuses it instead of racing the request that owns it.
PENDING_STUCK_MINUTES = 5

DELIVERY_NOTE_SAVEPOINT = "bosta_delivery_note"


def get_sales_order_defaults(sales_order_name):
	"""Return only fields that can safely prefill the operator's dialog, plus the COD
	that would be sent. An ineligible order is rejected here so no dialog is filled in
	for it."""
	_require_creator_role()
	order = _get_sales_order(sales_order_name)
	_validate_deliverable(order)
	cod = _cod_breakdown(order)
	existing = _active_shipment(order.name)
	retry_cod = (
		flt(existing.cod_amount, 2)
		if existing and existing.status == "Bosta Failed" and existing.cod_amount not in (None, "")
		else None
	)
	address = _read_address(order)
	return {
		"address_first_line": address["first_line"],
		"address_city": address["city"],
		**_city_prefill(address),
		"receiver_name": cstr(order.customer_name).strip(),
		"receiver_phone": _receiver_phone_prefill(order),
		"cod_amount": cod["cod"],
		"order_total": cod["total"],
		"advance_paid": cod["advance_paid"],
		"cod_limit": BOSTA_COD_LIMIT,
		"currency": frappe.get_cached_value("Company", order.company, "default_currency"),
		"retry_cod_amount": retry_cod,
	}


def _receiver_phone_prefill(order):
	"""The Sales Order's contact phone, else the Shopify buyer's phone.

	The buyer's phone is only on the Marketplace Order; which platforms may use it is decided
	in one place, ``sales_order_sync.buyer_phone_for_sales_order``. Read without per-user
	permissions for the same reason as ``_read_address``.
	"""
	phone = cstr(order.contact_mobile or order.contact_phone).strip()
	marketplace_order = order.get("custom_marketplace_order")
	if phone or not marketplace_order:
		return phone
	buyer = frappe.db.get_value(
		"Marketplace Order", marketplace_order, ["platform", "customer_phone_number"], as_dict=True
	)
	return sales_order_sync.buyer_phone_for_sales_order(buyer) if buyer else ""


def get_sales_order_shipment(sales_order_name):
	"""Permission-aware Bosta shipment summaries for a Sales Order form (#246/#338).

	Needs read on the Sales Order and on Bosta Shipment; a user without Bosta Shipment read
	gets an empty list, never shipment detail. All linked rows are returned so historical
	shipments remain visible; the service that creates shipments enforces one active row.
	Read with the caller's permissions, so user permissions apply.
	"""
	frappe.has_permission("Sales Order", "read", doc=sales_order_name, throw=True)
	if not frappe.has_permission("Bosta Shipment", "read"):
		return None
	fields = [
		"name",
		"status",
		"tracking_number",
		"bosta_state_value",
		"bosta_state_code",
		"bosta_delivery_type",
		"bosta_exception_reason",
		"last_bosta_status_on",
		"receiver_name",
		"receiver_phone",
		"city",
		"city_id",
		"address_first_line",
		"cod_amount",
		"shipment_fee",
		"shipment_fee_received_on",
		"delivery_note",
		"delivery_note_error",
		"handover_status",
		"handover_confirmed_by",
		"handover_confirmed_on",
		"stock_posting_status",
		"creation",
	]
	rows = frappe.get_list(
		"Bosta Shipment",
		filters={"sales_order": sales_order_name},
		fields=fields,
		order_by="creation desc, name desc",
		limit_page_length=0,
	)
	shipments = []
	current_row = next((row for row in rows if row.status != "Canceled"), rows[0] if rows else None)
	for row in rows:
		shipment = {
			"name": row.name,
			"status": row.status,
			"tracking_number": row.tracking_number or "",
			"bosta_state": row.bosta_state_value or "",
			"bosta_state_code": row.bosta_state_code or 0,
			"bosta_delivery_type": row.bosta_delivery_type or "",
			"bosta_exception_reason": row.bosta_exception_reason or "",
			"active": row.status != "Canceled",
			"last_update": cstr(row.last_bosta_status_on) if row.last_bosta_status_on else "",
			"receiver_name": row.receiver_name or "",
			"receiver_phone": row.receiver_phone or "",
			"city": row.city or "",
			"city_id": row.city_id or "",
			"address_first_line": row.address_first_line or "",
			"cod_amount": flt(row.cod_amount),
			# None until Bosta reported a fee: an unset Currency reads back as 0.
			"shipment_fee": flt(row.shipment_fee) if row.shipment_fee_received_on else None,
			"delivery_note": row.delivery_note or "",
			"delivery_note_error": row.delivery_note_error or "",
			"delivery_note_status": _readable_delivery_note_status(row.delivery_note),
			"delivery_note_posted": _delivery_note_is_submitted(row.delivery_note),
			"handover_status": row.handover_status or "Awaiting Handover",
			"handover_confirmed_by": row.handover_confirmed_by or "",
			"handover_confirmed_on": cstr(row.handover_confirmed_on) if row.handover_confirmed_on else "",
			"stock_posting_status": row.stock_posting_status or "Not Posted",
			"created_on": cstr(row.creation) if row.creation else "",
		}
		shipment["allocations"] = [
			{
				"item_code": allocation.item_code or "",
				"warehouse": allocation.warehouse or "",
				"qty": flt(allocation.qty),
				"uom": allocation.uom or "",
				"stock_qty": flt(allocation.stock_qty),
			}
			# The parent rows above were filtered with permission-aware get_list after
			# authorizing Sales Order and Bosta Shipment read. Allocation child rows have
			# no independent read grant for these roles; expose only rows of those parents.
			for allocation in frappe.get_all(
				"Bosta Shipment Item",
				filters={"parent": row.name, "parenttype": "Bosta Shipment"},
				fields=["item_code", "warehouse", "qty", "uom", "stock_qty"],
				order_by="idx asc",
				limit_page_length=0,
			)
		]
		shipments.append(shipment)
	if not shipments:
		return None
	current = next((shipment for shipment in shipments if shipment["name"] == current_row.name), shipments[0])
	return {**current, "shipments": shipments}


def _readable_delivery_note_status(delivery_note):
	"""The Delivery Note's status, only when the caller may read that Delivery Note."""
	if not delivery_note or not frappe.has_permission("Delivery Note", "read", doc=delivery_note):
		return ""
	return frappe.db.get_value("Delivery Note", delivery_note, "status") or ""


def _delivery_note_is_submitted(delivery_note):
	"""Expose only the linked Delivery Note's submitted state, after its read check."""
	if not delivery_note:
		return False
	if not frappe.has_permission("Delivery Note", "read", doc=delivery_note):
		return None
	return frappe.db.get_value("Delivery Note", delivery_note, "docstatus") == 1


def _read_address(order):
	"""The address for the dialog and for city resolution (#245, #305).

	The Sales Order's shipping Address wins; the Marketplace Order's formatted address is
	only a fallback. For a Shopify order the Marketplace Order's shipping address also
	supplies the country and province code. Read without per-user permissions: the caller can
	already read this Sales Order, and Address / Marketplace Order are not readable by every
	creator role.
	"""
	province = _shopify_province(order)
	if order.get("shipping_address_name"):
		address = frappe.db.get_value(
			"Address",
			order.shipping_address_name,
			["address_line1", "address_line2", "city", "state"],
			as_dict=True,
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
	marketplace_order = order.get("custom_marketplace_order")
	formatted = (
		frappe.db.get_value("Marketplace Order", marketplace_order, "customer_formatted_address")
		if marketplace_order
		else ""
	)
	formatted = cstr(formatted).strip()
	return {"first_line": formatted, "city": "", "hints": [], **province}


def _shopify_province(order):
	"""``country_code`` / ``province_code`` from a Shopify Marketplace Order's
	``shipping_address`` JSON."""
	marketplace_order = order.get("custom_marketplace_order")
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


def _city_prefill(address):
	"""``city_id`` / ``city_name`` / ``from_shopify`` for the dialog. A failed Bosta lookup (or a
	recent one, see ``locations.lookups_blocked``) only loses the prefill: the operator picks,
	and no popup shows."""
	try:
		result = city_match.resolve_city(address)
	except locations.BostaLookupUnavailable:
		return {}
	except Exception:
		frappe.clear_last_message()
		frappe.log_error(frappe.get_traceback(with_context=False), "Bosta city match failed")
		return {}
	if result.get("city_id"):
		# The name the dialog would show for this city (from GET /cities), not the directory's.
		result["city_name"] = locations.city_name_for(result["city_id"], result["city_name"])
	return result


def create_bosta_shipment(
	sales_order_name,
	receiver_name,
	receiver_phone,
	city_id,
	address_first_line,
	expected_cod=None,
	cod_amount=None,
):
	"""Reserve available Sales Order quantities, create Bosta's delivery, then submit a
	standard Delivery Note for that reservation immediately.

	Transaction shape: commit, lock the Sales Order (first statement of the
	transaction), validate and reserve all available quantities by Sales Order item row,
	persist a ``Pending Bosta`` row and commit, call Bosta, persist the result and commit,
	then re-lock the Sales Order for the Delivery Note. A Bosta acceptance followed by a
	Delivery Note failure is visible and resumable without ever submitting a second Bosta
	delivery. An unknown Bosta outcome keeps its reservation until Check Bosta resolves it.

	``city_id`` is the Bosta city; no district is sent, Bosta resolves it from the city code and
	the address (#318). ``expected_cod`` is the amount the dialog showed; if the amount
	recomputed now differs the request is refused so the operator never sends an amount they
	have not seen.
	"""
	_require_creator_role()
	if not cstr(address_first_line).strip():
		frappe.throw(_("Enter the address first line."))
	# Before the lock: this may call Bosta (cached lists). No code means nothing is sent.
	city = locations.get_city(city_id)
	order = lock_and_load_sales_order(sales_order_name)
	_validate_deliverable(order)
	BostaClient().ensure_ready()

	existing = _active_shipment(order.name)
	if existing:
		details = {
			"receiver_name": receiver_name,
			"receiver_phone": receiver_phone,
			"city": city["name"],
			"city_id": city["id"],
			"city_code": city["code"],
			"address_first_line": address_first_line,
		}
		return _resume_existing(existing, order, expected_cod, details, cod_amount)

	cod = _current_cod(order, expected_cod, cod_amount)
	shipment = frappe.get_doc(
		{
			"doctype": "Bosta Shipment",
			"sales_order": order.name,
			"company": order.company,
			"receiver_name": receiver_name,
			"receiver_phone": receiver_phone,
			"city": city["name"],
			"city_id": city["id"],
			"city_code": city["code"],
			"address_first_line": address_first_line,
			"cod_amount": cod,
			"bosta_requested_on": now_datetime(),
		}
	)
	_reserve_available_quantity(shipment, order)
	save_as_system(shipment, insert=True)
	# The unique row must survive a network/remote failure, preventing a second request
	# that could duplicate a delivery after an ambiguous timeout.
	frappe.db.commit()
	return _send_to_bosta(shipment, order.name, cod)


def check_bosta_shipment(shipment_name):
	"""Safely resolve a stuck shipment by searching Bosta for its business reference.

	Found: link the delivery; stock posts only if SEND state 21 was already observed.
	Not found: mark the shipment ``Bosta Failed`` so a retry is allowed.
	"""
	_require_creator_role()
	BostaClient().ensure_ready()
	sales_order = frappe.db.get_value("Bosta Shipment", shipment_name, "sales_order")
	if not sales_order:
		frappe.throw(_("Bosta Shipment {0} does not exist.").format(escape_html(cstr(shipment_name))))
	order = lock_and_load_sales_order(sales_order)
	shipment = frappe.get_doc("Bosta Shipment", shipment_name)
	_check_stuck(shipment)

	if not shipment.tracking_number:
		frappe.db.commit()
		remote = _find_remote_delivery(shipment)
		order = lock_and_load_sales_order(sales_order)
		shipment.reload()
		if not shipment.tracking_number:
			if not remote:
				shipment.status = "Bosta Failed"
				shipment.reservation_status = "Released"
				shipment.last_error = _(
					"Bosta has no delivery with reference {0}. A retry is allowed."
				).format(shipment.unique_business_reference)
				save_as_system(shipment)
				frappe.db.commit()
				return {"shipment": shipment.name, "tracking_number": "", "delivery_note": "", "found": False}
			_link_bosta_delivery(shipment, remote)
			frappe.db.commit()
	if (
		shipment.bosta_state_code == 21
		and shipment.bosta_delivery_type == "SEND"
		and shipment.handover_status == "Awaiting Handover"
	):
		shipment.handover_status = "Confirmed"
		shipment.handover_confirmed_by = "Administrator"
		shipment.handover_confirmed_on = now_datetime()
		save_as_system(shipment)
		return _create_delivery_note(shipment, order.name) | {"found": True}
	if shipment.handover_status == "Confirmed" and shipment.stock_posting_status != "Posted":
		return _create_delivery_note(shipment, order.name) | {"found": True}
	return _found_shipment_result(shipment)


def _found_shipment_result(shipment):
	"""Keep the response shape stable when a Bosta delivery was already found."""
	return {
		"shipment": shipment.name,
		"tracking_number": shipment.tracking_number or "",
		"delivery_note": shipment.delivery_note or "",
		"stock_posting_status": shipment.stock_posting_status or "Not Posted",
		"found": True,
	}


def lock_and_load_sales_order(sales_order_name):
	"""Commit, lock, then load one Sales Order (the Noon ``_lock_and_load_order`` pattern).

	MariaDB's snapshot isolation scopes the read view to the whole transaction, so the
	commit must be the statement right before the ``FOR UPDATE`` lock (see
	docs/frappe-conventions.md, Concurrency).
	"""
	frappe.db.commit()
	frappe.db.get_value("Sales Order", sales_order_name, "name", for_update=True)
	return _get_sales_order(sales_order_name)


def _require_creator_role():
	frappe.only_for(CREATOR_ROLES)


def _get_sales_order(sales_order_name):
	order = frappe.get_doc("Sales Order", sales_order_name)
	# Creators are decided by CREATOR_ROLES; Stock User can read but not submit Sales Orders.
	order.check_permission("read")
	return order


def excluded_platform(order):
	"""The marketplace platform of ``order`` when it fulfils its own orders (no Bosta), else ``None``.

	Read from the linked Marketplace Order, the authority: the Sales Order's
	``custom_marketplace_platform`` is only fetched when the document is saved."""
	marketplace_order = order.get("custom_marketplace_order")
	platform = (
		frappe.db.get_value("Marketplace Order", marketplace_order, "platform")
		if marketplace_order
		else order.get("custom_marketplace_platform")
	)
	return platform if platform in BOSTA_EXCLUDED_PLATFORMS else None


def _validate_deliverable(order):
	"""Reject before inserting a shipment or calling Bosta (H2).

	Every create path (single, bulk plan and bulk create) goes through here, so a Sales Order of a
	platform that fulfils its own orders is refused for all of them."""
	platform = excluded_platform(order)
	if platform:
		frappe.throw(
			_("Bosta shipments cannot be created for {0} orders: {0} fulfils its own orders.").format(platform)
		)
	if order.docstatus != 1:
		frappe.throw(_("Bosta Shipment can only be created from a submitted Sales Order."))
	if order.status in ("Closed", "On Hold"):
		frappe.throw(_("Sales Order {0} is {1}.").format(order.name, _(order.status)))
	if not any(
		abs(flt(item.delivered_qty)) < abs(flt(item.qty)) and not item.delivered_by_supplier
		for item in order.items
	):
		frappe.throw(_("The Sales Order has no remaining quantity to deliver."))


def _cod_breakdown(order):
	"""COD is the OUTSTANDING amount: total minus the mirrored advance payments (D4)."""
	company_currency = frappe.get_cached_value("Company", order.company, "default_currency")
	if order.currency != company_currency:
		frappe.throw(
			_("Sales Order {0} is in {1}; Bosta COD is only supported in the company currency {2}.").format(
				order.name, order.currency, company_currency
			)
		)
	total = flt(order.rounded_total) or flt(order.grand_total)
	advance_paid = flt(order.advance_paid)
	cod = max(flt(total - advance_paid, 2), 0)
	if cod > BOSTA_COD_LIMIT:
		frappe.throw(_("The COD amount {0} exceeds Bosta's limit of {1}.").format(cod, BOSTA_COD_LIMIT))
	return {"total": flt(total, 2), "advance_paid": flt(advance_paid, 2), "cod": cod}


def _current_cod(order, expected_cod, cod_amount=None):
	default_cod = _cod_breakdown(order)["cod"]
	if expected_cod is not None and flt(expected_cod, 2) != default_cod:
		frappe.throw(
			_(
				"The COD amount changed from {0} to {1} since the dialog was opened. Reopen the dialog and check it."
			).format(flt(expected_cod, 2), default_cod)
		)
	if cod_amount in (None, ""):
		return default_cod
	try:
		amount = Decimal(str(cod_amount))
		if not amount.is_finite():
			raise InvalidOperation
		cod = float(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
	except (InvalidOperation, TypeError, ValueError):
		frappe.throw(_("Enter a valid COD amount in the company currency."))
	if cod < 0:
		frappe.throw(_("COD cannot be negative."))
	if cod > BOSTA_COD_LIMIT:
		frappe.throw(_("The COD amount {0} exceeds Bosta's limit of {1}.").format(cod, BOSTA_COD_LIMIT))
	return cod


def _active_shipment(sales_order):
	# The prior active_sales_order field is retained only for old data. A pending
	# operation is now identified by its status and reservation lifecycle instead.
	rows = frappe.get_all(
		"Bosta Shipment",
		filters={
			"sales_order": sales_order,
			"status": ["in", ["Pending Bosta", "Bosta Created", "Delivery Note Failed", "Bosta Failed"]],
		},
		fields=["name", "status", "tracking_number", "reservation_status"],
		order_by="creation desc, name desc",
	)
	return frappe.get_doc("Bosta Shipment", rows[0].name) if rows else None


def _resume_existing(shipment, order, expected_cod=None, details=None, cod_amount=None):
	if shipment.status in ("Delivery Note Failed", "Bosta Created") and shipment.bosta_delivery_id:
		return _found_shipment_result(shipment)
	if shipment.status == "Bosta Failed" and not shipment.tracking_number:
		# Retrying a Bosta Failed shipment repeats its recorded COD, even if the
		# Sales Order's calculated default has since changed.
		_current_cod(order, expected_cod)
		cod = flt(shipment.cod_amount, 2) if shipment.cod_amount not in (None, "") else _current_cod(order, expected_cod, cod_amount)
		frappe.db.commit()
		# The earlier attempt may have reached Bosta (e.g. a timeout): look first.
		remote = _find_remote_delivery(shipment)
		if remote:
			lock_and_load_sales_order(order.name)
			shipment.reload()
			if shipment.reservation_status != "Reserved":
				_reserve_available_quantity(shipment, _get_sales_order(order.name))
			_link_bosta_delivery(shipment, remote)
			frappe.db.commit()
			return _found_shipment_result(shipment)
		lock_and_load_sales_order(order.name)
		shipment.reload()
		# Nothing reached Bosta, so the operator may correct the receiver or address.
		shipment.update(details or {})
		_reserve_available_quantity(shipment, _get_sales_order(order.name))
		shipment.status = "Pending Bosta"
		shipment.cod_amount = cod
		shipment.bosta_requested_on = now_datetime()
		save_as_system(shipment)
		frappe.db.commit()
		return _send_to_bosta(shipment, order.name, cod)
	if shipment.status == "Pending Bosta":
		frappe.throw(
			_("Bosta Shipment {0} is still pending. If it is stuck, use Check Bosta on it.").format(
				shipment.name
			)
		)
	frappe.throw(_("Bosta Shipment {0} already exists for this Sales Order.").format(shipment.name))


def _check_stuck(shipment):
	if shipment.status == "Pending Bosta":
		started = shipment.bosta_requested_on or shipment.creation
		if get_datetime(started) > add_to_date(now_datetime(), minutes=-PENDING_STUCK_MINUTES):
			frappe.throw(_("This shipment may still be in progress. Try again in a few minutes."))
		return
	if shipment.status in ("Bosta Failed", "Delivery Note Failed") or (
		shipment.status == "Bosta Created" and not shipment.delivery_note
	):
		return
	frappe.throw(_("Bosta Shipment {0} does not need checking.").format(shipment.name))


def _send_to_bosta(shipment, sales_order, cod):
	"""Call Bosta's create endpoint for an already-persisted shipment (no lock held)."""
	payload = _build_delivery_payload(shipment, cod)
	try:
		response = BostaClient().create_delivery(payload)
	except Exception as exc:
		# Ambiguous: the delivery may or may not exist. Never leave the row Pending forever.
		shipment.status = "Bosta Failed"
		shipment.last_error = _(
			"The Bosta request failed ({0}). It may or may not have reached Bosta. "
			"Use Check Bosta before retrying."
		).format(cstr(exc)[:200])
		save_as_system(shipment)
		frappe.db.commit()
		raise

	if not is_success_response(response):
		shipment.status = "Bosta Failed"
		shipment.reservation_status = "Released"
		shipment.last_error = BostaClient().error_message(response)
		save_as_system(shipment)
		frappe.db.commit()
		frappe.throw(escape_html(shipment.last_error))

	data = _delivery_data(response_body(response))
	if not _delivery_id(data) or not cstr(data.get("trackingNumber")).strip():
		shipment.status = "Bosta Failed"
		shipment.last_error = _("Bosta accepted the request without a delivery ID and tracking number.")
		save_as_system(shipment)
		frappe.db.commit()
		frappe.throw(shipment.last_error)

	shipment.cod_amount = flt(cod, 2)
	_link_bosta_delivery(shipment, data)
	frappe.db.commit()
	return {"shipment": shipment.name, "tracking_number": shipment.tracking_number, "delivery_note": ""}


def confirm_physical_handover(shipment_name):
	"""Record an authorized physical handover and post its reserved stock once."""
	frappe.only_for(HANDOVER_ROLES)
	shipment = frappe.get_doc("Bosta Shipment", shipment_name)
	shipment.check_permission("read")
	if shipment.status == "Canceled":
		frappe.throw(_("A canceled Bosta Shipment cannot be handed over."))
	if not shipment.tracking_number or not shipment.bosta_delivery_id:
		frappe.throw(_("Confirm Bosta delivery details before recording physical handover."))
	if shipment.handover_status == "Review Required":
		frappe.throw(_("This shipment requires review before stock can be posted."))
	if shipment.handover_status != "Confirmed":
		shipment.handover_status = "Confirmed"
		shipment.handover_confirmed_by = frappe.session.user
		shipment.handover_confirmed_on = now_datetime()
		save_as_system(shipment)
		frappe.db.commit()
	result = _create_delivery_note(shipment, shipment.sales_order)
	shipment.reload()
	result["handover_status"] = shipment.handover_status
	result["stock_posting_status"] = shipment.stock_posting_status
	result["handover_confirmed_by"] = shipment.handover_confirmed_by
	result["handover_confirmed_on"] = cstr(shipment.handover_confirmed_on)
	return result


def _delivery_data(body):
	return body.get("data") if isinstance(body.get("data"), dict) else body


def _delivery_id(data):
	return cstr(data.get("_id") or data.get("id")).strip()


def _link_bosta_delivery(shipment, data):
	shipment.bosta_delivery_id = _delivery_id(data)
	shipment.tracking_number = cstr(data.get("trackingNumber")).strip()
	shipment.status = "Bosta Created"
	shipment.last_error = ""
	save_as_system(shipment)


def _find_remote_delivery(shipment):
	"""The Bosta delivery whose businessReference is ours, or ``None`` when Bosta has none.

	Only recognised response shapes count as an answer; anything else raises, so an
	unexpected reply can never be mistaken for "not found" (which would allow a duplicate).
	"""
	client = BostaClient()
	response = client.search_by_business_reference(shipment.unique_business_reference)
	if not is_success_response(response):
		frappe.throw(escape_html(client.error_message(response)))
	data = response_body(response).get("data")
	if not data:
		return None
	# Live shape (seen 2026-10-19): data = {count, deliveries, limit, page}. More matches
	# than the page holds cannot be checked, so it is treated as ambiguous, never as none.
	if isinstance(data, dict) and isinstance(data.get("deliveries"), list):
		if cint(data.get("count")) > len(data["deliveries"]):
			frappe.throw(_("Bosta returned an ambiguous search result. Check the delivery in Bosta."))
		data = data["deliveries"]
	if isinstance(data, dict) and data.get("_id"):
		data = [data]
	if not isinstance(data, list):
		seen = ", ".join(sorted(data)[:10]) if isinstance(data, dict) else type(data).__name__
		frappe.throw(
			_(
				"Bosta returned a search result in an unrecognised format (keys seen: {0}). "
				"The Bosta search response shape is unverified; check the delivery in Bosta."
			).format(escape_html(seen))
		)
	matches = [
		item
		for item in data
		if isinstance(item, dict)
		and cstr(item.get("businessReference")) == shipment.unique_business_reference
		and _delivery_id(item)
		and cstr(item.get("trackingNumber")).strip()
	]
	if len(matches) > 1 or (data and not matches):
		frappe.throw(_("Bosta returned an ambiguous search result. Check the delivery in Bosta."))
	return matches[0] if matches else None


def _build_delivery_payload(shipment, cod):
	"""The MVP sends only Bosta's required fields and approved exceptions. The drop-off
	address is the city code and the first line only: Bosta resolves the district (#318)."""
	settings = frappe.get_single("Bosta Settings")
	return {
		"type": BOSTA_DELIVERY_TYPE,
		"cod": cod,
		"receiver": {"firstName": shipment.receiver_name, "phone": shipment.receiver_phone},
		"dropOffAddress": {"city": shipment.city_code, "firstLine": shipment.address_first_line},
		"businessLocationId": settings.business_location_id,
		# businessReference is the field Bosta documents for searching; keep it identical
		# to the unique reference so a stuck shipment can be found again.
		"businessReference": shipment.unique_business_reference,
		"uniqueBusinessReference": shipment.unique_business_reference,
	}


def _create_delivery_note(shipment, sales_order):
	"""Create and submit the Delivery Note inside a savepoint, under the Sales Order lock."""
	if shipment.handover_status != "Confirmed":
		return {"shipment": shipment.name, "delivery_note": "", "tracking_number": shipment.tracking_number, "stock_posting_status": shipment.stock_posting_status}
	lock_and_load_sales_order(sales_order)
	shipment.reload()
	# The caller decided before the lock; re-check the state it may have changed.
	if shipment.status == "Canceled":
		frappe.throw(_("Bosta Shipment {0} was canceled by Bosta.").format(shipment.name))
	if shipment.delivery_note or shipment.status == "Completed":
		return {
			"shipment": shipment.name,
			"delivery_note": shipment.delivery_note,
			"tracking_number": shipment.tracking_number,
			"stock_posting_status": shipment.stock_posting_status,
		}
	if shipment.stock_posting_status == "Posted":
		return {"shipment": shipment.name, "delivery_note": shipment.delivery_note, "tracking_number": shipment.tracking_number, "stock_posting_status": shipment.stock_posting_status}
	if shipment.reservation_status != "Reserved":
		_reserve_available_quantity(shipment, _get_sales_order(sales_order))
	frappe.db.savepoint(DELIVERY_NOTE_SAVEPOINT)
	# The caller's Bosta role was checked up front; the document work runs as that user
	# with permission checks skipped (they remain the Delivery Note's owner).
	try:
		delivery_note = make_delivery_note(sales_order)
		_reserved_delivery_note_items(delivery_note, shipment)
		if not delivery_note.items:
			frappe.throw(_("The Sales Order has no remaining quantity to deliver."))
		delivery_note.insert(ignore_permissions=True)
		delivery_note.flags.ignore_permissions = True
		delivery_note.submit()
	except Exception as exc:
		rollback_to_savepoint(DELIVERY_NOTE_SAVEPOINT)
		frappe.log_error(frappe.get_traceback(with_context=False), "Bosta Delivery Note creation failed")
		shipment.status = "Delivery Note Failed"
		shipment.stock_posting_status = "Failed"
		shipment.last_error = _("Delivery Note creation failed. See Error Log for details.")
		shipment.delivery_note_error = _("Delivery Note creation failed: {0}").format(
			frappe.utils.strip_html(cstr(exc))[:300]
		)
		save_as_system(shipment)
		frappe.db.commit()
		raise

	shipment.delivery_note = delivery_note.name
	shipment.status = "Completed"
	shipment.stock_posting_status = "Posted"
	shipment.reservation_status = "Fulfilled"
	shipment.last_error = ""
	shipment.delivery_note_error = ""
	fulfilled_items = {row.so_detail: row for row in shipment.shipment_items}
	for delivery_item in delivery_note.items:
		allocation = fulfilled_items[delivery_item.so_detail]
		allocation.fulfilled_qty = delivery_item.stock_qty
		allocation.delivery_note_item = delivery_item.name
	save_as_system(shipment)
	frappe.db.release_savepoint(DELIVERY_NOTE_SAVEPOINT)
	frappe.db.commit()
	return {
		"shipment": shipment.name,
		"delivery_note": delivery_note.name,
		"tracking_number": shipment.tracking_number,
		"stock_posting_status": shipment.stock_posting_status,
	}


def _reserve_available_quantity(shipment, order):
	"""Reserve all currently available stock quantity per Sales Order item row.

	The caller holds the Sales Order row lock. Submitted Delivery Notes and other
	active shipment reservations are subtracted by exact ``so_detail``.
	"""
	precision = frappe.get_precision("Sales Order Item", "stock_qty")
	qty_precision = frappe.get_precision("Sales Order Item", "qty")
	ordered = {item.name: flt(item.stock_qty, precision) for item in order.items}
	delivered = _submitted_delivery_note_quantities(order.name)
	reserved = _active_reservation_quantities(order.name, exclude=shipment.name)
	allocations = []
	for item in order.items:
		if item.delivered_by_supplier:
			continue
		available = flt(ordered[item.name] - delivered.get(item.name, 0) - reserved.get(item.name, 0), precision)
		if available <= 0:
			continue
		conversion_factor = flt(item.conversion_factor) or 1
		allocations.append(
			{
				"doctype": "Bosta Shipment Item",
				"so_detail": item.name,
				"item_code": item.item_code,
				"warehouse": item.warehouse,
				"qty": flt(available / conversion_factor, qty_precision),
				"uom": item.uom,
				"conversion_factor": conversion_factor,
				"stock_qty": available,
				"fulfilled_qty": 0,
			}
		)
	if not allocations:
		frappe.throw(_("The Sales Order has no available quantity to reserve for Bosta."))
	shipment.set("shipment_items", allocations)
	shipment.reservation_status = "Reserved"


def _submitted_delivery_note_quantities(sales_order):
	"""Stock quantity already delivered, grouped by the exact Sales Order row."""
	# This system-internal aggregate runs after the caller has passed the Sales Order
	# read check; parameterized SQL keeps the child-row join bounded and exact.
	rows = frappe.db.sql(
		"""
		SELECT dni.so_detail, SUM(dni.stock_qty) AS stock_qty
		FROM `tabDelivery Note Item` dni
		INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		WHERE dni.against_sales_order = %s
			AND dni.so_detail IS NOT NULL AND dni.so_detail != ''
			AND dn.docstatus = 1 AND IFNULL(dn.is_return, 0) = 0
		GROUP BY dni.so_detail
		""",
		(sales_order,),
		as_dict=True,
	)
	return {row.so_detail: flt(row.stock_qty, frappe.get_precision("Sales Order Item", "stock_qty")) for row in rows}


def _active_reservation_quantities(sales_order, exclude=None):
	# Trusted service read after the creator-role and Sales Order checks above.
	shipments = frappe.get_all(
		"Bosta Shipment",
		filters={"sales_order": sales_order, "reservation_status": "Reserved", "name": ["!=", exclude or ""]},
		pluck="name",
	)
	if not shipments:
		return {}
	rows = frappe.get_all(
		"Bosta Shipment Item",
		filters={"parent": ["in", shipments], "parenttype": "Bosta Shipment", "parentfield": "shipment_items"},
		fields=["so_detail", "stock_qty"],
	)
	precision = frappe.get_precision("Sales Order Item", "stock_qty")
	reserved = {}
	for row in rows:
		reserved[row.so_detail] = flt(reserved.get(row.so_detail, 0) + flt(row.stock_qty, precision), precision)
	return reserved


def _reserved_delivery_note_items(delivery_note, shipment):
	"""Limit the standard ERPNext mapper output to this shipment's reserved SO rows."""
	allocations = {row.so_detail: row for row in shipment.shipment_items}
	items = []
	for row in delivery_note.items:
		allocation = allocations.get(row.so_detail)
		if not allocation:
			continue
		row.qty = allocation.qty
		row.uom = allocation.uom
		row.conversion_factor = allocation.conversion_factor
		row.stock_qty = allocation.stock_qty
		if allocation.warehouse:
			row.warehouse = allocation.warehouse
		else:
			allocation.warehouse = row.warehouse
		items.append(row)
	delivery_note.set("items", items)
	if len(delivery_note.items) != len(allocations):
		frappe.throw(_("The Sales Order allocation no longer matches the Delivery Note."))


def rollback_to_savepoint(savepoint):
	"""Roll back to the savepoint; False when it was already gone (nothing was undone)."""
	try:
		frappe.db.rollback(save_point=savepoint)
	except Exception as exc:
		# The savepoint can already be gone (e.g. MariaDB rolled the whole transaction
		# back on a deadlock); anything else is a real error.
		if "SAVEPOINT" not in cstr(exc).upper():
			raise
		return False
	return True
