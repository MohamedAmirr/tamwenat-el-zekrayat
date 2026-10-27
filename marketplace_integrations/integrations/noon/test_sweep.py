# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.noon.sweep import sweep_all_accounts, sweep_changed_bins
from marketplace_integrations.integrations.noon.sync import (
	PushPaused,
	enqueue_push,
	is_push_paused,
	push_paused_message,
)

SWEEP = "marketplace_integrations.integrations.noon.sweep"
TICK = datetime(2026, 10, 19, 12, 0, 0)


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

	def test_hourly_sweep_skips_a_paused_kind_but_still_queues_the_other(self):
		def enqueue(worker, kind, account_name, mapping_names=None):
			if kind == "stock":
				raise PushPaused(push_paused_message(kind))
			return {"queued": True}

		with patch(f"{SWEEP}.frappe.get_all", return_value=["ACC-1"]):
			with patch(f"{SWEEP}.enqueue_push", side_effect=enqueue) as queue:
				self.assertEqual(sweep_all_accounts(), 1)
		self.assertEqual([call.args[1] for call in queue.call_args_list], ["pricing", "stock"])


class UnitTestNoonPushSwitch(UnitTestCase):
	def test_enqueue_refuses_a_paused_account_without_enqueueing(self):
		for kind, message in (
			("stock", "Inventory push is paused for this account."),
			("pricing", "Price push is paused for this account."),
		):
			with patch("marketplace_integrations.integrations.noon.sync.is_push_paused", return_value=True):
				with patch("marketplace_integrations.integrations.noon.sync.frappe.enqueue") as enqueue:
					with self.assertRaises(PushPaused) as raised:
						enqueue_push(lambda **kwargs: None, kind, "ACC")
			self.assertEqual(str(raised.exception), message)
			enqueue.assert_not_called()

	def test_manual_enqueue_runs_with_the_switch_off_and_tells_the_worker(self):
		for kind in ("stock", "pricing"):
			with patch("marketplace_integrations.integrations.noon.sync.is_push_paused", return_value=True):
				with patch("marketplace_integrations.integrations.noon.sync.frappe.enqueue") as enqueue:
					enqueue.return_value = object()
					result = enqueue_push(lambda **kwargs: None, kind, "ACC", ["M1"], manual=True)
			self.assertEqual(result, {"queued": True, "switch_off": True})
			enqueue.assert_called_once()
			self.assertIs(enqueue.call_args.kwargs["manual"], True)
			self.assertEqual(enqueue.call_args.kwargs["job_id"], f"marketplace-noon-{kind}-ACC")
			self.assertTrue(enqueue.call_args.kwargs["deduplicate"])

	def test_automatic_enqueue_passes_manual_false_and_flags_no_override(self):
		with patch("marketplace_integrations.integrations.noon.sync.is_push_paused", return_value=False):
			with patch("marketplace_integrations.integrations.noon.sync.frappe.enqueue") as enqueue:
				enqueue.return_value = object()
				result = enqueue_push(lambda **kwargs: None, "stock", "ACC")
		self.assertEqual(result, {"queued": True, "switch_off": False})
		self.assertIs(enqueue.call_args.kwargs["manual"], False)

	def test_a_manual_click_while_a_job_is_queued_is_deduplicated_not_doubled(self):
		with patch("marketplace_integrations.integrations.noon.sync.is_push_paused", return_value=True):
			with patch("marketplace_integrations.integrations.noon.sync.frappe.enqueue", return_value=None):
				result = enqueue_push(lambda **kwargs: None, "stock", "ACC", None, manual=True)
		self.assertEqual(result, {"queued": False, "switch_off": True})

	def test_whitelisted_manual_endpoints_enqueue_with_manual_true_even_when_both_switches_are_off(self):
		from marketplace_integrations.api import noon_catalog
		from marketplace_integrations.integrations.noon import inventory, pricing

		with patch("marketplace_integrations.integrations.noon.sync.is_push_paused", return_value=True):
			with patch("marketplace_integrations.integrations.noon.sync.frappe.enqueue") as enqueue:
				enqueue.return_value = object()
				with patch.object(noon_catalog, "authorize_selection", return_value=["M1"]):
					with patch.object(noon_catalog, "get_account"):
						both = noon_catalog.queue_account_sync("ACC", ["M1"])
				with patch.object(pricing, "authorize_selection", return_value=None):
					with patch.object(pricing, "get_account"):
						price = pricing.queue_pricing_push("ACC")
				with patch.object(inventory, "authorize_selection", return_value=None):
					with patch.object(inventory, "get_account"):
						stock = inventory.queue_stock_push("ACC")
		self.assertEqual(set(both), {"pricing", "stock"})
		self.assertTrue(all(result["queued"] and result["switch_off"] for result in (*both.values(), price, stock)))
		self.assertEqual(enqueue.call_count, 4)
		self.assertTrue(all(call.kwargs["manual"] is True for call in enqueue.call_args_list))

	def test_switches_are_independent_and_read_the_matching_field(self):
		def get_value(doctype, name, field):
			return {"noon_inventory_push_enabled": 0, "noon_price_push_enabled": 1}[field]

		with patch("marketplace_integrations.integrations.noon.sync.frappe.db.get_value", get_value):
			self.assertTrue(is_push_paused("ACC", "stock"))
			self.assertFalse(is_push_paused("ACC", "pricing"))


class UnitTestNoonChangedStockSweep(UnitTestCase):
	def setUp(self):
		self.cache = MagicMock()
		self.cache.get_value.return_value = None
		self.get_all_calls = []
		self.accounts = [SimpleNamespace(name="ACC", warehouse="WH", noon_warehouse_code="NOON-WH")]
		self.mappings = [
			SimpleNamespace(name="M1", item="ITEM-1"),
			SimpleNamespace(name="M2", item="ITEM-2"),
			SimpleNamespace(name="M3", item="ITEM-3"),
		]
		self.changed_items = ["ITEM-1", "ITEM-1", "ITEM-3"]
		for target, value in (
			(f"{SWEEP}.now_datetime", TICK),
			(f"{SWEEP}.frappe.cache", lambda: self.cache),
			(f"{SWEEP}.frappe.get_all", self._get_all),
		):
			patcher = (
				patch(target, new=value)
				if not isinstance(value, datetime)
				else patch(target, return_value=value)
			)
			patcher.start()
			self.addCleanup(patcher.stop)

	def _get_all(self, doctype, **kwargs):
		self.get_all_calls.append((doctype, kwargs))
		if doctype == "Marketplace Account":
			return self.accounts
		if doctype == "Marketplace Product Mapping":
			return self.mappings
		return self.changed_items

	def _run(self, enqueue_result=None, **kwargs):
		with patch(f"{SWEEP}.enqueue_push", **({"return_value": {"queued": True}} | kwargs)) as enqueue:
			sweep_changed_bins()
		return enqueue

	def test_enqueues_one_scoped_job_for_changed_enabled_mappings_only(self):
		enqueue = self._run()
		enqueue.assert_called_once()
		args = enqueue.call_args.args
		self.assertEqual(args[1:], ("stock", "ACC", ["M1", "M3"]))
		account_filters = self.get_all_calls[0][1]["filters"]
		self.assertEqual(
			account_filters,
			{"platform": "Noon", "enabled": 1, "noon_inventory_push_enabled": 1},
		)
		bin_call = next(call for call in self.get_all_calls if call[0] == "Bin")
		self.assertEqual(bin_call[1]["filters"]["warehouse"], "WH")
		self.assertEqual(bin_call[1]["filters"]["item_code"], ["in", ["ITEM-1", "ITEM-2", "ITEM-3"]])
		# Missing watermark: look back one overlap window (10 min) from the tick.
		self.assertEqual(bin_call[1]["filters"]["modified"], [">", datetime(2026, 10, 19, 11, 50, 0)])
		self.cache.set_value.assert_called_once()
		self.assertEqual(self.cache.set_value.call_args.args[1], TICK)
		self.assertIn("noon", self.cache.set_value.call_args.args[0])

	def test_uses_stored_watermark_minus_overlap(self):
		self.cache.get_value.return_value = datetime(2026, 10, 19, 11, 58, 0)
		self._run()
		bin_call = next(call for call in self.get_all_calls if call[0] == "Bin")
		self.assertEqual(bin_call[1]["filters"]["modified"], [">", datetime(2026, 10, 19, 11, 48, 0)])

	def test_nothing_changed_enqueues_nothing_and_advances_watermark(self):
		self.changed_items = []
		enqueue = self._run()
		enqueue.assert_not_called()
		self.cache.set_value.assert_called_once()

	def test_deduplicated_enqueue_holds_watermark_back(self):
		enqueue = self._run(return_value={"queued": False})
		enqueue.assert_called_once()
		self.cache.set_value.assert_not_called()

	def test_account_without_warehouse_is_skipped(self):
		self.accounts = [SimpleNamespace(name="ACC", warehouse=None, noon_warehouse_code="NOON-WH")]
		enqueue = self._run()
		enqueue.assert_not_called()
		self.cache.set_value.assert_not_called()

	def test_switch_turned_off_before_enqueue_is_a_quiet_skip(self):
		with patch(f"{SWEEP}.frappe.log_error") as log_error:
			enqueue = self._run(side_effect=PushPaused("paused"))
		enqueue.assert_called_once()
		log_error.assert_not_called()
		self.cache.set_value.assert_not_called()

	def test_one_account_failure_is_isolated(self):
		self.accounts = [
			SimpleNamespace(name="BAD", warehouse="WH", noon_warehouse_code="X"),
			SimpleNamespace(name="GOOD", warehouse="WH", noon_warehouse_code="X"),
		]
		outcomes = [frappe.ValidationError("boom"), {"queued": True}]
		with patch(f"{SWEEP}.frappe.log_error") as log_error:
			enqueue = self._run(side_effect=outcomes)
		self.assertEqual(enqueue.call_count, 2)
		log_error.assert_called_once()
		self.assertEqual(self.cache.set_value.call_args.args[0].rsplit(":", 1)[1], "GOOD")

	def test_no_noon_http_in_the_sweep(self):
		with patch("marketplace_integrations.integrations.noon.client.NoonClient.request") as request:
			self._run()
		request.assert_not_called()
