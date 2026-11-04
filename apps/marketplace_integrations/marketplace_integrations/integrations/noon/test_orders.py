# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon FBPI order ingestion tests - covers order normalization, aggregation,
mapping resolution, import isolation, pagination, and the manual refresh path.

Like test_orders.py for Shopify, this mocks the Noon API client and never
makes real HTTP calls. Fixture documents (Marketplace Account, Item, Mapping)
are built directly via make_* helpers.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from marketplace_integrations.integrations.noon.expected_change import (
	record_out_of_stock as record_noon_out_of_stock,
)
from marketplace_integrations.integrations.noon.order_mapper import normalize_order
from marketplace_integrations.integrations.noon.orders import (
	_ORDER_IMPORT_RQ_JOB_PREFIX,
	enqueue_order_refresh,
	import_order,
	import_orders_created_between,
	list_orders,
	poll_all_accounts,
	queue_order_refresh,
	refresh_order,
	run_order_refresh,
)
from marketplace_integrations.services.linked_sales_order_fixture import LinkedSalesOrderFixture
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order

_DEFAULT_ITEMS = object()


def _make_noon_connection(connection_name, project_code=None):
	"""Create a test Noon Connection."""
	if project_code is None:
		project_code = f"test-project-{frappe.generate_hash(length=8)}"
	return (
		frappe.get_doc(
			{
				"doctype": "Noon Connection",
				"connection_name": connection_name,
				"project_code": project_code,
				"key_id": "test-key-id",
				"private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds1xfn8fH9gFKvKHxsXNpKZDqz5Fng0Z5HqL8H7\nDUgkY4L8Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7\nZ5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7\nZ5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7\nwIDAQABAoIBABj/Dn/+BDrP5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m\n5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m\n5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m\nwIDAQABCkEA5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m\n-----END RSA PRIVATE KEY-----",
				"environment": "Static Sandbox",
				"enabled": 1,
			}
		)
		.insert()
		.name
	)


def _make_noon_account(
	account_name,
	connection=None,
	warehouse_code="NOON-WH-001",
	country_code="ae",
	enabled=1,
):
	"""Create a test Marketplace Account for Noon platform.

	The external_account_id must match the connection's project_code (Noon requirement).
	"""
	project_code = f"test-project-{frappe.generate_hash(length=8)}"
	if connection is None:
		connection = _make_noon_connection(account_name + " Connection", project_code)

	return frappe.get_doc(
		{
			"doctype": "Marketplace Account",
			"account_name": account_name,
			"platform": "Noon",
			"external_account_id": project_code,
			"connection_doctype": "Noon Connection",
			"connection": connection,
			"noon_warehouse_code": warehouse_code,
			"noon_country_code": country_code,
			"enabled": enabled,
		}
	).insert()


def _make_item(item_code):
	"""Get or create an Item for testing."""
	if frappe.db.exists("Item", item_code):
		return item_code
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": "Products",
			"stock_uom": "Nos",
			"is_stock_item": 0,
		}
	).insert()
	return item_code


def _make_mapping(account_name, item_code, partner_sku):
	"""Create a Marketplace Product Mapping linking an Item to a Noon partner SKU."""
	return frappe.get_doc(
		{
			"doctype": "Marketplace Product Mapping",
			"marketplace_account": account_name,
			"item": item_code,
			"external_reference": partner_sku,
			"enabled": 1,
		}
	).insert()


def _order_payload(
	fbpi_order_nr="ORD-001",
	mp_order_nr="MP-001",
	mp_code="noon",
	mp_country_code="ae",
	warehouse_code="NOON-WH-001",
	items=_DEFAULT_ITEMS,
	**overrides,
):
	"""Build a minimal Noon FBPI order payload (either ListFbpiOrders element or GetFbpiOrder response)."""
	if items is _DEFAULT_ITEMS:
		items = [
			{
				"mp_item_nr": 1,
				"partner_sku": "SKU-001",
				"mp_status": "MP_ITEM_STATUS_CONFIRMED",
				"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
				"delivered_invoice_price": 100.0,
			}
		]
	payload = {
		"fbpi_order_nr": fbpi_order_nr,
		"mp_code": mp_code,
		"mp_order_nr": mp_order_nr,
		"mp_country_code": mp_country_code,
		"customer_country_code": mp_country_code,
		"merchant_code": "MERCH-001",
		"currency_code": "AED",
		"warehouse_code": warehouse_code,
		"order_created_at": "2026-09-30T10:00:00Z",
		"items": items,
	}
	payload.update(overrides)
	return payload


def _start_noon_client_customer_data_patch(test_case):
	"""Patch ``NoonClient`` for tests that exercise ``import_order()``/
	``refresh_order()`` without themselves mocking the customer-data fetch
	(review finding G). Without this, ``_maybe_fetch_customer_data()``
	builds a real ``NoonClient`` from this module's dummy test PEM key -
	today that happens to fail before any HTTP call and gets caught and
	swallowed into the Error Log, but relying on that internal construction
	detail always failing first is fragile. Configured to return an empty,
	well-formed 200 response - same shape as
	``test_import_order_repeat_poll_retains_customer_fields_when_fetch_skipped``'s
	own inline mock - so no customer fields get set and no Error Log noise
	is produced. Returns the mock ``NoonClient`` class in case a caller needs
	to assert against it."""
	client_patch = patch("marketplace_integrations.integrations.noon.orders.NoonClient")
	client_class = client_patch.start()
	test_case.addCleanup(client_patch.stop)
	response = MagicMock()
	response.status_code = 200
	response.json.return_value = {}
	client_class.return_value.request.return_value = response
	return client_class


class UnitTestNoonOrderNormalization(UnitTestCase):
	"""Unit tests for order_mapper.normalize_order() - no DB, just transformation logic."""

	def setUp(self):
		self.account = MagicMock()
		self.account.name = "TEST-ACCOUNT"
		self.account.noon_warehouse_code = "NOON-WH-001"
		self.account.noon_country_code = "ae"

	def test_normalize_order_maps_required_fields(self):
		"""Normalized order includes all required ORDER_FIELDS."""
		payload = _order_payload()
		result = normalize_order(self.account, payload)

		self.assertEqual(result["external_order_id"], "ORD-001")
		self.assertEqual(result["platform_order_reference"], "MP-001")
		self.assertEqual(result["order_code"], "MP-001")
		self.assertEqual(result["presentment_currency"], "AED")
		self.assertIsNotNone(result["received_on"])
		self.assertIsNotNone(result["items"])

	def test_normalize_order_aggregates_multiple_units_of_same_item_mapped(self):
		"""Multiple one-unit entries for the same resolved Item aggregate into one
		row with summed quantity (decision 13)."""
		_make_item("ITEM-123")
		with patch(
			"marketplace_integrations.integrations.noon.order_mapper.resolve_mapping_and_item",
			return_value={"item": "ITEM-123"},
		):
			payload = _order_payload(
				items=[
					{
						"mp_item_nr": 1,
						"partner_sku": "SKU-001",
						"mp_status": "MP_ITEM_STATUS_CONFIRMED",
						"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
						"delivered_invoice_price": 100.0,
					},
					{
						"mp_item_nr": 2,
						"partner_sku": "SKU-001",
						"mp_status": "MP_ITEM_STATUS_CONFIRMED",
						"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
						"delivered_invoice_price": 100.0,
					},
					{
						"mp_item_nr": 3,
						"partner_sku": "SKU-001",
						"mp_status": "MP_ITEM_STATUS_CONFIRMED",
						"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
						"delivered_invoice_price": 100.0,
					},
				]
			)
			result = normalize_order(self.account, payload)

		self.assertEqual(len(result["items"]), 1)
		row = result["items"][0]
		self.assertEqual(
			row["external_item_id"],
			"sku:SKU-001:MP_ITEM_STATUS_CONFIRMED:INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
		)
		self.assertEqual(row["original_quantity"], 3)
		self.assertEqual(row["fulfilled_quantity"], 3)
		self.assertEqual(row["current_quantity"], 3)
		self.assertEqual(row["transaction_amount"], 300.0)

	def test_normalize_order_aggregates_mixed_status_units_into_separate_rows(self):
		"""Units of the same partner_sku with DIFFERENT statuses (e.g. one
		cancelled, two still confirmed) produce separate rows, each with its
		own correct summed quantity/amount for just that status group -
		review finding A. The common single-status case above still collapses
		to one row; this is the genuinely-mixed case."""
		_make_item("ITEM-123")
		with patch(
			"marketplace_integrations.integrations.noon.order_mapper.resolve_mapping_and_item",
			return_value={"item": "ITEM-123"},
		):
			payload = _order_payload(
				items=[
					{
						"mp_item_nr": 1,
						"partner_sku": "SKU-001",
						"mp_status": "MP_ITEM_STATUS_CANCELLED",
						"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
						"delivered_invoice_price": 100.0,
					},
					{
						"mp_item_nr": 2,
						"partner_sku": "SKU-001",
						"mp_status": "MP_ITEM_STATUS_CONFIRMED",
						"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
						"delivered_invoice_price": 100.0,
					},
					{
						"mp_item_nr": 3,
						"partner_sku": "SKU-001",
						"mp_status": "MP_ITEM_STATUS_CONFIRMED",
						"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
						"delivered_invoice_price": 100.0,
					},
				]
			)
			result = normalize_order(self.account, payload)

		self.assertEqual(len(result["items"]), 2)
		by_status = {row["external_status"]: row for row in result["items"]}
		self.assertEqual(set(by_status), {"CANCELLED", "CONFIRMED"})

		cancelled_row = by_status["CANCELLED"]
		self.assertEqual(
			cancelled_row["external_item_id"],
			"sku:SKU-001:MP_ITEM_STATUS_CANCELLED:INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
		)
		self.assertEqual(cancelled_row["current_quantity"], 1)
		self.assertEqual(cancelled_row["transaction_amount"], 100.0)

		confirmed_row = by_status["CONFIRMED"]
		self.assertEqual(
			confirmed_row["external_item_id"],
			"sku:SKU-001:MP_ITEM_STATUS_CONFIRMED:INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
		)
		self.assertEqual(confirmed_row["current_quantity"], 2)
		self.assertEqual(confirmed_row["transaction_amount"], 200.0)

	def test_normalize_order_unmapped_sku_gets_own_row(self):
		"""An unmapped partner_sku still gets its own row, grouped by the SKU itself
		since there's no resolved Item to key on (decision 7)."""
		with patch(
			"marketplace_integrations.integrations.noon.order_mapper.resolve_mapping_and_item",
			return_value=None,
		):
			payload = _order_payload(
				items=[
					{
						"mp_item_nr": 1,
						"partner_sku": "UNMAPPED-SKU",
						"mp_status": "MP_ITEM_STATUS_CONFIRMED",
						"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
						"delivered_invoice_price": 50.0,
					}
				]
			)
			result = normalize_order(self.account, payload)

		self.assertEqual(len(result["items"]), 1)
		row = result["items"][0]
		self.assertEqual(
			row["external_item_id"],
			"sku:UNMAPPED-SKU:MP_ITEM_STATUS_CONFIRMED:INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
		)
		self.assertEqual(row["external_sku"], "UNMAPPED-SKU")
		self.assertEqual(row["original_quantity"], 1)
		self.assertEqual(result["_unmapped_skus"], ["UNMAPPED-SKU"])

	def test_normalize_order_maps_mp_item_status_enum(self):
		"""mp_item_status enum values are mapped to short form (decision 14),
		unrecognized values passed through unchanged."""
		with patch(
			"marketplace_integrations.integrations.noon.order_mapper.resolve_mapping_and_item",
			return_value=None,
		):
			payload = _order_payload(
				items=[
					{
						"mp_item_nr": 1,
						"partner_sku": "SKU",
						"mp_status": "MP_ITEM_STATUS_CANCELLED",
						"integration_status": "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK",
						"delivered_invoice_price": 100.0,
					}
				]
			)
			result = normalize_order(self.account, payload)

		row = result["items"][0]
		self.assertEqual(row["external_status"], "CANCELLED")
		self.assertEqual(row["integration_status"], "OUT_OF_STOCK")

	def test_normalize_order_passes_unrecognized_enums_through(self):
		"""Unrecognized enum values (future Noon additions or _UNSPECIFIED) are
		preserved as-is rather than dropped (safety net)."""
		with patch(
			"marketplace_integrations.integrations.noon.order_mapper.resolve_mapping_and_item",
			return_value=None,
		):
			payload = _order_payload(
				items=[
					{
						"mp_item_nr": 1,
						"partner_sku": "SKU",
						"mp_status": "MP_ITEM_STATUS_FUTURE",
						"integration_status": "INTEGRATION_ITEM_STATUS_UNSPECIFIED",
						"delivered_invoice_price": 100.0,
					}
				]
			)
			result = normalize_order(self.account, payload)

		row = result["items"][0]
		self.assertEqual(row["external_status"], "MP_ITEM_STATUS_FUTURE")
		self.assertEqual(row["integration_status"], "INTEGRATION_ITEM_STATUS_UNSPECIFIED")

	def test_normalize_order_rejects_missing_required_order_fields(self):
		"""Missing or blank any of REQUIRED_ORDER_FIELDS raises ValidationError
		and normalizes nothing (decision 4)."""
		payload = _order_payload()
		payload.pop("fbpi_order_nr")
		with self.assertRaises(frappe.ValidationError) as cm:
			normalize_order(self.account, payload)
		self.assertIn("fbpi_order_nr", str(cm.exception))

	def test_normalize_order_rejects_missing_required_item_fields(self):
		"""Missing or blank any of REQUIRED_ITEM_FIELDS raises ValidationError."""
		payload = _order_payload(
			items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "SKU",
					# missing mp_status
					"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					"delivered_invoice_price": 100.0,
				}
			]
		)
		with self.assertRaises(frappe.ValidationError) as cm:
			normalize_order(self.account, payload)
		self.assertIn("mp_status", str(cm.exception))

	def test_normalize_order_rejects_warehouse_code_mismatch(self):
		"""Order's warehouse_code must match account's noon_warehouse_code
		(decision 20 - a scope violation is a visible rejection, not a silent skip)."""
		payload = _order_payload(warehouse_code="DIFFERENT-WH")
		with self.assertRaises(frappe.ValidationError) as cm:
			normalize_order(self.account, payload)
		self.assertIn("warehouse", str(cm.exception).lower())

	def test_normalize_order_rejects_country_code_mismatch(self):
		"""Order's mp_country_code must match account's noon_country_code
		(decision 20)."""
		payload = _order_payload(mp_country_code="sa")
		with self.assertRaises(frappe.ValidationError) as cm:
			normalize_order(self.account, payload)
		self.assertIn("country", str(cm.exception).lower())

	def test_normalize_order_requires_account_warehouse_code(self):
		"""Account must have noon_warehouse_code set (would fail the mismatch
		check anyway, but explicitly validated)."""
		self.account.noon_warehouse_code = ""
		payload = _order_payload()
		with self.assertRaises(frappe.ValidationError) as cm:
			normalize_order(self.account, payload)
		self.assertIn("warehouse", str(cm.exception).lower())

	def test_normalize_order_requires_account_country_code(self):
		"""Account must have noon_country_code set."""
		self.account.noon_country_code = ""
		payload = _order_payload()
		with self.assertRaises(frappe.ValidationError) as cm:
			normalize_order(self.account, payload)
		self.assertIn("country", str(cm.exception).lower())

	def test_normalize_order_rejects_non_dict_payload(self):
		"""Raw order must be a dict; a list or string raises ValidationError."""
		with self.assertRaises(frappe.ValidationError) as cm:
			normalize_order(self.account, "not a dict")
		self.assertIn("object", str(cm.exception).lower())

	def test_normalize_order_rejects_items_not_a_list(self):
		"""Items field must be a list."""
		payload = _order_payload(items="not a list")
		with self.assertRaises(frappe.ValidationError) as cm:
			normalize_order(self.account, payload)
		self.assertIn("list", str(cm.exception).lower())

	def test_normalize_order_rejects_non_dict_item_entries(self):
		"""Each item entry must be a dict."""
		payload = _order_payload(items=["not a dict"])
		with self.assertRaises(frappe.ValidationError) as cm:
			normalize_order(self.account, payload)
		self.assertIn("object", str(cm.exception).lower())

	def test_normalize_order_rejects_missing_delivered_invoice_price(self):
		"""A missing delivered_invoice_price rejects the whole order rather
		than silently coercing to 0.0 (review finding 7 - it's a required
		field, decision 4)."""
		payload = _order_payload(
			items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "SKU",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					# delivered_invoice_price missing
				}
			]
		)
		with self.assertRaises(frappe.ValidationError) as cm:
			normalize_order(self.account, payload)
		self.assertIn("delivered_invoice_price", str(cm.exception))

	def test_normalize_order_rejects_non_numeric_delivered_invoice_price(self):
		"""A non-numeric delivered_invoice_price is malformed, not missing,
		but still rejects the whole order (review finding 7)."""
		payload = _order_payload(
			items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "SKU",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					"delivered_invoice_price": "not-a-number",
				}
			]
		)
		with self.assertRaises(frappe.ValidationError) as cm:
			normalize_order(self.account, payload)
		self.assertIn("delivered_invoice_price", str(cm.exception))

	def test_normalize_order_rejects_malformed_order_created_at(self):
		"""A malformed order_created_at rejects the whole order rather than
		silently storing it with a blank received_on (review finding 9,
		decision 4)."""
		payload = _order_payload(order_created_at="not-a-timestamp")
		with self.assertRaises(frappe.ValidationError) as cm:
			normalize_order(self.account, payload)
		self.assertIn("order_created_at", str(cm.exception))

	def test_normalize_order_preserves_latest_payload(self):
		"""The raw order is preserved verbatim in normalized['latest_payload']
		(decision 3)."""
		payload = _order_payload()
		result = normalize_order(self.account, payload)
		self.assertEqual(result["latest_payload"], payload)


class IntegrationTestNoonOrderImport(IntegrationTestCase):
	"""Integration tests for order import - touches DB, creates real docs."""

	def setUp(self):
		# Patch warehouse code validation to avoid making real Noon API calls
		self.warehouse_patch = patch(
			"marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"
		)
		self.warehouse_patch.start()
		self.addCleanup(self.warehouse_patch.stop)

		_start_noon_client_customer_data_patch(self)

		self._fixtures = []
		suffix = frappe.generate_hash(length=8)
		self.account = _make_noon_account(f"Order Import {suffix}")
		_make_item("ITEM-MAPPED")
		_make_mapping(self.account.name, "ITEM-MAPPED", "SKU-MAPPED")

	def tearDown(self):
		"""Clean up Marketplace Orders created during tests. See
		IntegrationTestImportOrdersUpdatedBetween's tearDown comment in
		shopify/test_orders.py for the naming-series revert-on-delete gotcha:
		orders must be deleted BEFORE the account to avoid corrupting the next
		test's name allocation."""
		# delete_doc (not frappe.db.delete) so child rows go too - this teardown commits.
		for order_name in frappe.get_all(
			"Marketplace Order", filters={"marketplace_account": self.account.name}, pluck="name"
		):
			frappe.delete_doc("Marketplace Order", order_name, ignore_permissions=True, force=True)
		# Linked Sales Orders (#178 tests): after the orders, before the commit below.
		for fixture in self._fixtures:
			fixture.cleanup()
		frappe.db.delete(
			"Marketplace Sync Log",
			filters={
				"sync_type": "Order Import",
				"marketplace_account": self.account.name,
			},
		)
		frappe.db.delete("Marketplace Product Mapping", filters={"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()

	def test_import_order_creates_marketplace_order_with_aggregated_items(self):
		"""import_order() normalizes and upserts a Marketplace Order with
		aggregated items (unmapped SKUs bundled, mapped items grouped)."""
		payload = _order_payload(
			items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "SKU-MAPPED",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					"delivered_invoice_price": 50.0,
				},
				{
					"mp_item_nr": 2,
					"partner_sku": "SKU-MAPPED",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					"delivered_invoice_price": 50.0,
				},
			]
		)
		order, unmapped_skus = import_order(self.account, payload)

		self.assertEqual(order.doctype, "Marketplace Order")
		self.assertEqual(order.external_order_id, "ORD-001")
		self.assertEqual(len(order.items), 1)
		self.assertEqual(order.items[0].current_quantity, 2)
		self.assertEqual(unmapped_skus, [])

	def test_import_order_unmapped_sku_logged_in_sync_log(self):
		"""An order with unmapped SKUs imports successfully but triggers a
		Marketplace Sync Log row (status="Invalid", decision 19) listing them."""
		payload = _order_payload(
			items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "UNMAPPED-SKU",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					"delivered_invoice_price": 100.0,
				}
			]
		)
		order, unmapped_skus = import_order(self.account, payload)

		self.assertEqual(order.doctype, "Marketplace Order")
		self.assertEqual(unmapped_skus, ["UNMAPPED-SKU"])
		# The unmapped log is written by the caller (import_orders_created_between),
		# not by import_order itself - import_order just returns the list.

	def test_import_order_idempotent_on_same_fbpi_order_nr(self):
		"""Importing the same order twice updates, not duplicates (same
		external_order_id -> same order_identity_key -> existing row updated)."""
		payload = _order_payload(fbpi_order_nr="ORD-IDEMPOTENT")

		order1, _ = import_order(self.account, payload)
		order2, _ = import_order(self.account, payload)

		self.assertEqual(order1.name, order2.name)
		count = frappe.db.count(
			"Marketplace Order",
			{"marketplace_account": self.account.name, "external_order_id": "ORD-IDEMPOTENT"},
		)
		self.assertEqual(count, 1)

	def test_import_order_sku_mapped_between_polls_updates_same_row(self):
		"""A SKU imported before it has a Marketplace Product Mapping keeps the
		same external_item_id after a mapping is later created, so re-polling
		the same order updates the existing row instead of duplicating it
		(review finding 4 - external_item_id must be stable across mapping
		status changes)."""
		payload = _order_payload(
			fbpi_order_nr="ORD-LATE-MAP",
			items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "SKU-LATE-MAP",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					"delivered_invoice_price": 75.0,
				}
			],
		)

		expected_external_item_id = (
			"sku:SKU-LATE-MAP:MP_ITEM_STATUS_CONFIRMED:INTEGRATION_ITEM_STATUS_ACKNOWLEDGED"
		)
		order1, unmapped_skus1 = import_order(self.account, payload)
		self.assertEqual(unmapped_skus1, ["SKU-LATE-MAP"])
		self.assertEqual(len(order1.items), 1)
		self.assertEqual(order1.items[0].external_item_id, expected_external_item_id)

		_make_item("ITEM-LATE-MAP")
		_make_mapping(self.account.name, "ITEM-LATE-MAP", "SKU-LATE-MAP")

		order2, unmapped_skus2 = import_order(self.account, payload)
		self.assertEqual(unmapped_skus2, [])
		self.assertEqual(order2.name, order1.name)
		self.assertEqual(len(order2.items), 1)
		self.assertEqual(order2.items[0].external_item_id, expected_external_item_id)
		self.assertEqual(order2.items[0].current_quantity, 1)

	def test_import_order_status_transition_does_not_duplicate_rows(self):
		"""A unit whose status changes between polls (e.g. CONFIRMED/
		ACKNOWLEDGED -> CONFIRMED/SHIPPED) gets a new status-keyed row rather
		than mutating the old one in place - the old row is RETAINED (never
		deleted) per upsert_marketplace_order()'s stated "rows absent from a
		later payload are retained" policy, so the row count stays stable at
		2 (one retained per historical status), not growing on every
		additional poll.

		BLOCKER fix (post-#145 final review): retaining the old row must not
		mean retaining its OLD quantity/amount too, or the same physical unit
		is counted once under its stale ACKNOWLEDGED row and again under its
		new SHIPPED row - double-counting the order's real total. The old
		row's current_quantity/fulfilled_quantity/transaction_amount must be
		zeroed the moment its exact status combination no longer appears in
		the payload, so the SUM across every row for this order always
		equals the real, bounded physical unit count (1 here), never 2x it."""
		payload = _order_payload(
			fbpi_order_nr="ORD-STATUS-TRANSITION",
			items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "SKU-MAPPED",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					"delivered_invoice_price": 50.0,
				}
			],
		)
		order1, _ = import_order(self.account, payload)
		self.assertEqual(len(order1.items), 1)
		self.assertEqual(order1.items[0].integration_status, "ACKNOWLEDGED")
		self.assertEqual(order1.items[0].current_quantity, 1)
		self.assertEqual(order1.items[0].transaction_amount, 50.0)

		shipped_payload = _order_payload(
			fbpi_order_nr="ORD-STATUS-TRANSITION",
			items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "SKU-MAPPED",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_SHIPPED",
					"delivered_invoice_price": 50.0,
				}
			],
		)
		order2, _ = import_order(self.account, shipped_payload)
		self.assertEqual(order2.name, order1.name)
		self.assertEqual(len(order2.items), 2)
		by_status = {row.integration_status: row for row in order2.items}
		self.assertEqual(sorted(by_status), ["ACKNOWLEDGED", "SHIPPED"])

		# The stale ACKNOWLEDGED row is retained for history but zeroed out -
		# the physical units it once reported now belong only to the SHIPPED
		# row.
		acknowledged_row = by_status["ACKNOWLEDGED"]
		self.assertEqual(acknowledged_row.current_quantity, 0)
		self.assertEqual(acknowledged_row.fulfilled_quantity, 0)
		self.assertEqual(acknowledged_row.transaction_amount, 0.0)

		shipped_row = by_status["SHIPPED"]
		self.assertEqual(shipped_row.current_quantity, 1)
		self.assertEqual(shipped_row.fulfilled_quantity, 1)
		self.assertEqual(shipped_row.transaction_amount, 50.0)

		# The order's real total is 1 physical unit - summing every row's
		# current_quantity/transaction_amount must equal that, not 2x it.
		self.assertEqual(sum(row.current_quantity for row in order2.items), 1)
		self.assertEqual(sum(row.transaction_amount for row in order2.items), 50.0)

		# Re-polling the same (already-transitioned) payload again must not
		# add a third row for the same, still-shipped unit, and must not
		# re-inflate the already-zeroed ACKNOWLEDGED row.
		order3, _ = import_order(self.account, shipped_payload)
		self.assertEqual(order3.name, order1.name)
		self.assertEqual(len(order3.items), 2)
		by_status_3 = {row.integration_status: row for row in order3.items}
		self.assertEqual(by_status_3["ACKNOWLEDGED"].current_quantity, 0)
		self.assertEqual(by_status_3["SHIPPED"].current_quantity, 1)
		self.assertEqual(sum(row.current_quantity for row in order3.items), 1)

	def test_identical_repoll_after_a_status_transition_does_not_save(self):
		"""The stale-group zero-out is already stored after the transition poll, so the next
		identical poll compares equal and leaves the order untouched."""

		def payload(integration_status):
			return _order_payload(
				fbpi_order_nr="ORD-NOOP-REPOLL",
				items=[
					{
						"mp_item_nr": 1,
						"partner_sku": "SKU-MAPPED",
						"mp_status": "MP_ITEM_STATUS_CONFIRMED",
						"integration_status": integration_status,
						"delivered_invoice_price": 50.0,
					}
				],
			)

		import_order(self.account, payload("INTEGRATION_ITEM_STATUS_ACKNOWLEDGED"))
		order, _ = import_order(self.account, payload("INTEGRATION_ITEM_STATUS_SHIPPED"))
		old_modified = "2026-01-01 00:00:00"
		frappe.db.set_value("Marketplace Order", order.name, "modified", old_modified, update_modified=False)

		import_order(self.account, payload("INTEGRATION_ITEM_STATUS_SHIPPED"))

		self.assertEqual(str(frappe.db.get_value("Marketplace Order", order.name, "modified")), old_modified)

	def test_import_order_zeroes_stale_row_for_disabled_mapping_not_resolvable(self):
		"""The stale-row zeroing diff is a pure literal-key comparison against
		THIS poll's own computed groups - it does not re-check whether the
		SKU is currently mapped/enabled. A mapping being disabled or removed
		between polls must not prevent an already-transitioned unit's stale
		row from being correctly zeroed."""
		payload = _order_payload(
			fbpi_order_nr="ORD-STALE-UNMAPPED",
			items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "SKU-MAPPED",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					"delivered_invoice_price": 20.0,
				}
			],
		)
		import_order(self.account, payload)

		# Disable the mapping - the SKU becomes "unmapped" from this point on.
		mapping_name = frappe.db.get_value(
			"Marketplace Product Mapping",
			{"marketplace_account": self.account.name, "external_reference": "SKU-MAPPED"},
		)
		frappe.db.set_value("Marketplace Product Mapping", mapping_name, "enabled", 0)

		shipped_payload = _order_payload(
			fbpi_order_nr="ORD-STALE-UNMAPPED",
			items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "SKU-MAPPED",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_SHIPPED",
					"delivered_invoice_price": 20.0,
				}
			],
		)
		order2, unmapped_skus = import_order(self.account, shipped_payload)
		self.assertEqual(unmapped_skus, ["SKU-MAPPED"])
		by_status = {row.integration_status: row for row in order2.items}
		self.assertEqual(by_status["ACKNOWLEDGED"].current_quantity, 0)
		self.assertEqual(by_status["SHIPPED"].current_quantity, 1)
		self.assertEqual(sum(row.current_quantity for row in order2.items), 1)

	def test_import_order_repeat_poll_retains_customer_fields_when_fetch_skipped(self):
		"""Customer details fetched on the first import (decision 17: fetch
		once) survive a later poll where the fetch is skipped - the skipped
		poll's normalized payload doesn't mention the customer fields, and
		upsert_marketplace_order() would otherwise blank them out (review
		finding 5)."""
		payload = _order_payload(fbpi_order_nr="ORD-CUSTOMER")
		fake_response = MagicMock()
		fake_response.status_code = 200
		fake_response.json.return_value = {
			"first_name": "Amira",
			"last_name": "Hassan",
			"city": "Dubai",
			"administrative_division": "Dubai",
		}

		with patch("marketplace_integrations.integrations.noon.orders.NoonClient") as client_class:
			client_class.return_value.request.return_value = fake_response
			order1, _ = import_order(self.account, payload)

		self.assertEqual(order1.customer_first_name, "Amira")
		self.assertEqual(order1.customer_last_name, "Hassan")

		# Second poll: the fetch must be skipped entirely (decision 17), yet
		# the previously stored customer fields must survive the upsert.
		with patch("marketplace_integrations.integrations.noon.orders.NoonClient") as client_class2:
			order2, _ = import_order(self.account, payload)
			client_class2.assert_not_called()

		self.assertEqual(order2.name, order1.name)
		order2.reload()
		self.assertEqual(order2.customer_first_name, "Amira")
		self.assertEqual(order2.customer_last_name, "Hassan")
		self.assertEqual(order2.customer_city, "Dubai")
		self.assertEqual(order2.customer_formatted_address, "Dubai")

	# #178 part 3 reverses #146 decision 7's "never bump once a Sales Order is
	# linked": a poll that CHANGES a linked order bumps the sync debounce, a poll
	# that merely re-confirms it (or only changes volatile fields) does not.

	def _item(self, integration_status="INTEGRATION_ITEM_STATUS_ACKNOWLEDGED"):
		return [
			{
				"mp_item_nr": 1,
				"partner_sku": "SKU-MAPPED",
				"mp_status": "MP_ITEM_STATUS_CONFIRMED",
				"integration_status": integration_status,
				"delivered_invoice_price": 50.0,
			}
		]

	def _linked_order(self, fbpi_order_nr):
		order, _ = import_order(self.account, _order_payload(fbpi_order_nr=fbpi_order_nr, items=self._item()))
		# A REAL Sales Order (not a made-up name) so link validation on re-import passes.
		fixture = LinkedSalesOrderFixture()
		self._fixtures.append(fixture)
		fixture.link(order.name)
		return order

	def _debounce(self, order):
		return frappe.db.get_value(
			"Marketplace Order",
			order.name,
			["sync_debounce_until", "pending_sync_trigger_source"],
			as_dict=True,
		)

	def test_import_of_a_changed_linked_order_bumps_once_with_noon_order_import(self):
		order = self._linked_order("ORD-LINKED-CHANGED")

		import_order(
			self.account,
			_order_payload(
				fbpi_order_nr="ORD-LINKED-CHANGED", items=self._item("INTEGRATION_ITEM_STATUS_SHIPPED")
			),
		)

		state = self._debounce(order)
		self.assertIsNotNone(state.sync_debounce_until)
		self.assertEqual(state.pending_sync_trigger_source, "Noon Order Import")

	def test_replayed_import_of_a_linked_order_does_not_bump_again(self):
		order = self._linked_order("ORD-LINKED-REPLAY")
		payload = _order_payload(
			fbpi_order_nr="ORD-LINKED-REPLAY", items=self._item("INTEGRATION_ITEM_STATUS_SHIPPED")
		)
		import_order(self.account, payload)
		frappe.db.set_value("Marketplace Order", order.name, "sync_debounce_until", None)

		import_order(self.account, payload)

		self.assertIsNone(self._debounce(order).sync_debounce_until)

	def test_unchanged_reimport_of_a_linked_order_does_not_bump(self):
		order = self._linked_order("ORD-LINKED-SAME")

		import_order(self.account, _order_payload(fbpi_order_nr="ORD-LINKED-SAME", items=self._item()))

		self.assertIsNone(self._debounce(order).sync_debounce_until)

	def test_reimport_with_a_same_day_order_created_at_change_does_not_bump(self):
		order = self._linked_order("ORD-LINKED-VOLATILE")

		import_order(
			self.account,
			_order_payload(
				fbpi_order_nr="ORD-LINKED-VOLATILE",
				items=self._item(),
				order_created_at="2026-09-30T10:00:01Z",
			),
		)

		self.assertIsNone(self._debounce(order).sync_debounce_until)

	def test_reimport_with_a_different_day_order_created_at_bumps(self):
		"""received_on feeds the Sales Order transaction/delivery date, so a changed date
		is snapshot-relevant evidence (a same-day change is not)."""
		order = self._linked_order("ORD-LINKED-DATE")

		import_order(
			self.account,
			_order_payload(
				fbpi_order_nr="ORD-LINKED-DATE", items=self._item(), order_created_at="2026-10-05T10:00:00Z"
			),
		)

		self.assertIsNotNone(self._debounce(order).sync_debounce_until)

	def test_self_initiated_import_labels_its_bump_with_the_given_trigger_source(self):
		"""Our own create-shipment / mark-out-of-stock / refresh re-imports pass
		bump_trigger_source="Manual" so their sync is not mislabelled "Noon Order Import"."""
		order = self._linked_order("ORD-LINKED-SELF")

		import_order(
			self.account,
			_order_payload(
				fbpi_order_nr="ORD-LINKED-SELF", items=self._item("INTEGRATION_ITEM_STATUS_SHIPPED")
			),
			bump_trigger_source="Manual",
		)

		state = self._debounce(order)
		self.assertIsNotNone(state.sync_debounce_until)
		self.assertEqual(state.pending_sync_trigger_source, "Manual")

	def _new_order_sync_state(self, fbpi_order_nr, mp_status, integration_status):
		from marketplace_integrations.services.sales_order_sync import sync_sales_order

		units = [
			{
				"mp_item_nr": 1,
				"partner_sku": "SKU-MAPPED",
				"mp_status": mp_status,
				"integration_status": integration_status,
				"delivered_invoice_price": 50.0,
			}
		]
		order, _ = import_order(self.account, _order_payload(fbpi_order_nr=fbpi_order_nr, items=units))

		# Created Syncing, so the debounce must be set or it would sit there for ever.
		self.assertEqual(frappe.db.get_value("Marketplace Order", order.name, "erpnext_sync_status"), "Syncing")
		self.assertIsNotNone(self._debounce(order).sync_debounce_until)

		result = sync_sales_order(order.name, trigger_source="Noon Order Import")
		values = frappe.db.get_value(
			"Marketplace Order",
			order.name,
			["erpnext_sync_status", "sync_reason_code", "erpnext_sales_order"],
			as_dict=True,
		)
		return result, values

	def test_new_order_with_every_unit_cancelled_becomes_terminal_cancelled(self):
		result, values = self._new_order_sync_state(
			"ORD-NEW-CANCELLED", "MP_ITEM_STATUS_CANCELLED", "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED"
		)

		self.assertTrue(result["ok"])
		self.assertTrue(result["skipped"])
		self.assertEqual(values.erpnext_sync_status, "Cancelled")
		self.assertFalse(values.sync_reason_code)
		self.assertFalse(values.erpnext_sales_order)

	def test_new_order_with_units_out_of_stock_but_not_cancelled_ends_in_sync_error(self):
		result, values = self._new_order_sync_state(
			"ORD-NEW-OOS", "MP_ITEM_STATUS_CONFIRMED", "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"
		)

		self.assertFalse(result["ok"])
		self.assertEqual(values.erpnext_sync_status, "Sync Error")
		self.assertTrue(values.sync_reason_code)

	def test_import_matching_the_out_of_stock_marker_is_an_echo_that_queues_the_sync_after_commit(self):
		order = self._linked_order("ORD-ECHO")
		record_noon_out_of_stock(order.name, ["1"])
		payload = _order_payload(
			fbpi_order_nr="ORD-ECHO", items=self._item("INTEGRATION_ITEM_STATUS_OUT_OF_STOCK")
		)

		with (
			patch("marketplace_integrations.services.echo_guard.is_job_enqueued", return_value=False),
			patch("marketplace_integrations.services.echo_guard.frappe.enqueue") as enqueue,
		):
			import_order(self.account, payload)

		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["marketplace_order"], order.name)
		self.assertEqual(kwargs["trigger_source"], "Noon Order Import")
		self.assertEqual(kwargs["job_id"], f"marketplace-echo-sync-{order.name}")
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertTrue(kwargs["deduplicate"])
		self.assertIsNone(self._debounce(order).sync_debounce_until)
		self.assertFalse(frappe.db.get_value("Marketplace Order", order.name, "expected_change_hash"))

	def test_import_differing_from_the_out_of_stock_marker_is_a_real_change(self):
		order = self._linked_order("ORD-ECHO-DIFF")
		record_noon_out_of_stock(order.name, ["1"])
		payload = _order_payload(
			fbpi_order_nr="ORD-ECHO-DIFF", items=self._item("INTEGRATION_ITEM_STATUS_SHIPPED")
		)

		with patch("marketplace_integrations.services.echo_guard.frappe.enqueue") as enqueue:
			import_order(self.account, payload)

		enqueue.assert_not_called()
		self.assertIsNotNone(self._debounce(order).sync_debounce_until)
		self.assertFalse(frappe.db.get_value("Marketplace Order", order.name, "expected_change_hash"))

	def test_own_refresh_leaves_the_out_of_stock_marker_for_the_later_poll(self):
		order = self._linked_order("ORD-ECHO-OWN")
		record_noon_out_of_stock(order.name, ["1"])
		payload = _order_payload(
			fbpi_order_nr="ORD-ECHO-OWN", items=self._item("INTEGRATION_ITEM_STATUS_OUT_OF_STOCK")
		)

		import_order(self.account, payload, bump_trigger_source="Manual", echo_guard=False)

		self.assertTrue(frappe.db.get_value("Marketplace Order", order.name, "expected_change_hash"))
		self.assertIsNotNone(self._debounce(order).sync_debounce_until)

	def test_unlinked_changed_order_only_bumps_on_first_eligibility(self):
		order, _ = import_order(
			self.account, _order_payload(fbpi_order_nr="ORD-UNLINKED", items=self._item())
		)
		self.assertEqual(self._debounce(order).pending_sync_trigger_source, "Noon Order Import")
		frappe.db.set_value("Marketplace Order", order.name, "sync_debounce_until", None)

		import_order(
			self.account,
			_order_payload(fbpi_order_nr="ORD-UNLINKED", items=self._item("INTEGRATION_ITEM_STATUS_SHIPPED")),
		)

		self.assertIsNone(self._debounce(order).sync_debounce_until)


class IntegrationTestNoonOrderImportConcurrency(IntegrationTestCase):
	"""#146 decision 10's single most important test: a value committed by a
	concurrent fulfillment write mid-poll (here ``noon_pending_oos_units``;
	the shipment child tables that first motivated it are gone, #415) must
	survive that poll's own later save of the same Marketplace Order.

	``Document.save()`` writes the whole in-memory row and, through
	``update_children()``, re-syncs EVERY ``Table`` field on a parent doc
	(confirmed directly against the installed ``frappe/model/document.py``).
	Before the #146 fix, a poll that read the order's row BEFORE a concurrent
	fulfillment write committed would carry a stale in-memory copy for the
	rest of its own transaction, and its own later ``doc.save()`` would
	silently overwrite the fulfillment write's just-committed values - the
	fulfillment write and the poll would never otherwise conflict or error,
	so this would fail silently. The fix is ``frappe.db.get_value(..., for_update=True)`` as
	the very FIRST read ``import_order()`` performs against this order row
	(before even the ``existing_name``/``had_sales_order_before`` checks),
	matched by the identical locking read at the top of every
	``integrations/noon/fulfillment.py`` write (``_lock_and_load_order()``)
	- this forces the poll to block until the concurrent writer's
	transaction actually commits, so by the time the poll's own
	``frappe.get_doc()`` call runs, it is guaranteed to observe the
	writer's already-committed row rather than a stale snapshot.

	This has to use two real OS threads, each opening its OWN real site
	connection (``frappe.init()``/``frappe.connect()`` - the same pattern a
	real RQ worker uses, confirmed against ``frappe/utils/background_jobs.py``'s
	worker bootstrap) against the same MariaDB. A single-threaded/single-
	connection test cannot exercise real InnoDB row locking at all: MariaDB
	locks are tracked per actual client connection, not per Python object or
	in-process transaction counter.
	"""

	def setUp(self):
		self.warehouse_patch = patch(
			"marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"
		)
		self.warehouse_patch.start()
		self.addCleanup(self.warehouse_patch.stop)
		_start_noon_client_customer_data_patch(self)

		suffix = frappe.generate_hash(length=8)
		self.account = _make_noon_account(f"Concurrency {suffix}")
		_make_item("ITEM-CONCURRENCY")
		_make_mapping(self.account.name, "ITEM-CONCURRENCY", "SKU-CONCURRENCY")

		self.payload = _order_payload(
			fbpi_order_nr=f"ORD-CONC-{suffix}",
			items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "SKU-CONCURRENCY",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					"delivered_invoice_price": 100.0,
				}
			],
		)
		order, _unmapped = import_order(self.account, self.payload)
		frappe.db.commit()
		self.order_name = order.name

	def tearDown(self):
		frappe.db.delete("Marketplace Order", filters={"marketplace_account": self.account.name})
		frappe.db.delete("Marketplace Sync Log", filters={"marketplace_account": self.account.name})
		frappe.db.delete("Marketplace Product Mapping", filters={"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()

	def test_concurrent_fulfillment_write_survives_concurrent_poll_save(self):
		from marketplace_integrations.integrations.noon import fulfillment

		site = frappe.local.site
		order_name = self.order_name
		account_name = self.account.name
		payload = self.payload

		# writer_locked: writer thread signals once it holds the row lock.
		# poll_about_to_read: poll thread signals right before it issues its
		# own locking read against the same row - the writer thread then
		# waits a further beat before committing, to give that read a real
		# chance to actually reach MariaDB and start waiting on the lock
		# (not just reach the Python call site) before the lock is released.
		writer_locked = threading.Event()
		poll_about_to_read = threading.Event()
		errors = []

		def writer_thread():
			try:
				frappe.init(site=site)
				frappe.connect()
				frappe.set_user("Administrator")
				order = fulfillment._lock_and_load_order(order_name)
				writer_locked.set()
				if not poll_about_to_read.wait(timeout=5):
					errors.append(("writer", "poll thread never signalled poll_about_to_read"))
				time.sleep(0.2)
				order.noon_pending_oos_units = frappe.as_json(["1"])
				order.save()
				frappe.db.commit()
			except Exception:
				errors.append(("writer", frappe.get_traceback()))
			finally:
				frappe.destroy()

		def poll_thread():
			try:
				frappe.init(site=site)
				frappe.connect()
				frappe.set_user("Administrator")
				if not writer_locked.wait(timeout=5):
					errors.append(("poll", "writer thread never signalled writer_locked"))
				from marketplace_integrations.integrations.noon.catalog import get_account

				account = get_account(account_name)
				# #146 round-4 fix: mirrors the ``frappe.db.commit()`` real
				# callers (``_run_import_for_account()``/``refresh_order()``)
				# now issue immediately after their own setup reads and
				# before the per-order savepoint/lock flow - without this,
				# ``get_account()``'s plain read above (not
				# ``import_order()``'s own locking read, which is already
				# first-in-FUNCTION) is what establishes this transaction's
				# read view, leaving the locking read below first-in-function
				# but not first-in-transaction.
				frappe.db.commit()
				poll_about_to_read.set()
				# import_order()'s very first statement against this order
				# row is its own for_update=True locking read - this call
				# must block here until the writer thread above commits.
				import_order(account, payload)
				frappe.db.commit()
			except Exception:
				errors.append(("poll", frappe.get_traceback()))
			finally:
				frappe.destroy()

		t_writer = threading.Thread(target=writer_thread)
		t_poll = threading.Thread(target=poll_thread)
		t_writer.start()
		t_poll.start()
		t_writer.join(timeout=15)
		t_poll.join(timeout=15)

		self.assertFalse(errors, f"Concurrency test thread(s) raised: {errors}")

		self.assertEqual(
			frappe.parse_json(frappe.db.get_value("Marketplace Order", order_name, "noon_pending_oos_units")),
			["1"],
			"The concurrently committed value must survive the poll's own save of the same order "
			"(#146 decision 10). A different value here means the poll saved a stale in-memory copy over "
			"it, i.e. the row lock did not actually serialize the two writers.",
		)

		# The poll's own normal work (no behavioral change to #145's
		# idempotent re-import) must still have completed correctly despite
		# being forced to wait on the lock.
		item_rows = frappe.get_all(
			"Marketplace Order Item",
			filters={"parenttype": "Marketplace Order", "parent": order_name},
			fields=["external_sku", "current_quantity"],
		)
		self.assertEqual(len(item_rows), 1)
		self.assertEqual(item_rows[0].external_sku, "SKU-CONCURRENCY")
		self.assertEqual(item_rows[0].current_quantity, 1)


class IntegrationTestNoonOrderImportBatchIsolation(IntegrationTestCase):
	"""Tests for import_orders_created_between() - per-order isolation, sync logging."""

	def setUp(self):
		# Patch warehouse code validation to avoid making real Noon API calls
		self.warehouse_patch = patch(
			"marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"
		)
		self.warehouse_patch.start()
		self.addCleanup(self.warehouse_patch.stop)
		_start_noon_client_customer_data_patch(self)

		suffix = frappe.generate_hash(length=8)
		self.account = _make_noon_account(f"Batch {suffix}")
		_make_item("ITEM-GOOD")
		_make_mapping(self.account.name, "ITEM-GOOD", "SKU-GOOD")

	def tearDown(self):
		frappe.db.delete("Marketplace Order", filters={"marketplace_account": self.account.name})
		frappe.db.delete(
			"Marketplace Sync Log",
			filters={
				"sync_type": "Order Import",
				"marketplace_account": self.account.name,
			},
		)
		frappe.db.delete("Marketplace Product Mapping", filters={"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()

	def test_one_bad_order_does_not_stop_the_rest(self):
		"""Per-order savepoint isolation (decision 6) - one bad order is rolled
		back and logged independently, but good orders after it still import."""

		def mock_list_orders(account, created_after=None, created_before=None):
			yield _order_payload(
				fbpi_order_nr="ORD-BAD",
				warehouse_code="WRONG-WH",  # Will fail warehouse validation
			)
			yield _order_payload(
				fbpi_order_nr="ORD-GOOD",
				items=[
					{
						"mp_item_nr": 1,
						"partner_sku": "SKU-GOOD",
						"mp_status": "MP_ITEM_STATUS_CONFIRMED",
						"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
						"delivered_invoice_price": 100.0,
					}
				],
			)

		with patch(
			"marketplace_integrations.integrations.noon.orders.list_orders",
			side_effect=mock_list_orders,
		):
			_imported, errors = import_orders_created_between(
				self.account, "2026-09-23T00:00:00Z", "2026-09-30T00:00:00Z"
			)

		self.assertEqual(len(errors), 1)
		self.assertEqual(errors[0]["fbpi_order_nr"], "ORD-BAD")
		self.assertIn("warehouse", errors[0]["error"].lower())
		self.assertEqual(len(_imported), 1)
		self.assertEqual(
			_imported[0], frappe.db.get_value("Marketplace Order", {"external_order_id": "ORD-GOOD"})
		)

	def test_per_order_failure_writes_sync_log_with_error_status(self):
		"""Each failed order import writes a Marketplace Sync Log row
		(sync_type="Order Import", status="Error") with the error traceback."""

		def mock_list_orders(account, created_after=None, created_before=None):
			yield _order_payload(fbpi_order_nr="ORD-FAIL", items=None)  # Missing items list

		with patch(
			"marketplace_integrations.integrations.noon.orders.list_orders",
			side_effect=mock_list_orders,
		):
			_imported, errors = import_orders_created_between(
				self.account, "2026-09-23T00:00:00Z", "2026-09-30T00:00:00Z"
			)

		self.assertEqual(len(errors), 1)
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={
				"sync_type": "Order Import",
				"status": "Error",
				"marketplace_account": self.account.name,
				"request_data": ["like", "%ORD-FAIL%"],
			},
			fields=["trigger_source", "marketplace_order", "traceback"],
		)
		self.assertEqual(len(logs), 1)
		self.assertIsNone(logs[0].marketplace_order)
		self.assertEqual(logs[0].trigger_source, "Manual")
		self.assertIn("items", logs[0].traceback.lower())

	def test_trigger_source_propagated_to_sync_log(self):
		"""The trigger_source parameter is preserved in failure logs."""

		def mock_list_orders(account, created_after=None, created_before=None):
			yield _order_payload(fbpi_order_nr="ORD-FAIL", warehouse_code="WRONG")

		with patch(
			"marketplace_integrations.integrations.noon.orders.list_orders",
			side_effect=mock_list_orders,
		):
			_imported, _errors = import_orders_created_between(
				self.account,
				"2026-09-23T00:00:00Z",
				"2026-09-30T00:00:00Z",
				trigger_source="Scheduled Reconciliation",
			)

		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={
				"sync_type": "Order Import",
				"status": "Error",
				"request_data": ["like", "%ORD-FAIL%"],
			},
			fields=["trigger_source"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].trigger_source, "Scheduled Reconciliation")

	def test_unmapped_sku_writes_sync_log_with_invalid_status(self):
		"""An order with unmapped SKUs writes a Marketplace Sync Log row
		(status="Invalid", decision 19) listing them."""

		def mock_list_orders(account, created_after=None, created_before=None):
			yield _order_payload(
				fbpi_order_nr="ORD-UNMAPPED",
				items=[
					{
						"mp_item_nr": 1,
						"partner_sku": "UNMAPPED-SKU",
						"mp_status": "MP_ITEM_STATUS_CONFIRMED",
						"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
						"delivered_invoice_price": 100.0,
					}
				],
			)

		with patch(
			"marketplace_integrations.integrations.noon.orders.list_orders",
			side_effect=mock_list_orders,
		):
			_imported, _errors = import_orders_created_between(
				self.account, "2026-09-23T00:00:00Z", "2026-09-30T00:00:00Z"
			)

		self.assertEqual(len(_imported), 1)
		self.assertEqual(len(_errors), 0)
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={
				"sync_type": "Order Import",
				"status": "Invalid",
				"marketplace_account": self.account.name,
			},
			fields=["response_data"],
		)
		self.assertEqual(len(logs), 1)
		response_data = frappe.parse_json(logs[0].response_data)
		self.assertEqual(response_data["unmapped_partner_skus"], ["UNMAPPED-SKU"])

	def test_mp_code_namshi_skipped_silently(self):
		"""Orders with mp_code != 'noon' (e.g. 'namshi_v2') are skipped entirely
		with no Sync Log row (decision 20 - different marketplace, not a rejected Noon order)."""

		def mock_list_orders(account, created_after=None, created_before=None):
			yield _order_payload(fbpi_order_nr="ORD-NAMSHI", mp_code="namshi_v2")
			yield _order_payload(
				fbpi_order_nr="ORD-NOON",
				mp_code="noon",
				items=[
					{
						"mp_item_nr": 1,
						"partner_sku": "SKU-GOOD",
						"mp_status": "MP_ITEM_STATUS_CONFIRMED",
						"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
						"delivered_invoice_price": 100.0,
					}
				],
			)

		with patch(
			"marketplace_integrations.integrations.noon.orders.list_orders",
			side_effect=mock_list_orders,
		):
			_imported, _errors = import_orders_created_between(
				self.account, "2026-09-23T00:00:00Z", "2026-09-30T00:00:00Z"
			)

		# This test mainly verifies that non-noon orders are silently skipped
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={
				"sync_type": "Order Import",
				"marketplace_account": self.account.name,
				"request_data": ["like", "%ORD-NAMSHI%"],
			},
		)
		self.assertEqual(len(logs), 0)

	def test_deadlock_on_locking_read_does_not_abort_the_batch(self):
		"""#146 round-3 review finding #1: a previous version of
		``import_order()`` retried once on ``frappe.QueryDeadlockError``
		(MariaDB error 1020) instead of reordering its locking read to be
		first. Swallowing that error inside ``import_order()`` meant the
		caller's (``_import_order_isolated()``'s) own savepoint - already
		destroyed by MariaDB's whole-transaction rollback when 1020 fired -
		was released via a bare ``frappe.db.release_savepoint()`` call with no
		surrounding try/except, raising "SAVEPOINT does not exist" and
		aborting the ENTIRE per-account run, silently skipping every order
		after the one that hit 1020.

		This fix removed the retry-and-swallow entirely: the locking read is
		now the first statement ``import_order()`` issues, which (per the
		docstring) removes the race that caused 1020 in the first place.

		Round-4 fix note: a later round found that being first-in-function
		was not enough on its own - the race this test originally modeled
		(an earlier plain read in the SAME transaction poisoning the locking
		read) is now structurally closed by an explicit ``frappe.db.commit()``
		each real caller (``_run_import_for_account()``, ``refresh_order()``)
		issues right before the per-order savepoint/lock flow (see
		``IntegrationTestNoonOrderImportConcurrency.
		test_concurrent_fulfillment_write_survives_concurrent_poll_save``,
		which now passes with zero errors, proving that race is closed, not
		just caught). 1020 can therefore no longer fire via that exact
		mechanism in a real run. This test still earns its keep as a
		defense-in-depth check for a DIFFERENT/residual trigger - e.g. a
		genuinely concurrent write landing in the narrow gap between the
		caller's commit and this function's own lock, or any other source of
		a 1020 this module hasn't anticipated - proving that catching and
		logging a per-order exception during batch processing (rather than
		letting it propagate and abort the whole run) is correct regardless
		of what raised it, not specifically re-testing the now-closed race.
		"""
		real_sql = frappe.db.sql
		state = {"fired": False}

		def fake_sql(query, *args, **kwargs):
			if not state["fired"] and isinstance(query, str) and "FOR UPDATE" in query:
				state["fired"] = True
				raise frappe.QueryDeadlockError("(1020, 'Record has changed since last read')")
			# Forward args/kwargs as received instead of defaulting a missing
			# `values` to None: frappe.db.sql()'s real default is a distinct
			# EmptyQueryValues sentinel, and passing an explicit None turns
			# into a stray (None,) query parameter, breaking parameterless
			# calls like frappe.db.savepoint()'s `self.sql(f"savepoint ...")`.
			return real_sql(query, *args, **kwargs)

		def mock_list_orders(account, created_after=None, created_before=None):
			yield _order_payload(fbpi_order_nr="ORD-DEADLOCK")
			yield _order_payload(
				fbpi_order_nr="ORD-AFTER-DEADLOCK",
				items=[
					{
						"mp_item_nr": 1,
						"partner_sku": "SKU-GOOD",
						"mp_status": "MP_ITEM_STATUS_CONFIRMED",
						"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
						"delivered_invoice_price": 100.0,
					}
				],
			)

		with (
			patch(
				"marketplace_integrations.integrations.noon.orders.list_orders",
				side_effect=mock_list_orders,
			),
			patch.object(frappe.db, "sql", side_effect=fake_sql),
		):
			imported, errors = import_orders_created_between(
				self.account, "2026-09-23T00:00:00Z", "2026-09-30T00:00:00Z"
			)

		self.assertEqual(len(errors), 1)
		self.assertEqual(errors[0]["fbpi_order_nr"], "ORD-DEADLOCK")
		self.assertEqual(len(imported), 1)
		self.assertEqual(
			imported[0], frappe.db.get_value("Marketplace Order", {"external_order_id": "ORD-AFTER-DEADLOCK"})
		)


class IntegrationTestNoonOrderPagination(IntegrationTestCase):
	"""Tests for list_orders() pagination logic."""

	def setUp(self):
		# Patch warehouse code validation to avoid making real Noon API calls
		self.warehouse_patch = patch(
			"marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"
		)
		self.warehouse_patch.start()
		self.addCleanup(self.warehouse_patch.stop)

		suffix = frappe.generate_hash(length=8)
		self.account = _make_noon_account(f"Pagination {suffix}")

	def tearDown(self):
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)

	def test_list_orders_paginates_with_next_token(self):
		"""list_orders() fetches multiple pages using next_token until exhausted
		(decision 11 - pagination contract requires fixed body, only next_token changes)."""

		def mock_request(method, path, json=None, params=None):
			response = MagicMock()
			response.status_code = 200
			if params and params.get("next_token") == "TOKEN-1":
				response.json.return_value = {
					"orders": [
						_order_payload(fbpi_order_nr="ORD-PAGE-2"),
						_order_payload(fbpi_order_nr="ORD-PAGE-2-B"),
					],
					"next_token": None,  # Last page
				}
			else:
				response.json.return_value = {
					"orders": [
						_order_payload(fbpi_order_nr="ORD-PAGE-1"),
						_order_payload(fbpi_order_nr="ORD-PAGE-1-B"),
					],
					"next_token": "TOKEN-1",
				}
			return response

		with patch("marketplace_integrations.integrations.noon.orders.NoonClient") as client_class:
			client = client_class.return_value
			client.request.side_effect = mock_request
			orders = list(list_orders(self.account))

		self.assertEqual(len(orders), 4)
		self.assertEqual(orders[0]["fbpi_order_nr"], "ORD-PAGE-1")
		self.assertEqual(orders[2]["fbpi_order_nr"], "ORD-PAGE-2")

	def test_list_orders_detects_duplicate_token_loop(self):
		"""list_orders() detects and rejects if next_token repeats (infinite loop)."""

		def mock_request(method, path, json=None, params=None):
			response = MagicMock()
			response.status_code = 200
			response.json.return_value = {
				"orders": [_order_payload()],
				"next_token": "TOKEN-LOOP",  # Same token every time
			}
			return response

		with patch("marketplace_integrations.integrations.noon.orders.NoonClient") as client_class:
			client = client_class.return_value
			client.request.side_effect = mock_request
			with self.assertRaises(frappe.ValidationError) as cm:
				list(list_orders(self.account))
			self.assertIn("pagination", str(cm.exception).lower())

	def test_list_orders_passes_warehouse_code_in_request_body(self):
		"""list_orders() sends warehouse_code in request body (decision 9)."""

		def mock_request(method, path, json=None, params=None):
			response = MagicMock()
			response.status_code = 200
			response.json.return_value = {"orders": [_order_payload()], "next_token": None}
			self.assertIn("warehouse_code", json)
			self.assertEqual(json["warehouse_code"], self.account.noon_warehouse_code)
			return response

		with patch("marketplace_integrations.integrations.noon.orders.NoonClient") as client_class:
			client = client_class.return_value
			client.request.side_effect = mock_request
			list(list_orders(self.account))


class IntegrationTestNoonOrderRefresh(IntegrationTestCase):
	"""Tests for refresh_order() - manual per-order troubleshooting action."""

	def setUp(self):
		# Patch warehouse code validation to avoid making real Noon API calls
		self.warehouse_patch = patch(
			"marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"
		)
		self.warehouse_patch.start()
		self.addCleanup(self.warehouse_patch.stop)
		_start_noon_client_customer_data_patch(self)

		suffix = frappe.generate_hash(length=8)
		self.account = _make_noon_account(f"Refresh {suffix}")
		_make_item("ITEM-TEST")
		_make_mapping(self.account.name, "ITEM-TEST", "SKU-TEST")
		payload = _order_payload(
			fbpi_order_nr="ORD-REFRESH",
			items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "SKU-TEST",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					"delivered_invoice_price": 100.0,
				}
			],
		)
		self.order = upsert_marketplace_order(
			self.account.name,
			{
				"external_order_id": "ORD-REFRESH",
				"platform_order_reference": "MP-001",
				"order_code": "MP-001",
				"presentment_currency": "AED",
				"items": [
					{
						"external_item_id": "sku:SKU-TEST:MP_ITEM_STATUS_CONFIRMED:INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
						"external_sku": "SKU-TEST",
						"pricing_type": "UNIT",
						"original_quantity": 1,
						"fulfilled_quantity": 1,
						"current_quantity": 1,
						"transaction_amount": 100.0,
						"external_status": "CONFIRMED",
						"integration_status": "ACKNOWLEDGED",
					}
				],
				"latest_payload": payload,
			},
		)

	def tearDown(self):
		frappe.db.delete("Marketplace Order", filters={"marketplace_account": self.account.name})
		frappe.db.delete(
			"Marketplace Sync Log",
			filters={
				"sync_type": "Order Import",
				"marketplace_account": self.account.name,
			},
		)
		frappe.db.delete("Marketplace Product Mapping", filters={"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()

	def test_refresh_order_fetches_and_updates_from_get_fbpi_order(self):
		"""refresh_order() calls GetFbpiOrder and reconciles the stored record.

		The refreshed unit's status has changed (ACKNOWLEDGED -> SHIPPED), so
		per review finding A this produces a NEW status-keyed row alongside
		the original one (retained per upsert_marketplace_order()'s stated
		policy), rather than mutating the original row's status in place."""
		fresh_payload = _order_payload(
			fbpi_order_nr="ORD-REFRESH",
			items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "SKU-TEST",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_SHIPPED",  # Status changed
					"delivered_invoice_price": 100.0,
				}
			],
		)

		with patch(
			"marketplace_integrations.integrations.noon.orders.get_order",
			return_value=fresh_payload,
		):
			result = refresh_order(self.order.name)

		self.assertIn("name", result)
		self.order.reload()
		self.assertEqual(len(self.order.items), 2)
		by_status = {row.integration_status: row for row in self.order.items}
		self.assertEqual(sorted(by_status), ["ACKNOWLEDGED", "SHIPPED"])
		# BLOCKER fix: the stale ACKNOWLEDGED row is retained but zeroed, so
		# refresh_order() can't double-count the same physical unit either.
		self.assertEqual(by_status["ACKNOWLEDGED"].current_quantity, 0)
		self.assertEqual(by_status["ACKNOWLEDGED"].transaction_amount, 0.0)
		self.assertEqual(by_status["SHIPPED"].current_quantity, 1)
		self.assertEqual(sum(row.current_quantity for row in self.order.items), 1)

	def test_refresh_order_validates_fbpi_order_nr_identity(self):
		"""GetFbpiOrder's returned fbpi_order_nr must match the stored
		external_order_id, or mismatch is rejected (decision 21)."""
		different_payload = _order_payload(fbpi_order_nr="ORD-DIFFERENT")

		with patch(
			"marketplace_integrations.integrations.noon.orders.get_order",
			return_value=different_payload,
		):
			with self.assertRaises(frappe.ValidationError) as cm:
				refresh_order(self.order.name)
			self.assertIn("does not match", str(cm.exception))

	def test_refresh_order_validates_mp_code_is_noon(self):
		"""GetFbpiOrder must return an order with mp_code='noon', not another
		marketplace (decision 21)."""
		namshi_payload = _order_payload(fbpi_order_nr="ORD-REFRESH", mp_code="namshi_v2")

		with patch(
			"marketplace_integrations.integrations.noon.orders.get_order",
			return_value=namshi_payload,
		):
			with self.assertRaises(frappe.ValidationError) as cm:
				refresh_order(self.order.name)
			self.assertIn("not a Noon-marketplace order", str(cm.exception))

	def test_refresh_order_requires_stored_external_order_id(self):
		"""Marketplace Order must have external_order_id set to fetch."""
		# A normal save rejects a blank ID. Simulate an already-stored bad row
		# so this exercises refresh_order's own guard.
		frappe.db.set_value("Marketplace Order", self.order.name, "external_order_id", "")
		with self.assertRaises(frappe.ValidationError) as cm:
			refresh_order(self.order.name)
		self.assertIn("no Noon FBPI order number", str(cm.exception))

	def test_refresh_order_failure_logs_and_reraises(self):
		"""Unlike bulk imports (which swallow per-order failures into an
		errors[] list), refresh_order's failure is logged and re-raised so
		the enqueued job's own failure is visible via RQ/Error Log."""

		def mock_get_order(account, fbpi_order_nr):
			frappe.throw("Remote API error")

		with patch(
			"marketplace_integrations.integrations.noon.orders.get_order",
			side_effect=mock_get_order,
		):
			with self.assertRaises(frappe.ValidationError):
				refresh_order(self.order.name)

		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={
				"sync_type": "Order Import",
				"status": "Error",
				"marketplace_account": self.account.name,
			},
			fields=["traceback"],
		)
		self.assertEqual(len(logs), 1)
		self.assertIn("Remote API error", logs[0].traceback)

	def _fresh_payload(self):
		return _order_payload(
			fbpi_order_nr="ORD-REFRESH",
			items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "SKU-TEST",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_SHIPPED",
					"delivered_invoice_price": 100.0,
				}
			],
		)

	def test_refresh_order_bumps_and_logs_with_the_given_trigger_source(self):
		"""Default is "Manual" (Desk); the webhook passes "Noon Webhook" through to the bump."""
		for trigger_source, expected in ((None, "Manual"), ("Noon Webhook", "Noon Webhook")):
			with self.subTest(trigger_source=trigger_source):
				kwargs = {} if trigger_source is None else {"trigger_source": trigger_source}
				with (
					patch(
						"marketplace_integrations.integrations.noon.orders.get_order",
						return_value=self._fresh_payload(),
					),
					patch(
						"marketplace_integrations.integrations.noon.orders.import_order",
						return_value=(self.order, []),
					) as imported,
				):
					refresh_order(self.order.name, **kwargs)
				self.assertEqual(imported.call_args.kwargs["bump_trigger_source"], expected)

	def test_refresh_order_failure_logs_the_given_trigger_source(self):
		with patch(
			"marketplace_integrations.integrations.noon.orders.get_order",
			side_effect=frappe.ValidationError("Remote API error"),
		):
			with self.assertRaises(frappe.ValidationError):
				refresh_order(self.order.name, trigger_source="Noon Webhook")
		self.assertEqual(
			frappe.db.count(
				"Marketplace Sync Log",
				{
					"marketplace_account": self.account.name,
					"status": "Error",
					"trigger_source": "Noon Webhook",
				},
			),
			1,
		)

	def test_webhook_refresh_404_logs_and_falls_back_once_to_account_import(self):
		"""GetFbpiOrder 404 (mocked client): the webhook-driven refresh logs it and queues the
		deduplicated account import exactly once instead of raising; nothing else is retried."""
		response = MagicMock(status_code=404)
		client = MagicMock()
		client.request.return_value = response
		with (
			patch("marketplace_integrations.integrations.noon.orders.NoonClient", return_value=client),
			patch("marketplace_integrations.integrations.noon.orders.enqueue_order_import") as fallback,
		):
			result = refresh_order(self.order.name, trigger_source="Noon Webhook")

		self.assertEqual(client.request.call_count, 1)
		fallback.assert_called_once_with(self.account.name, trigger_source="Noon Webhook")
		self.assertEqual(result["fallback"], "import")
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_account": self.account.name, "status": "Error"},
			fields=["trigger_source", "traceback"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].trigger_source, "Noon Webhook")
		self.assertIn("was not found", logs[0].traceback)

	def test_manual_refresh_404_still_raises_without_fallback(self):
		response = MagicMock(status_code=404)
		client = MagicMock()
		client.request.return_value = response
		with (
			patch("marketplace_integrations.integrations.noon.orders.NoonClient", return_value=client),
			patch("marketplace_integrations.integrations.noon.orders.enqueue_order_import") as fallback,
		):
			with self.assertRaises(frappe.ValidationError):
				refresh_order(self.order.name)
		fallback.assert_not_called()

	def test_run_order_refresh_passes_trigger_source_through(self):
		with patch("marketplace_integrations.integrations.noon.orders.refresh_order") as refreshed:
			run_order_refresh(self.order.name, trigger_source="Noon Webhook")
			run_order_refresh(self.order.name)
		self.assertEqual(
			[call.kwargs["trigger_source"] for call in refreshed.call_args_list], ["Noon Webhook", "Manual"]
		)

	def test_enqueue_order_refresh_has_no_permission_check_and_dedups_per_order(self):
		with (
			self.set_user("Guest"),
			patch("marketplace_integrations.integrations.noon.orders.frappe.enqueue") as enq,
		):
			enqueue_order_refresh(self.order.name, trigger_source="Noon Webhook")
		enq.assert_called_once()
		self.assertEqual(enq.call_args.kwargs["job_id"], f"marketplace-noon-order-refresh-{self.order.name}")
		self.assertTrue(enq.call_args.kwargs["deduplicate"])
		self.assertEqual(enq.call_args.kwargs["trigger_source"], "Noon Webhook")

	def test_queue_order_refresh_still_checks_write_permission(self):
		with patch("marketplace_integrations.integrations.noon.orders.frappe.enqueue") as enq:
			with self.set_user("Guest"):
				with self.assertRaises(frappe.PermissionError):
					queue_order_refresh(self.order.name)
			enq.assert_not_called()
			self.assertEqual(queue_order_refresh(self.order.name), {"queued": True})
		self.assertEqual(enq.call_args.kwargs["trigger_source"], "Manual")


class IntegrationTestNoonPoll(IntegrationTestCase):
	"""Tests for poll_all_accounts() - the scheduled hourly sweep."""

	def setUp(self):
		# Patch warehouse code validation to avoid making real Noon API calls
		self.warehouse_patch = patch(
			"marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"
		)
		self.warehouse_patch.start()
		self.addCleanup(self.warehouse_patch.stop)

		suffix = frappe.generate_hash(length=8)
		self.account = _make_noon_account(f"Poll {suffix}")
		_make_item("ITEM-POLL")
		_make_mapping(self.account.name, "ITEM-POLL", "SKU-POLL")

	def tearDown(self):
		frappe.db.delete("Marketplace Order", filters={"marketplace_account": self.account.name})
		frappe.db.delete("Marketplace Sync Log", filters={"marketplace_account": self.account.name})
		frappe.db.delete("Marketplace Product Mapping", filters={"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _unlinked_order(self, fbpi_order_nr):
		with patch("marketplace_integrations.integrations.noon.orders.NoonClient"):
			order, _unmapped = import_order(self.account, _order_payload(fbpi_order_nr=fbpi_order_nr))
		# The import itself may schedule the first sync; start each test from none pending.
		frappe.db.set_value(
			"Marketplace Order",
			order.name,
			{"sync_debounce_until": None, "pending_sync_trigger_source": None},
			update_modified=False,
		)
		return order.name

	def _debounce(self, name):
		return frappe.db.get_value(
			"Marketplace Order", name, ["sync_debounce_until", "pending_sync_trigger_source"], as_dict=True
		)

	def _run(self, trigger_source, catch_up_unlinked, import_result=([], [])):
		from marketplace_integrations.integrations.noon.orders import run_order_import

		base = "marketplace_integrations.integrations.noon.orders"
		with (
			patch(f"{base}.validate_connection_enabled"),
			patch(f"{base}._validate_order_import_setup"),
			patch(f"{base}.import_orders_created_between", return_value=import_result),
		):
			return run_order_import(
				self.account.name, trigger_source, catch_up_unlinked=catch_up_unlinked
			)

	def test_hourly_poll_does_not_run_the_catch_up(self):
		name = self._unlinked_order("ORD-HOURLY")

		result = self._run("Scheduled Reconciliation", catch_up_unlinked=False)

		self.assertEqual(result["scheduled"], 0)
		self.assertIsNone(self._debounce(name).sync_debounce_until)
		self.assertFalse(
			frappe.db.exists(
				"Marketplace Sync Log",
				{"marketplace_account": self.account.name, "trigger_source": "Scheduled Reconciliation"},
			)
		)

	def test_catch_up_schedules_unlinked_orders_skips_cancelled_and_linked_and_logs_a_summary(self):
		unlinked = self._unlinked_order("ORD-UNLINKED")
		cancelled = self._unlinked_order("ORD-CANCELLED")
		frappe.db.set_value("Marketplace Order", cancelled, "erpnext_sync_status", "Cancelled")
		linked = self._unlinked_order("ORD-LINKED")
		fixture = LinkedSalesOrderFixture()
		self.addCleanup(fixture.cleanup)
		fixture.link(linked)

		result = self._run("Scheduled Reconciliation", catch_up_unlinked=True, import_result=([1, 2], [3]))

		self.assertEqual(result, {"imported": 2, "errors": 1, "scheduled": 1})
		state = self._debounce(unlinked)
		self.assertIsNotNone(state.sync_debounce_until)
		self.assertEqual(state.pending_sync_trigger_source, "Scheduled Reconciliation")
		self.assertIsNone(self._debounce(cancelled).sync_debounce_until)
		self.assertIsNone(self._debounce(linked).sync_debounce_until)
		log = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_account": self.account.name, "trigger_source": "Scheduled Reconciliation"},
			fields=["status", "response_data"],
		)
		self.assertEqual(len(log), 1)
		self.assertEqual(log[0].status, "Error")
		self.assertEqual(
			frappe.parse_json(log[0].response_data),
			{"imported": 2, "first_syncs_scheduled": 1, "failures": 1},
		)

	def test_catch_up_run_twice_does_not_push_the_pending_window_forward(self):
		name = self._unlinked_order("ORD-TWICE")

		self._run("Scheduled Reconciliation", catch_up_unlinked=True)
		first = self._debounce(name).sync_debounce_until
		self._run("Scheduled Reconciliation", catch_up_unlinked=True)

		self.assertEqual(self._debounce(name).sync_debounce_until, first)

	def test_catch_up_has_its_own_job_id_so_it_is_not_deduplicated_against_the_hourly_job(self):
		from marketplace_integrations.integrations.noon.orders import enqueue_order_import

		with patch("marketplace_integrations.integrations.noon.orders.frappe.enqueue") as enqueue:
			enqueue_order_import(self.account.name, trigger_source="Scheduled Reconciliation")
			enqueue_order_import(
				self.account.name, trigger_source="Scheduled Reconciliation", catch_up_unlinked=True
			)

		hourly, nightly = (call.kwargs for call in enqueue.call_args_list)
		self.assertNotEqual(hourly["job_id"], nightly["job_id"])
		self.assertFalse(hourly["catch_up_unlinked"])
		self.assertTrue(nightly["catch_up_unlinked"])

	def test_poll_all_accounts_enqueues_one_deduplicated_job_per_account(self):
		"""poll_all_accounts() is lightweight (post-#145-review fix): it never
		lists/imports orders itself, it only enqueues one background job per
		enabled Noon account on the ``long`` queue, using the exact same
		job_id scheme (``_ORDER_IMPORT_RQ_JOB_PREFIX-<account>``) and worker
		(``run_order_import``) that ``queue_order_import()`` already uses for
		a manual Desk-triggered import - so a manual import and this hourly
		poll for the same account properly dedupe against each other via
		RQ's own deduplicate=True."""

		from marketplace_integrations.integrations.noon.orders import run_order_import

		with patch("marketplace_integrations.integrations.noon.orders.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			result = poll_all_accounts()

		# Filter enqueue calls to only this test's own account (ignore any stale
		# accounts from prior test runs - see Shopify's tearDown pattern in
		# shopify/test_orders.py's IntegrationTestImportOrdersUpdatedBetween).
		relevant_calls = [
			call for call in enqueue.call_args_list if call.kwargs.get("account_name") == self.account.name
		]
		self.assertEqual(len(relevant_calls), 1)

		# Assert the exact call signature for this account
		enqueue.assert_any_call(
			run_order_import,
			queue="long",
			timeout=3600,
			job_id=f"{_ORDER_IMPORT_RQ_JOB_PREFIX}-{self.account.name}",
			deduplicate=True,
			account_name=self.account.name,
			trigger_source="Scheduled Reconciliation",
			catch_up_unlinked=False,
		)
		# Result contains the count of all enabled accounts queued (including
		# stale ones from prior test runs), so just verify it's present and >= 1
		self.assertIn("queued", result)
		self.assertGreaterEqual(result["queued"], 1)

	def test_poll_all_accounts_isolates_enqueue_failures(self):
		"""One account's enqueue failure (e.g. a transient Redis error) doesn't
		stop the remaining accounts in the same sweep from being enqueued -
		the per-account isolation this function always had, now scoped to the
		enqueue call itself rather than a full inline import run."""

		account_b = _make_noon_account(f"Poll B {frappe.generate_hash(length=8)}")

		def mock_enqueue(worker, **kwargs):
			if kwargs.get("account_name") == self.account.name:
				raise Exception("boom: redis unreachable")
			return MagicMock()

		try:
			with patch(
				"marketplace_integrations.integrations.noon.orders.frappe.enqueue",
				side_effect=mock_enqueue,
			) as enqueue:
				result = poll_all_accounts()

			# Filter enqueue calls to only the two accounts created by this test
			# (ignore any stale accounts from prior test runs - see Shopify's pattern).
			relevant_calls = [
				call
				for call in enqueue.call_args_list
				if call.kwargs.get("account_name") in (self.account.name, account_b.name)
			]
			self.assertEqual(len(relevant_calls), 2)
			# Only account_b's job counts toward "queued" since self.account's enqueue raised.
			# The result will include all other stale accounts that enqueued successfully,
			# so just verify it's present and >= 1 (at least account_b was queued).
			self.assertIn("queued", result)
			self.assertGreaterEqual(result["queued"], 1)
		finally:
			frappe.db.delete("Marketplace Order", filters={"marketplace_account": account_b.name})
			frappe.delete_doc("Marketplace Account", account_b.name, ignore_permissions=True, force=True)
			frappe.delete_doc("Noon Connection", account_b.connection, ignore_permissions=True, force=True)
			frappe.db.commit()
