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

	def test_event_uses_documented_name_and_safe_fields(self):
		shipment = MagicMock(
			bosta_delivery_id="bosta-delivery-id",
			last_bosta_event_timestamp="",
			delivery_note="",
		)
		with (
			patch.object(statuses.frappe.db, "get_value", return_value="BOSTA-SHP-00001"),
			patch.object(statuses.frappe, "get_doc", return_value=shipment),
			patch.object(statuses, "now_datetime", return_value="2026-10-13 12:00:00"),
		):
			self.assertTrue(
				statuses.process_webhook_event(
					self._payload(
						state=47,
						exceptionReason="Postponed by customer",
						exceptionCode=3,
					)
				)
			)

		self.assertEqual(shipment.bosta_state_code, 47)
		self.assertEqual(shipment.bosta_state_value, "Exception")
		self.assertEqual(shipment.last_bosta_event_timestamp, 1689252908261)
		self.assertEqual(shipment.bosta_exception_reason, "Postponed by customer")
		self.assertEqual(shipment.bosta_exception_code, 3)
		shipment.save.assert_called_once_with(ignore_permissions=True)

	def test_rejects_unknown_state_and_delivery_id_mismatch(self):
		for payload, delivery_id in (
			(self._payload(state=999), "bosta-delivery-id"),
			(self._payload(_id="another-delivery"), "bosta-delivery-id"),
		):
			with self.subTest(payload=payload):
				shipment = MagicMock(
					bosta_delivery_id=delivery_id,
					last_bosta_event_timestamp="",
				)
				with (
					patch.object(statuses.frappe.db, "get_value", return_value="BOSTA-SHP-00001"),
					patch.object(statuses.frappe, "get_doc", return_value=shipment),
				):
					with self.assertRaises(frappe.ValidationError):
						statuses.process_webhook_event(payload)
				shipment.save.assert_not_called()

	def test_duplicate_event_has_no_second_write_or_delivery_note_cancel(self):
		shipment = MagicMock(
			bosta_delivery_id="bosta-delivery-id",
			last_bosta_event_timestamp="1689252908261",
			delivery_note="DN-00001",
		)
		with (
			patch.object(statuses.frappe.db, "get_value", return_value="BOSTA-SHP-00001"),
			patch.object(statuses.frappe, "get_doc", return_value=shipment),
			patch.object(statuses, "_cancel_linked_delivery_note") as cancel_delivery_note,
		):
			self.assertFalse(statuses.process_webhook_event(self._payload(state=49)))
		shipment.save.assert_not_called()
		cancel_delivery_note.assert_not_called()

	def test_canceled_send_cancels_only_a_submitted_linked_delivery_note(self):
		shipment = MagicMock(
			bosta_delivery_id="bosta-delivery-id",
			last_bosta_event_timestamp="",
			delivery_note="DN-00001",
		)
		delivery_note = MagicMock(docstatus=1)
		with (
			patch.object(statuses.frappe.db, "get_value", return_value="BOSTA-SHP-00001"),
			patch.object(statuses.frappe, "get_doc", side_effect=[shipment, delivery_note]),
			patch.object(statuses, "now_datetime", return_value="2026-10-13 12:00:00"),
		):
			self.assertTrue(statuses.process_webhook_event(self._payload(state=49)))
		delivery_note.cancel.assert_called_once_with()

	def test_canceled_event_does_not_cancel_non_submitted_delivery_note(self):
		shipment = MagicMock(
			bosta_delivery_id="bosta-delivery-id",
			last_bosta_event_timestamp="",
			delivery_note="DN-00001",
		)
		delivery_note = MagicMock(docstatus=2)
		with (
			patch.object(statuses.frappe.db, "get_value", return_value="BOSTA-SHP-00001"),
			patch.object(statuses.frappe, "get_doc", side_effect=[shipment, delivery_note]),
			patch.object(statuses, "now_datetime", return_value="2026-10-13 12:00:00"),
		):
			statuses.process_webhook_event(self._payload(state=49))
		delivery_note.cancel.assert_not_called()

	def test_canceled_event_preserves_the_state_when_delivery_note_cancellation_fails(self):
		shipment = MagicMock(
			bosta_delivery_id="bosta-delivery-id",
			last_bosta_event_timestamp="",
			delivery_note="DN-00001",
		)
		delivery_note = MagicMock(docstatus=1)
		delivery_note.cancel.side_effect = frappe.ValidationError("linked invoice")
		with (
			patch.object(statuses.frappe.db, "get_value", return_value="BOSTA-SHP-00001"),
			patch.object(statuses.frappe, "get_doc", side_effect=[shipment, delivery_note]),
			patch.object(statuses, "now_datetime", return_value="2026-10-13 12:00:00"),
			patch.object(statuses.frappe, "log_error") as log_error,
		):
			self.assertTrue(statuses.process_webhook_event(self._payload(state=49)))

		self.assertIn("could not cancel Delivery Note", shipment.last_error)
		log_error.assert_called_once()
