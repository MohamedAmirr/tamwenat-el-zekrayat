# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Permission-checked Desk boundary for the Noon AWB pool. Thin: the Noon
call runs in a queued worker (``integrations/noon/awb_pool.py``), never in the
Desk request."""

import frappe

from marketplace_integrations.integrations.noon.awb_pool import count_available, queue_generate
from marketplace_integrations.integrations.noon.catalog import get_account


@frappe.whitelist(methods=["POST"])
def generate_awbs(account: str, qty: int):
	"""Queue generation of ``qty`` (1-1000) Noon AWBs for one Noon account."""
	account_doc = get_account(account)
	account_doc.check_permission("write")
	frappe.has_permission("Noon AWB", ptype="create", throw=True)
	return queue_generate(account_doc.name, qty)


@frappe.whitelist()
def get_available_count(account: str):
	"""Number of Available pool AWBs for one Marketplace Account."""
	frappe.has_permission("Noon AWB", ptype="read", throw=True)
	frappe.get_doc("Marketplace Account", account).check_permission("read")
	return count_available(account)
