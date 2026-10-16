# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bosta delivery creation and the corresponding ERPNext Delivery Note."""

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from frappe import _
from frappe.utils import add_to_date, cstr, escape_html, flt, get_datetime, now_datetime

from marketplace_integrations.integrations.bosta.client import (
	BostaClient,
	is_success_response,
	response_body,
)
from marketplace_integrations.marketplace_integrations.doctype.bosta_shipment.bosta_shipment import (
	save_as_system,
)

BOSTA_DELIVERY_TYPE = 10

# Roles that may create/check Bosta shipments (D3). Sales User is read-only.
CREATOR_ROLES = ("Sales Manager", "Stock User", "System Manager")

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
	return {
		**_address_prefill(order),
		"receiver_name": cstr(order.customer_name).strip(),
		"receiver_phone": _receiver_phone_prefill(order),
		"cod_amount": cod["cod"],
		"order_total": cod["total"],
		"advance_paid": cod["advance_paid"],
		"cod_limit": BOSTA_COD_LIMIT,
	}


def _receiver_phone_prefill(order):
	"""The Sales Order's contact phone, else the marketplace buyer's phone.

	Marketplace syncs (Shopify) do not set the Sales Order contact fields; the buyer's phone
	is only on the Marketplace Order. Read without per-user permissions for the same reason
	as ``_address_prefill``.
	"""
	phone = cstr(order.contact_mobile or order.contact_phone).strip()
	marketplace_order = order.get("custom_marketplace_order")
	if phone or not marketplace_order:
		return phone
	return cstr(frappe.db.get_value("Marketplace Order", marketplace_order, "customer_phone_number")).strip()


def get_sales_order_shipment(sales_order_name):
	"""The Sales Order's current Bosta shipment for its form (#246), or None.

	Needs read on the Sales Order and on Bosta Shipment; a user without Bosta Shipment read
	gets None, never any shipment detail. The active shipment (not Canceled; at most one,
	``active_sales_order`` is unique) wins; otherwise the latest Canceled one is returned
	with ``active`` False. Read with the caller's permissions, so user permissions apply.
	"""
	frappe.has_permission("Sales Order", "read", doc=sales_order_name, throw=True)
	if not frappe.has_permission("Bosta Shipment", "read"):
		return None
	fields = ["name", "status", "tracking_number", "bosta_state_value"]
	for active, status_filter in ((True, ["!=", "Canceled"]), (False, "Canceled")):
		rows = frappe.get_list(
			"Bosta Shipment",
			filters={"sales_order": sales_order_name, "status": status_filter},
			fields=fields,
			order_by="creation desc, name desc",
			limit=1,
		)
		if rows:
			row = rows[0]
			return {
				"name": row.name,
				"status": row.status,
				"tracking_number": row.tracking_number or "",
				"bosta_state": row.bosta_state_value or "",
				"active": active,
			}
	return None


def _address_prefill(order):
	"""Address first line and source city for the dialog (#245).

	The Sales Order's shipping Address wins; the Marketplace Order's formatted address is
	only a fallback for the first line (it has no structured city, so none is offered).
	The district is never guessed. Read without per-user permissions: the caller can already
	read this Sales Order, and Address / Marketplace Order are not readable by every creator
	role.
	"""
	if order.get("shipping_address_name"):
		address = frappe.db.get_value(
			"Address",
			order.shipping_address_name,
			["address_line1", "address_line2", "city"],
			as_dict=True,
		)
		if address:
			lines = [cstr(line).strip() for line in (address.address_line1, address.address_line2)]
			return {
				"address_first_line": ", ".join(line for line in lines if line),
				"address_city": cstr(address.city).strip(),
			}
	marketplace_order = order.get("custom_marketplace_order")
	formatted = (
		frappe.db.get_value("Marketplace Order", marketplace_order, "customer_formatted_address")
		if marketplace_order
		else ""
	)
	return {"address_first_line": cstr(formatted).strip(), "address_city": ""}


def create_bosta_shipment(
	sales_order_name,
	receiver_name,
	receiver_phone,
	city,
	district_id,
	address_first_line,
	expected_cod=None,
):
	"""Create Bosta's delivery, then the full remaining ERPNext Delivery Note.

	Transaction shape: commit, lock the Sales Order (first statement of the
	transaction), validate and decide, persist a ``Pending Bosta`` row and commit
	(releasing the lock - the unique active-shipment index now blocks a duplicate), call
	Bosta, persist the result and commit, then re-lock the Sales Order for the Delivery
	Note. A Bosta acceptance followed by a Delivery Note failure is visible and resumable
	without ever submitting a second Bosta delivery.

	``expected_cod`` is the amount the dialog showed; if the amount recomputed now differs
	the request is refused so the operator never sends an amount they have not seen.
	"""
	_require_creator_role()
	if not cstr(district_id).strip():
		frappe.throw(_("Choose a Bosta district."))
	if not cstr(address_first_line).strip():
		frappe.throw(_("Enter the address first line."))
	order = lock_and_load_sales_order(sales_order_name)
	_validate_deliverable(order)
	BostaClient().ensure_ready()

	existing = _active_shipment(order.name)
	if existing:
		details = {
			"receiver_name": receiver_name,
			"receiver_phone": receiver_phone,
			"city": city,
			"district_id": district_id,
			"address_first_line": address_first_line,
		}
		return _resume_existing(existing, order, expected_cod, details)

	cod = _current_cod(order, expected_cod)
	shipment = frappe.get_doc(
		{
			"doctype": "Bosta Shipment",
			"sales_order": order.name,
			"receiver_name": receiver_name,
			"receiver_phone": receiver_phone,
			"city": city,
			"district_id": district_id,
			"address_first_line": address_first_line,
			"bosta_requested_on": now_datetime(),
		}
	)
	save_as_system(shipment, insert=True)
	# The unique row must survive a network/remote failure, preventing a second request
	# that could duplicate a delivery after an ambiguous timeout.
	frappe.db.commit()
	return _send_to_bosta(shipment, order.name, cod)


def check_bosta_shipment(shipment_name):
	"""Safely resolve a stuck shipment by searching Bosta for its business reference.

	Found: link the delivery and continue to the Delivery Note. Not found: mark the
	shipment ``Bosta Failed`` so a retry is allowed.
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
				shipment.last_error = _(
					"Bosta has no delivery with reference {0}. A retry is allowed."
				).format(shipment.unique_business_reference)
				save_as_system(shipment)
				frappe.db.commit()
				return {"shipment": shipment.name, "found": False}
			_link_bosta_delivery(shipment, remote)
			frappe.db.commit()
	return _create_delivery_note(shipment, order.name) | {"found": True}


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


def _validate_deliverable(order):
	"""Reject before inserting a shipment or calling Bosta (H2)."""
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


def _current_cod(order, expected_cod):
	cod = _cod_breakdown(order)["cod"]
	if expected_cod is not None and flt(expected_cod, 2) != cod:
		frappe.throw(
			_(
				"The COD amount changed from {0} to {1} since the dialog was opened. Reopen the dialog and check it."
			).format(flt(expected_cod, 2), cod)
		)
	return cod


def _active_shipment(sales_order):
	name = frappe.db.get_value(
		"Bosta Shipment", {"sales_order": sales_order, "status": ["!=", "Canceled"]}, "name"
	)
	return frappe.get_doc("Bosta Shipment", name) if name else None


def _resume_existing(shipment, order, expected_cod=None, details=None):
	if shipment.status in ("Delivery Note Failed", "Bosta Created") and shipment.bosta_delivery_id:
		frappe.db.commit()
		return _create_delivery_note(shipment, order.name)
	if shipment.status == "Bosta Failed" and not shipment.tracking_number:
		cod = _current_cod(order, expected_cod)
		frappe.db.commit()
		# The earlier attempt may have reached Bosta (e.g. a timeout): look first.
		remote = _find_remote_delivery(shipment)
		if remote:
			lock_and_load_sales_order(order.name)
			shipment.reload()
			_link_bosta_delivery(shipment, remote)
			frappe.db.commit()
			return _create_delivery_note(shipment, order.name)
		lock_and_load_sales_order(order.name)
		shipment.reload()
		# Nothing reached Bosta, so the operator may correct the receiver or address.
		shipment.update(details or {})
		shipment.status = "Pending Bosta"
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

	_link_bosta_delivery(shipment, data)
	frappe.db.commit()
	return _create_delivery_note(shipment, sales_order)


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
	"""The MVP sends only Bosta's required fields and approved exceptions."""
	settings = frappe.get_single("Bosta Settings")
	drop_off_address = {"city": shipment.city, "firstLine": shipment.address_first_line}
	if cstr(shipment.district_id).strip():
		drop_off_address["districtId"] = shipment.district_id
	else:
		drop_off_address.update({"cityId": shipment.city_id, "districtName": shipment.district_name})

	return {
		"type": BOSTA_DELIVERY_TYPE,
		"cod": cod,
		"receiver": {"firstName": shipment.receiver_name, "phone": shipment.receiver_phone},
		"dropOffAddress": drop_off_address,
		"businessLocationId": settings.business_location_id,
		# businessReference is the field Bosta documents for searching; keep it identical
		# to the unique reference so a stuck shipment can be found again.
		"businessReference": shipment.unique_business_reference,
		"uniqueBusinessReference": shipment.unique_business_reference,
	}


def _create_delivery_note(shipment, sales_order):
	"""Create and submit the Delivery Note inside a savepoint, under the Sales Order lock."""
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
		}
	frappe.db.savepoint(DELIVERY_NOTE_SAVEPOINT)
	# The caller's Bosta role was checked up front; the document work runs as that user
	# with permission checks skipped (they remain the Delivery Note's owner).
	try:
		delivery_note = make_delivery_note(sales_order)
		if not delivery_note.items:
			frappe.throw(_("The Sales Order has no remaining quantity to deliver."))
		delivery_note.insert(ignore_permissions=True)
		delivery_note.flags.ignore_permissions = True
		delivery_note.submit()
	except Exception as exc:
		rollback_to_savepoint(DELIVERY_NOTE_SAVEPOINT)
		frappe.log_error(frappe.get_traceback(with_context=False), "Bosta Delivery Note creation failed")
		shipment.status = "Delivery Note Failed"
		shipment.last_error = _("Delivery Note creation failed. See Error Log for details.")
		shipment.delivery_note_error = _("Delivery Note creation failed: {0}").format(
			frappe.utils.strip_html(cstr(exc))[:300]
		)
		save_as_system(shipment)
		frappe.db.commit()
		raise

	shipment.delivery_note = delivery_note.name
	shipment.status = "Completed"
	shipment.last_error = ""
	shipment.delivery_note_error = ""
	save_as_system(shipment)
	frappe.db.release_savepoint(DELIVERY_NOTE_SAVEPOINT)
	frappe.db.commit()
	return {
		"shipment": shipment.name,
		"delivery_note": delivery_note.name,
		"tracking_number": shipment.tracking_number,
	}


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
