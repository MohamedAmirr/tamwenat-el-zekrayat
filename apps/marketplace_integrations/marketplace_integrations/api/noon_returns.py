# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Permission-checked Desk boundary for the Noon FBPI return intake (#153, #549).

Thin by design, same shape as ``api/noon_orders.py``: authorizes the
caller, then enqueues the actual ``ListReturnReferences`` lookup on the
``long`` queue - this module never makes a Noon HTTP call in the Desk
request itself. The batch confirm delegates to ``returns.apply_outcomes``.
"""

import frappe

from marketplace_integrations.integrations.noon.returns import (
	RQ_JOB_PREFIX,
	_account_merchant_codes,
	get_lookup_result_state,
	queue_manual_lookup,
)
from marketplace_integrations.integrations.noon.returns import (
	apply_outcomes as apply_outcomes_service,
)

RESULT_FIELDS = [
	"name",
	"purchase_item_nr",
	"partner_sku",
	"order_nr",
	"marketplace_order",
	"merchant_code",
	"inspection_outcome",
	"receipt_acknowledged_on",
]


def _noon_account_for_writer(account: str):
	"""The account document, after the document-level write check and the Noon-bound check."""
	account_doc = frappe.get_doc("Marketplace Account", account)
	account_doc.check_permission("write")
	if account_doc.platform != "Noon" or account_doc.connection_doctype != "Noon Connection":
		frappe.throw(
			frappe._("Marketplace Account {0} is not bound to a Noon Connection.").format(account_doc.name),
			frappe.ValidationError,
		)
	return account_doc


@frappe.whitelist(methods=["POST"])
def list_merchant_codes(account: str):
	"""The merchant codes a lookup may be scoped to for one Noon account (empty when none are known yet)."""
	return _account_merchant_codes(_noon_account_for_writer(account)) or []


@frappe.whitelist(methods=["POST"])
def lookup_return(account: str, barcode: str, merchant_code: str | None = None):
	"""Queue a manual Noon return-reference lookup for one barcode, scoped
	to one Noon Marketplace Account and optionally one merchant code.

	Requires write permission on the Marketplace Account (checked again,
	redundantly with ``queue_manual_lookup()``'s own check) and rejects a
	non-Noon account before queueing anything, matching
	``api/noon_orders.py::import_orders()``'s pattern. Returns ``queued`` and
	the ``lookup_key`` to poll ``get_lookup_result`` with.
	"""
	account_doc = _noon_account_for_writer(account)
	return queue_manual_lookup(account_doc.name, barcode, merchant_code)


@frappe.whitelist(methods=["POST"])
def get_lookup_result(account: str, lookup_key: str):
	"""The state of one queued lookup (queued, done or error) and, once done, the Noon Returns it
	saved that the caller may read."""
	account_doc = _noon_account_for_writer(account)
	lookup_key = frappe.utils.cstr(lookup_key)
	# Only keys this feature issues are readable; never an arbitrary cache key.
	result = (
		get_lookup_result_state(account_doc.name, lookup_key)
		if lookup_key.startswith(f"{RQ_JOB_PREFIX}-result-")
		else None
	)
	if not result:
		message = frappe._("The lookup result expired. Look the barcode up again.")
		return {"state": "error", "error": message, "rows": []}
	rows = []
	if result["state"] == "done" and result["names"]:
		rows = frappe.get_list(
			"Noon Return",
			filters={"name": ["in", result["names"]], "marketplace_account": account_doc.name},
			fields=RESULT_FIELDS,
			order_by="creation asc",
		)
	return {"state": result["state"], "error": result["error"], "rows": rows}


@frappe.whitelist(methods=["POST"])
def apply_outcomes(rows):
	"""Acknowledge receipt and record the inspection outcome of up to 200 Noon Returns at once.

	``rows`` is a list of ``{noon_return, outcome}``; each row is checked and applied on its own."""
	return apply_outcomes_service(frappe.parse_json(rows))
