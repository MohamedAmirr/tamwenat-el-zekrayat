# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase


class IntegrationTestTalabatSettings(IntegrationTestCase):
	def test_fields_persist(self):
		settings = frappe.get_single("Talabat Settings")
		settings.client_id = "test-client-id"
		settings.client_secret = "test-client-secret"
		settings.environment = "Sandbox"
		settings.save()

		settings.reload()
		self.assertEqual(settings.client_id, "test-client-id")
		self.assertEqual(settings.get_password("client_secret"), "test-client-secret")
		self.assertEqual(settings.environment, "Sandbox")

	def test_get_base_url(self):
		settings = frappe.get_single("Talabat Settings")

		settings.environment = "Sandbox"
		settings.save()
		self.assertEqual(settings.get_base_url(), "https://sandbox.partner.deliveryhero.io")

		settings.environment = "Production"
		settings.save()
		self.assertEqual(settings.get_base_url(), "https://talabat.partner.deliveryhero.io")
