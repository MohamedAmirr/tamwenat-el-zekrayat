"""Shared test cleanup for `IntegrationTestCase` classes in this app.

`IntegrationTestCase` rolls back once, at class teardown, and code under test
calls `frappe.db.commit()` mid-test - so anything created before that commit
becomes permanent junk in the site DB. Subclass `CleanupTestCase` instead: it
snapshots the leak-prone doctypes before each test and, after the test (and its
own tearDown/cleanups), deletes whatever is new and commits. See
docs/frappe-conventions.md, "Tests".
"""

import frappe
from frappe.tests import IntegrationTestCase

# Dependency-safe order: children/referrers first, then what they point at.
CLEANUP_DOCTYPES = (
	"Sales Order",
	"Marketplace Order",
	"Marketplace Product Mapping",
	"Shopify Webhook Delivery",
	"Marketplace Account",
	"Shopify Connection",
	"Talabat Connection",
	"Item Price",
	"Item",
	"Price List",
	# Users/roles some permission tests create; Custom DocPerm before Role, User before Role.
	"Custom DocPerm",
	"User",
	"Role",
	# delete_doc of the User/Item/... rows above can itself leave these behind (e.g. Notification Settings).
	"Deleted Document",
)


def snapshot_names():
	return {dt: set(frappe.get_all(dt, pluck="name", ignore_permissions=True)) for dt in CLEANUP_DOCTYPES}


def delete_new_rows(before):
	"""Delete every leak-prone row not in `before`, then commit."""
	frappe.set_user("Administrator")
	for dt in CLEANUP_DOCTYPES:
		for name in set(frappe.get_all(dt, pluck="name", ignore_permissions=True)) - before[dt]:
			if dt == "Sales Order" and frappe.db.get_value(dt, name, "docstatus") == 1:
				frappe.get_doc(dt, name).cancel()  # submitted docs can't be deleted directly
			# delete_doc handles child tables (Item, Price List, ...). delete_permanently skips the
			# Deleted Document row; its delete_dynamic_links job is enqueued with now=frappe.in_test,
			# so under run-tests it runs inline and never touches the (overflow-prone) queue.
			frappe.delete_doc(dt, name, force=True, ignore_permissions=True, delete_permanently=True)
	frappe.db.commit()


class CleanupTestCase(IntegrationTestCase):
	def run(self, result=None):
		before = snapshot_names()
		try:
			return super().run(result)
		finally:
			delete_new_rows(before)
