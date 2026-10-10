import os

import frappe
from frappe.tests import UnitTestCase

import marketplace_integrations
from marketplace_integrations.api.permission import has_app_permission
from marketplace_integrations.tests.utils import make_role_user

APP = "marketplace_integrations"


class TestAppsScreenEntry(UnitTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")

	def test_administrator_has_permission(self):
		frappe.set_user("Administrator")
		self.assertTrue(has_app_permission())

	def test_system_manager_has_permission(self):
		email, _role = make_role_user(self, "Apps Screen Manager", {"User": ["read"]})
		frappe.get_doc("User", email).add_roles("System Manager")
		frappe.clear_cache(user=email)
		frappe.set_user(email)
		self.assertTrue(has_app_permission())

	def test_plain_user_has_no_permission(self):
		email, _role = make_role_user(self, "Apps Screen Plain", {"User": ["read"]})
		frappe.set_user(email)
		self.assertFalse(has_app_permission())

	def test_hook_entry_and_permission_path_resolve(self):
		entries = frappe.get_hooks("add_to_apps_screen", app_name=APP)
		self.assertEqual(len(entries), 1)
		entry = entries[0]
		self.assertEqual(entry["name"], APP)
		self.assertEqual(entry["title"], "Marketplace Integrations")
		self.assertEqual(entry["route"], "/desk/tamwenat-marketplaces")
		self.assertIs(frappe.get_attr(entry["has_permission"]), has_app_permission)

	def test_logo_file_exists_in_public(self):
		logo = frappe.get_hooks("add_to_apps_screen", app_name=APP)[0]["logo"]
		prefix = f"/assets/{APP}/"
		self.assertTrue(logo.startswith(prefix))
		public = os.path.join(os.path.dirname(marketplace_integrations.__file__), "public")
		self.assertTrue(os.path.isfile(os.path.join(public, logo[len(prefix) :])))
