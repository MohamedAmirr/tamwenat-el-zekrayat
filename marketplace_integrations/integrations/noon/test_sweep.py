# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.noon.sweep import sweep_all_accounts


class UnitTestNoonSweep(UnitTestCase):
	def test_hourly_sweep_queues_both_jobs_for_each_enabled_account(self):
		with patch(
			"marketplace_integrations.integrations.noon.sweep.frappe.get_all", return_value=["ACC-1", "ACC-2"]
		) as accounts:
			with patch(
				"marketplace_integrations.integrations.noon.sweep.enqueue_push", return_value={"queued": True}
			) as enqueue:
				self.assertEqual(sweep_all_accounts(), 4)
		accounts.assert_called_once_with(
			"Marketplace Account",
			filters={"platform": "Noon", "enabled": 1},
			pluck="name",
			order_by="name asc",
		)
		self.assertEqual(
			[call.args[1:] for call in enqueue.call_args_list],
			[
				("pricing", "ACC-1"),
				("stock", "ACC-1"),
				("pricing", "ACC-2"),
				("stock", "ACC-2"),
			],
		)
