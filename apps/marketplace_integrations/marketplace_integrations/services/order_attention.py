# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The "Needs Attention" flag of a Sales Order (#533, epic #535).

Two read-only Custom Fields (``allow_on_submit``) say that a person has to look at an order: ``custom_needs_attention``
(Check) and ``custom_attention_reason`` (the problem and the next step, in plain words, hidden: only the form's escaped
banner and the list pill show it). The Sales Order list and form show them (``public/js/sales_order_list.js``,
``public/js/sales_order.js``).

``flag_attention`` and ``clear_attention`` are system calls: they check no permission, the caller decides (the person's
"Mark as Resolved" button checks Sales Order write in ``api/order_attention.py``). Neither commits; the caller does.
Both write without hooks and keep ``modified`` (the flag is a marker, not an edit of the order), and both lock the
order row first, so two writers cannot overwrite each other.

Contract for callers: ``keep_stored_attention`` (Sales Order ``validate`` / ``before_update_after_submit``) puts the
stored values back on every document save, so a stale form or a server ``doc.save()`` cannot wipe the flag. The flag is
therefore written only by these helpers: call ``flag_attention`` AFTER any save of the same order, never before.

The reason is plain text: tags and line breaks are removed when it is written (Frappe renders a read-only Small Text
without escaping, so the stored value must be safe as it is), and the screens escape it again.
"""

import re
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, strip_html_tags

FLAG_FIELD = "custom_needs_attention"
REASON_FIELD = "custom_attention_reason"
_WRITE_MARKER = "order_attention_write"


def clean_reason(reason):
	"""Plain text on one line: tags, angle brackets and line breaks are gone, runs of spaces collapse to one."""
	text = strip_html_tags(reason or "").replace("<", "").replace(">", "")
	return re.sub(r"\s+", " ", text).strip()


@contextmanager
def _own_write():
	frappe.flags[_WRITE_MARKER] = True
	try:
		yield
	finally:
		frappe.flags[_WRITE_MARKER] = False


def keep_stored_attention(doc, method=None):
	"""Sales Order ``validate`` and ``before_update_after_submit``: the flag and reason are written by
	``flag_attention`` / ``clear_attention`` only, so a document copy loaded before such a write must not put its old
	value back. Restores the stored values (empty on a new or copied order). Never raises."""
	if frappe.flags.get(_WRITE_MARKER):
		return
	before = doc.get_doc_before_save()
	doc.set(FLAG_FIELD, cint(before.get(FLAG_FIELD)) if before else 0)
	doc.set(REASON_FIELD, (before.get(REASON_FIELD) if before else "") or "")


def _lock(sales_order):
	"""Lock the order row and return ``(docstatus, flagged, reason)``, or None when the order does not exist."""
	row = frappe.db.get_value(
		"Sales Order", sales_order, ["docstatus", FLAG_FIELD, REASON_FIELD], as_dict=True, for_update=True
	)
	if not row:
		return None
	return cint(row.docstatus), bool(cint(row.get(FLAG_FIELD))), (row.get(REASON_FIELD) or "").strip()


def _write(sales_order, values):
	with _own_write():
		frappe.db.set_value("Sales Order", sales_order, values, update_modified=False)
	# An open form shows the new values after its next refresh (the same message Frappe sends after a save).
	frappe.publish_realtime(
		"doc_update",
		{
			"modified": frappe.db.get_value("Sales Order", sales_order, "modified"),
			"doctype": "Sales Order",
			"name": sales_order,
		},
		doctype="Sales Order",
		docname=sales_order,
		after_commit=True,
	)


def _comment(sales_order, text):
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Comment",
			"reference_doctype": "Sales Order",
			"reference_name": sales_order,
			"content": text,
		}
	).insert(ignore_permissions=True)


def flag_attention(sales_order, reason):
	"""Mark a SUBMITTED order as Needs Attention with ``reason`` (the problem and the next step). Returns True when it
	wrote, False when it did nothing: the order is missing (logged), is not submitted, or is already flagged with this
	reason. Already flagged with a different reason: the new reason is appended on a new line, so an earlier problem is
	not lost. Adds a timeline comment. Call it after any save of the same order (see the module note)."""
	reason = clean_reason(reason)
	if not reason:
		frappe.throw(_("A reason is needed to flag an order."))
	state = _lock(sales_order)
	if state is None:
		frappe.log_error(f"Sales Order {sales_order} does not exist, so it could not be flagged.", "Needs Attention")
		return False
	docstatus, flagged, current = state
	if docstatus != 1:
		return False
	entries = current.split("\n") if flagged and current else []
	if reason in entries:
		return False
	_write(sales_order, {FLAG_FIELD: 1, REASON_FIELD: "\n".join([*entries, reason])})
	_comment(sales_order, _("Needs Attention: {0}").format(frappe.utils.escape_html(reason)))
	return True


def clear_attention(sales_order, comment=True):
	"""Clear the flag and the reason. Returns the old reason, or None when the order is missing or was not flagged.

	``comment`` adds the timeline comment that keeps the old reason."""
	state = _lock(sales_order)
	if state is None or not state[1]:
		return None
	old_reason = state[2]
	_write(sales_order, {FLAG_FIELD: 0, REASON_FIELD: ""})
	if comment:
		_comment(
			sales_order,
			_("Needs Attention resolved. The reason was: {0}").format(frappe.utils.escape_html(old_reason)),
		)
	return old_reason
