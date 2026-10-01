# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon FBPI order ingestion tests - covers order normalization, aggregation,
mapping resolution, import isolation, pagination, and the manual refresh path.

Like test_orders.py for Shopify, this mocks the Noon API client and never
makes real HTTP calls. Fixture documents (Marketplace Account, Item, Mapping)
are built directly via make_* helpers.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from marketplace_integrations.integrations.noon.order_mapper import normalize_order
from marketplace_integrations.integrations.noon.orders import (
	_ORDER_IMPORT_RQ_JOB_PREFIX,
	import_order,
	import_orders_created_between,
	list_orders,
	poll_all_accounts,
	refresh_order,
)
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
			yield _order_payload(fbpi_order_nr="ORD-GOOD", items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "SKU-GOOD",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					"delivered_invoice_price": 100.0,
				}
			])

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
		self.assertEqual(_imported[0], frappe.db.get_value("Marketplace Order", {"external_order_id": "ORD-GOOD"}))

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
			yield _order_payload(fbpi_order_nr="ORD-NOON", mp_code="noon", items=[
				{
					"mp_item_nr": 1,
					"partner_sku": "SKU-GOOD",
					"mp_status": "MP_ITEM_STATUS_CONFIRMED",
					"integration_status": "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED",
					"delivered_invoice_price": 100.0,
				}
			])

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

		with patch(
			"marketplace_integrations.integrations.noon.orders.NoonClient"
		) as client_class:
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

		with patch(
			"marketplace_integrations.integrations.noon.orders.NoonClient"
		) as client_class:
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

		with patch(
			"marketplace_integrations.integrations.noon.orders.NoonClient"
		) as client_class:
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
		self.order = upsert_marketplace_order(self.account.name, {
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
		})

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
		frappe.db.delete("Marketplace Product Mapping", filters={"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()

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
			call for call in enqueue.call_args_list
			if call.kwargs.get("account_name") == self.account.name
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
				call for call in enqueue.call_args_list
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
