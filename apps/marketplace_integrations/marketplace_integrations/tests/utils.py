"""Shared test helpers (not a test module: no ``test_`` prefix, so it is never collected)."""

import frappe


def make_role_user(testcase, label, perms):
	"""Create a user holding one fresh role granted exactly ``perms``
	({doctype: [ptype, ...]}), and register the cleanup that removes everything.

	Permissions are granted only through ``frappe.permissions``. For each doctype
	the first ptype goes through ``add_permission`` (creates the role's single
	Custom DocPerm row; it is a no-op once that row exists, and the new row always
	has read=1 and export=1, so "read" is always included) and the remaining ptypes
	through ``update_permission_property`` on that row. Both copy the doctype's
	standard DocPerm rows into Custom DocPerm the first time they are used. Never
	insert a Custom DocPerm row directly: with no copy, that row becomes the only
	rule and every other role loses access.

	Because the cleanup commits, a test class using this helper must delete its
	own fixtures in tearDown; relying on class-level rollback leaks them.

	Returns ``(email, role_name)``.
	"""
	# Before changing anything: doctypes whose Custom DocPerm rows exist already
	# are not ours to remove on cleanup.
	had = {dt: frappe.db.exists("Custom DocPerm", {"parent": dt}) for dt in perms}

	role_name = f"{label} {frappe.generate_hash(length=8)}"
	email = f"{label.lower().replace(' ', '-')}-{frappe.generate_hash(length=10)}@example.com"

	def cleanup():
		previous_user = frappe.session.user
		frappe.set_user("Administrator")
		try:
			# Deleting a Role does not delete its Custom DocPerm rows.
			frappe.db.delete("Custom DocPerm", {"role": role_name})
			for doctype, existed in had.items():
				if not existed:
					# The copy of standard rows made by add_permission is ours.
					frappe.db.delete("Custom DocPerm", {"parent": doctype})
			frappe.delete_doc("User", email, force=True, ignore_permissions=True, delete_permanently=True)
			frappe.delete_doc("Role", role_name, force=True, ignore_permissions=True, delete_permanently=True)
			for doctype in perms:
				frappe.clear_cache(doctype=doctype)
			frappe.clear_cache(user=email)
			frappe.db.commit()
		finally:
			frappe.set_user(previous_user)

	testcase.addCleanup(cleanup)

	frappe.get_doc({"doctype": "Role", "role_name": role_name}).insert()
	frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": f"{label} Test User",
			"send_welcome_email": 0,
			"roles": [{"role": role_name}],
		}
	).insert()

	for doctype, ptypes in perms.items():
		frappe.permissions.add_permission(doctype, role_name, ptype=ptypes[0])
		for ptype in ptypes[1:]:
			frappe.permissions.update_permission_property(doctype, role_name, 0, ptype, 1)
		frappe.clear_cache(doctype=doctype)
	frappe.clear_cache(user=email)
	return email, role_name
