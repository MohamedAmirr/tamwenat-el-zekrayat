# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""``sync_sales_order()`` itself is never exercised for real here (that's
``test_sales_order_sync.py``'s job) - it is patched on the debounce module so
these tests verify only the debounce/sweep orchestration: when it gets
called, and how many times."""

from unittest.mock import call, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from marketplace_integrations.services import sales_order_sync_debounce as debounce
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order

SYNC_TARGET = "marketplace_integrations.services.sales_order_sync_debounce.sync_sales_order"


def _calls_for_order(mocked, order_name):
	"""Filter a mocked frappe.enqueue()/sync_sales_order() call list down to
	calls for one Marketplace Order - the shared test.localhost site is never
	empty (IntegrationTestCase only rolls back at class end, per
	docs/dev-environment.md), so a sweep can find due orders left behind by
	other tests/other test methods in this same class. Same pattern as
	integrations/shopify/test_inventory_sync.py's ``_calls_for_account()``."""
	return [
		call
		for call in mocked.call_args_list
		if call.kwargs.get("marketplace_order") == order_name or call.args[:1] == (order_name,)
	]


class IntegrationTestSalesOrderSyncDebounce(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.company = frappe.defaults.get_global_default("company")
		self.account = self._make_account()

	def _make_account(self):
		connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"SO Sync Debounce {self.suffix}",
				"client_id": f"so-sync-debounce-client-{self.suffix}",
				"client_secret": "test-secret",
				"chain_id": f"so-sync-debounce-chain-{self.suffix}",
			}
		).insert()
		return frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"SO Sync Debounce Account {self.suffix}",
				"platform": "Talabat",
				"external_account_id": f"so-sync-debounce-vendor-{self.suffix}",
				"connection_doctype": "Talabat Connection",
				"connection": connection.name,
				"erpnext_company": self.company,
			}
		).insert()

	def _make_order(self):
		return upsert_marketplace_order(
			self.account.name,
			{
				"external_order_id": f"so-sync-debounce-order-{self.suffix}-{frappe.generate_hash(length=4)}",
				"latest_payload": {"order_id": "external-order"},
				"items": [],
			},
		)

	def test_bump_sets_debounce_window_forward(self):
		order = self._make_order()
		before = now_datetime()

		debounce.bump_sync_debounce(order.name)

		value = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")
		self.assertIsNotNone(value)
		self.assertGreater(value, before)

	def test_second_bump_within_window_pushes_it_further_out(self):
		order = self._make_order()

		debounce.bump_sync_debounce(order.name, delay_seconds=45)
		first = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")
		debounce.bump_sync_debounce(order.name, delay_seconds=90)
		second = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")

		self.assertGreater(second, first)

	def test_two_bumps_result_in_exactly_one_eventual_sync(self):
		"""Two bump_sync_debounce() calls for the same order within the delay
		window, followed by a sweep tick once the (later) window is due, run
		sync_sales_order() exactly once. ``frappe.enqueue`` is patched to call
		its job function immediately (synchronously) in place of a real RQ
		worker, so the sweep -> _run_debounced_sync hand-off is exercised
		end-to-end without needing a running worker."""
		order = self._make_order()

		debounce.bump_sync_debounce(order.name, delay_seconds=-10)
		debounce.bump_sync_debounce(order.name, delay_seconds=-5)

		def run_immediately(func, **kwargs):
			job_kwargs = {
				key: value
				for key, value in kwargs.items()
				if key not in {"queue", "timeout", "job_id", "deduplicate"}
			}
			return func(**job_kwargs)

		with patch(SYNC_TARGET) as mocked_sync, patch("frappe.enqueue", side_effect=run_immediately):
			debounce.sweep_due_sales_order_syncs()

		self.assertEqual(_calls_for_order(mocked_sync, order.name), [call(order.name)])
		self.assertIsNone(frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until"))

	def test_stale_expected_debounce_until_no_ops(self):
		order = self._make_order()
		debounce.bump_sync_debounce(order.name, delay_seconds=45)
		stale_expected = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")

		# A newer webhook pushes the window further out after the sweep tick
		# already read the (now stale) value above.
		debounce.bump_sync_debounce(order.name, delay_seconds=90)
		current_expected = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")
		self.assertNotEqual(stale_expected, current_expected)

		with patch(SYNC_TARGET) as mocked_sync:
			debounce._run_debounced_sync(order.name, stale_expected)

		mocked_sync.assert_not_called()
		# The window is left untouched - the next sweep tick still owns it.
		self.assertEqual(
			frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until"), current_expected
		)

	def test_run_debounced_sync_clears_the_window_and_calls_sync_on_a_match(self):
		order = self._make_order()
		debounce.bump_sync_debounce(order.name, delay_seconds=45)
		expected = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")

		with patch(SYNC_TARGET) as mocked_sync:
			debounce._run_debounced_sync(order.name, expected)

		mocked_sync.assert_called_once_with(order.name)
		self.assertIsNone(frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until"))

	def test_sweep_enqueues_one_deduplicated_job_per_due_order(self):
		order = self._make_order()
		debounce.bump_sync_debounce(order.name, delay_seconds=-1)  # already due

		with patch("frappe.enqueue") as mocked_enqueue:
			debounce.sweep_due_sales_order_syncs()

		calls = _calls_for_order(mocked_enqueue, order.name)
		self.assertEqual(len(calls), 1)
		kwargs = calls[0].kwargs
		self.assertEqual(kwargs["marketplace_order"], order.name)
		self.assertEqual(kwargs["job_id"], f"marketplace-sync-{order.name}")
		self.assertTrue(kwargs["deduplicate"])

	def test_sweep_ignores_orders_with_no_debounce_pending(self):
		order = self._make_order()  # sync_debounce_until never set

		with patch("frappe.enqueue") as mocked_enqueue:
			debounce.sweep_due_sales_order_syncs()

		self.assertEqual(_calls_for_order(mocked_enqueue, order.name), [])

	def test_sweep_ignores_orders_not_yet_due(self):
		order = self._make_order()
		debounce.bump_sync_debounce(order.name, delay_seconds=3600)

		with patch("frappe.enqueue") as mocked_enqueue:
			debounce.sweep_due_sales_order_syncs()

		self.assertEqual(_calls_for_order(mocked_enqueue, order.name), [])

	def test_concurrent_sweep_ticks_do_not_double_enqueue(self):
		"""Verified the same way enqueue_account_sync()'s dedup is verified
		for the inventory sweep (integrations/shopify/test_inventory_sync.py):
		a second frappe.enqueue() call for the same job_id is rejected by RQ's
		own deduplicate=True (simulated here by returning None), so a
		concurrent/overlapping tick must not treat that rejection as an error
		or retry it itself."""
		order = self._make_order()
		debounce.bump_sync_debounce(order.name, delay_seconds=-1)

		with patch("frappe.enqueue", return_value=None) as mocked_enqueue:
			debounce.sweep_due_sales_order_syncs()
			debounce.sweep_due_sales_order_syncs()

		calls = _calls_for_order(mocked_enqueue, order.name)
		self.assertEqual(len(calls), 2)
		self.assertEqual(calls[0].kwargs["job_id"], calls[1].kwargs["job_id"])
		self.assertTrue(calls[0].kwargs["deduplicate"])
		self.assertTrue(calls[1].kwargs["deduplicate"])
