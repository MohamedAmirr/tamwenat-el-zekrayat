# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Edit a Shopify Sales Order before the carrier picks the parcel up (epic #502, issue #500 part 2).

"Edit Order" (``api/marketplace_outbound.py``) REPLACES ERPNext's "Update Items" for Shopify orders. Staff change
quantities (0 removes a row) and add items (rate = the Item Price of the Marketplace Account's ERPNext Selling
Price List, refused before Shopify is called when there is none; after the commit the re-baseline queues a planner
update to Shopify's price, ``rebaseline_after_outbound_edit(correct_rates=True)``). Rate edits of existing rows are
out of scope. The sequence, with no row lock held across the Shopify call:

1. ``get_edit_preview``: no network, no lock. Stage lock, the #356 Shopify checks on the PROPOSED rows
   (``marketplace_outbound._shopify_edit_preview``), the fingerprint staff confirm.
2. ``_dry_run``: for a reserved (Confirmed) order, the extra stock the new quantities need against the free stock,
   under the Bin locks, committed before the next step. A quantity increase without stock is refused HERE.
3. ``marketplace_outbound.send_shopify_order_edit``: Marketplace Order lock, marker ``in_flight`` committed, Shopify
   called, marker ``completed`` committed.
4. ``apply_edit`` (ONE transaction): ERPNext's ``update_child_qty_rate``, the draft Delivery Note rows matched by
   ``so_detail`` (when a draft note exists), the reservation re-check. If this fails Shopify still holds the edit:
   nothing of the transaction stays, the marker stays ``completed``, the order is re-imported with the OLD baseline
   and the planner brings ERPNext to Shopify's state (Retry Sync does it at once).
5. Re-import and re-baseline (unchanged from #356).

Stages. "Picked up and later" means a SUBMITTED Delivery Note exists for the order: a Bosta note is submitted by
the pickup event (#500 part 1) and a plain or manual note by the person, so a submitted note is exactly the stages
Picked Up, In Transit, Delivered and Returned / Failed (``delivery_note_workflow.STATE_DOCSTATUS``). Any such edit is
refused for everyone, no override role. While the draft note has a started carrier shipment (Shipment Requested or
Created) edits are ALLOWED (user decision). With a started BOSTA shipment (issue #501) Bosta can only update the COD,
not the items, the parcel description or the label: the edit first sends the new COD (``cod_plan``,
``_update_bosta_cod``: ``integrations/bosta/editing.update_cod``; old value kept, so reversible), then Shopify, then
ERPNext. If Shopify rejects the edit the old COD is sent back (``_restore_bosta_cod``); if that fails too, or an
answer was unknown, the note is listed in Needs Attention as "Bosta COD Differs" with Retry. A stored label is
requested again after each COD update. Only the shipment status Created is edited this way; a shipment that is
Creating, Unknown or being cancelled refuses the edit, one Bosta cancelled changes the note's rows only. Whenever
the rows of an editable draft note change, its ``custom_cod_confirmed`` is reset so the person confirms the COD
again.

Draft Delivery Notes (the form's and the bulk path's are the same document). Edit Order, and a change that comes
from Shopify, are REFUSED (before anything is sent; the planner holds the change as Needs Attention, reason
``ORDER_LOCKED_BY_SHIPMENT``) when the order has more than one draft note or a draft that does not ship the whole
order, with a message naming the note (``draft_note_problem``). A Bosta shipment that is Creating or Unknown holds
a change that comes from Shopify the same way (``update_hold_reason``), because the COD to send is not known.

Hooks (``hooks.py``): the Sales Order Item hooks refuse any item, quantity or rate edit after pickup (Shopify
orders only, also when the edit comes from Shopify), and refuse every edit before pickup that is not Edit Order
(``apply_edit`` sets ``_EDIT_FLAG``) or Shopify's own (``marketplace_syncing_orders``), so ERPNext's
``update_child_qty_rate`` called directly is refused; they note which Confirmed orders had rows changed; the Sales
Order ``on_update_after_submit`` hook then refuses an edit that would leave the reservation partial, judged on the
final rows (``update_child_qty_rate`` changes the rows first and reserves last). An edit that comes from Shopify
itself (the planner sets ``frappe.flags.marketplace_syncing_orders``) is never refused for stock: Shopify is right,
the order moves to Waiting for Stock.

ERPNext behaviour relied on (``docs/frappe-best-practices.md``, Version compatibility): ``update_child_qty_rate``
as in ``docs/frappe-conventions.md``, and ``make_delivery_note(kwargs={"filtered_children": [...]})`` for the rows a
draft note lacks.
"""

import html

import frappe
from frappe import _
from frappe.utils import cint, cstr, escape_html, flt, strip_html_tags

from marketplace_integrations.services import carrier_log, order_reservation
from marketplace_integrations.services import marketplace_outbound as outbound
from marketplace_integrations.services.carrier_log import EDITABLE_STATUSES
from marketplace_integrations.services.noon_item_edit_guard import row_changed

PLATFORM = "Shopify"
SKIP_FLAG = "skip_carrier_shipment"  # services/carrier_shipments.SKIP_FLAG (not imported: it pulls the adapters)
_ROWS_CHANGED_FLAG = "marketplace_order_rows_changed"
#: Set (to the Sales Order name) by ``apply_edit`` around ``update_child_qty_rate``: the row hooks refuse every other
#: item, quantity or rate edit of a submitted Shopify order that does not come from Shopify itself.
_EDIT_FLAG = "marketplace_order_edit_in_progress"
_NOTE_ROW_SKIP = ("name", "parent", "parentfield", "parenttype", "idx", "docstatus", "creation", "modified", "modified_by", "owner")


# ---------------------------------------------------------------------------
# Stage lock
# ---------------------------------------------------------------------------


def is_shopify_order(marketplace_order):
	return bool(marketplace_order) and frappe.get_cached_value("Marketplace Order", marketplace_order, "platform") == PLATFORM


def _notes(sales_order):
	"""The non-cancelled Delivery Notes of the order (system read: a note the user cannot read still locks it)."""
	parents = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": sales_order, "docstatus": ["<", 2]},
		pluck="parent",
		distinct=True,
		limit_page_length=0,
	)
	if not parents:
		return []
	return frappe.get_all(
		"Delivery Note",
		filters={"name": ["in", parents], "docstatus": ["<", 2]},
		fields=["name", "docstatus", "is_return", "custom_integration_status"],
		limit_page_length=0,
	)


def picked_up_note(sales_order):
	"""The submitted Delivery Note of the order (stages Picked Up and later), else ``None``."""
	return next((note.name for note in _notes(sales_order) if note.docstatus == 1), None)


def picked_up_message(note):
	return _(
		"The carrier already has this order (Delivery Note {0}), so its items can no longer be changed."
	).format(note)


def lock_reason(sales_order):
	"""Why the items of the order cannot be changed now (the stage lock), or ``None``."""
	submitted = picked_up_note(sales_order)
	return picked_up_message(submitted) if submitted else None


def started_shipment_note(sales_order):
	"""The draft Delivery Note whose carrier shipment is started (Integration Status not Not Created / Creation
	Failed), else ``None``. An edit is allowed then; a Bosta shipment gets the new COD (``cod_plan``, issue #501)."""
	return next(
		(
			note.name
			for note in _notes(sales_order)
			if note.docstatus == 0 and cstr(note.custom_integration_status) not in EDITABLE_STATUSES
		),
		None,
	)


def _bosta_note(sales_order):
	"""The draft Delivery Note with a started shipment when it is a Bosta shipment, else ``None``."""
	name = started_shipment_note(sales_order)
	if not name:
		return None
	from marketplace_integrations.integrations.bosta import delivery_note_shipment as bosta

	doc = frappe.get_doc("Delivery Note", name)
	return doc if bosta.applies_to(doc) else None


def _whole_order_note(note, sales_order):
	"""Whether the note ships every undelivered unit of the order (its rows matched by ``so_detail``) and nothing of
	another order."""
	if any(row.against_sales_order and row.against_sales_order != sales_order.name for row in note.items):
		return False
	shipped = {}
	for row in note.items:
		shipped[row.so_detail] = shipped.get(row.so_detail, 0) + flt(row.qty)
	return all(
		flt(shipped.get(row.name, 0), 6) == flt(flt(row.qty) - flt(row.delivered_qty), 6)
		for row in sales_order.items
		if not row.delivered_by_supplier
	)


def draft_note_problem(sales_order):
	"""Why the items of the order cannot be changed because of its draft Delivery Notes (plain text naming the
	note), else ``None``: more than one draft note, or a draft that does not ship the whole order (or also holds
	items of another order). Edit Order and a Shopify-originated change both refuse then, before anything is sent:
	the note's rows are brought in line with the order by ``refresh_draft_note``, which must never guess for a
	partial note. The same for a note made on the form and one made by the bulk path."""
	drafts = [note.name for note in _notes(sales_order) if note.docstatus == 0 and not note.is_return]
	if not drafts:
		return None
	if len(drafts) > 1:
		return _(
			"Sales Order {0} has more than one draft Delivery Note ({1}), so its items cannot be changed here. "
			"Keep one Delivery Note for the whole order first."
		).format(sales_order, ", ".join(drafts))
	note = frappe.get_doc("Delivery Note", drafts[0])
	details = [row.so_detail for row in note.items if row.so_detail]
	if len(details) != len(set(details)):
		# One order row split over several note rows (batches): refresh_draft_note sets each row to the full
		# pending quantity, which would double it.
		return _(
			"Delivery Note {0} ships one item of the order in more than one row (for example by batch), so the items "
			"of the order cannot be changed here. Merge those rows into one first."
		).format(drafts[0])
	if not _whole_order_note(note, frappe.get_doc("Sales Order", sales_order)):
		return _(
			"Delivery Note {0} does not ship the whole order on its own, so the items of the order cannot be changed "
			"here. Make it ship the whole order, or delete it, first."
		).format(drafts[0])
	return None


def cod_plan(sales_order, edit, reasons, sent=None):
	"""The Bosta COD update this edit needs (issue #501), or ``None`` when there is nothing to send: no draft Delivery
	Note with a started Bosta shipment, or one whose shipment Bosta cancelled. Returns ``_dict(note, old, new,
	label)`` (``label``: a label is stored and will be requested again). Whatever forbids the edit while a Bosta
	shipment is started is appended to ``reasons`` and ``None`` is returned: the shipment result is not known yet,
	Bosta does not allow editing in its current state, the COD already differs from Bosta's, the note ships only part
	of the order, or the new COD is over Bosta's limit. The new COD is the COD rule (``cod_from_totals``) on the
	totals the order WILL have, calculated on a copy that is never saved. No network.

	``sent``: the plan whose COD ``edit_order`` has ALREADY sent to Bosta. The re-check under the Marketplace Order lock
	then sees the snapshot at ``sent.new`` against ``custom_cod_amount`` at ``sent.old`` (that update wrote the snapshot
	only): that is this edit's own difference, not a "COD differs" blocker."""
	note = _bosta_note(sales_order.name)
	if not note:
		return None
	from marketplace_integrations.integrations.bosta import delivery_note_shipment as bosta
	from marketplace_integrations.integrations.bosta import editing

	status = cstr(note.custom_integration_status)
	if status == carrier_log.CANCELLED:
		return None
	if status != carrier_log.CREATED:
		reasons.append(
			_(
				"The Bosta shipment of Delivery Note {0} is {1}. Wait for the result, or check it with Bosta, before editing the order."
			).format(note.name, _(status))
		)
		return None
	blocker = editing.cod_edit_blocker(note)
	if blocker:
		reasons.append(blocker)
		return None
	own_update = (
		sent is not None
		and editing.snapshot_cod(note.custom_carrier_request_snapshot) == flt(sent.new, 2)
		and flt(note.custom_cod_amount, 2) == flt(sent.old, 2)
	)
	if not own_update and editing.cod_differs(note.custom_cod_amount, note.custom_carrier_request_snapshot):
		reasons.append(
			_(
				"The COD of Delivery Note {0} differs from the COD Bosta has. Fix it first (Needs Attention, Bosta COD Differs)."
			).format(note.name)
		)
		return None
	if not _whole_order_note(note, sales_order):
		reasons.append(
			_("Delivery Note {0} ships only part of the order, so its Bosta COD cannot be updated by an edit.").format(
				note.name
			)
		)
		return None
	proposed = _proposed(sales_order, edit)
	proposed.calculate_taxes_and_totals()
	values = frappe.db.get_value("Sales Order", sales_order.name, ["advance_paid"], as_dict=True)
	new = bosta.cod_from_totals(
		flt(proposed.grand_total),
		{
			"advance_paid": values.advance_paid,
			"rounded_total": proposed.rounded_total,
			"grand_total": proposed.grand_total,
		},
	)
	try:
		bosta._check_cod(new)
	except frappe.ValidationError as exc:
		frappe.clear_messages()
		reasons.append(html.unescape(strip_html_tags(cstr(exc))).strip())
		return None
	return frappe._dict(
		note=note.name,
		old=flt(note.custom_cod_amount, 2),
		new=new,
		label=bool(note.custom_shipping_label),
	)


def _cod_warnings(plan):
	"""What the dialog says about Bosta (plain text): the COD that will be sent and the label that follows it."""
	if not plan or plan.new == plan.old:
		return []
	warnings = [
		_("Bosta COD of Delivery Note {0}: {1} to {2}. Bosta is updated first.").format(
			plan.note, f"{plan.old:.2f}", f"{plan.new:.2f}"
		)
	]
	if plan.label:
		warnings.append(_("The saved Bosta label is replaced by a new one that shows the new COD."))
	return warnings


def draft_note(sales_order):
	"""The draft, non-return Delivery Note of the order, else ``None``."""
	return next((note.name for note in _notes(sales_order) if note.docstatus == 0 and not note.is_return), None)


# ---------------------------------------------------------------------------
# Hooks (hooks.py)
# ---------------------------------------------------------------------------


def guard_row_change(doc, method=None):
	"""Sales Order Item ``before_update_after_submit``: refuse a changed row after pickup."""
	if row_changed(doc):
		_guard_row(doc)


def guard_row_add(doc, method=None):
	"""Sales Order Item ``before_insert``: refuse an added row after pickup."""
	_guard_row(doc)


def guard_row_remove(doc, method=None):
	"""Sales Order Item ``on_trash``: refuse a removed row after pickup."""
	_guard_row(doc)


def _guard_row(row):
	"""Refuse the edit once a submitted Delivery Note exists, and refuse any edit that does not come from Edit Order
	(``apply_edit`` sets ``_EDIT_FLAG``) or from Shopify itself (the planner: ``marketplace_syncing_orders``), so
	ERPNext's ``update_child_qty_rate`` called directly cannot change what Shopify does not know. Otherwise remember
	the order so the Sales Order hook judges its reservation. Only rows of a submitted Shopify Sales Order are
	looked at."""
	if row.get("parenttype") != "Sales Order" or not row.get("parent"):
		return
	values = frappe.db.get_value("Sales Order", row.parent, ["docstatus", "custom_marketplace_order"], as_dict=True)
	if not values or values.docstatus != 1 or not is_shopify_order(values.custom_marketplace_order):
		return
	note = picked_up_note(row.parent)
	if note:
		frappe.throw(picked_up_message(note), title=_("Order items are locked"))
	if frappe.flags.get(_EDIT_FLAG) != row.parent and values.custom_marketplace_order not in (
		frappe.flags.get("marketplace_syncing_orders") or ()
	):
		frappe.throw(
			_("The items of a Shopify order are changed with Edit Order, which sends the change to Shopify first."),
			title=_("Order items are locked"),
		)
	frappe.flags.setdefault(_ROWS_CHANGED_FLAG, set()).add(row.parent)


def guard_reservation(doc, method=None):
	"""Sales Order ``on_update_after_submit``: when item rows of a fully reserved (Confirmed) Shopify order were
	changed in this request, refuse the edit if the final rows would need more stock than is free. Not applied to an
	edit that comes from Shopify (the planner): that order moves to Waiting for Stock instead."""
	changed = frappe.flags.get(_ROWS_CHANGED_FLAG)
	if not changed or doc.name not in changed:
		return
	changed.discard(doc.name)
	if doc.docstatus != 1 or doc.get("custom_marketplace_order") in (frappe.flags.get("marketplace_syncing_orders") or ()):
		return
	if frappe.db.get_value("Sales Order", doc.name, order_reservation.STAGE_FIELD) != order_reservation.CONFIRMED:
		return
	short = order_reservation.edit_shortfalls(doc, {})
	if short:
		frappe.throw(
			_("The order would no longer be fully reserved: {0}").format(order_reservation._describe_shortfall(short)),
			title=_("Not enough stock"),
		)


# ---------------------------------------------------------------------------
# Preview (no network, no lock)
# ---------------------------------------------------------------------------


def _not_applicable():
	return {"state": outbound.NOT_APPLICABLE, "changes": [], "reasons": [], "rows": []}


def get_edit_preview(sales_order, items=None):
	"""What an edit would do. ``items`` is ``[{"docname", "qty"}]``; without it only the editable rows and the
	reasons that already forbid any edit are returned (the dialog's first call)."""
	sales_order = outbound._as_sales_order(sales_order)
	marketplace_order = sales_order.get("custom_marketplace_order")
	if not is_shopify_order(marketplace_order):
		return _not_applicable()
	order = frappe.get_doc("Marketplace Order", marketplace_order)
	edit = _parse_items(sales_order, items) if items is not None else frappe._dict(qty={}, add=[])
	return _preview(sales_order, edit, order, listing=items is None)


def _parse_items(sales_order, items):
	"""``items`` ``[{"docname", "item_code", "qty"}]`` -> ``{"qty": {row name: qty}, "add": [{"item_code", "qty"}]}``.
	An entry with a ``docname`` is an existing row (its item cannot change); one without is an added item
	(quantity 0 is ignored). Rows not listed keep their quantity."""
	items = frappe.parse_json(items) if isinstance(items, str) else items
	if not isinstance(items, list):
		frappe.throw(_("Send the item quantities as a list."))
	rows = {row.name: row for row in sales_order.items}
	edit = frappe._dict(qty={}, add=[])
	taken = {row.item_code for row in sales_order.items}
	for entry in items:
		if not isinstance(entry, dict):
			frappe.throw(_("Each item must have a row name or an item code and a quantity."))
		name = cstr(entry.get("docname"))
		item_code = cstr(entry.get("item_code")).strip()
		qty = flt(entry.get("qty"))
		if qty < 0:
			frappe.throw(_("Item quantities cannot be negative."))
		if name:
			if name not in rows:
				frappe.throw(
						_("{0} is not an item row of this Sales Order.").format(frappe.bold(escape_html(name)))
					)
			if item_code and item_code != rows[name].item_code:
				frappe.throw(
					_("The item of row {0} cannot be changed. Set its quantity to 0 and add the other item.").format(
						frappe.bold(rows[name].idx)
					)
				)
			edit.qty[name] = qty
		elif qty > 0:
			if not item_code:
				frappe.throw(_("Choose the item of the new row."))
			if item_code in taken:
				frappe.throw(_("Item {0} is already in the order. Change its quantity instead.").format(escape_html(item_code)))
			taken.add(item_code)
			edit.add.append(frappe._dict(item_code=item_code, qty=qty))
	return edit


def _resolve_added(order, edit):
	"""Fill the added rows (item name, stock UOM, the account's warehouse and the rate = the current Item Price of
	the Marketplace Account's ERPNext Selling Price List; the Shopify re-import corrects it after the commit).
	Returns the reasons that forbid the edit (no price list, no price): refused before Shopify is called."""
	reasons = []
	if not edit.add:
		return reasons
	from marketplace_integrations.services.listing_price import CatalogBuildError, resolve_listing_price

	account = frappe.get_cached_doc("Marketplace Account", order.marketplace_account)
	if not account.erpnext_selling_price_list:
		return [_("The Marketplace Account has no ERPNext Selling Price List, so a new item cannot be priced.")]
	for row in edit.add:
		item = frappe.get_cached_doc("Item", row.item_code)
		row.item_name, row.uom, row.warehouse = item.item_name, item.stock_uom, account.warehouse
		try:
			row.rate = flt(resolve_listing_price(account.erpnext_selling_price_list, item, item.stock_uom))
		except CatalogBuildError as exc:
			reasons.append(html.unescape(strip_html_tags(cstr(exc))).strip())
			row.rate = 0
	return reasons


def _proposed(sales_order, edit):
	"""A copy of the Sales Order, never saved, with the proposed quantities (a row at 0 is dropped) and the added
	rows."""
	proposed = frappe.get_doc("Sales Order", sales_order.name)
	for row in list(proposed.items):
		qty = flt(edit.qty.get(row.name, row.qty))
		if qty <= 0:
			proposed.remove(row)
			continue
		row.qty = qty
		row.amount = flt(row.rate) * qty
	for row in edit.add:
		proposed.append(
			"items",
			{
				"item_code": row.item_code,
				"item_name": row.get("item_name"),
				"uom": row.get("uom"),
				"conversion_factor": 1,
				"qty": row.qty,
				"rate": row.get("rate"),
				"amount": flt(row.get("rate")) * row.qty,
				"warehouse": row.get("warehouse"),
			},
		)
	return proposed


def _preview(sales_order, edit, order, listing=False, sent=None):
	"""The #356 preview of the proposed rows plus the stage lock and the draft note rule; ``listing`` drops "no
	changes". ``sent``: the Bosta COD plan already sent (see ``cod_plan``)."""
	reasons = []
	locked = lock_reason(sales_order.name)
	if locked:
		reasons.append(locked)
	note_problem = draft_note_problem(sales_order.name)
	if note_problem:
		reasons.append(note_problem)
	reasons += _resolve_added(order, edit)
	proposed = _proposed(sales_order, edit)
	if not proposed.items:
		reasons.append(_("Removing every item would cancel the order. Cancel the order instead."))
	preview = outbound._shopify_edit_preview(proposed, order, draft_note_allowed=True)
	no_changes = _("There are no item changes to send to Shopify.")
	reasons += [reason for reason in preview["reasons"] if not (listing and reason == no_changes)]
	plan = None if note_problem else cod_plan(sales_order, edit, reasons, sent=sent)
	return {
		**preview,
		"state": outbound.BLOCKED if reasons else outbound.ELIGIBLE,
		"reasons": reasons,
		"warnings": _cod_warnings(plan),
		"cod": plan,
		"rows": [
			{"docname": row.name, "item_code": row.item_code, "item_name": row.item_name, "qty": flt(row.qty)}
			for row in sales_order.items
		],
	}


# ---------------------------------------------------------------------------
# Edit Order
# ---------------------------------------------------------------------------


def edit_order(sales_order, items, fingerprint):
	"""Validate, send to Shopify, then change ERPNext in one transaction (module docstring). Commits."""
	sales_order = outbound._as_sales_order(sales_order)
	marketplace_order = sales_order.get("custom_marketplace_order")
	if not is_shopify_order(marketplace_order):
		frappe.throw(_("Sales Order {0} is not linked to a Shopify order.").format(sales_order.name))
	new_qty = _parse_items(sales_order, items)
	order = frappe.get_doc("Marketplace Order", marketplace_order)
	preview = _preview(sales_order, new_qty, order)  # also prices the added rows (new_qty.add)
	if preview["state"] != outbound.ELIGIBLE:
		frappe.throw(" ".join(preview["reasons"]) or _("No Shopify item changes are ready to send."))
	if cstr(fingerprint).strip() != preview["fingerprint"]:
		frappe.throw(_("The item changes are not the ones you reviewed. Review them again."))
	_dry_run(sales_order.name, new_qty)
	plan = preview.get("cod")
	sent = bool(plan) and plan.new != plan.old
	if sent:
		_update_bosta_cod(plan)
	try:
		result = outbound.send_shopify_order_edit(
			sales_order.name,
			preview["fingerprint"],
			# the re-check under the lock must not read the COD this edit just sent as "differs"
			preview_fn=lambda doc, locked_order: _preview(doc, new_qty, locked_order, sent=plan if sent else None),
			apply_fn=lambda: apply_edit(sales_order.name, new_qty, cod=plan.new if plan else None),
		)
		if sent and not result.get("applied"):
			# Shopify took the edit and Bosta holds the new COD, but ERPNext could not follow (apply_edit rolled back):
			# keep ``custom_cod_amount`` equal to what Bosta has, or "Bosta COD Differs" would offer a Retry that sends
			# the old COD back. The planner brings the order, and the COD rule, to Shopify's state afterwards.
			frappe.db.set_value("Delivery Note", plan.note, "custom_cod_amount", plan.new, update_modified=False)
			frappe.db.commit()
		return result
	except Exception:
		if sent and not frappe.db.get_value("Marketplace Order", marketplace_order, "outbound_edit_fingerprint"):
			# Shopify did not take the edit (no marker is left: it is set before the call and kept when Shopify may
			# hold the edit): put the old COD back at Bosta.
			_restore_bosta_cod(plan)
		raise


def _update_bosta_cod(plan):
	"""Step 1 of an edit with a started Bosta shipment: the COD goes to Bosta first, because it can be put back.
	Anything but a confirmed update stops the edit before Shopify or ERPNext is touched."""
	from marketplace_integrations.integrations.bosta import editing

	result = editing.update_cod(plan.note, plan.new)
	if result.outcome == "updated":
		return
	if result.outcome == "failed":
		frappe.throw(
			_("Bosta did not accept the new COD: {0}. Nothing was changed.").format(escape_html(result.message)),
			title=_("Bosta COD"),
		)
	frappe.throw(
		_(
			"Could not confirm that Bosta updated the COD. Shopify and the order were not changed. Delivery Note {0} "
			"is listed in Needs Attention as Bosta COD Differs: Retry there puts the old COD back."
		).format(plan.note),
		title=_("Bosta COD"),
	)


def _restore_bosta_cod(plan):
	"""Shopify did not take the edit: send the old COD back to Bosta. If that does not work either, the
	difference stays listed in Needs Attention (Bosta COD Differs) with Retry, and the user is told."""
	from marketplace_integrations.integrations.bosta import editing

	try:
		result = editing.update_cod(plan.note, plan.old)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Bosta COD could not be restored", message=f"{plan.note}\n{frappe.get_traceback()}")
		result = None
	if not result or result.outcome != "updated":
		frappe.msgprint(
			_(
				"Bosta still has the new COD of Delivery Note {0}. It is listed in Needs Attention as Bosta COD "
				"Differs: use Retry there to put {1} back."
			).format(plan.note, f"{plan.old:.2f}"),
			indicator="red",
			alert=False,
		)


def _short_message(short):
	return _("Not enough free stock to keep the whole order reserved: {0}").format(
		order_reservation._describe_shortfall(short)
	)


def _dry_run(sales_order, new_qty):
	"""Refuse, before Shopify is called, a quantity increase the stock cannot cover (a Confirmed order only: a
	waiting order has nothing reserved to lose). Commits before the locks and again right after, so no lock is
	held across the Shopify call."""
	if frappe.db.get_value("Sales Order", sales_order, order_reservation.STAGE_FIELD) != order_reservation.CONFIRMED:
		return
	frappe.db.commit()
	frappe.db.get_value("Sales Order", sales_order, "name", for_update=True)
	short = order_reservation.edit_shortfalls(
		frappe.get_doc("Sales Order", sales_order), new_qty.qty, added=new_qty.add, lock=True
	)
	frappe.db.commit()
	if short:
		frappe.throw(_short_message(short), title=_("Not enough stock"))


def apply_edit(sales_order, new_qty, cod=None):
	"""The local transaction after Shopify accepted: locks (committed before, nothing between the commit and the
	first lock), ``update_child_qty_rate``, the draft Delivery Note rows, the reservation re-check.
	``cod``: the COD already sent to Bosta, written to the draft note (#501). Commits at the end; raises (the
	caller rolls back) on any refusal.

	Lock order: Sales Order row, then the draft Delivery Note row, then the Bins. The pickup submit
	(``carrier_shipments._submit_created``) takes the same order, so an edit and a pickup submit of the same order
	wait for each other instead of deadlocking."""
	from erpnext.controllers.accounts_controller import update_child_qty_rate

	frappe.db.commit()
	frappe.db.get_value("Sales Order", sales_order, "name", for_update=True)
	draft = draft_note(sales_order)
	if draft:
		frappe.db.get_value("Delivery Note", draft, "name", for_update=True)
	order = frappe.get_doc("Sales Order", sales_order)
	reason = lock_reason(order.name) or draft_note_problem(order.name)
	if reason:
		frappe.throw(reason)
	stage = order.get(order_reservation.STAGE_FIELD)
	short = order_reservation.edit_shortfalls(order, new_qty.qty, added=new_qty.add, lock=True)
	if short and stage == order_reservation.CONFIRMED:
		frappe.throw(_short_message(short), title=_("Not enough stock"))
	frappe.flags[_EDIT_FLAG] = order.name
	try:
		update_child_qty_rate("Sales Order", frappe.as_json(_trans_items(order, new_qty)), order.name)
	finally:
		frappe.flags.pop(_EDIT_FLAG, None)
	refresh_draft_note(order.name, cod=cod)
	if stage == order_reservation.CONFIRMED:
		order_reservation.reserve_or_wait(order.name, refuse_partial=True)
	elif stage == order_reservation.WAITING_FOR_STOCK:
		# A held order (Shopify raised a quantity the stock could not cover) keeps what it has reserved: a shortfall
		# here releases nothing.
		order_reservation.reserve_or_wait(order.name, keep_reservation=True)
	frappe.db.commit()


def _trans_items(order, new_qty):
	"""``update_child_qty_rate`` rows; a row at 0 is left out, which makes ERPNext delete it. ``description`` is
	sent because ERPNext assigns it from the payload and would otherwise blank it. The rate is the row's own
	(rate edits are out of scope)."""
	rows = [
		{
			"docname": row.name,
			"item_code": row.item_code,
			"description": row.description,
			"qty": flt(new_qty.qty.get(row.name, row.qty)),
			"rate": flt(row.rate),
			"uom": row.uom,
			"conversion_factor": row.conversion_factor,
			"delivery_date": cstr(row.delivery_date) if row.delivery_date else None,
		}
		for row in order.items
		if flt(new_qty.qty.get(row.name, row.qty)) > 0
	]
	# An added row has no docname: ERPNext inserts it. Its rate is the Item Price resolved at the preview.
	rows += [
		{
			"item_code": row.item_code,
			"qty": row.qty,
			"rate": row.rate,
			"uom": row.uom,
			"conversion_factor": 1,
			"warehouse": row.warehouse,
			"delivery_date": cstr(order.delivery_date),
		}
		for row in new_qty.add
	]
	return rows


# ---------------------------------------------------------------------------
# Draft Delivery Note (the form's note and the bulk path's note are the same document)
# ---------------------------------------------------------------------------


def refresh_draft_note(sales_order, cod=None):
	"""Bring the draft Delivery Note of the order (if any) in line with the Sales Order: quantities of its rows
	matched by ``so_detail``, rows whose Sales Order row is gone removed, rows it lacks added through ERPNext's own
	mapper. Rows without ``so_detail`` are left alone. A note without a draft does nothing (a person or the bulk
	path creates it from the current Sales Order, so this works the same for a note of either path).

	PRECONDITION: the order has ONE draft note that shipped the whole order before this change
	(``draft_note_problem`` is None; ``apply_edit`` checks it under the locks, the planner path through
	``update_hold_reason`` before it applies anything). Only then is it right to set a row to the full pending
	quantity and to add the rows the note lacks; a partial or second draft is never touched (the change is refused
	or held before it gets here). The COD of an
	editable draft Bosta note is not recomputed once confirmed: its confirmation is reset instead, so the person
	confirms the COD again. The COD of a draft Bosta note whose shipment is started (#501) is set to ``cod`` (what
	the edit already sent to Bosta) or, when the change comes from Shopify, to the COD rule on the new totals; Bosta
	is not called here, so for the second case the difference is listed in Needs Attention (Bosta COD Differs) with
	Retry. Returns the note name or ``None``."""
	name = draft_note(sales_order)
	if not name:
		return None
	order = frappe.get_doc("Sales Order", sales_order)
	pending = {
		row.name: flt(row.qty) - flt(row.delivered_qty) for row in order.items if not row.delivered_by_supplier
	}
	sales_rows = {row.name: row for row in order.items}
	note = frappe.get_doc("Delivery Note", name)
	changed = False
	for row in list(note.items):
		if not row.so_detail:
			continue
		qty = pending.get(row.so_detail, 0)
		if qty <= 0:
			note.remove(row)
			changed = True
		else:
			source_rate = flt(sales_rows[row.so_detail].rate)
			if flt(row.qty) != qty:
				row.qty = qty
				changed = True
			if flt(row.rate) != source_rate:
				row.rate = source_rate
				changed = True
	present = {row.so_detail for row in note.items}
	missing = [row_name for row_name, qty in pending.items() if qty > 0 and row_name not in present]
	if missing:
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		for mapped in make_delivery_note(sales_order, kwargs={"filtered_children": missing}).items:
			note.append("items", {key: value for key, value in mapped.as_dict().items() if key not in _NOTE_ROW_SKIP})
		changed = True
	if not changed:
		return name
	if not note.items:
		frappe.throw(_("Delivery Note {0} would have no items left.").format(name))
	for idx, row in enumerate(note.items, start=1):
		row.idx = idx
	note.flags.ignore_permissions = True
	started = cstr(note.get("custom_integration_status")) not in EDITABLE_STATUSES
	if started:
		# The carrier shipment is started: the carrier adapter would refuse a row change. The rows follow the
		# order anyway (decision, #500); only that guard is skipped. Bosta gets the COD only (#501, see above).
		note.flags[SKIP_FLAG] = True
	elif cint(note.get("custom_cod_confirmed")):
		# The items changed, so the COD must be confirmed again (single form and bulk path both require it
		# before a shipment is created; a draft whose rows an edit changed is never re-sent by the bulk path).
		note.custom_cod_confirmed = 0
	note.save()
	if started and cstr(note.custom_integration_status) == carrier_log.CREATED:
		from marketplace_integrations.integrations.bosta import delivery_note_shipment as bosta

		if bosta.applies_to(note):
			new_cod = bosta.default_cod(note) if cod is None else flt(cod, 2)
			note.db_set("custom_cod_amount", new_cod, update_modified=False)
	return name


# ---------------------------------------------------------------------------
# An edit that comes from Shopify (the reconciliation planner)
# ---------------------------------------------------------------------------


def update_hold_reason(sales_order):
	"""Why Shopify's change cannot be applied to this Sales Order now (Needs Attention, reason
	``ORDER_LOCKED_BY_SHIPMENT``), or ``None``. Shopify is right, but ERPNext cannot follow without guessing:

	- the parcel is already with the carrier (the stage lock);
	- the order has more than one draft Delivery Note, or a draft that does not ship the whole order
	  (``draft_note_problem``): the planner must not overwrite the note;
	- the draft note's Bosta shipment is Creating or Unknown: its result is not known, so the COD to send is not
	  either. The change is applied by a later Retry Sync, once the result is known."""
	if not is_shopify_order(frappe.db.get_value("Sales Order", sales_order, "custom_marketplace_order")):
		return None
	reason = lock_reason(sales_order) or draft_note_problem(sales_order)
	if reason:
		return reason
	note = _bosta_note(sales_order)
	if note and cstr(note.custom_integration_status) in (carrier_log.CREATING, carrier_log.UNKNOWN):
		return _(
			"The Bosta shipment of Delivery Note {0} is {1}, so its COD cannot be updated yet. Wait for the result, or "
			"check it with Bosta."
		).format(note.name, _(cstr(note.custom_integration_status)))
	return None


def _held_for_stock(sales_order):
	"""Whether Shopify's quantity increase may be held for stock: the order is Confirmed (reserved), or it is
	already held for an edit (a re-run of the planner must hold it again, not apply the increase). An order that
	is Waiting for Stock for another reason has nothing reserved to lose, so Shopify's change is just applied."""
	stage, marketplace_order = frappe.db.get_value(
		"Sales Order", sales_order, [order_reservation.STAGE_FIELD, "custom_marketplace_order"]
	)
	if stage == order_reservation.CONFIRMED:
		return True
	from marketplace_integrations.services import sync_reason_codes

	return stage == order_reservation.WAITING_FOR_STOCK and bool(marketplace_order) and (
		frappe.db.get_value("Marketplace Order", marketplace_order, "sync_reason_code")
		== sync_reason_codes.WAITING_FOR_STOCK_EDIT
	)


def hold_for_stock(sales_order, previous, current):
	"""Run BEFORE the planner applies Shopify's change (``sales_order_sync._reconcile_submitted_order``). When
	Shopify raised a quantity (or added a line) of a Confirmed (reserved) order and the free stock cannot cover the
	extra, apply NOTHING: the Sales Order moves to the stage Waiting for Stock with the Stock Manager ToDo (the Sales
	Order list shows it in red) and the plain text of the shortfall is returned. The caller keeps the baseline, so
	the increase is not lost; the Bin sweep re-runs the planner. Only a Confirmed order, or one already held this
	way, is looked at (``_held_for_stock``). ``None`` when there is nothing to hold (not a Shopify order, other
	stage, no increase, enough stock, reservation off). Takes the Bin locks; the caller commits."""
	if not is_shopify_order(frappe.db.get_value("Sales Order", sales_order, "custom_marketplace_order")):
		return None
	if not _held_for_stock(sales_order):
		return None
	from marketplace_integrations.services.sales_order_reconciliation import _items_by_external_id

	order = frappe.get_doc("Sales Order", sales_order)
	rows = {row.name: row for row in order.items}
	before, after = _items_by_external_id(previous) or {}, _items_by_external_id(current) or {}
	new_qty, added, increased = {}, [], False
	for external_id, line in after.items():
		old = before.get(external_id)
		qty = flt(line.get("qty"))
		if old is None:
			if qty > 0:
				added.append(frappe._dict(item_code=line.get("item_code"), qty=qty, warehouse=line.get("warehouse")))
				increased = True
		elif old.get("sales_order_item") in rows:
			new_qty[old["sales_order_item"]] = qty
			increased = increased or qty > flt(rows[old["sales_order_item"]].qty)
	if not increased:
		return None
	for external_id, old in before.items():
		if external_id not in after and old.get("sales_order_item") in rows:
			new_qty[old["sales_order_item"]] = 0
	short = order_reservation.edit_shortfalls(order, new_qty, added=added, lock=True)
	if not short:
		return None
	order_reservation.hold_for_edit(sales_order, short)
	return _short_message(short)


def after_marketplace_update(sales_order):
	"""The planner just applied Shopify's change to the Sales Order (inside its savepoint): refresh the draft note
	and settle the reservation. Shopify wins, so a shortfall moves the order to Waiting for Stock instead of
	refusing the change. A reservation failure is logged and undone alone; it never blocks the change."""
	if not is_shopify_order(frappe.db.get_value("Sales Order", sales_order, "custom_marketplace_order")):
		return
	refresh_draft_note(sales_order)
	if frappe.db.get_value("Sales Order", sales_order, order_reservation.STAGE_FIELD) not in (
		order_reservation.CONFIRMED,
		order_reservation.WAITING_FOR_STOCK,
	):
		return
	savepoint = "marketplace_order_edit_reservation"
	frappe.db.savepoint(savepoint)
	try:
		order_reservation.reserve_or_wait(sales_order)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(title="Shopify order reservation after an edit failed", message=f"{sales_order}\n{frappe.get_traceback()}")
