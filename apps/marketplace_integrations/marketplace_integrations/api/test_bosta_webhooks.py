# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.api import bosta_webhooks
from marketplace_integrations.integrations.bosta import statuses


class UnitTestBostaWebhooks(UnitTestCase):
	def _settings(self, *, enabled=1, secret="bosta-webhook-secret"):
		settings = MagicMock(enabled=enabled)
		settings.get_password.return_value = secret
		return settings

	def _payload(self, **updates):
		payload = {
			"_id": "bosta-delivery-id",
			"trackingNumber": 48089608,
			"state": 24,
			"type": "SEND",
			"timeStamp": 1689252908261,
		}
		payload.update(updates)
		return payload

	def test_authenticates_and_passes_json_to_status_service(self):
		settings = self._settings()
		payload = self._payload()
		with (
			patch.object(bosta_webhooks, "_get_bosta_settings", return_value=settings),
			patch.object(
				bosta_webhooks.frappe,
				"get_request_header",
				return_value="bosta-webhook-secret",
			),
			patch.object(bosta_webhooks, "_webhook_payload", return_value=payload),
			patch.object(bosta_webhooks, "process_webhook_event") as process_event,
		):
			self.assertEqual(bosta_webhooks.delivery_event(), {"accepted": True})
		process_event.assert_called_once_with(payload)

	def test_rejects_disabled_or_mismatched_secret_before_reading_body(self):
		for settings, header in (
			(self._settings(enabled=0), "bosta-webhook-secret"),
			(self._settings(), "wrong-secret"),
			(self._settings(secret=""), "bosta-webhook-secret"),
		):
			with self.subTest(settings=settings.enabled, header=header):
				with (
					patch.object(bosta_webhooks, "_get_bosta_settings", return_value=settings),
					patch.object(bosta_webhooks.frappe, "get_request_header", return_value=header),
					patch.object(bosta_webhooks, "_webhook_payload") as get_json,
				):
					with self.assertRaises(frappe.PermissionError):
						bosta_webhooks.delivery_event()
				get_json.assert_not_called()

	def test_malformed_body_is_an_error_but_unusable_events_are_not(self):
		for payload in (None, [], {}, self._payload(state="not-a-number"), self._payload(timeStamp=None)):
			with self.subTest(payload=payload), self.assertRaises(frappe.ValidationError):
				statuses.process_webhook_event(payload)
