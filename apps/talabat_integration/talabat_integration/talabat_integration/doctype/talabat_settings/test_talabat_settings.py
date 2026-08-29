# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase


class IntegrationTestTalabatSettings(IntegrationTestCase):
	def test_fields_persist(self):
		settings = frappe.get_single("Talabat Settings")
		settings.client_id = "test-client-id"
		settings.client_secret = "test-client-secret"
		settings.chain_id = "test-chain-id"
		settings.environment = "Sandbox"
		settings.save()

		settings.reload()
		self.assertEqual(settings.client_id, "test-client-id")
		self.assertEqual(settings.get_password("client_secret"), "test-client-secret")
		self.assertEqual(settings.chain_id, "test-chain-id")
		self.assertEqual(settings.environment, "Sandbox")

	def test_client_secret_with_angle_brackets_persists_exactly(self):
		# Regression: Frappe's Document.save() runs HTML/XSS sanitization on any
		# field value containing "<"/">", and doesn't exclude Password fields by
		# default. A real Talabat secret containing "<...>" got silently mutilated
		# on save (48 chars -> 34 chars) before "ignore_xss_filter" was added to
		# this field. This proves the fix holds.
		settings = frappe.get_single("Talabat Settings")
		tricky_secret = "fge1g^d5aQJZ@GiY@L2@c+(37jykOX<p<+P0kJ!M&40>$RR9"
		settings.client_secret = tricky_secret
		settings.save()

		settings.reload()
		self.assertEqual(settings.get_password("client_secret"), tricky_secret)

	def test_get_base_url(self):
		settings = frappe.get_single("Talabat Settings")

		settings.chain_id = "test-chain-id"
		settings.environment = "Sandbox"
		settings.save()
		self.assertEqual(settings.get_base_url(), "https://sandbox.partner.deliveryhero.io")

		settings.environment = "Production"
		settings.save()
		self.assertEqual(settings.get_base_url(), "https://talabat.partner.deliveryhero.io")
