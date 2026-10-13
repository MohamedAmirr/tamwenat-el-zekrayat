# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.bosta import statuses


class UnitTestBostaStatuses(UnitTestCase):
	def test_updates_only_documented_state_snapshot(self):
		shipment = MagicMock()
		response = {"success": True, "data": {"state": {"code": 10, "value": "Pickup requested"}}}

		with patch.object(statuses, "now_datetime", return_value="2026-10-13 12:00:00"):
			self.assertTrue(statuses.update_shipment_status(shipment, response))

		self.assertEqual(shipment.bosta_state_code, 10)
		self.assertEqual(shipment.bosta_state_value, "Pickup requested")
		self.assertEqual(
			frappe.parse_json(shipment.bosta_status_snapshot), {"code": 10, "value": "Pickup requested"}
		)
		self.assertEqual(shipment.last_bosta_status_on, "2026-10-13 12:00:00")
		shipment.save.assert_called_once_with(ignore_permissions=True)

	def test_ignores_missing_or_malformed_documented_state(self):
		shipment = MagicMock()
		self.assertFalse(statuses.update_shipment_status(shipment, {"success": True, "data": {}}))
		self.assertFalse(statuses.update_shipment_status(shipment, {"success": True, "data": {"state": []}}))
		shipment.save.assert_not_called()

	def test_only_documented_delivered_state_stops_polling(self):
		self.assertTrue(statuses.is_bosta_delivery_complete("Delivered"))
		self.assertFalse(statuses.is_bosta_delivery_complete("Pickup requested"))
		self.assertFalse(statuses.is_bosta_delivery_complete("Returned"))

	def test_disabled_settings_skips_polling_without_querying_shipments(self):
		settings = MagicMock(enabled=0)
		with (
			patch.object(statuses.frappe, "get_single", return_value=settings),
			patch.object(statuses.frappe, "get_all") as get_all,
		):
			self.assertEqual(statuses.poll_incomplete_shipments(), 0)
		get_all.assert_not_called()

	def test_one_poll_failure_does_not_stop_later_shipments(self):
		settings = MagicMock(enabled=1)
		first = MagicMock(name="first", tracking_number="first-tracking", bosta_state_value="")
		first.name = "BOSTA-SHP-00001"
		second = MagicMock(name="second", tracking_number="second-tracking", bosta_state_value="")
		second.name = "BOSTA-SHP-00002"
		client = MagicMock()
		response = MagicMock(status_code=200)
		response.json.return_value = {"success": True, "data": {"state": {"code": 10}}}
		client.get_delivery.side_effect = [RuntimeError("network failure"), response]

		with (
			patch.object(statuses.frappe, "get_single", return_value=settings),
			patch.object(statuses.frappe, "get_all", return_value=[first.name, second.name]),
			patch.object(statuses.frappe, "get_doc", side_effect=[first, second]),
			patch.object(statuses, "BostaClient", return_value=client),
			patch.object(statuses, "update_shipment_status", return_value=True) as update_status,
			patch.object(statuses.frappe, "log_error") as log_error,
		):
			self.assertEqual(statuses.poll_incomplete_shipments(), 1)

		self.assertEqual(client.get_delivery.call_count, 2)
		update_status.assert_called_once_with(second, response.json())
		log_error.assert_called_once()
