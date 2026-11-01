# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Move the Bosta Shipment data onto Delivery Notes and retire the old Bosta flow (epic #413, #408). Idempotent.

Runs before fixtures sync, so it creates the Custom Fields it writes itself (from the fixture file).

0. All checks run first and change nothing when they stop the patch (the old fields, columns, permissions
   and notifications stay for the next run): the DocType row without its table, step 1, and the
   ambiguity checks of step 2.
1. Every Bosta Shipment that is not Canceled must have an existing Delivery Note (one that was deleted
   counts as missing) and a Bosta delivery id (a row still being created has none). Otherwise the patch
   stops and lists all of them: drain or cancel them on the old code first, then migrate again.
2. Each shipment with a Delivery Note and a Bosta delivery id is copied onto the note (Created, ids,
   tracking number in ``lr_no``, COD confirmed, fee, state, city, label File re-attached) with one
   Integration Log row. Two shipments for one note: the non-Canceled one wins, two live ones with
   different delivery ids stop the patch (also a note that already holds another id). Rows are handled
   in ``name`` order. A second run changes nothing.
3. The Sales Order Bosta summary is recalculated for every order of a migrated note.
4. The old Sales Order Bosta delivery fields (Custom Fields, their Property Setters), the "Bosta User"
   role, the Bosta Custom DocPerms and the Bosta Notifications are deleted.

Frappe's ``remove_orphan_doctypes`` runs at the very end of migrate (after the patches) and deletes only
the DocType rows: the ``tabBosta Shipment`` tables stay, and are dropped by hand afterwards
(docs/user-actions.md). If the DocType row exists but its table does not, the patch fails loudly.
"""

import json
import os

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import cstr, flt

OLD = "Bosta Shipment"
NEW_FIELD_DOCTYPES = ("Delivery Note", "Integration Request", "Sales Order")
NEW_FIELD_NAMES = (
	"custom_shipping_",
	"custom_integration_status",
	"custom_carrier_",
	"custom_cod_",
	"custom_bosta_",
	"custom_last_",
	"custom_actual_shipping_cost",
	"custom_operation_type",
	"custom_result",
	"custom_external_reference",
	"custom_delivery_id",
	"custom_attempt_no",
	"custom_completed_at",
	"custom_http_status",
	"custom_error_code",
	"custom_correlation_id",
	"custom_dedupe_key",
)


OLD_SALES_ORDER_FIELDS = ("custom_bosta_delivery_tab", "custom_bosta_delivery_html", "custom_bosta_delivery_status")
OLD_DOCTYPES = (OLD, "Bosta Shipment Item")


def execute():
	# Every check runs before anything is changed: a stopped run must leave the old fields, columns,
	# permissions and notifications in place so it can be run again after the data is fixed. (The
	# ALTER TABLE in the retire step commits implicitly, so nothing could be rolled back later.)
	has_table = frappe.db.table_exists(OLD)
	if frappe.db.exists("DocType", OLD) and not has_table:
		raise Exception(
			f"The DocType {OLD} exists but its table is missing. Restore the table (or restore a backup) "
			"before migrating: the Bosta data would be lost."
		)
	chosen = {}
	if has_table:
		_stop_on_unmovable_shipments()
		chosen = _plan_copies()
	_retire_old_sales_order_fields()
	_retire_notifications()
	frappe.db.delete("Custom DocPerm", {"parent": ["in", list(OLD_DOCTYPES)]})
	if has_table:
		_ensure_custom_fields()
		_copy_shipments(chosen)
	_retire_role()


def _retire_old_sales_order_fields():
	"""The old Sales Order delivery tab, HTML and status fields. Deleting a Custom Field leaves its column
	behind, so the column (the old status text) is dropped too."""
	for fieldname in OLD_SALES_ORDER_FIELDS:
		frappe.delete_doc(
			"Custom Field", f"Sales Order-{fieldname}", force=True, ignore_permissions=True, ignore_missing=True
		)
	frappe.db.delete("Property Setter", {"doc_type": "Sales Order", "field_name": ["in", list(OLD_SALES_ORDER_FIELDS)]})
	for fieldname in OLD_SALES_ORDER_FIELDS:
		if frappe.db.has_column("Sales Order", fieldname):
			frappe.db.sql_ddl(f"alter table `tabSales Order` drop column `{fieldname}`")


def _retire_notifications():
	for name in frappe.get_all("Notification", filters={"document_type": ["in", list(OLD_DOCTYPES)]}, pluck="name"):
		frappe.delete_doc("Notification", name, force=True, ignore_permissions=True)
	if frappe.db.exists("Notification", "Bosta Awaiting Your Action"):
		frappe.delete_doc("Notification", "Bosta Awaiting Your Action", force=True, ignore_permissions=True)


def _ensure_custom_fields():
	path = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures", "custom_field.json")
	with open(path) as handle:
		entries = json.load(handle)
	by_doctype = {}
	for entry in entries:
		if entry["dt"] in NEW_FIELD_DOCTYPES and entry["fieldname"].startswith(NEW_FIELD_NAMES):
			field = {k: v for k, v in entry.items() if k not in ("doctype", "name", "modified", "module")}
			by_doctype.setdefault(entry["dt"], []).append(field)
	create_custom_fields(by_doctype, ignore_validate=True)


def _stop_on_unmovable_shipments():
	rows = frappe.db.sql(
		"""select s.name, s.sales_order, s.delivery_note, s.status, s.tracking_number, s.bosta_delivery_id
		from `tabBosta Shipment` s
		left join `tabDelivery Note` dn on dn.name = s.delivery_note
		where s.status != 'Canceled' and (dn.name is null or ifnull(s.bosta_delivery_id, '') = '')
		order by s.name""",
		as_dict=True,
	)
	if rows:
		lines = "\n".join(
			f"  {r.name} ({r.sales_order}, {r.status}, note {r.delivery_note or '-'}, "
			f"tracking {r.tracking_number or '-'}, delivery {r.bosta_delivery_id or '-'})"
			for r in rows
		)
		raise Exception(
			"These Bosta Shipments are not Canceled and have no existing Delivery Note or no Bosta delivery "
			"id, so they cannot be moved. Create a Delivery Note for them, let them finish, or cancel them "
			"on the old code first, then migrate again:\n" + lines
		)


def _state_name(code):
	from marketplace_integrations.integrations.bosta.statuses import BOSTA_STATE_NAMES

	return BOSTA_STATE_NAMES.get(int(code)) if code not in (None, "") else None


def _chosen_per_note(shipments):
	"""``{delivery note: shipment}``: of several shipments of one note the non-Canceled wins, then the last
	by name. Returns the choices and the notes whose shipments cannot be told apart."""
	by_note = {}
	for row in shipments:
		by_note.setdefault(row.delivery_note, []).append(row)
	chosen, ambiguous = {}, []
	for note, rows in by_note.items():
		live = [r for r in rows if r.status != "Canceled"]
		if len({r.bosta_delivery_id for r in live}) > 1:
			ambiguous.append(f"  {note}: " + ", ".join(f"{r.name} ({r.bosta_delivery_id})" for r in live))
			continue
		chosen[note] = (live or rows)[-1]
	return chosen, ambiguous


def _plan_copies():
	"""``{delivery note: shipment}`` to copy; raises (changing nothing) when a note has two different live
	delivery ids or already holds another one."""
	shipments = frappe.db.sql(
		"""select s.* from `tabBosta Shipment` s
		join `tabDelivery Note` dn on dn.name = s.delivery_note
		where ifnull(s.bosta_delivery_id, '') != ''
		order by s.name""",
		as_dict=True,
	)
	chosen, ambiguous = _chosen_per_note(shipments)
	can_hold = frappe.db.has_column("Delivery Note", "custom_carrier_delivery_id")
	for note, row in chosen.items():
		held = cstr(frappe.db.get_value("Delivery Note", note, "custom_carrier_delivery_id")) if can_hold else ""
		if held and held != cstr(row.bosta_delivery_id):
			ambiguous.append(f"  {note}: holds Bosta delivery {held}, shipment {row.name} says {row.bosta_delivery_id}")
	if ambiguous:
		raise Exception(
			"These Delivery Notes have more than one different Bosta delivery, so the right one cannot be "
			"chosen. Settle them on the old code first, then migrate again:\n" + "\n".join(ambiguous)
		)
	return chosen


def _copy_shipments(chosen):
	from marketplace_integrations.integrations.bosta import statuses

	supplier = frappe.db.get_value("Supplier", {"supplier_name": "Bosta"}, "name")
	for note, row in chosen.items():
		current = frappe.db.get_value(
			"Delivery Note", note, ["custom_carrier_delivery_id", "lr_no", "transporter"], as_dict=True
		)
		if cstr(current.custom_carrier_delivery_id) != cstr(row.bosta_delivery_id):
			values = {
				"custom_integration_status": "Cancelled" if row.status == "Canceled" else "Created",
				"custom_carrier_delivery_id": row.bosta_delivery_id,
				"custom_carrier_reference": row.unique_business_reference,
				"custom_cod_amount": flt(row.cod_amount, 2),
				"custom_cod_confirmed": 1,
				"custom_actual_shipping_cost": flt(row.shipment_fee, 2),
				"custom_shipping_status": _state_name(row.bosta_state_code),
				"custom_bosta_last_event_at": cstr(row.last_bosta_event_timestamp) or None,
				"custom_bosta_city": row.city_code,
				"custom_bosta_city_name": row.city,
				"custom_last_sync_at": row.last_bosta_status_on,
			}
			if not current.lr_no and row.tracking_number:
				values["lr_no"] = row.tracking_number
			if not current.transporter and supplier:
				values["transporter"] = supplier
			_attach_label(row, values)
			frappe.db.set_value("Delivery Note", note, values, update_modified=False)
			_log_once(row)
		statuses.update_sales_order_summary_of(note)


def _attach_label(row, values):
	files = frappe.get_all(
		"File",
		filters={"attached_to_doctype": OLD, "attached_to_name": row.name},
		fields=["name", "file_url"],
		order_by="creation desc",
	)
	for file in files:
		frappe.db.set_value(
			"File", file.name, {"attached_to_doctype": "Delivery Note", "attached_to_name": row.delivery_note}
		)
	if files:
		values["custom_shipping_label"] = files[0].file_url


def _log_once(row):
	filters = {
		"integration_request_service": "Bosta",
		"reference_doctype": "Delivery Note",
		"reference_docname": row.delivery_note,
		"custom_operation_type": "Create",
	}
	if frappe.db.exists("Integration Request", filters):
		return
	frappe.get_doc(
		{
			"doctype": "Integration Request",
			**filters,
			"status": "Completed",
			"custom_result": "Success",
			"custom_attempt_no": 1,
			"custom_external_reference": row.unique_business_reference,
			"custom_delivery_id": row.bosta_delivery_id,
			"output": f"Migrated from Bosta Shipment {row.name}",
		}
	).insert(ignore_permissions=True)


def _retire_role():
	if not frappe.db.exists("Role", "Bosta User"):
		return
	frappe.db.delete("Has Role", {"role": "Bosta User"})
	frappe.db.delete("Custom DocPerm", {"role": "Bosta User"})
	frappe.delete_doc("Role", "Bosta User", force=True, ignore_permissions=True)
