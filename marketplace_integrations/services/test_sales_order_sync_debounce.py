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
from marketplace_integrations.services.linked_sales_order_fixture import LinkedSalesOrderFixture
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

		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook")

		value = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")
		self.assertIsNotNone(value)
		self.assertGreater(value, before)

	def test_bump_records_the_pending_trigger_source(self):
		order = self._make_order()

		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook")

		self.assertEqual(
			frappe.db.get_value("Marketplace Order", order.name, "pending_sync_trigger_source"),
			"Talabat Webhook",
		)

	def test_second_bump_within_window_pushes_it_further_out(self):
		order = self._make_order()

		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook", delay_seconds=45)
		first = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")
		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook", delay_seconds=90)
		second = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")

		self.assertGreater(second, first)

	def test_second_bump_with_a_different_trigger_source_overwrites_the_first(self):
		"""#135: the latest bump_sync_debounce() call's trigger_source wins - no
		merge logic, a simple overwrite matching the timestamp's own
		always-pushes-forward semantics."""
		order = self._make_order()

		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook", delay_seconds=45)
		debounce.bump_sync_debounce(order.name, trigger_source="Shopify Webhook", delay_seconds=90)

		self.assertEqual(
			frappe.db.get_value("Marketplace Order", order.name, "pending_sync_trigger_source"),
			"Shopify Webhook",
		)

	def test_two_bumps_result_in_exactly_one_eventual_sync(self):
		"""Two bump_sync_debounce() calls for the same order within the delay
		window, followed by a sweep tick once the (later) window is due, run
		sync_sales_order() exactly once. ``frappe.enqueue`` is patched to call
		its job function immediately (synchronously) in place of a real RQ
		worker, so the sweep -> _run_debounced_sync hand-off is exercised
		end-to-end without needing a running worker."""
		order = self._make_order()

		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook", delay_seconds=-10)
		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook", delay_seconds=-5)

		def run_immediately(func, **kwargs):
			job_kwargs = {
				key: value
				for key, value in kwargs.items()
				if key not in {"queue", "timeout", "job_id", "deduplicate"}
			}
			return func(**job_kwargs)

		with patch(SYNC_TARGET) as mocked_sync, patch("frappe.enqueue", side_effect=run_immediately):
			debounce.sweep_due_sales_order_syncs()

		self.assertEqual(
			_calls_for_order(mocked_sync, order.name),
			[call(order.name, trigger_source="Talabat Webhook")],
		)
		self.assertIsNone(frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until"))

	def test_stale_expected_debounce_until_no_ops(self):
		order = self._make_order()
		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook", delay_seconds=45)
		stale_expected = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")

		# A newer webhook pushes the window further out after the sweep tick
		# already read the (now stale) value above.
		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook", delay_seconds=90)
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
		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook", delay_seconds=45)
		expected = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")

		with patch(SYNC_TARGET) as mocked_sync:
			debounce._run_debounced_sync(order.name, expected)

		mocked_sync.assert_called_once_with(order.name, trigger_source="Talabat Webhook")
		self.assertIsNone(frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until"))
		self.assertIsNone(frappe.db.get_value("Marketplace Order", order.name, "pending_sync_trigger_source"))

	def test_run_debounced_sync_restores_the_window_when_the_sync_crashes(self):
		order = self._make_order()
		debounce.bump_sync_debounce(order.name, trigger_source="Shopify Webhook", delay_seconds=45)
		expected = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")

		# The test runs in one transaction: neutralise the rollback/commit the crash path does.
		with patch(SYNC_TARGET, side_effect=RuntimeError("deadlock")), patch("frappe.db.rollback"), patch(
			"frappe.db.commit"
		):
			with self.assertRaises(RuntimeError):
				debounce._run_debounced_sync(order.name, expected)

		self.assertIsNotNone(frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until"))
		self.assertEqual(
			frappe.db.get_value("Marketplace Order", order.name, "pending_sync_trigger_source"),
			"Shopify Webhook",
		)

	def test_run_debounced_sync_passes_through_shopify_webhook_trigger_source(self):
		order = self._make_order()
		debounce.bump_sync_debounce(order.name, trigger_source="Shopify Webhook", delay_seconds=45)
		expected = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")

		with patch(SYNC_TARGET) as mocked_sync:
			debounce._run_debounced_sync(order.name, expected)

		mocked_sync.assert_called_once_with(order.name, trigger_source="Shopify Webhook")

	def test_run_debounced_sync_passes_through_talabat_webhook_trigger_source(self):
		order = self._make_order()
		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook", delay_seconds=45)
		expected = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")

		with patch(SYNC_TARGET) as mocked_sync:
			debounce._run_debounced_sync(order.name, expected)

		mocked_sync.assert_called_once_with(order.name, trigger_source="Talabat Webhook")

	def test_run_debounced_sync_falls_back_to_manual_default_when_trigger_source_is_blank(self):
		"""Defensive fallback (#135): shouldn't happen given bump_sync_debounce()
		always sets both fields together, but a blank pending_sync_trigger_source
		at execution time must fall back to sync_sales_order()'s own "Manual"
		default rather than crashing."""
		order = self._make_order()
		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook", delay_seconds=45)
		expected = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")
		frappe.db.set_value(
			"Marketplace Order", order.name, "pending_sync_trigger_source", None, update_modified=False
		)

		with patch(SYNC_TARGET) as mocked_sync:
			debounce._run_debounced_sync(order.name, expected)

		mocked_sync.assert_called_once_with(order.name)

	def test_sweep_enqueues_one_deduplicated_job_per_due_order(self):
		order = self._make_order()
		# already due
		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook", delay_seconds=-1)

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
		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook", delay_seconds=3600)

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
		debounce.bump_sync_debounce(order.name, trigger_source="Talabat Webhook", delay_seconds=-1)

		with patch("frappe.enqueue", return_value=None) as mocked_enqueue:
			debounce.sweep_due_sales_order_syncs()
			debounce.sweep_due_sales_order_syncs()

		calls = _calls_for_order(mocked_enqueue, order.name)
		self.assertEqual(len(calls), 2)
		self.assertEqual(calls[0].kwargs["job_id"], calls[1].kwargs["job_id"])
		self.assertTrue(calls[0].kwargs["deduplicate"])
		self.assertTrue(calls[1].kwargs["deduplicate"])

	# -------------------------------------------------------------------
	# #178 part 3: bump_if_linked_order_changed() / order_evidence_fingerprint()
	# -------------------------------------------------------------------

	def _link(self, order):
		# A REAL Sales Order (not a made-up name), so link validation on any later save passes.
		fixture = LinkedSalesOrderFixture()
		self.addCleanup(fixture.cleanup)
		fixture.link(order.name)

	def _fingerprint_before(self, order):
		return debounce.linked_order_fingerprint(order.marketplace_account, order.external_order_id)

	def _debounce_state(self, order):
		return frappe.db.get_value(
			"Marketplace Order",
			order.name,
			["sync_debounce_until", "pending_sync_trigger_source"],
			as_dict=True,
		)

	def test_changed_fingerprint_on_a_linked_order_bumps_once_with_the_trigger_source(self):
		order = self._make_order()
		self._link(order)
		before = self._fingerprint_before(order)
		frappe.db.set_value("Marketplace Order", order.name, "external_status", "CANCELLED")

		bumped = debounce.bump_if_linked_order_changed(order.name, before, "Noon Order Import")

		self.assertTrue(bumped)
		state = self._debounce_state(order)
		self.assertIsNotNone(state.sync_debounce_until)
		self.assertEqual(state.pending_sync_trigger_source, "Noon Order Import")

	def test_unchanged_fingerprint_does_not_bump_and_a_replay_does_not_bump_again(self):
		order = self._make_order()
		self._link(order)
		before = self._fingerprint_before(order)

		self.assertFalse(debounce.bump_if_linked_order_changed(order.name, before, "Noon Order Import"))
		self.assertIsNone(self._debounce_state(order).sync_debounce_until)

		frappe.db.set_value("Marketplace Order", order.name, "external_status", "CANCELLED")
		self.assertTrue(debounce.bump_if_linked_order_changed(order.name, before, "Noon Order Import"))
		# Replay: the next import starts from the already-changed state.
		replay_before = self._fingerprint_before(order)
		self.assertFalse(debounce.bump_if_linked_order_changed(order.name, replay_before, "Noon Order Import"))

	def test_shopify_successful_payment_transaction_changes_the_fingerprint(self):
		order = self._make_order()
		frappe.db.set_value("Marketplace Order", order.name, "platform", "Shopify")
		frappe.db.set_value("Marketplace Order", order.name, "payment_transactions", frappe.as_json([]))
		order.reload()
		before = debounce.order_evidence_fingerprint(order)
		frappe.db.set_value(
			"Marketplace Order",
			order.name,
			"payment_transactions",
			frappe.as_json(
				[
					{
						"id": "gid://shopify/OrderTransaction/123",
						"kind": "SALE",
						"status": "SUCCESS",
						"gateway": "Kashier",
						"amountSet": {"shopMoney": {"amount": "25.00", "currencyCode": "EGP"}},
						"processedAt": "2026-10-23T10:00:00Z",
					}
				]
			),
		)
		order.reload()
		self.assertNotEqual(before, debounce.order_evidence_fingerprint(order))

	def test_shopify_successful_transaction_gateway_amount_and_date_changes_fingerprint(self):
		order = self._make_order()
		frappe.db.set_value("Marketplace Order", order.name, "platform", "Shopify")
		self._link(order)
		base = {
			"id": "gid://shopify/OrderTransaction/123",
			"kind": "SALE",
			"status": "SUCCESS",
			"gateway": None,
			"amountSet": {"shopMoney": {"amount": "25.00", "currencyCode": "EGP"}},
			"processedAt": "2026-10-23T10:00:00Z",
			"createdAt": "2026-10-23T09:00:00Z",
		}
		frappe.db.set_value("Marketplace Order", order.name, "payment_transactions", frappe.as_json([base]))
		order.reload()
		for field, value in (
			("gateway", "Kashier"),
			("amountSet", {"shopMoney": {"amount": "26.00", "currencyCode": "EGP"}}),
			("processedAt", "2026-10-23T11:00:00Z"),
			("createdAt", "2026-10-23T08:00:00Z"),
		):
			before = self._fingerprint_before(order)
			changed = {**base, field: value}
			frappe.db.set_value(
				"Marketplace Order", order.name, "payment_transactions", frappe.as_json([changed])
			)
			order.reload()
			self.assertTrue(
				debounce.bump_if_linked_order_changed(order.name, before, "Shopify Order Import"), field
			)

	def test_unlinked_order_never_bumps(self):
		order = self._make_order()
		before = self._fingerprint_before(order)
		self.assertIsNone(before)
		frappe.db.set_value("Marketplace Order", order.name, "external_status", "CANCELLED")

		self.assertFalse(debounce.bump_if_linked_order_changed(order.name, before, "Noon Order Import"))
		self.assertIsNone(self._debounce_state(order).sync_debounce_until)

	def test_volatile_field_only_change_does_not_bump(self):
		order = self._make_order()
		self._link(order)
		before = self._fingerprint_before(order)
		frappe.db.set_value(
			"Marketplace Order",
			order.name,
			{
				"last_platform_update_on": now_datetime(),
				"latest_payload": frappe.as_json({"order_id": "external-order", "updated_at": "2099-01-01"}),
			},
		)

		self.assertFalse(debounce.bump_if_linked_order_changed(order.name, before, "Noon Order Import"))

	def test_fingerprint_ignores_zeroed_noon_rows_but_sees_status_and_shipment_changes(self):
		def row(**values):
			return frappe._dict({"external_item_id": "sku:A:x:y", "external_sku": "A", **values})

		base = frappe._dict(
			platform="Noon",
			items=[row(external_status="CONFIRMED", integration_status="ACKNOWLEDGED", current_quantity=2)],
			noon_shipments=[],
			noon_shipment_units=[],
			refunds=None,
		)
		with_stale = frappe._dict(
			base, items=base["items"] + [row(external_item_id="sku:A:z", current_quantity=0)]
		)
		shipped = frappe._dict(
			base, items=[row(external_status="CONFIRMED", integration_status="SHIPPED", current_quantity=2)]
		)
		in_shipment = frappe._dict(
			base,
			noon_shipments=[frappe._dict(integration_shipment_nr="S-1", status="Created")],
			noon_shipment_units=[frappe._dict(integration_shipment_nr="S-1", mp_item_nr="1")],
		)
		fingerprint = debounce.order_evidence_fingerprint

		self.assertEqual(fingerprint(base), fingerprint(with_stale))
		self.assertNotEqual(fingerprint(base), fingerprint(shipped))
		self.assertNotEqual(fingerprint(base), fingerprint(in_shipment))

	def test_linked_refunded_order_does_not_bump_when_unchanged_and_bumps_on_a_new_refund(self):
		"""The "before" side is built from the same document + builder as the "after"
		side, so refund evidence is symmetric: an unchanged re-import of a refunded
		linked order never bumps, a newly persisted refund does."""
		order = self._make_order()
		self._link(order)
		frappe.db.set_value("Marketplace Order", order.name, "refunds", frappe.as_json([{"id": "R-1"}]))
		before = self._fingerprint_before(order)

		self.assertFalse(debounce.bump_if_linked_order_changed(order.name, before, "Manual"))

		frappe.db.set_value(
			"Marketplace Order", order.name, "refunds", frappe.as_json([{"id": "R-1"}, {"id": "R-2"}])
		)
		self.assertTrue(debounce.bump_if_linked_order_changed(order.name, before, "Manual"))

	def test_fingerprint_sees_snapshot_inputs_dates_currency_and_missing_flags(self):
		def row(**values):
			return frappe._dict({"external_item_id": "line-1", "external_sku": "A", **values})

		base = frappe._dict(platform="Talabat", items=[row(fulfilled_total_price=10)], refunds=None)
		fingerprint = debounce.order_evidence_fingerprint
		variants = [
			frappe._dict(base, received_on="2026-09-01 10:00:00"),
			frappe._dict(base, processed_on="2026-09-01 10:00:00"),
			frappe._dict(base, promised_for="2026-09-02 10:00:00"),
			frappe._dict(base, presentment_currency="USD"),
			frappe._dict(base, items=[row(fulfilled_total_price=10, fulfilled_total_price_missing=1)]),
			frappe._dict(base, items=[row(fulfilled_total_price=10, transaction_amount_missing=1)]),
		]
		for variant in variants:
			self.assertNotEqual(fingerprint(base), fingerprint(variant))

		# A time-of-day-only change does not change the snapshot's date, so no bump.
		dated = frappe._dict(base, received_on="2026-09-01 10:00:00")
		same_day = frappe._dict(base, received_on="2026-09-01 23:59:59")
		self.assertEqual(fingerprint(dated), fingerprint(same_day))

	# -------------------------------------------------------------------
	# #180: sweep_stuck_sales_order_reconciliations()
	# -------------------------------------------------------------------

	def test_a_bump_never_reopens_an_order_cancelled_before_any_sales_order(self):
		order = self._make_order()
		frappe.db.set_value("Marketplace Order", order.name, "erpnext_sync_status", "Cancelled")

		debounce.bump_sync_debounce(order.name, "Shopify Webhook")

		values = frappe.db.get_value(
			"Marketplace Order", order.name, ["erpnext_sync_status", "sync_debounce_until"], as_dict=True
		)
		self.assertEqual(values.erpnext_sync_status, "Cancelled")
		self.assertTrue(values.sync_debounce_until)

	def test_the_stuck_sweep_ignores_a_cancelled_order_but_picks_a_lost_syncing_one(self):
		cancelled = self._stuck_order(status="Cancelled", platform="Shopify")
		syncing = self._stuck_order(status="Syncing", platform="Shopify")
		candidates = debounce._stuck_reconciliation_candidates(500)
		self.assertNotIn(cancelled.name, candidates)
		self.assertIn(syncing.name, candidates)

	def _stuck_order(
		self,
		status="Synced",
		so_closed=False,
		platform=None,
		checked_hours_ago=None,
		per_delivered=0,
		per_billed=0,
		so_cancelled=False,
		outbound_cancelled=False,
		cancelled_at=None,
	):
		order = self._make_order()
		fixture = LinkedSalesOrderFixture()
		self.addCleanup(fixture.cleanup)
		fixture.link(order.name)
		values = {"erpnext_sync_status": status}
		if platform:
			values["platform"] = platform
		if outbound_cancelled:
			values["outbound_cancelled_on"] = now_datetime()
		if cancelled_at:
			values["cancelled_at"] = cancelled_at
		if checked_hours_ago is not None:
			values["last_reconciliation_check_on"] = add_to_date(now_datetime(), hours=-checked_hours_ago)
		frappe.db.set_value("Marketplace Order", order.name, values, update_modified=False)
		if so_closed:
			frappe.db.set_value(
				"Sales Order",
				fixture.name,
				{
					"docstatus": 1,
					"status": "Closed",
					"per_delivered": per_delivered,
					"per_billed": per_billed,
				},
			)
			# LIFO: runs before fixture.cleanup, which cannot delete a submitted Sales Order.
			self.addCleanup(
				frappe.db.set_value, "Sales Order", fixture.name, {"docstatus": 0, "status": "Draft"}
			)
		if so_cancelled:
			frappe.db.set_value("Sales Order", fixture.name, {"docstatus": 2, "status": "Cancelled"})
			self.addCleanup(
				frappe.db.set_value, "Sales Order", fixture.name, {"docstatus": 0, "status": "Draft"}
			)
		return order

	def _candidates(self, limit=100000):
		return debounce._stuck_reconciliation_candidates(limit)

	def test_stuck_sweep_picks_closed_attention_orders_for_every_platform(self):
		picked = [
			self._stuck_order(status="Needs Action", platform="Talabat"),
			self._stuck_order(status="Sync Error", platform="Shopify"),
			self._stuck_order(so_closed=True, platform="Noon"),
			self._stuck_order(so_closed=True, platform="Talabat"),
		]
		ignored = self._stuck_order(status="Synced")
		unlinked = self._make_order()
		frappe.db.set_value("Marketplace Order", unlinked.name, "erpnext_sync_status", "Sync Error")
		names = self._candidates()
		for order in picked:
			self.assertIn(order.name, names)
		self.assertNotIn(ignored.name, names)
		self.assertNotIn(unlinked.name, names)

	def test_stuck_sweep_picks_unlinked_shopify_orders_that_never_synced(self):
		def unlinked(platform, status, **extra):
			order = self._make_order()
			frappe.db.set_value(
				"Marketplace Order",
				order.name,
				{"platform": platform, "erpnext_sync_status": status, **extra},
				update_modified=False,
			)
			return order.name

		lost = unlinked("Shopify", "Syncing")
		failed_long_ago = unlinked(
			"Shopify",
			"Sync Error",
			last_reconciliation_check_on=add_to_date(now_datetime(), hours=-30),
		)
		recently_checked = unlinked(
			"Shopify",
			"Sync Error",
			last_reconciliation_check_on=add_to_date(now_datetime(), hours=-2),
		)
		cancelled = unlinked("Shopify", "Syncing", cancelled_at=now_datetime())
		other_platform = unlinked("Talabat", "Syncing")
		names = self._candidates()
		self.assertIn(lost, names)
		self.assertIn(failed_long_ago, names)
		for skipped in (recently_checked, cancelled, other_platform):
			self.assertNotIn(skipped, names)

	def test_cancelled_and_closed_with_delivery_orders_are_candidates(self):
		"""#181: the platform order may still be active behind an ERPNext cancel/close."""
		cancelled = self._stuck_order(so_cancelled=True)
		delivered = self._stuck_order(so_closed=True, per_delivered=50)
		billed = self._stuck_order(so_closed=True, per_billed=100)
		names = self._candidates()
		for order in (cancelled, delivered, billed):
			self.assertIn(order.name, names)

	def test_cancelled_and_closed_with_delivery_orders_back_off_for_24h(self):
		status = "Needs Action"
		recent_cancelled = self._stuck_order(status=status, so_cancelled=True, checked_hours_ago=20)
		old_cancelled = self._stuck_order(status=status, so_cancelled=True, checked_hours_ago=25)
		recent_delivered = self._stuck_order(
			status=status, so_closed=True, per_delivered=50, checked_hours_ago=20
		)
		old_delivered = self._stuck_order(status=status, so_closed=True, per_delivered=50, checked_hours_ago=25)
		names = self._candidates()
		self.assertNotIn(recent_cancelled.name, names)
		self.assertNotIn(recent_delivered.name, names)
		self.assertIn(old_cancelled.name, names)
		self.assertIn(old_delivered.name, names)

	def test_converged_cancelled_and_closed_with_delivery_orders_are_skipped(self):
		"""Synced and already checked: nothing left to report, so no daily re-check."""
		cancelled = self._stuck_order(so_cancelled=True, checked_hours_ago=48)
		delivered = self._stuck_order(so_closed=True, per_delivered=50, checked_hours_ago=48)
		names = self._candidates()
		self.assertNotIn(cancelled.name, names)
		self.assertNotIn(delivered.name, names)

	def test_never_checked_cancelled_order_is_a_candidate(self):
		order = self._stuck_order(so_cancelled=True, checked_hours_ago=None)
		self.assertIn(order.name, self._candidates())

	def test_orders_with_an_outbound_cancel_marker_are_never_candidates(self):
		for kwargs in (
			{"so_closed": True},
			{"so_cancelled": True},
			{"so_closed": True, "per_delivered": 50},
			{"status": "Needs Action"},
		):
			with self.subTest(**kwargs):
				order = self._stuck_order(outbound_cancelled=True, **kwargs)
				self.assertNotIn(order.name, self._candidates())

	def test_shopify_outbound_cancel_marker_is_skipped_only_after_cancel_echo(self):
		pending = self._stuck_order(
			status="Needs Action", platform="Shopify", outbound_cancelled=True
		)
		confirmed = self._stuck_order(
			status="Needs Action",
			platform="Shopify",
			outbound_cancelled=True,
			cancelled_at=now_datetime(),
		)
		names = self._candidates()
		self.assertIn(pending.name, names)
		self.assertNotIn(confirmed.name, names)

	def test_sweep_does_not_overwrite_a_webhook_bump_made_after_the_candidate_query(self):
		order = self._stuck_order(status="Sync Error")
		real = debounce._stuck_reconciliation_candidates

		def candidates_then_webhook_bump(limit):
			names = real(limit)
			debounce.bump_sync_debounce(order.name, "Talabat Webhook")
			return names

		with patch.object(debounce, "_stuck_reconciliation_candidates", candidates_then_webhook_bump):
			debounce.sweep_stuck_sales_order_reconciliations()
		state = frappe.db.get_value(
			"Marketplace Order",
			order.name,
			["pending_sync_trigger_source", "last_reconciliation_check_on"],
			as_dict=True,
		)
		self.assertEqual(state.pending_sync_trigger_source, "Talabat Webhook")
		self.assertFalse(state.last_reconciliation_check_on)

	def test_failed_bump_leaves_the_check_time_unset(self):
		order = self._stuck_order(status="Sync Error")
		with (
			patch.object(debounce, "_stuck_reconciliation_candidates", return_value=[order.name]),
			patch.object(debounce.frappe.db, "set_value", side_effect=RuntimeError("boom")),
			self.assertRaises(RuntimeError),
		):
			debounce.sweep_stuck_sales_order_reconciliations()
		self.assertFalse(frappe.db.get_value("Marketplace Order", order.name, "last_reconciliation_check_on"))

	def test_sweep_end_to_end_bumps_syncs_once_and_second_pass_changes_nothing(self):
		order = self._stuck_order(so_closed=True, status="Synced")
		so_name = frappe.db.get_value("Marketplace Order", order.name, "erpnext_sales_order")
		mutations = []

		def fake_sync(name, trigger_source="Manual"):
			# Stands in for reopen + cancel: one mutation, ends Synced and no longer Closed.
			mutations.append((name, trigger_source))
			frappe.db.set_value("Sales Order", so_name, "status", "Cancelled")
			frappe.db.set_value("Marketplace Order", name, "erpnext_sync_status", "Synced")

		debounce.sweep_stuck_sales_order_reconciliations()
		until = frappe.db.get_value("Marketplace Order", order.name, "sync_debounce_until")
		with patch(SYNC_TARGET, side_effect=fake_sync):
			debounce._run_debounced_sync(order.name, until)
		self.assertEqual(mutations, [(order.name, "Scheduled Sweep")])

		# Advance past the 24h back-off and sweep again: nothing is stuck any more.
		frappe.db.set_value(
			"Marketplace Order",
			order.name,
			"last_reconciliation_check_on",
			add_to_date(now_datetime(), hours=-25),
			update_modified=False,
		)
		self.assertNotIn(order.name, self._candidates())
		debounce.sweep_stuck_sales_order_reconciliations()
		state = frappe.db.get_value(
			"Marketplace Order",
			order.name,
			["sync_debounce_until", "pending_sync_trigger_source"],
			as_dict=True,
		)
		self.assertFalse(state.sync_debounce_until)
		self.assertFalse(state.pending_sync_trigger_source)
		self.assertEqual(len(mutations), 1)
		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "status"), "Cancelled")

	def test_stuck_sweep_skips_orders_with_a_pending_debounce(self):
		order = self._stuck_order(status="Sync Error")
		debounce.bump_sync_debounce(order.name, "Talabat Webhook")
		self.assertNotIn(order.name, self._candidates())

	def test_stuck_sweep_honors_batch_cap_and_oldest_check_first(self):
		newer = self._stuck_order(status="Sync Error", checked_hours_ago=48)
		older = self._stuck_order(status="Sync Error", checked_hours_ago=72)
		names = self._candidates()
		self.assertLess(names.index(older.name), names.index(newer.name))
		self.assertEqual(len(self._candidates(limit=1)), 1)

	def test_stuck_sweep_backs_off_attention_and_closed_orders_for_24h(self):
		recent_attention = self._stuck_order(status="Sync Error", checked_hours_ago=2)
		old_attention = self._stuck_order(status="Needs Action", checked_hours_ago=25)
		recent_closed = self._stuck_order(so_closed=True, checked_hours_ago=20)
		old_closed = self._stuck_order(so_closed=True, checked_hours_ago=25)
		names = self._candidates()
		self.assertNotIn(recent_attention.name, names)
		self.assertNotIn(recent_closed.name, names)
		self.assertIn(old_attention.name, names)
		self.assertIn(old_closed.name, names)

	def test_stuck_sweep_bumps_with_scheduled_sweep_and_second_run_is_a_no_op(self):
		order = self._stuck_order(so_closed=True)
		debounce.sweep_stuck_sales_order_reconciliations()
		state = frappe.db.get_value(
			"Marketplace Order",
			order.name,
			["sync_debounce_until", "pending_sync_trigger_source", "last_reconciliation_check_on"],
			as_dict=True,
		)
		self.assertEqual(state.pending_sync_trigger_source, "Scheduled Sweep")
		self.assertTrue(state.sync_debounce_until)
		self.assertTrue(state.last_reconciliation_check_on)
		debounce.sweep_stuck_sales_order_reconciliations()
		again = frappe.db.get_value(
			"Marketplace Order",
			order.name,
			["sync_debounce_until", "last_reconciliation_check_on"],
			as_dict=True,
		)
		self.assertEqual(again.sync_debounce_until, state.sync_debounce_until)
		self.assertEqual(again.last_reconciliation_check_on, state.last_reconciliation_check_on)

	def test_stuck_sweep_is_registered_hourly_and_resolves(self):
		path = "marketplace_integrations.services.sales_order_sync_debounce.sweep_stuck_sales_order_reconciliations"
		self.assertIn(path, frappe.get_hooks("scheduler_events")["hourly"])
		self.assertIs(frappe.get_attr(path), debounce.sweep_stuck_sales_order_reconciliations)
