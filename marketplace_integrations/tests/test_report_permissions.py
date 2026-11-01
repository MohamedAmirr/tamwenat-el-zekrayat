# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Script Reports check ``frappe.has_permission(report.ref_doctype, "report")``
(frappe/desk/query_report.py), so the doctypes our reports use as ref_doctype
must grant the "report" ptype to every role the report lists (System Manager and,
since #323, the platform roles). The other top-level doctypes grant it too so their
list Report view works. Administrator bypasses the check, which is how a missing
grant goes unnoticed.

The report and page ``roles`` below are asserted against the shipped JSON, so the
test does not depend on the site having migrated yet.
"""

import json

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.tests.utils import make_role_user

# ref_doctype of the app's Script Reports, plus every other top-level doctype of the app: the
# list view's Report view uses the same "report" ptype.
REPORT_REF_DOCTYPES = (
	"Marketplace Account",
	"Marketplace Order",
	"Marketplace Sync Log",
	"Marketplace Product Mapping",
	"Noon Connection",
	"Noon Return",
	"Shopify Connection",
	"Shopify Webhook Delivery",
	"Talabat Connection",
)


class IntegrationTestReportPermissions(IntegrationTestCase):
	def test_system_manager_can_run_reports(self):
		email, _role = make_role_user(self, "Report System Manager", {})
		frappe.get_doc("User", email).add_roles("System Manager")
		frappe.clear_cache(user=email)

		for doctype in REPORT_REF_DOCTYPES:
			with self.subTest(doctype=doctype):
				self.assertTrue(frappe.has_permission(doctype, "report", user=email))

	def test_user_without_system_manager_cannot_run_reports(self):
		email, _role = make_role_user(self, "Report Plain User", {})

		for doctype in REPORT_REF_DOCTYPES:
			with self.subTest(doctype=doctype):
				self.assertFalse(frappe.has_permission(doctype, "report", user=email))


PLATFORM_ROLES = ("Talabat User", "Noon User", "Shopify User")

# JSON file -> roles it must list (#323). Bosta has no role of its own (epic #413).
REPORT_ROLES = {
	"marketplace_channel_status": ("System Manager", *PLATFORM_ROLES),
	"marketplace_needs_attention": ("System Manager", *PLATFORM_ROLES),
	"shopify_order_import_failures": ("System Manager", "Shopify User"),
	"shopify_reconciliation_summary": ("System Manager", "Shopify User"),
}
PAGE_ROLES = {
	"marketplace_talabat_catalog_mapping": ("System Manager", "Talabat User"),
	"marketplace_shopify_catalog_mapping": ("System Manager", "Shopify User"),
	"marketplace_products": ("System Manager", *PLATFORM_ROLES),
	"marketplace_product_management": ("System Manager", *PLATFORM_ROLES),
}


def read_standard_json(kind, folder):
	path = frappe.get_app_path(
		"marketplace_integrations", "marketplace_integrations", kind, folder, f"{folder}.json"
	)
	with open(path) as handle:
		return json.load(handle)


class IntegrationTestPlatformRolesForReportsAndPages(IntegrationTestCase):
	def test_report_json_lists_the_intended_roles(self):
		for folder, roles in REPORT_ROLES.items():
			with self.subTest(report=folder):
				listed = [row["role"] for row in read_standard_json("report", folder)["roles"]]
				self.assertCountEqual(listed, roles)

	def test_page_json_lists_the_intended_roles(self):
		for folder, roles in PAGE_ROLES.items():
			with self.subTest(page=folder):
				listed = [row["role"] for row in read_standard_json("page", folder)["roles"]]
				self.assertCountEqual(listed, roles)

	def test_each_report_role_holds_the_report_permission_on_the_ref_doctype(self):
		# Mirrors frappe/desk/query_report.py: the report's own roles are not enough, the user also
		# needs the "report" ptype on ref_doctype.
		for folder, roles in REPORT_ROLES.items():
			ref_doctype = read_standard_json("report", folder)["ref_doctype"]
			for role in roles:
				if role == "System Manager":
					continue
				with self.subTest(report=folder, role=role):
					email, _role = make_role_user(self, "Report Platform", {})
					frappe.get_doc("User", email).add_roles(role)
					frappe.clear_cache(user=email)
					self.assertTrue(frappe.has_permission(ref_doctype, "report", user=email))
