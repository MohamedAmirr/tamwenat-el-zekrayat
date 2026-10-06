# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import json
from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import UnitTestCase

from marketplace_integrations.patches.v0_0 import backfill_last_synced_snapshot as backfill


class UnitTestBackfillLastSyncedSnapshot(UnitTestCase):
	def setUp(self):
		self.order = SimpleNamespace(
			name="MKTORD-TEST",
			erpnext_sales_order="SAL-ORD-TEST",
			last_synced_hash="matching-hash",
		)
		self.snapshot = {"items": [{"external_item_id": "line-1", "item_code": "ITEM-1"}]}

	def test_matching_hash_persists_a_line_identified_baseline(self):
		cases = (
			(None, []),
			([], []),
			([{"id": "r2"}, {"id": "r1"}], ["r1", "r2"]),
		)
		for refunds, expected_refund_ids in cases:
			with self.subTest(refunds=refunds):
				marketplace_order = SimpleNamespace(name=self.order.name, refunds=refunds)
				sales_order = self._sales_order()
				with (
					patch.object(backfill.frappe, "get_doc", side_effect=[marketplace_order, sales_order]),
					patch.object(backfill, "build_sales_order_snapshot", return_value=self.snapshot),
					patch.object(backfill, "snapshot_hash", return_value="matching-hash"),
					patch.object(backfill.frappe.db, "set_value") as set_value,
				):
					self.assertTrue(backfill._backfill(self.order))

				set_value.assert_called_once()
				args = set_value.call_args.args
				self.assertEqual(args[:3], ("Marketplace Order", self.order.name, "last_synced_snapshot"))
				stored = json.loads(args[3])
				self.assertEqual(stored["items"][0]["sales_order_item"], "SOI-1")
				self.assertEqual(stored["refund_ids"], expected_refund_ids)

	def test_unpairable_sales_order_rows_do_not_persist_a_baseline(self):
		sales_order = self._sales_order()
		sales_order.items.append(SimpleNamespace(name="SOI-2", item_code="ITEM-2"))
		with (
			patch.object(
				backfill.frappe,
				"get_doc",
				side_effect=[SimpleNamespace(name=self.order.name, refunds=None), sales_order],
			),
			patch.object(backfill, "build_sales_order_snapshot", return_value=self.snapshot),
			patch.object(backfill, "snapshot_hash", return_value="matching-hash"),
			patch.object(backfill.frappe.db, "set_value") as set_value,
		):
			self.assertFalse(backfill._backfill(self.order))

		set_value.assert_not_called()

	def _sales_order(self):
		return SimpleNamespace(
			name=self.order.erpnext_sales_order,
			items=[SimpleNamespace(name="SOI-1", item_code="ITEM-1")],
		)

	def test_divergent_or_missing_hash_does_not_persist_a_baseline(self):
		for hash_value in ("different-hash", None):
			with self.subTest(hash_value=hash_value):
				self.order.last_synced_hash = hash_value
				with (
					patch.object(backfill.frappe, "get_doc", side_effect=[object(), object()]),
					patch.object(backfill, "build_sales_order_snapshot", return_value=self.snapshot),
					patch.object(backfill, "snapshot_hash", return_value="matching-hash"),
					patch.object(backfill.frappe.db, "set_value") as set_value,
				):
					self.assertFalse(backfill._backfill(self.order))

				set_value.assert_not_called()

	def test_sync_blocker_relabels_submitted_orders_without_an_error_log(self):
		self._execute_with(backfill.SyncBlocker("cancelled"), docstatus=1, status="Synced")
		self.mark.assert_called_once_with(self.order.name)
		self.log_error.assert_not_called()

	def test_sync_blocker_does_not_relabel_draft_or_sync_error_orders(self):
		for docstatus, status in ((0, "Synced"), (1, "Sync Error")):
			with self.subTest(docstatus=docstatus, status=status):
				self._execute_with(backfill.SyncBlocker("cancelled"), docstatus=docstatus, status=status)
				self.mark.assert_not_called()
				self.log_error.assert_not_called()

	def test_real_exception_is_logged_and_relabels_only_submitted_orders(self):
		self._execute_with(RuntimeError("boom"), docstatus=1, status="Synced")
		self.mark.assert_called_once_with(self.order.name)
		self.log_error.assert_called_once()

		self._execute_with(RuntimeError("boom"), docstatus=0, status="Synced")
		self.mark.assert_not_called()
		self.log_error.assert_called_once()

	def test_matching_hash_is_written_for_draft_and_sync_error_orders_without_relabel(self):
		for docstatus, status in ((0, "Synced"), (1, "Sync Error")):
			with self.subTest(docstatus=docstatus, status=status):
				self._execute_with(True, docstatus=docstatus, status=status)
				self.mark.assert_not_called()

	def test_diverged_hash_relabels_only_submitted_non_error_orders(self):
		for docstatus, status, relabelled in ((1, "Synced", True), (0, "Synced", False), (1, "Sync Error", False)):
			with self.subTest(docstatus=docstatus, status=status):
				self._execute_with(False, docstatus=docstatus, status=status)
				self.assertEqual(self.mark.called, relabelled)

	def _execute_with(self, backfill_result, docstatus, status):
		order = SimpleNamespace(
			name=self.order.name,
			erpnext_sales_order=self.order.erpnext_sales_order,
			last_synced_hash="matching-hash",
			last_synced_snapshot=None,
			erpnext_sync_status=status,
		)
		backfill_kwargs = (
			{"side_effect": backfill_result}
			if isinstance(backfill_result, Exception)
			else {"return_value": backfill_result}
		)
		with (
			patch.object(backfill.frappe.db, "table_exists", return_value=True),
			patch.object(backfill.frappe.db, "has_column", return_value=True),
			patch.object(backfill.frappe, "get_all", return_value=[order]),
			patch.object(backfill.frappe.db, "get_value", return_value=docstatus),
			patch.object(backfill, "_backfill", **backfill_kwargs),
			patch.object(backfill, "_mark_no_baseline") as self.mark,
			patch.object(backfill.frappe, "log_error") as self.log_error,
		):
			backfill.execute()
