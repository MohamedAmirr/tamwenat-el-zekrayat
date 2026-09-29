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
			self.assertEqual(
				frappe.db.count(doctype), before[doctype], f"{doctype} row count changed"
			)

	def test_refund_evidence_is_persisted_without_interpretation(self):
		order_gid = _order_gid(frappe.generate_hash(length=8))
		payload = make_order_payload(order_gid)

		with patch(EXECUTE_TARGET, return_value={"order": payload}):
			result = orders.import_order(self.account.name, order_gid)

		result.reload()
		refunds = frappe.parse_json(result.refunds)
		self.assertEqual(len(refunds), 1)
		restock_types = [
			node["restockType"] for node in refunds[0]["refundLineItems"]["nodes"]
		]
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

	def test_one_bad_order_does_not_stop_the_rest(self):
		good_gid = _order_gid(frappe.generate_hash(length=8))
		bad_gid = _order_gid(frappe.generate_hash(length=8))
		nodes = {"orders": {"pageInfo": {"hasNextPage": False}, "nodes": [
			{"id": bad_gid, "updatedAt": "2026-09-27T10:00:00Z"},
			{"id": good_gid, "updatedAt": "2026-09-27T10:01:00Z"},
		]}}

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
			filters={"sync_type": "Order Import", "status": "Error", "request_data": ["like", f"%{bad_gid}%"]},
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
			filters={"sync_type": "Order Import", "status": "Error", "request_data": ["like", f"%{bad_gid}%"]},
			fields=["trigger_source"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].trigger_source, "Scheduled Reconciliation")


class IntegrationTestReconcileAllAccountsOrders(IntegrationTestCase):
	"""Review finding #3: reconcile_all_accounts_orders() must isolate one
	account's failure (an unresolvable/throwing connection) from every other
	account's run in the same scheduled sweep - account B's orders must still
	be imported and its watermark still advance despite account A's failure."""

	def setUp(self):
		self.account_a = make_shopify_account(f"Reconcile Fails {frappe.generate_hash(length=8)}")
		self.account_b = make_shopify_account(f"Reconcile Succeeds {frappe.generate_hash(length=8)}")

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
