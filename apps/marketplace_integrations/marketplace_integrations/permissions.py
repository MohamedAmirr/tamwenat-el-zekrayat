"""Platform row filtering for Marketplace Account and its child-of-account doctypes (#322).

Each platform role (``Talabat User``, ``Noon User``, ``Shopify User``) sees only the rows
that belong to its platform. The platform of a row is always
``Marketplace Account.platform``: for Marketplace Account itself that is its own field,
for Marketplace Order, Marketplace Product Mapping and Marketplace Sync Log it is the
platform of the linked ``marketplace_account``. A row with no account is therefore
invisible to platform users (a Sync Log without an account is System Manager only).

System Manager and Administrator are never filtered. A user with none of the platform roles
and not a System Manager gets nothing here; DocPerm already denies them, the condition is
a second layer. Bosta is a carrier with no Marketplace Account and no role of its own.

``permission_query_conditions`` filters lists/reports; ``has_permission`` filters direct
document access (open by name). Both are registered in ``hooks.py``.
"""

import frappe

PLATFORM_ROLES = {
	"Talabat": "Talabat User",
	"Noon": "Noon User",
	"Shopify": "Shopify User",
}

NO_ROWS = "1=0"


def _unrestricted(user, roles):
	return user == "Administrator" or "System Manager" in roles


def get_user_platforms(user=None):
	"""Platforms the user may see through their platform roles (empty for none)."""
	user = user or frappe.session.user
	roles = frappe.get_roles(user)
	return [platform for platform, role in PLATFORM_ROLES.items() if role in roles]


def _platform_list(platforms):
	return ", ".join(frappe.db.escape(platform) for platform in platforms)


def _condition(user, doctype, account_field):
	user = user or frappe.session.user
	if _unrestricted(user, frappe.get_roles(user)):
		return ""
	platforms = get_user_platforms(user)
	if not platforms:
		return NO_ROWS
	platform_list = _platform_list(platforms)
	if account_field is None:
		return f"`tab{doctype}`.`platform` in ({platform_list})"
	return (
		f"`tab{doctype}`.`{account_field}` in "
		f"(select `name` from `tabMarketplace Account` where `platform` in ({platform_list}))"
	)


def _allowed(user, platform):
	user = user or frappe.session.user
	if _unrestricted(user, frappe.get_roles(user)):
		return True
	return bool(platform) and platform in get_user_platforms(user)


def _account_platform(doc):
	account = doc.get("marketplace_account")
	if not account:
		return None
	return frappe.db.get_value("Marketplace Account", account, "platform")


def marketplace_account_query_conditions(user=None, doctype=None):
	return _condition(user, "Marketplace Account", None)


def marketplace_order_query_conditions(user=None, doctype=None):
	return _condition(user, "Marketplace Order", "marketplace_account")


def marketplace_product_mapping_query_conditions(user=None, doctype=None):
	return _condition(user, "Marketplace Product Mapping", "marketplace_account")


def marketplace_sync_log_query_conditions(user=None, doctype=None):
	return _condition(user, "Marketplace Sync Log", "marketplace_account")


def marketplace_account_has_permission(doc, ptype=None, user=None, debug=False):
	return _allowed(user, doc.get("platform"))


def marketplace_order_has_permission(doc, ptype=None, user=None, debug=False):
	return _allowed(user, _account_platform(doc))


def marketplace_product_mapping_has_permission(doc, ptype=None, user=None, debug=False):
	return _allowed(user, _account_platform(doc))


def marketplace_sync_log_has_permission(doc, ptype=None, user=None, debug=False):
	return _allowed(user, _account_platform(doc))
