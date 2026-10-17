# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Every test here mocks ShopifyClient.execute() directly - never a real
Shopify call - following the same pattern as test_products.py/
test_inventory_sync.py. This module lives outside any doctype/ folder, so
IntegrationTestCase's auto-test-record generation never runs for it (see
docs/frappe-conventions.md's "A test module that lives outside any doctype/
folder" note) - fixture documents (Marketplace Account, Item) are built
directly, same as always."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify import orders
from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.test_marketplace_account import (
	STAND_IN_CONNECTION,
	STAND_IN_CONNECTION_DOCTYPE,
)
from marketplace_integrations.services.linked_sales_order_fixture import LinkedSalesOrderFixture

EXECUTE_TARGET = "marketplace_integrations.integrations.shopify.client.ShopifyClient.execute"

# #109's own acceptance criterion: "Refund evidence produces no accounting or
# stock document." Every doctype the shared upsert must never touch, per this
# issue's "how enforced" section.
NO_SIDE_EFFECT_DOCTYPES = (
	"Stock Ledger Entry",
	"Stock Entry",
	"GL Entry",
	"Journal Entry",
	"Sales Order",
	"Sales Invoice",
	"Delivery Note",
	"Payment Entry",
)


def _order_gid(suffix):
	return f"gid://shopify/Order/{suffix}"


def make_order_payload(order_gid, updated_at="2026-09-27T10:05:00Z", **overrides):
	"""Build one Shopify GraphQL Order node, including a refund with a
	refundLineItem carrying restockType - the exact fixture shape #109's own
	acceptance criterion requires a test for."""
	value = {
		"id": order_gid,
		"name": "#2001",
		"createdAt": "2026-09-27T10:00:00Z",
		"updatedAt": updated_at,
		"cancelledAt": None,
		"cancelReason": None,
		"closed": False,
		"closedAt": None,
		"displayFinancialStatus": "PARTIALLY_REFUNDED",
		"displayFulfillmentStatus": "FULFILLED",
		"currencyCode": "EGP",
		"presentmentCurrencyCode": "EGP",
		"currentSubtotalPriceSet": {"shopMoney": {"amount": "80.00", "currencyCode": "EGP"}},
		"currentTotalDiscountsSet": {"shopMoney": {"amount": "0.00", "currencyCode": "EGP"}},
		"currentTotalTaxSet": {"shopMoney": {"amount": "8.00", "currencyCode": "EGP"}},
		"currentShippingPriceSet": {"shopMoney": {"amount": "5.00", "currencyCode": "EGP"}},
		"currentTotalDutiesSet": {"shopMoney": {"amount": "0.00", "currencyCode": "EGP"}},
		"currentTotalPriceSet": {"shopMoney": {"amount": "93.00", "currencyCode": "EGP"}},
		"transactions": [
			{
				"id": "gid://shopify/OrderTransaction/1",
				"kind": "SALE",
				"status": "SUCCESS",
				"amountSet": {"shopMoney": {"amount": "111.00", "currencyCode": "EGP"}},
				"createdAt": "2026-09-27T10:00:05Z",
				"processedAt": "2026-09-27T10:00:05Z",
			}
		],
		"refunds": [
			{
				"id": "gid://shopify/Refund/1",
				"createdAt": "2026-09-27T10:04:00Z",
				"note": "Customer changed their mind",
				"totalRefundedSet": {"shopMoney": {"amount": "18.00", "currencyCode": "EGP"}},
				"refundLineItems": {
					"pageInfo": {"hasNextPage": False},
					"nodes": [
						{
							"id": "gid://shopify/RefundLineItem/1",
							"quantity": 1,
							"restockType": "RESTOCK",
							"lineItem": {"id": "gid://shopify/LineItem/1"},
							"subtotalSet": {"shopMoney": {"amount": "16.00", "currencyCode": "EGP"}},
							"totalTaxSet": {"shopMoney": {"amount": "2.00", "currencyCode": "EGP"}},
						}
					],
				},
			}
		],
		"lineItems": {
			"pageInfo": {"hasNextPage": False},
			"nodes": [
				{
					"id": "gid://shopify/LineItem/1",
					"name": "Coffee",
					"sku": "COFFEE-1",
					"quantity": 3,
					"currentQuantity": 2,
					"unfulfilledQuantity": 2,
					"nonFulfillableQuantity": 0,
					"variant": {"id": "gid://shopify/ProductVariant/1"},
					"originalUnitPriceSet": {"shopMoney": {"amount": "20.00", "currencyCode": "EGP"}},
					"discountAllocations": [],
				}
			],
		},
	}
	value.update(overrides)
	return value


class IntegrationTestImportOrderNoSideEffects(IntegrationTestCase):
	def setUp(self):
		self.account = make_shopify_account(f"Order Import {frappe.generate_hash(length=8)}")

	def test_refund_with_restock_line_item_creates_no_accounting_or_stock_documents(self):
		before = {doctype: frappe.db.count(doctype) for doctype in NO_SIDE_EFFECT_DOCTYPES}
		order_gid = _order_gid(frappe.generate_hash(length=8))
		payload = make_order_payload(order_gid)

		with patch(EXECUTE_TARGET, return_value={"order": payload}):
			result = orders.import_order(self.account.name, order_gid)

		self.assertTrue(result)
		self.assertEqual(result.doctype, "Marketplace Order")
		for doctype in NO_SIDE_EFFECT_DOCTYPES:
			self.assertEqual(frappe.db.count(doctype), before[doctype], f"{doctype} row count changed")

	def test_shipping_lines_are_persisted_and_query_asks_for_them(self):
		order_gid = _order_gid(frappe.generate_hash(length=8))
		payload = make_order_payload(order_gid)
		payload["requiresShipping"] = True
		payload["shippingLines"] = {
			"pageInfo": {"hasNextPage": False},
			"nodes": [{"title": "Express", "code": "express", "carrierIdentifier": "gid://x/1"}],
		}

		with patch(EXECUTE_TARGET, return_value={"order": payload}) as execute:
			result = orders.import_order(self.account.name, order_gid)

		query, variables = execute.call_args.args[:2]
		for needle in ("shippingLines(first: $shippingLinesFirst)", "provinceCode", "countryCodeV2"):
			self.assertIn(needle, query)
		self.assertEqual(variables["shippingLinesFirst"], 10)
		result.reload()
		stored = frappe.parse_json(result.shipping_lines)
		self.assertTrue(stored["requiresShipping"])
		self.assertEqual(stored["shippingLines"][0]["code"], "express")

	def test_refund_evidence_is_persisted_without_interpretation(self):
		order_gid = _order_gid(frappe.generate_hash(length=8))
		payload = make_order_payload(order_gid)

		with patch(EXECUTE_TARGET, return_value={"order": payload}):
			result = orders.import_order(self.account.name, order_gid)

		result.reload()
		refunds = frappe.parse_json(result.refunds)
		self.assertEqual(len(refunds), 1)
		restock_types = [node["restockType"] for node in refunds[0]["refundLineItems"]["nodes"]]
		self.assertEqual(restock_types, ["RESTOCK"])


class IntegrationTestImportOrderStaleGuard(IntegrationTestCase):
	def setUp(self):
		self.account = make_shopify_account(f"Order Stale {frappe.generate_hash(length=8)}")

	def test_older_updated_at_does_not_overwrite_a_newer_stored_record(self):
		order_gid = _order_gid(frappe.generate_hash(length=8))
		newer_payload = make_order_payload(
			order_gid, updated_at="2026-09-27T12:00:00Z", displayFinancialStatus="PAID"
		)
		older_payload = make_order_payload(
			order_gid, updated_at="2026-09-27T09:00:00Z", displayFinancialStatus="PENDING"
		)

		with patch(EXECUTE_TARGET, return_value={"order": newer_payload}):
			orders.import_order(self.account.name, order_gid)
		with patch(EXECUTE_TARGET, return_value={"order": older_payload}):
			stale_result = orders.import_order(self.account.name, order_gid)

		stale_result.reload()
		self.assertEqual(stale_result.external_financial_status, "PAID")

	def test_equal_or_newer_updated_at_still_applies(self):
		order_gid = _order_gid(frappe.generate_hash(length=8))
		first_payload = make_order_payload(
			order_gid, updated_at="2026-09-27T12:00:00Z", displayFinancialStatus="PENDING"
		)
		retried_payload = make_order_payload(
			order_gid, updated_at="2026-09-27T12:00:00Z", displayFinancialStatus="PAID"
		)

		with patch(EXECUTE_TARGET, return_value={"order": first_payload}):
			orders.import_order(self.account.name, order_gid)
		with patch(EXECUTE_TARGET, return_value={"order": retried_payload}):
			result = orders.import_order(self.account.name, order_gid)

		result.reload()
		self.assertEqual(result.external_financial_status, "PAID")


class IntegrationTestImportOrderNullOrder(IntegrationTestCase):
	"""#109 decision 5's third webhook edge case: order(id:) returns null for
	an order older than the 60-day read_orders window, or genuinely deleted.
	import_order() itself must return None and must not create any
	Marketplace Order - the caller (integrations/shopify/webhooks.py's
	_handle_topic()) is what turns this into a Processed-with-note outcome;
	see test_webhooks.py for that layer."""

	def setUp(self):
		self.account = make_shopify_account(f"Order Null {frappe.generate_hash(length=8)}")

	def test_null_order_returns_none_and_creates_nothing(self):
		order_gid = _order_gid(frappe.generate_hash(length=8))
		before = frappe.db.count("Marketplace Order")

		with patch(EXECUTE_TARGET, return_value={"order": None}):
			result = orders.import_order(self.account.name, order_gid)

		self.assertIsNone(result)
		self.assertEqual(frappe.db.count("Marketplace Order"), before)


class IntegrationTestImportOrdersUpdatedBetween(IntegrationTestCase):
	"""Review finding #2/#9: the shared "list then import" helper used by both
	run_order_import() and reconcile_all_accounts_orders()."""

	def setUp(self):
		self.account = make_shopify_account(f"Order Range {frappe.generate_hash(length=8)}")
		self._fixtures = []

	def tearDown(self):
		# _log_order_import_failure()'s frappe.db.commit() (review finding
		# #2a) is reachable from several tests in this class - per
		# docs/frappe-conventions.md's commit/test transaction gotcha, that
		# permanently persists this test's rows past IntegrationTestCase's
		# class-scoped rollback (same gotcha as
		# IntegrationTestRunCatalogSweep's tearDown in test_products.py).
		# Explicitly clean up here rather than relying on that rollback,
		# which a commit() already defeats for whatever was in the
		# transaction at that point.
		#
		# Marketplace Order rows (e.g. test_one_bad_order_does_not_stop_the_
		# rest's successfully-imported "good" order) must be deleted BEFORE
		# the account, not left dangling: deleting a doc named by a
		# `.#####`-style naming series reverts that series' counter when
		# it was the latest-issued name (frappe.model.naming), so the very
		# next test's make_shopify_account() call in this same class can be
		# handed back that exact same Account/Connection name. A leftover
		# order still carrying the old (reused) name then reads as if it
		# belongs to the new account, corrupting the next test's counts.
		# delete_doc (not frappe.db.delete) so child rows go too - this teardown commits.
		for order_name in frappe.get_all(
			"Marketplace Order", filters={"marketplace_account": self.account.name}, pluck="name"
		):
			frappe.delete_doc("Marketplace Order", order_name, ignore_permissions=True, force=True)
		# Linked Sales Orders (#178 tests) go after the orders that point at them and
		# before the commit below.
		for fixture in self._fixtures:
			fixture.cleanup()
		frappe.db.delete(
			"Marketplace Sync Log",
			filters={
				"sync_type": "Order Import",
				"status": "Error",
				"marketplace_account": self.account.name,
			},
		)
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Shopify Connection", self.account.connection, ignore_permissions=True, force=True)
		# The commit() above (if a test reached it) already made this test's
		# account/connection/log rows permanent - these deletes must be
		# committed too, in the SAME way, or they'd only land in a fresh
		# post-commit transaction that the class-level rollback then undoes
		# right back to "still present", silently defeating this cleanup.
		frappe.db.commit()

	def test_one_bad_order_does_not_stop_the_rest(self):
		good_gid = _order_gid(frappe.generate_hash(length=8))
		bad_gid = _order_gid(frappe.generate_hash(length=8))
		nodes = {
			"orders": {
				"pageInfo": {"hasNextPage": False},
				"nodes": [
					{"id": bad_gid, "updatedAt": "2026-09-27T10:00:00Z"},
					{"id": good_gid, "updatedAt": "2026-09-27T10:01:00Z"},
				],
			}
		}

		def fake_execute(query, variables=None):
			if "ShopifyOrderIds" in query:
				return nodes
			if variables.get("id") == bad_gid:
				frappe.throw("boom")
			return {"order": make_order_payload(good_gid)}

		with patch(EXECUTE_TARGET, side_effect=fake_execute):
			client = orders.ShopifyClient(self.account.connection)
			imported, errors = orders.import_orders_updated_between(
				self.account, client, "2026-09-01T00:00:00Z"
			)

		self.assertEqual(len(errors), 1)
		self.assertEqual(errors[0]["order_gid"], bad_gid)
		self.assertEqual(len(imported), 1)

	def test_per_order_failure_writes_a_marketplace_sync_log_row(self):
		# #139 section 4: a failed order import must ALSO write a Marketplace
		# Sync Log row (sync_type="Order Import", status="Error"), in addition
		# to the aggregated frappe.log_error() call its callers already make.
		bad_gid = _order_gid(frappe.generate_hash(length=8))
		nodes = {
			"orders": {
				"pageInfo": {"hasNextPage": False},
				"nodes": [{"id": bad_gid, "updatedAt": "2026-09-27T10:00:00Z"}],
			}
		}

		def fake_execute(query, variables=None):
			if "ShopifyOrderIds" in query:
				return nodes
			frappe.throw("boom")

		with patch(EXECUTE_TARGET, side_effect=fake_execute):
			client = orders.ShopifyClient(self.account.connection)
			orders.import_orders_updated_between(self.account, client, "2026-09-01T00:00:00Z")

		# Scoped by bad_gid (unique per test invocation) rather than just
		# sync_type/status: IntegrationTestCase rolls back at the class level
		# here, not per test method (see IntegrationTestRunCatalogSweep's
		# tearDown comment in test_products.py for the same gotcha), so a
		# loose sync_type/status-only filter would also match rows other test
		# methods in this class write.
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={
				"sync_type": "Order Import",
				"status": "Error",
				"request_data": ["like", f"%{bad_gid}%"],
			},
			fields=["name", "trigger_source", "marketplace_order", "traceback"],
		)
		self.assertEqual(len(logs), 1)
		self.assertFalse(logs[0].marketplace_order)
		self.assertEqual(logs[0].trigger_source, "Manual")
		self.assertIn("boom", logs[0].traceback)

	def test_per_order_failure_uses_the_passed_in_trigger_source(self):
		bad_gid = _order_gid(frappe.generate_hash(length=8))
		nodes = {
			"orders": {
				"pageInfo": {"hasNextPage": False},
				"nodes": [{"id": bad_gid, "updatedAt": "2026-09-27T10:00:00Z"}],
			}
		}

		def fake_execute(query, variables=None):
			if "ShopifyOrderIds" in query:
				return nodes
			frappe.throw("boom")

		with patch(EXECUTE_TARGET, side_effect=fake_execute):
			client = orders.ShopifyClient(self.account.connection)
			orders.import_orders_updated_between(
				self.account, client, "2026-09-01T00:00:00Z", trigger_source="Scheduled Reconciliation"
			)

		# Scoped by bad_gid for the same class-level-rollback reason as
		# test_per_order_failure_writes_a_marketplace_sync_log_row above.
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={
				"sync_type": "Order Import",
				"status": "Error",
				"request_data": ["like", f"%{bad_gid}%"],
			},
			fields=["trigger_source"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].trigger_source, "Scheduled Reconciliation")

	# #178 part 3: only the SCHEDULED sweep (bump_linked_changes=True) bumps the sync
	# debounce, and only when a LINKED order's evidence changed.  A manual range
	# import never bumps.

	def _import_range(
		self,
		gid,
		payload_overrides=None,
		trigger_source="Scheduled Reconciliation",
		bump_linked_changes=True,
		extra_nodes=None,
		bump_unlinked=False,
	):
		nodes = {
			"orders": {
				"pageInfo": {"hasNextPage": False},
				"nodes": [{"id": gid, "updatedAt": "2026-09-27T10:01:00Z"}, *(extra_nodes or [])],
			}
		}

		def fake_execute(query, variables=None):
			if "ShopifyOrderIds" in query:
				return nodes
			order_gid = (variables or {}).get("id") or gid
			return {"order": make_order_payload(order_gid, **(payload_overrides or {}))}

		with patch(EXECUTE_TARGET, side_effect=fake_execute):
			client = orders.ShopifyClient(self.account.connection)
			return orders.import_orders_updated_between(
				self.account,
				client,
				"2026-09-01T00:00:00Z",
				trigger_source=trigger_source,
				bump_linked_changes=bump_linked_changes,
				bump_unlinked=bump_unlinked,
			)

	def _linked_order(self, gid):
		self._import_range(gid)
		name = frappe.db.get_value("Marketplace Order", {"external_order_id": gid}, "name")
		fixture = LinkedSalesOrderFixture()
		self._fixtures.append(fixture)
		fixture.link(name)
		return name

	def _debounce(self, name):
		return frappe.db.get_value(
			"Marketplace Order", name, ["sync_debounce_until", "pending_sync_trigger_source"], as_dict=True
		)

	def test_scheduled_import_of_a_changed_linked_order_bumps_and_a_replay_does_not(self):
		gid = _order_gid(frappe.generate_hash(length=8))
		name = self._linked_order(gid)

		self._import_range(gid, {"cancelledAt": "2026-09-27T10:02:00Z"})

		state = self._debounce(name)
		self.assertIsNotNone(state.sync_debounce_until)
		self.assertEqual(state.pending_sync_trigger_source, "Scheduled Reconciliation")

		frappe.db.set_value("Marketplace Order", name, "sync_debounce_until", None)
		self._import_range(gid, {"cancelledAt": "2026-09-27T10:02:00Z"})
		self.assertIsNone(self._debounce(name).sync_debounce_until)

	def test_manual_import_of_a_changed_linked_order_does_not_bump(self):
		gid = _order_gid(frappe.generate_hash(length=8))
		name = self._linked_order(gid)

		# What run_order_import() does: "Manual" trigger, bump_linked_changes left at its default.
		self._import_range(
			gid,
			{"cancelledAt": "2026-09-27T10:02:00Z"},
			trigger_source="Manual",
			bump_linked_changes=False,
		)

		self.assertIsNone(self._debounce(name).sync_debounce_until)

	def test_manual_import_schedules_the_first_sync_of_a_new_order_but_not_a_cancelled_one(self):
		new_gid = _order_gid(frappe.generate_hash(length=8))
		cancelled_gid = _order_gid(frappe.generate_hash(length=8))

		self._import_range(
			new_gid, trigger_source="Manual", bump_linked_changes=False, bump_unlinked=True
		)
		self._import_range(
			cancelled_gid,
			{"cancelledAt": "2026-09-27T10:02:00Z"},
			trigger_source="Manual",
			bump_linked_changes=False,
			bump_unlinked=True,
		)

		new_name = frappe.db.get_value("Marketplace Order", {"external_order_id": new_gid}, "name")
		cancelled_name = frappe.db.get_value("Marketplace Order", {"external_order_id": cancelled_gid}, "name")
		state = self._debounce(new_name)
		self.assertIsNotNone(state.sync_debounce_until)
		self.assertEqual(state.pending_sync_trigger_source, "Manual")
		self.assertIsNone(self._debounce(cancelled_name).sync_debounce_until)

	def test_unchanged_linked_order_does_not_bump(self):
		gid = _order_gid(frappe.generate_hash(length=8))
		name = self._linked_order(gid)

		self._import_range(gid)

		self.assertIsNone(self._debounce(name).sync_debounce_until)

	def test_unlinked_order_does_not_bump(self):
		gid = _order_gid(frappe.generate_hash(length=8))
		self._import_range(gid)
		name = frappe.db.get_value("Marketplace Order", {"external_order_id": gid}, "name")

		self._import_range(gid, {"cancelledAt": "2026-09-27T10:02:00Z"})

		self.assertIsNone(self._debounce(name).sync_debounce_until)

	def test_fingerprint_or_bump_failure_for_one_order_does_not_stop_the_others(self):
		bad_gid = _order_gid(frappe.generate_hash(length=8))
		good_gid = _order_gid(frappe.generate_hash(length=8))
		bad_name = self._linked_order(bad_gid)
		good_name = self._linked_order(good_gid)
		overrides = {"cancelledAt": "2026-09-27T10:02:00Z"}

		real_fingerprint = orders.linked_order_fingerprint

		def flaky_fingerprint(account_name, order_gid):
			if order_gid == bad_gid:
				raise frappe.ValidationError("boom: fingerprint read failed")
			return real_fingerprint(account_name, order_gid)

		with patch.object(orders, "linked_order_fingerprint", side_effect=flaky_fingerprint):
			imported, errors = self._import_range(
				bad_gid, overrides, extra_nodes=[{"id": good_gid, "updatedAt": "2026-09-27T10:01:30Z"}]
			)

		self.assertEqual([error["order_gid"] for error in errors], [bad_gid])
		self.assertEqual(len(imported), 1)
		self.assertIsNone(self._debounce(bad_name).sync_debounce_until)
		self.assertIsNotNone(self._debounce(good_name).sync_debounce_until)

	def test_run_order_import_does_not_ask_for_bumping(self):
		with patch.object(orders, "import_orders_updated_between", return_value=([], [])) as mocked:
			orders.run_order_import(
				f"job-{frappe.generate_hash(length=6)}", "Administrator", self.account.name, "s", "e"
			)

		self.assertFalse(mocked.call_args.kwargs.get("bump_linked_changes", False))


class IntegrationTestReconcileAllAccountsOrders(IntegrationTestCase):
	"""Review finding #3: reconcile_all_accounts_orders() must isolate one
	account's failure (an unresolvable/throwing connection) from every other
	account's run in the same scheduled sweep - account B's orders must still
	be imported and its watermark still advance despite account A's failure."""

	def setUp(self):
		self.account_a = make_shopify_account(f"Reconcile Fails {frappe.generate_hash(length=8)}")
		self.account_b = make_shopify_account(f"Reconcile Succeeds {frappe.generate_hash(length=8)}")

	def tearDown(self):
		# reconcile_all_accounts_orders()'s per-account frappe.db.commit()
		# on success (self.account_b's branch) permanently persists this
		# test's rows past IntegrationTestCase's class-scoped rollback -
		# same gotcha as IntegrationTestImportOrdersUpdatedBetween's
		# tearDown above. Orders must go before their account (naming-series
		# revert-on-delete gotcha, see that tearDown's comment).
		for account_name in (self.account_a.name, self.account_b.name):
			frappe.db.delete("Marketplace Order", filters={"marketplace_account": account_name})
		frappe.delete_doc("Marketplace Account", self.account_a.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Marketplace Account", self.account_b.name, ignore_permissions=True, force=True)
		frappe.delete_doc(
			"Shopify Connection", self.account_a.connection, ignore_permissions=True, force=True
		)
		frappe.delete_doc(
			"Shopify Connection", self.account_b.connection, ignore_permissions=True, force=True
		)
		frappe.db.commit()

	def test_one_accounts_failure_does_not_stop_another_accounts_import_or_watermark(self):
		order_gid_b = _order_gid(frappe.generate_hash(length=8))
		payload_b = make_order_payload(order_gid_b)

		def fake_execute(client_self, query, variables=None):
			if client_self.connection_name == self.account_a.connection:
				raise frappe.ValidationError("boom: account A connection unreachable")
			if client_self.connection_name == self.account_b.connection:
				if "ShopifyOrderIds" in query:
					return {
						"orders": {
							"pageInfo": {"hasNextPage": False},
							"nodes": [{"id": order_gid_b, "updatedAt": "2026-09-27T10:01:00Z"}],
						}
					}
				return {"order": payload_b}
			return {"orders": {"pageInfo": {"hasNextPage": False}, "nodes": []}}

		with patch(EXECUTE_TARGET, autospec=True, side_effect=fake_execute):
			orders.reconcile_all_accounts_orders()

		self.account_a.reload()
		self.account_b.reload()

		self.assertFalse(self.account_a.shopify_orders_reconciled_through)
		self.assertTrue(self.account_b.shopify_orders_reconciled_through)

		imported = frappe.get_all(
			"Marketplace Order",
			filters={"marketplace_account": self.account_b.name, "external_order_id": order_gid_b},
		)
		self.assertEqual(len(imported), 1)
		self.assertEqual(
			frappe.db.count("Marketplace Order", {"marketplace_account": self.account_a.name}), 0
		)

	def test_scheduled_sweep_is_the_only_caller_that_asks_for_bumping(self):
		with patch.object(orders, "import_orders_updated_between", return_value=([], [])) as mocked:
			orders.reconcile_all_accounts_orders()

		self.assertTrue(mocked.call_args_list)
		for call in mocked.call_args_list:
			self.assertIs(call.kwargs.get("bump_linked_changes"), True)


class IntegrationTestQueueOrderImportDateRange(IntegrationTestCase):
	"""The 60-day limit is measured between the two instants (`(end - start).days
	> 60`), so the dialog's start T00:00:00Z / end T23:59:59Z allows up to 61
	inclusive calendar days. frappe.enqueue and the status cache are patched, so
	nothing is queued."""

	def setUp(self):
		self.account = make_shopify_account(f"Import Range {frappe.generate_hash(length=8)}")
		self.addCleanup(frappe.delete_doc, "Shopify Connection", self.account.connection, force=True)
		self.addCleanup(frappe.delete_doc, "Marketplace Account", self.account.name, force=True)

	def queue(self, start, end, account=None, queued=True):
		with (
			patch.object(orders.frappe, "enqueue", return_value=object() if queued else None) as enqueue,
			patch.object(orders, "_set_import_status"),
		):
			result = orders.queue_order_import(account or self.account.name, start, end)
		return result, enqueue

	def test_exactly_sixty_days_is_accepted(self):
		result, enqueue = self.queue("2026-01-01T00:00:00Z", "2026-03-02T00:00:00Z")

		self.assertEqual(result["status"], "queued")
		enqueue.assert_called_once()

	def test_sixty_one_days_is_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "limited to 60 days"):
			self.queue("2026-01-01T00:00:00Z", "2026-03-03T00:00:00Z")

	def test_sixty_days_and_a_fraction_is_accepted_because_the_limit_floors_whole_days(self):
		result, _enqueue = self.queue("2026-01-01T00:00:00Z", "2026-03-02T23:59:59Z")

		self.assertEqual(result["status"], "queued")

	def test_dialog_range_allows_sixty_one_inclusive_calendar_days_but_not_sixty_two(self):
		# 2026-01-01 .. 2026-03-02 is 61 calendar days inclusive.
		result, _enqueue = self.queue("2026-01-01T00:00:00Z", "2026-03-02T23:59:59Z")
		self.assertEqual(result["status"], "queued")

		with self.assertRaisesRegex(frappe.ValidationError, "limited to 60 days"):
			self.queue("2026-01-01T00:00:00Z", "2026-03-03T23:59:59Z")

	def test_start_after_end_or_missing_dates_are_rejected(self):
		for start, end in (
			("2026-02-02T00:00:00Z", "2026-02-01T00:00:00Z"),
			(None, "2026-02-01T00:00:00Z"),
			("2026-02-01T00:00:00Z", None),
			("", ""),
		):
			with self.subTest(start=start, end=end), self.assertRaisesRegex(
				frappe.ValidationError, "valid Shopify order import date range"
			):
				self.queue(start, end)

	def test_equal_start_and_end_is_accepted(self):
		result, _enqueue = self.queue("2026-02-01T10:00:00Z", "2026-02-01T10:00:00Z")

		self.assertEqual(result["status"], "queued")

	def test_offset_input_is_normalized_to_utc_z(self):
		_result, enqueue = self.queue("2026-02-01T02:00:00+02:00", "2026-02-02T05:30:00+02:00")

		self.assertEqual(enqueue.call_args.kwargs["start"], "2026-02-01T00:00:00Z")
		self.assertEqual(enqueue.call_args.kwargs["end"], "2026-02-02T03:30:00Z")

	def test_non_shopify_account_is_rejected(self):
		account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Import Range Talabat {frappe.generate_hash(length=8)}",
				"platform": "Talabat",
				"external_account_id": f"import-range-{frappe.generate_hash(length=8)}",
				"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
				"connection": STAND_IN_CONNECTION,
			}
		).insert()
		self.addCleanup(frappe.delete_doc, "Marketplace Account", account.name, force=True)

		with self.assertRaisesRegex(frappe.ValidationError, "is not a Shopify account"):
			self.queue("2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z", account=account.name)

	def test_a_second_import_for_the_same_account_is_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "already running"):
			self.queue("2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z", queued=False)
