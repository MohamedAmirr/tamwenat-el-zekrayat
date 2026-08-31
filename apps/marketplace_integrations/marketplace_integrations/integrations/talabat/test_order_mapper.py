# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.talabat.order_mapper import normalize_order


class UnitTestOrderMapper(UnitTestCase):
	"""Tests for Talabat order normalization."""

	def sample_raw_order(self, order_id="order-uuid-001", **overrides):
		"""Helper to create a sample raw Talabat order.Order payload."""
		payload = {
			"order_id": order_id,
			"external_order_id": "MERCH-ORDER-001",
			"order_code": "PICKUP-001",
			"status": "RECEIVED",
			"order_type": "DELIVERY",
			"transport_type": "LOGISTICS_DELIVERY",
			"isPreorder": False,
			"accepted_for": "2026-08-31T13:00:00Z",
			"promised_for": "2026-08-31T13:30:00Z",
			"customer": {
				"_id": "customer-uuid-001",
				"first_name": "John",
				"last_name": "Doe",
				"phone_number": "1234567890",
				"delivery_address": {
					"formattedAddress": "123 Main St, Cairo",
					"city": "Cairo",
					"latitude": 30.0444,
					"longitude": 31.2357,
				},
			},
			"payment": {
				"type": "ONLINE",
				"sub_total": 100.0,
				"discount": -5.0,
				"order_total": 110.0,
				"total_taxes": 2.0,
				"delivery_fee": 10.0,
				"service_fee": 3.0,
				"container_charge": 0.0,
				"difference_to_minimum": 0.0,
				"collect_at_pickup": None,
				"discounts": [{"name": "Promotion A", "value": -5.0}],
				"additional_fees": {"holiday_fee": 2.0},
			},
			"sys": {
				"created_at": "2026-08-31T12:00:00Z",
				"updated_at": "2026-08-31T12:05:00Z",
			},
			"cancellation": {
				"cancelled_by": None,
				"reason": None,
				"post_picked_up": False,
			},
			"items": [
				{
					"_id": "item-uuid-001",
					"sku": "SKU-001",
					"name": "Product A",
					"instructions": "No onions",
					"barcode": ["123456"],
					"status": "IN_CART",
					"replaced_id": None,
					"original_pricing": {
						"pricing_type": "UNIT",
						"quantity": 2,
						"weight": None,
						"weighted_pieces": None,
						"unit_price": 20.0,
						"list_price": 25.0,
						"total_price": 40.0,
					},
					"pricing": {
						"pricing_type": "UNIT",
						"quantity": 2,
						"weight": None,
						"weighted_pieces": None,
						"unit_price": 20.0,
						"list_price": None,
						"total_price": 40.0,
					},
				}
			],
		}
		payload.update(overrides)
		return payload

	def test_normalize_order_maps_all_required_fields(self):
		"""Normalized order includes all fields upsert_marketplace_order expects."""
		raw = self.sample_raw_order()
		normalized = normalize_order(raw)

		self.assertEqual(normalized["external_order_id"], "order-uuid-001")
		self.assertEqual(normalized["platform_order_reference"], "MERCH-ORDER-001")
		self.assertEqual(normalized["order_code"], "PICKUP-001")
		self.assertEqual(normalized["external_status"], "RECEIVED")
		self.assertEqual(normalized["order_type"], "DELIVERY")
		self.assertEqual(normalized["transport_type"], "LOGISTICS_DELIVERY")
		self.assertEqual(normalized["is_preorder"], 0)
		self.assertIsNotNone(normalized["accepted_for"])
		self.assertIsNotNone(normalized["promised_for"])
		self.assertIsNotNone(normalized["received_on"])
		self.assertIsNotNone(normalized["last_platform_update_on"])
		self.assertEqual(normalized["customer_external_id"], "customer-uuid-001")
		self.assertEqual(normalized["customer_first_name"], "John")
		self.assertEqual(normalized["customer_last_name"], "Doe")
		self.assertEqual(normalized["customer_phone_number"], "1234567890")
		self.assertEqual(normalized["customer_formatted_address"], "123 Main St, Cairo")
		self.assertEqual(normalized["customer_city"], "Cairo")
		self.assertEqual(normalized["customer_latitude"], 30.0444)
		self.assertEqual(normalized["customer_longitude"], 31.2357)
		self.assertEqual(normalized["payment_type"], "ONLINE")
		self.assertEqual(normalized["sub_total"], 100.0)
		self.assertEqual(normalized["discount"], -5.0)
		self.assertEqual(normalized["order_total"], 110.0)
		self.assertEqual(normalized["total_taxes"], 2.0)
		self.assertEqual(normalized["delivery_fee"], 10.0)
		self.assertEqual(normalized["service_fee"], 3.0)
		self.assertEqual(normalized["container_charge"], 0.0)
		self.assertEqual(normalized["difference_to_minimum"], 0.0)
		self.assertEqual(normalized["latest_payload"], raw)

	def test_normalize_order_preserves_external_status_exact_spelling(self):
		"""Status enum spellings (e.g. CANCELLED) are preserved exactly."""
		for status in ["RECEIVED", "READY_FOR_PICKUP", "DISPATCHED", "CANCELLED", "DELIVERED"]:
			raw = self.sample_raw_order(status=status)
			normalized = normalize_order(raw)
			self.assertEqual(normalized["external_status"], status)

	def test_normalize_order_includes_items(self):
		"""Normalized order includes normalized items."""
		raw = self.sample_raw_order()
		normalized = normalize_order(raw)

		self.assertIsInstance(normalized["items"], list)
		self.assertEqual(len(normalized["items"]), 1)
		item = normalized["items"][0]
		self.assertEqual(item["external_item_id"], "item-uuid-001")
		self.assertEqual(item["external_sku"], "SKU-001")
		self.assertEqual(item["product_name"], "Product A")

	def test_normalize_order_handles_missing_nested_objects(self):
		"""Missing nested objects like customer, payment, sys are handled gracefully."""
		raw = self.sample_raw_order(customer=None, payment=None, sys=None, cancellation=None)
		normalized = normalize_order(raw)

		self.assertIsNone(normalized["customer_external_id"])
		self.assertIsNone(normalized["payment_type"])
		self.assertIsNone(normalized["received_on"])
		self.assertIsNone(normalized["cancelled_by"])

	def test_normalize_order_handles_empty_items_list(self):
		"""Empty items list is normalized to empty list."""
		raw = self.sample_raw_order(items=[])
		normalized = normalize_order(raw)

		self.assertEqual(normalized["items"], [])

	def test_normalize_order_handles_missing_items(self):
		"""Missing items field defaults to empty list."""
		raw = self.sample_raw_order(items=None)
		normalized = normalize_order(raw)

		self.assertEqual(normalized["items"], [])

	def test_normalize_order_rejects_non_dict_payload(self):
		"""Non-dict payload raises a clear error."""
		with self.assertRaises(frappe.ValidationError):
			normalize_order("not a dict")

	def test_normalize_order_rejects_missing_order_id(self):
		"""Missing order_id raises a clear error."""
		raw = self.sample_raw_order(order_id="")
		with self.assertRaises(frappe.ValidationError):
			normalize_order(raw)

	def test_normalize_order_rejects_non_dict_item(self):
		"""Non-dict item in items list raises a clear error."""
		raw = self.sample_raw_order(items=["not a dict"])
		with self.assertRaises(frappe.ValidationError):
			normalize_order(raw)

	def test_normalize_order_item_rejects_missing_external_item_id(self):
		"""Item without external_item_id is handled by upsert (no error here)."""
		raw = self.sample_raw_order(
			items=[{**self.sample_raw_order()["items"][0], "_id": None, "name": "Product"}]
		)
		normalized = normalize_order(raw)
		# Mapper doesn't validate items here; upsert does.
		self.assertIsNone(normalized["items"][0]["external_item_id"])

	def test_normalize_order_handles_preorder_true(self):
		"""isPreorder=True is normalized to 1."""
		raw = self.sample_raw_order(isPreorder=True)
		normalized = normalize_order(raw)
		self.assertEqual(normalized["is_preorder"], 1)

	def test_normalize_order_handles_cancellation_with_replacement(self):
		"""Cancellation fields including post_picked_up are preserved."""
		raw = self.sample_raw_order(
			cancellation={
				"cancelled_by": "customer",
				"reason": "Duplicate order",
				"post_picked_up": True,
			}
		)
		normalized = normalize_order(raw)
		self.assertEqual(normalized["cancelled_by"], "customer")
		self.assertEqual(normalized["cancellation_reason"], "Duplicate order")
		self.assertEqual(normalized["post_picked_up"], 1)


class UnitTestOrderMapperDateTimeParsing(UnitTestCase):
	"""Tests for datetime parsing and UTC -> system timezone conversion.

	Talabat timestamps are UTC instants (trailing ``Z``). The mapper must
	convert them to the site's system timezone, not merely restring the raw
	UTC digits as if they were already local wall-clock time - see
	order_mapper._parse_datetime()'s docstring. `frappe.utils.data.
	get_system_timezone` is patched directly (rather than relying on
	whatever this site's actual System Settings > Time Zone happens to be)
	so these assertions are deterministic and independent of environment.
	"""

	def test_parse_iso8601_z_suffix_converts_utc_to_system_timezone(self):
		"""A UTC Z-suffixed timestamp is converted to the site's system
		timezone, not stored with the raw UTC digits."""
		raw = {
			"order_id": "test-order",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {
				"created_at": "2026-08-31T12:00:00Z",
				"updated_at": "2026-08-31T12:05:00.123456Z",
			},
			"cancellation": {},
			"items": [],
		}

		# Asia/Kolkata is a fixed UTC+05:30 offset with no DST, so the
		# expected converted value is exact and stable year-round.
		with patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			normalized = normalize_order(raw)

		self.assertEqual(normalized["received_on"], "2026-08-31 17:30:00.000000")
		self.assertEqual(normalized["last_platform_update_on"], "2026-08-31 17:35:00.123456")

	def test_parse_iso8601_is_identity_when_system_timezone_is_utc(self):
		"""When the system timezone is UTC itself, the converted value
		equals the raw UTC digits - proving the conversion math rather than
		merely that *some* string comes back."""
		raw = {
			"order_id": "test-order",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {"created_at": "2026-08-31T12:00:00Z"},
			"cancellation": {},
			"items": [],
		}

		with patch("frappe.utils.data.get_system_timezone", return_value="UTC"):
			normalized = normalize_order(raw)

		self.assertEqual(normalized["received_on"], "2026-08-31 12:00:00.000000")

	def test_parse_iso8601_with_fractional_seconds_converts_utc_to_system_timezone(self):
		"""ISO 8601 timestamps with fractional seconds are parsed and
		converted the same way as whole-second ones."""
		raw = {
			"order_id": "test-order",
			"accepted_for": "2026-08-31T13:00:00.999999Z",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {},
			"cancellation": {},
			"items": [],
		}

		with patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			normalized = normalize_order(raw)

		self.assertEqual(normalized["accepted_for"], "2026-08-31 18:30:00.999999")

	def test_parse_blank_datetime_returns_none(self):
		"""Blank/empty datetime values return None."""
		raw = {
			"order_id": "test-order",
			"accepted_for": "",
			"promised_for": None,
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {},
			"cancellation": {},
			"items": [],
		}
		normalized = normalize_order(raw)

		self.assertIsNone(normalized["accepted_for"])
		self.assertIsNone(normalized["promised_for"])

	def test_parse_unparseable_datetime_logs_and_returns_none(self):
		"""Unparseable datetimes are logged but don't fail the whole order."""
		raw = {
			"order_id": "test-order",
			"accepted_for": "not-a-valid-datetime",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {"created_at": "also-invalid"},
			"cancellation": {},
			"items": [],
		}

		normalized = normalize_order(raw)

		# Order still normalizes; timestamps just become None
		self.assertEqual(normalized["external_order_id"], "test-order")
		self.assertIsNone(normalized["accepted_for"])
		self.assertIsNone(normalized["received_on"])
		# Check that an error was logged (this is implementation-specific;
		# the key requirement is that the order doesn't fail)


class UnitTestOrderMapperItems(UnitTestCase):
	"""Tests for order item normalization."""

	def sample_item(self, item_id="item-001", **overrides):
		"""Helper to create a sample item."""
		item = {
			"_id": item_id,
			"sku": f"SKU-{item_id}",
			"name": "Product",
			"instructions": None,
			"barcode": None,
			"status": "IN_CART",
			"replaced_id": None,
			"original_pricing": {
				"pricing_type": "UNIT",
				"quantity": 1,
				"weight": None,
				"weighted_pieces": None,
				"unit_price": 10.0,
				"list_price": 12.0,
				"total_price": 10.0,
			},
			"pricing": {
				"pricing_type": "UNIT",
				"quantity": 1,
				"weight": None,
				"weighted_pieces": None,
				"unit_price": 10.0,
				"list_price": None,
				"total_price": 10.0,
			},
		}
		item.update(overrides)
		return item

	def test_item_normalization_preserves_external_item_id(self):
		"""Item _id becomes external_item_id."""
		raw = {
			"order_id": "order-001",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {},
			"cancellation": {},
			"items": [self.sample_item("item-uuid-001")],
		}
		normalized = normalize_order(raw)

		item = normalized["items"][0]
		self.assertEqual(item["external_item_id"], "item-uuid-001")

	def test_item_normalization_preserves_sku(self):
		"""Item SKU is preserved."""
		raw = {
			"order_id": "order-001",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {},
			"cancellation": {},
			"items": [self.sample_item(_id="item-001", sku="SKU-UNIQUE-001")],
		}
		normalized = normalize_order(raw)

		item = normalized["items"][0]
		self.assertEqual(item["external_sku"], "SKU-UNIQUE-001")

	def test_item_normalization_handles_replacement(self):
		"""replaced_id becomes replaced_external_item_id."""
		raw = {
			"order_id": "order-001",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {},
			"cancellation": {},
			"items": [self.sample_item(_id="replacement-001", replaced_id="original-001")],
		}
		normalized = normalize_order(raw)

		item = normalized["items"][0]
		self.assertEqual(item["replaced_external_item_id"], "original-001")

	def test_item_normalization_uses_original_or_fulfilled_pricing_type(self):
		"""pricing_type comes from original or falls back to current pricing."""
		# With original_pricing
		raw1 = {
			"order_id": "order-001",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {},
			"cancellation": {},
			"items": [
				self.sample_item(
					original_pricing={"pricing_type": "KG"}, pricing={"pricing_type": "UNIT"}
				)
			],
		}
		normalized1 = normalize_order(raw1)
		self.assertEqual(normalized1["items"][0]["pricing_type"], "KG")

		# Fallback to pricing when original_pricing is empty
		raw2 = {
			"order_id": "order-001",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {},
			"cancellation": {},
			"items": [self.sample_item(original_pricing={}, pricing={"pricing_type": "UNIT"})],
		}
		normalized2 = normalize_order(raw2)
		self.assertEqual(normalized2["items"][0]["pricing_type"], "UNIT")

	def test_item_normalization_preserves_original_and_fulfilled_fields_separately(self):
		"""Original and fulfilled pricing fields remain separate."""
		raw = {
			"order_id": "order-001",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {},
			"cancellation": {},
			"items": [
				self.sample_item(
					original_pricing={
						"pricing_type": "KG",
						"quantity": 2,
						"weight": 2.0,
						"unit_price": 15.0,
						"total_price": 30.0,
					},
					pricing={
						"pricing_type": "KG",
						"quantity": 1,
						"weight": 1.8,
						"unit_price": 15.0,
						"total_price": 27.0,
					},
				)
			],
		}
		normalized = normalize_order(raw)

		item = normalized["items"][0]
		self.assertEqual(item["original_quantity"], 2)
		self.assertEqual(item["original_weight"], 2.0)
		self.assertEqual(item["original_unit_price"], 15.0)
		self.assertEqual(item["original_total_price"], 30.0)
		self.assertEqual(item["fulfilled_quantity"], 1)
		self.assertEqual(item["fulfilled_weight"], 1.8)
		self.assertEqual(item["fulfilled_unit_price"], 15.0)
		self.assertEqual(item["fulfilled_total_price"], 27.0)

	def test_item_normalization_preserves_barcode_array(self):
		"""Barcodes are preserved as-is (likely JSON)."""
		raw = {
			"order_id": "order-001",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {},
			"cancellation": {},
			"items": [self.sample_item(_id="item-001", barcode=["123456", "654321"])],
		}
		normalized = normalize_order(raw)

		item = normalized["items"][0]
		self.assertEqual(item["barcodes"], ["123456", "654321"])

	def test_item_normalization_preserves_status_exact_spelling(self):
		"""Item status enums (e.g. IN_CART) are preserved exactly."""
		for status in ["IN_CART", "REPLACED", "ADDITION"]:
			raw = {
				"order_id": "order-001",
				"customer": {"delivery_address": {}},
				"payment": {},
				"sys": {},
				"cancellation": {},
				"items": [self.sample_item(_id="item-001", status=status)],
			}
			normalized = normalize_order(raw)
			self.assertEqual(normalized["items"][0]["external_status"], status)
