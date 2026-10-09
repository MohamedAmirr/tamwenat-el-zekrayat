# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Permission-checked Desk boundary for the manual Noon FBPI return
reference barcode lookup (#153).

Thin by design, same shape as ``api/noon_orders.py``: authorizes the
caller, then enqueues the actual ``ListReturnReferences`` lookup on the
``long`` queue - this module never makes a Noon HTTP call in the Desk
request itself.
"""

import frappe

from marketplace_integrations.integrations.noon.returns import queue_manual_lookup


@frappe.whitelist(methods=["POST"])
def lookup_return(account: str, barcode: str):
	"""Queue a manual Noon return-reference lookup for one barcode, scoped
	to one Noon Marketplace Account.

	Requires write permission on the Marketplace Account (checked again,
	redundantly with ``queue_manual_lookup()``'s own check) and rejects a
	non-Noon account before queueing anything, matching
	``api/noon_orders.py::import_orders()``'s pattern.
	"""
	account_doc = frappe.get_doc("Marketplace Account", account)
	account_doc.check_permission("write")
	if account_doc.platform != "Noon" or account_doc.connection_doctype != "Noon Connection":
		frappe.throw(
			frappe._("Marketplace Account {0} is not bound to a Noon Connection.").format(account_doc.name),
			frappe.ValidationError,
		)

	return queue_manual_lookup(account_doc.name, barcode)
