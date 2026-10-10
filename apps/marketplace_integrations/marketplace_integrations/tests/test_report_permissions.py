# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Script Reports check ``frappe.has_permission(report.ref_doctype, "report")``
(frappe/desk/query_report.py), so the doctypes our reports use as ref_doctype
must grant the "report" ptype to System Manager, the reports' own role.
Administrator bypasses the check, which is how a missing grant goes unnoticed.
"""

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.tests.utils import make_role_user

# ref_doctype of the app's Script Reports
REPORT_REF_DOCTYPES = ("Marketplace Account", "Marketplace Order", "Marketplace Sync Log")


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
