# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase


class IntegrationTestBostaSettings(IntegrationTestCase):
	def setUp(self):
		self.settings = frappe.get_single("Bosta Settings")
		self.settings.enabled = 0
		self.settings.api_key = ""
		self.settings.business_location_id = ""

	def test_disabled_settings_generate_a_webhook_url_and_secret(self):
		with patch(
			"marketplace_integrations.marketplace_integrations.doctype.bosta_settings.bosta_settings.get_url",
			return_value="https://example.com/api/method/marketplace_integrations.api.bosta_webhooks.delivery_event",
		):
			self.settings.webhook_url = ""
			self.settings.webhook_secret = ""
			self.settings.save()

		self.settings.reload()
		self.assertEqual(
			self.settings.webhook_url,
			"https://example.com/api/method/marketplace_integrations.api.bosta_webhooks.delivery_event",
		)
		self.assertTrue(self.settings.get_password("webhook_secret", raise_exception=False))

	def test_enabled_settings_require_api_key_and_business_location(self):
		self.settings.enabled = 1
		with self.assertRaises(frappe.ValidationError):
			self.settings.save()

		self.settings.api_key = "bosta-key"
		with self.assertRaises(frappe.ValidationError):
			self.settings.save()

		# Password persistence may update this Single's modified timestamp even
		# though the validation above rejects the incomplete configuration.
		# Reload before the valid save so this test exercises the validation rule,
		# not Frappe's stale-document guard.
		self.settings.reload()
		self.settings.enabled = 1
		self.settings.api_key = "bosta-key"
		self.settings.business_location_id = "business-location-id"
		self.settings.save()
		self.settings.reload()
		self.assertEqual(self.settings.business_location_id, "business-location-id")
		self.assertEqual(self.settings.get_password("api_key", raise_exception=False), "bosta-key")

	def test_webhook_secret_copy_requires_write_permission(self):
		self.settings.webhook_secret = "copyable-bosta-secret"
		self.settings.save()
		self.assertEqual(
			self.settings.get_webhook_secret_for_copy(), {"webhook_secret": "copyable-bosta-secret"}
		)

		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"bosta-secret-{frappe.generate_hash(length=8)}@example.com",
				"first_name": "Bosta Secret Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()
		with self.set_user(user.name), self.assertRaises(frappe.PermissionError):
			self.settings.get_webhook_secret_for_copy()
