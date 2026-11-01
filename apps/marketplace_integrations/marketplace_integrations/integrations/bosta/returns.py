# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""A Bosta shipment that came back (epic #413, #411): one draft Sales Return, never submitted.

When Bosta reports a returned state (``statuses.RETURNED_STATE_CODES``) on a submitted Bosta Delivery Note,
``statuses._write_state`` queues ``create_returned_draft``. It makes ONE draft return Delivery Note through
ERPNext's own ``make_sales_return`` and marks it ``custom_return_source`` = "Returned by Bosta". Stock moves
only when staff submit that return after checking the goods (the Needs Attention row "Bosta Return" stays
open until a submitted return exists). No Bosta call is made: a shipment the customer never received needs
no Customer Return Pickup (type 25).

Not built here (open questions in docs/bosta-integration/README.md): the type 25 pickup request, the receipt
confirmation and the staff Notification.
"""

import frappe

RETURNED_BY_BOSTA = "Returned by Bosta"
_JOB = "bosta-returned-draft|{}"


def queue_returned_draft(delivery_note):
	"""Queue the one draft-return job of a note (deduplicated; runs after the caller's commit)."""
	frappe.enqueue(
		"marketplace_integrations.integrations.bosta.returns.create_returned_draft",
		queue="short",
		timeout=120,
		job_id=_JOB.format(delivery_note),
		deduplicate=True,
		enqueue_after_commit=True,
		delivery_note=delivery_note,
	)


def create_returned_draft(delivery_note):
	"""Make the draft return of ``delivery_note`` unless one already exists. Returns its name, else ``None``.

	Commit, then take the ORIGINAL note's row lock as the next statement: two jobs (a replayed event, the
	nightly run) are serialised there, and the second one finds the first one's draft. The insert bypasses
	permissions because a system job acts on Bosta's evidence, not on a person's request; the draft is never
	submitted here."""
	frappe.set_user("Administrator")
	frappe.db.commit()
	original = frappe.db.get_value(
		"Delivery Note", delivery_note, ["docstatus", "is_return"], as_dict=True, for_update=True
	)
	if not original or original.docstatus != 1 or original.is_return:
		frappe.db.commit()
		return None
	if frappe.db.exists(
		"Delivery Note",
		{"return_against": delivery_note, "custom_return_source": RETURNED_BY_BOSTA, "docstatus": ["<", 2]},
	):
		frappe.db.commit()
		return None
	try:
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

		draft = make_sales_return(delivery_note)
		draft.custom_return_source = RETURNED_BY_BOSTA
		# The tracking number belongs to the original shipment: the return must not look like it to the
		# webhook match.
		draft.lr_no = None
		draft.lr_date = None
		draft.insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title="Bosta: the draft return could not be created",
			message=f"{delivery_note}\n{frappe.get_traceback()}",
		)
		return None
	frappe.db.commit()
	return draft.name
