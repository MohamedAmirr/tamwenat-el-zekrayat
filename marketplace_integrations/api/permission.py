import frappe


def has_app_permission():
	"""Show the app on the Desk apps screen only to Administrator and System Manager,
	matching the Tamwenat Marketplaces Workspace and Desktop Icon."""
	return frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles()
