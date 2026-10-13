# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.api import bosta_webhooks


class UnitTestBostaWebhooks(UnitTestCase):
	def _settings(self, *, enabled=1, secret="bosta-webhook-secret"):
		settings = MagicMock(enabled=enabled)
		settings.get_password.return_value = secret
		return settings

	def test_accepts_the_configured_custom_header_without_parsing_body(self):
		settings = self._settings()
		with (
			patch.object(bosta_webhooks.frappe, "get_single", return_value=settings),
			patch.object(
				bosta_webhooks.frappe,
				"get_request_header",
				return_value="bosta-webhook-secret",
			),
		):
			self.assertEqual(bosta_webhooks.delivery_event(), {"accepted": True})

	def test_rejects_disabled_or_mismatched_secret(self):
		for settings, header in (
			(self._settings(enabled=0), "bosta-webhook-secret"),
			(self._settings(), "wrong-secret"),
			(self._settings(secret=""), "bosta-webhook-secret"),
		):
			with self.subTest(settings=settings.enabled, header=header):
				with (
					patch.object(bosta_webhooks.frappe, "get_single", return_value=settings),
					patch.object(bosta_webhooks.frappe, "get_request_header", return_value=header),
				):
					with self.assertRaises(frappe.PermissionError):
						bosta_webhooks.delivery_event()
