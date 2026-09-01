# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized Desk API boundary for Talabat outlet availability actions."""

import frappe

from marketplace_integrations.integrations.talabat import outlet


@frappe.whitelist(methods=["POST"])
def refresh_outlet_status(account: str):
	"""Refresh one Talabat account's confirmed outlet status."""
	_account_with_write_permission(account)
	return _status_response(outlet.get_outlet_status(account))


@frappe.whitelist(methods=["POST"])
def set_outlet_status(
	account: str, status: str, closed_reason: str | None = None, closed_until: str | None = None
):
	"""Set one supported Talabat outlet state and return its confirmed cache."""
	_account_with_write_permission(account)
	return _status_response(outlet.update_outlet_status(account, status, closed_reason, closed_until))


def _account_with_write_permission(account):
	doc = frappe.get_doc("Marketplace Account", account)
	doc.check_permission("write")
	return doc


def _status_response(account):
	return {
		"name": account.name,
		"external_status": account.external_status,
		"external_status_reason": account.external_status_reason,
		"external_status_until": account.external_status_until,
		"external_status_checked_on": account.external_status_checked_on,
		"external_status_error": account.external_status_error,
	}
