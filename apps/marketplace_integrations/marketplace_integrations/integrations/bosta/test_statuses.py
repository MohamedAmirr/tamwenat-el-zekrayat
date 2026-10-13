# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.bosta import statuses


class UnitTestBostaStatuses(UnitTestCase):
	def test_polled_state_requires_a_documented_integer_code(self):
		state = statuses._polled_state({"data": {"state": {"code": 10, "value": "Pickup requested"}}})
		self.assertEqual(state, {"code": 10, "snapshot": {"code": 10, "value": "Pickup requested"}})
		for body in ({}, {"data": {}}, {"data": {"state": []}}, {"data": {"state": {"value": "x"}}}):
			self.assertIsNone(statuses._polled_state(body))

	def test_disabled_settings_skips_polling_without_querying_shipments(self):
		with (
			patch.object(statuses, "retry_failed_cancellations"),
			patch.object(statuses.frappe, "get_single", return_value=MagicMock(enabled=0)),
			patch.object(statuses.frappe, "get_all") as get_all,
		):
			self.assertEqual(statuses.poll_incomplete_shipments(), 0)
		get_all.assert_not_called()
