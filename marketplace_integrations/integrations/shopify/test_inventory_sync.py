# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Every test here mocks ShopifyClient.execute() with a small in-memory fake
store (FakeShopify) - never a real Shopify call - and mocks the Location
use-time re-check separately. Fixtures use unique suffixes because the worker
commits per chunk (class-scoped rollback does not cover committed rows)."""

import random
import uuid
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from marketplace_integrations.integrations.shopify import inventory_sync
from marketplace_integrations.integrations.shopify.constants import (
	SHOPIFY_INVENTORY_MAX_STALE_RETRIES,
	SHOPIFY_INVENTORY_SWEEP_OVERLAP_MINUTES,
	SHOPIFY_WEBHOOK_TOPIC_HEADER_TO_ENUM,
	SHOPIFY_WEBHOOK_TOPICS,
)
from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.tests.utils import make_role_user

EXECUTE_TARGET = "marketplace_integrations.integrations.shopify.client.ShopifyClient.execute"
FETCH_LOCATIONS_TARGET = (
	"marketplace_integrations.integrations.shopify.inventory_sync.fetch_shop_and_active_locations"
)
LOCATION = "gid://shopify/Location/1"


class FakeShopify:
	"""In-memory Shopify inventory: dispatches on the query text and enforces
	compare-and-set the way the real API is expected to (a mismatching
	changeFromQuantity returns CHANGE_FROM_QUANTITY_STALE and applies nothing).

	CAUTION - circular validation: by default this fake bakes in the SAME
	assumptions the code under test makes (userErrors apply nothing, a freshly
	activated level starts at on_hand 0, the stale code is CHANGE_FROM_QUANTITY_STALE, and
	reads always carry an on_hand entry). The switches below (partial_apply,
	activate_starts_at, stale_code, error injection) make it behave differently
	so the tests are not only agreeing with the code's own guesses."""

	def __init__(self):
		self.variants = {}  # variant gid -> state dict
		self.calls = []  # (kind, variables)
		self.before_set = []  # callables run before the next set is evaluated
		self.bad_item_ids = set()
		self.fail_next_reads = 0
		self.partial_apply = False  # on a userError, still apply the OK items
		self.activate_starts_at = 0  # on_hand of a freshly activated level
		self.stale_code = "CHANGE_FROM_QUANTITY_STALE"
		self.extra_errors = []  # userErrors appended to the next stale response
		self.track_errors = set()  # item ids whose inventoryItemUpdate returns userErrors
		self.track_raises = set()  # item ids whose inventoryItemUpdate raises
		self.activate_errors = set()  # item ids whose inventoryActivate returns userErrors

	def add(self, variant_gid, tracked=True, has_level=True, on_hand=0):
		self.variants[variant_gid] = {
			"item_id": variant_gid.replace("ProductVariant", "InventoryItem"),
			"tracked": tracked,
			"has_level": has_level,
			"on_hand": on_hand,
		}

	def kinds(self):
		return [kind for kind, _variables in self.calls]

	def calls_of(self, kind):
		return [variables for call_kind, variables in self.calls if call_kind == kind]

	def by_item_id(self, item_id):
		for state in self.variants.values():
			if state["item_id"] == item_id:
				return state
		raise KeyError(item_id)

	def __call__(self, query, variables=None):
		variables = variables or {}
		if "inventorySetQuantities" in query:
			self.calls.append(("set", variables))
			assert "@idempotent(key: $idempotencyKey)" in query
			for hook in self.before_set[:1]:
				self.before_set.pop(0)
				hook()
			quantities = variables["input"]["quantities"]
			if any(q["inventoryItemId"] in self.bad_item_ids for q in quantities):
				if self.partial_apply:
					for q in quantities:
						state = self.by_item_id(q["inventoryItemId"])
						if (
							q["inventoryItemId"] not in self.bad_item_ids
							and state["on_hand"] == q["changeFromQuantity"]
						):
							state["on_hand"] = q["quantity"]
				return {
					"inventorySetQuantities": {
						"userErrors": [{"code": "INVALID_QUANTITY", "field": ["x"], "message": "Bad item."}]
					}
				}
			for q in quantities:
				if self.by_item_id(q["inventoryItemId"])["on_hand"] != q["changeFromQuantity"]:
					return {
						"inventorySetQuantities": {
							"userErrors": [
								{"code": self.stale_code, "field": None, "message": "stale"},
								*self.extra_errors,
							]
						}
					}
			for q in quantities:
				self.by_item_id(q["inventoryItemId"])["on_hand"] = q["quantity"]
			return {"inventorySetQuantities": {"inventoryAdjustmentGroup": {"id": "g"}, "userErrors": []}}
		if "inventoryActivate" in query:
			self.calls.append(("activate", variables))
			assert "@idempotent(key: $idempotencyKey)" in query
			if variables["inventoryItemId"] in self.activate_errors:
				return {
					"inventoryActivate": {
						"inventoryLevel": None,
						"userErrors": [{"field": ["x"], "message": "Cannot activate."}],
					}
				}
			state = self.by_item_id(variables["inventoryItemId"])
			state["has_level"] = True
			state["on_hand"] = self.activate_starts_at
			return {"inventoryActivate": {"inventoryLevel": {"id": "l"}, "userErrors": []}}
		if "inventoryItemUpdate" in query:
			self.calls.append(("track", variables))
			if variables["id"] in self.track_raises:
				frappe.throw("track boom")
			if variables["id"] in self.track_errors:
				return {
					"inventoryItemUpdate": {
						"inventoryItem": None,
						"userErrors": [{"field": ["x"], "message": "Cannot track."}],
					}
				}
			self.by_item_id(variables["id"])["tracked"] = True
			return {"inventoryItemUpdate": {"inventoryItem": {"id": variables["id"]}, "userErrors": []}}
		if "nodes(ids" in query:
			self.calls.append(("read", variables))
			if self.fail_next_reads:
				self.fail_next_reads -= 1
				frappe.throw("boom")
			nodes = []
			for gid in variables["ids"]:
				state = self.variants.get(gid)
				if state is None:
					nodes.append(None)
					continue
				level = None
				if state["has_level"]:
					quantities = []
					if state["on_hand"] is not None:
						quantities = [{"name": "on_hand", "quantity": state["on_hand"]}]
					level = {"quantities": quantities}
				nodes.append(
					{
						"id": gid,
						"inventoryItem": {
							"id": state["item_id"],
							"tracked": state["tracked"],
							"inventoryLevel": level,
						},
					}
				)
			return {"nodes": nodes}
		raise AssertionError(f"Unexpected query in test: {query}")


def _variant_gid():
	return f"gid://shopify/ProductVariant/{random.randint(10**8, 10**9)}"


class InventoryFixtureMixin:
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.warehouse = frappe.get_all(
			"Warehouse", filters={"disabled": 0, "is_group": 0}, pluck="name", limit=1
		)[0]
		self._created_accounts = []
		self.account = self._make_account("Main")
		self.shopify = FakeShopify()

	def tearDown(self):
		for account_name in self._created_accounts:
			frappe.cache().delete_value(inventory_sync._watermark_cache_key(account_name))
			connection = frappe.db.get_value("Marketplace Account", account_name, "connection")
			frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": account_name})
			if frappe.db.exists("Marketplace Account", account_name):
				frappe.delete_doc("Marketplace Account", account_name, ignore_permissions=True, force=True)
			if connection and frappe.db.exists("Shopify Connection", connection):
				frappe.delete_doc("Shopify Connection", connection, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDown()

	def _make_account(self, marker):
		account = make_shopify_account(f"Inventory {marker} Account {self.suffix}")
		account.db_set("warehouse", self.warehouse, update_modified=False)
		account.db_set("shopify_location_gid", LOCATION, update_modified=False)
		account.reload()
		self._created_accounts.append(account.name)
		return account

	def _make_item(self, marker, qty=None, disabled=0, is_stock_item=1, has_variants=0):
		# Own helper: test_products.make_item() creates non-stock Items, which the
		# sync (correctly) skips.
		item_code = f"Inventory {marker} Item {self.suffix}"
		if not frappe.db.exists("Item", item_code):
			doc = {
				"doctype": "Item",
				"item_code": item_code,
				"item_name": item_code,
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": is_stock_item,
				"has_variants": has_variants,
			}
			if has_variants:
				# A template Item requires at least one attribute row.
				doc["attributes"] = [{"attribute": "Size"}]
			frappe.get_doc(doc).insert()
		if disabled:
			frappe.db.set_value("Item", item_code, "disabled", 1)
		if qty is not None:
			bin_name = frappe.db.get_value(
				"Bin", {"item_code": item_code, "warehouse": self.warehouse}, "name"
			)
			if bin_name:
				frappe.db.set_value("Bin", bin_name, "actual_qty", qty)
			else:
				frappe.get_doc(
					{"doctype": "Bin", "item_code": item_code, "warehouse": self.warehouse, "actual_qty": qty}
				).insert()
		return item_code

	def _make_mapping(self, item, variant_gid=None, account=None, **fields):
		variant_gid = variant_gid or _variant_gid()
		doc = {
			"doctype": "Marketplace Product Mapping",
			"marketplace_account": (account or self.account).name,
			"item": item,
			"external_reference": variant_gid,
			"external_variant_id": variant_gid,
			"external_product_id": "gid://shopify/Product/1",
		}
		doc.update(fields)
		return frappe.get_doc(doc).insert()

	def _sync(self, mapping_names=None, **kwargs):
		locations = {"shop_id": "s", "active_locations": [{"id": LOCATION, "name": "Main"}]}
		with (
			patch(EXECUTE_TARGET, side_effect=self.shopify),
			patch(FETCH_LOCATIONS_TARGET, return_value=locations),
		):
			return inventory_sync.sync_account_inventory(self.account.name, mapping_names, **kwargs)

	def _one(self, qty, shopify_on_hand=0, is_stock_item=1, has_variants=0, **shopify_state):
		shopify_state.setdefault("on_hand", shopify_on_hand)
		item = self._make_item(
			f"One{random.randint(0, 10**6)}", qty=qty, is_stock_item=is_stock_item, has_variants=has_variants
		)
		mapping = self._make_mapping(item)
		self.shopify.add(mapping.external_reference, **shopify_state)
		return mapping


class IntegrationTestQuantityMapping(InventoryFixtureMixin, IntegrationTestCase):
	def test_negative_stock_sends_zero(self):
		mapping = self._one(-5, shopify_on_hand=4)

		self._sync([mapping.name])

		self.assertEqual(self.shopify.variants[mapping.external_reference]["on_hand"], 0)
		self.assertEqual(self.shopify.calls_of("set")[0]["input"]["quantities"][0]["quantity"], 0)

	def test_fractional_stock_is_floored_to_an_int(self):
		mapping = self._one(7.9)

		self._sync([mapping.name])

		sent = self.shopify.calls_of("set")[0]["input"]["quantities"][0]["quantity"]
		self.assertEqual(sent, 7)
		self.assertIsInstance(sent, int)

	def test_sends_on_hand_with_location_reason_reference_and_change_from_quantity(self):
		mapping = self._one(10, shopify_on_hand=3)

		self._sync([mapping.name])

		set_input = self.shopify.calls_of("set")[0]["input"]
		self.assertEqual(set_input["name"], "on_hand")
		self.assertTrue(set_input["reason"])
		self.assertTrue(set_input["referenceDocumentUri"])
		self.assertNotIn("ignoreCompareQuantity", set_input)
		quantity = set_input["quantities"][0]
		self.assertEqual(quantity["locationId"], LOCATION)
		self.assertEqual(quantity["changeFromQuantity"], 3)
		self.assertNotIn("compareQuantity", quantity)

	def test_stock_uom_only_sales_uom_is_ignored(self):
		other_uom = frappe.get_all("UOM", filters={"name": ["!=", "Nos"]}, pluck="name", limit=1)[0]
		item = self._make_item("Uom", qty=24)
		mapping = self._make_mapping(item, sales_uom=other_uom)
		self.shopify.add(mapping.external_reference)

		self._sync([mapping.name])

		self.assertEqual(self.shopify.calls_of("set")[0]["input"]["quantities"][0]["quantity"], 24)

	def test_success_writes_only_the_inventory_status_fields(self):
		mapping = self._one(5)

		self._sync([mapping.name])

		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Synced")
		self.assertTrue(mapping.inventory_last_synced_on)
		self.assertFalse(mapping.inventory_last_error)
		# The shared price-push status fields are untouched (decision 4).
		self.assertEqual(mapping.sync_status, "Not Synced")
		self.assertFalse(mapping.last_synced_on)
		self.assertFalse(mapping.last_error)

	def test_error_does_not_touch_the_shared_price_push_status(self):
		mapping = self._one(5)
		self.shopify.bad_item_ids.add(self.shopify.variants[mapping.external_reference]["item_id"])
		mapping.db_set("sync_status", "Synced", update_modified=False)

		self._sync([mapping.name])

		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Error")
		self.assertEqual(mapping.inventory_last_error, "Bad item.")
		self.assertEqual(mapping.sync_status, "Synced")


class IntegrationTestSkipIfEqual(InventoryFixtureMixin, IntegrationTestCase):
	def test_equal_quantity_sends_no_mutation_and_counts_as_synced(self):
		mapping = self._one(5, shopify_on_hand=5)

		payload = self._sync([mapping.name])

		self.assertEqual(self.shopify.kinds(), ["read"])
		self.assertEqual(payload["succeeded"], 1)
		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Synced")

	def test_stock_ledger_entry_count_is_unchanged_across_a_full_reconciliation(self):
		for qty, on_hand in ((5, 1), (0, 3), (8, 8), (-2, 4)):
			self._one(qty, shopify_on_hand=on_hand)
		before = frappe.db.count("Stock Ledger Entry")

		payload = self._sync(None)

		self.assertEqual(payload["total"], 4)

		self.assertEqual(frappe.db.count("Stock Ledger Entry"), before)

	def test_no_inventory_webhook_topic_is_registered(self):
		for header, topic in SHOPIFY_WEBHOOK_TOPIC_HEADER_TO_ENUM.items():
			self.assertFalse(topic.startswith("INVENTORY_"), topic)
			self.assertFalse(header.startswith("inventory_levels/"), header)
			self.assertFalse(header.startswith("inventory_items/"), header)
		self.assertFalse(any(topic.startswith("INVENTORY_") for topic in SHOPIFY_WEBHOOK_TOPICS))


class IntegrationTestStaleCompare(InventoryFixtureMixin, IntegrationTestCase):
	def test_stale_compare_rereads_and_retries_with_the_new_value(self):
		mapping = self._one(9, shopify_on_hand=2)
		state = self.shopify.variants[mapping.external_reference]
		self.shopify.before_set.append(lambda: state.update(on_hand=4))

		payload = self._sync([mapping.name])

		sets = self.shopify.calls_of("set")
		self.assertEqual(len(sets), 2)
		self.assertEqual(sets[0]["input"]["quantities"][0]["changeFromQuantity"], 2)
		self.assertEqual(sets[1]["input"]["quantities"][0]["changeFromQuantity"], 4)
		self.assertEqual(self.shopify.kinds().count("read"), 2)
		self.assertEqual(state["on_hand"], 9)
		self.assertEqual(payload["succeeded"], 1)

	def test_real_shopify_stale_code_triggers_one_reread_and_resend(self):
		# The literal code from shopify.dev InventorySetQuantitiesUserErrorCode (2026-07),
		# not the fake's default, so a renamed constant can't pass by agreeing with itself.
		self.shopify.stale_code = "CHANGE_FROM_QUANTITY_STALE"
		mapping = self._one(9, shopify_on_hand=2)
		state = self.shopify.variants[mapping.external_reference]
		self.shopify.before_set.append(lambda: state.update(on_hand=4))

		payload = self._sync([mapping.name])

		self.assertEqual(len(self.shopify.calls_of("set")), 2)
		self.assertEqual(self.shopify.kinds().count("read"), 2)
		self.assertEqual(state["on_hand"], 9)
		self.assertEqual(payload["succeeded"], 1)

	def test_unknown_legacy_code_does_not_trigger_the_stale_retry(self):
		self.shopify.stale_code = "COMPARE_FAILED"  # not a real Shopify code
		mapping = self._one(9, shopify_on_hand=2)
		state = self.shopify.variants[mapping.external_reference]
		self.shopify.before_set.append(lambda: state.update(on_hand=4))

		self._sync([mapping.name])

		self.assertEqual(self.shopify.kinds().count("read"), 1)

	def test_stale_compare_gives_up_after_bounded_retries_and_marks_error(self):
		mapping = self._one(9, shopify_on_hand=2)
		state = self.shopify.variants[mapping.external_reference]
		self.shopify.before_set.extend(
			[
				lambda n=n: state.update(on_hand=100 + n)
				for n in range(SHOPIFY_INVENTORY_MAX_STALE_RETRIES + 5)
			]
		)

		payload = self._sync([mapping.name])

		self.assertEqual(len(self.shopify.calls_of("set")), 1 + SHOPIFY_INVENTORY_MAX_STALE_RETRIES)
		self.assertEqual(payload["failed"], 1)
		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Error")
		self.assertIn("kept changing", mapping.inventory_last_error)

	def test_whole_chunk_is_retried_on_stale_compare(self):
		first = self._one(9, shopify_on_hand=2)
		second = self._one(8, shopify_on_hand=1)
		state = self.shopify.variants[first.external_reference]
		self.shopify.before_set.append(lambda: state.update(on_hand=4))

		self._sync([first.name, second.name])

		sets = self.shopify.calls_of("set")
		self.assertEqual(len(sets), 2)
		self.assertEqual(len(sets[0]["input"]["quantities"]), 2)
		self.assertEqual(len(sets[1]["input"]["quantities"]), 2)

	def test_non_stale_user_error_falls_back_to_per_item_and_isolates_the_bad_item(self):
		good = self._one(6, shopify_on_hand=1)
		bad = self._one(7, shopify_on_hand=1)
		self.shopify.bad_item_ids.add(self.shopify.variants[bad.external_reference]["item_id"])

		payload = self._sync([good.name, bad.name])

		sets = self.shopify.calls_of("set")
		self.assertEqual([len(call["input"]["quantities"]) for call in sets], [2, 1, 1])
		self.assertEqual(self.shopify.variants[good.external_reference]["on_hand"], 6)
		self.assertEqual(self.shopify.variants[bad.external_reference]["on_hand"], 1)
		good.reload()
		bad.reload()
		self.assertEqual(good.inventory_sync_status, "Synced")
		self.assertEqual(bad.inventory_sync_status, "Error")
		self.assertEqual(bad.inventory_last_error, "Bad item.")
		self.assertEqual((payload["succeeded"], payload["failed"]), (1, 1))

	def test_one_failing_chunk_does_not_abort_the_run(self):
		with patch.object(inventory_sync, "SHOPIFY_INVENTORY_SYNC_CHUNK_SIZE", 1):
			first = self._one(3)
			second = self._one(4)
			self.shopify.fail_next_reads = 1

			payload = self._sync([first.name, second.name])

		self.assertEqual((payload["succeeded"], payload["failed"]), (1, 1))
		first.reload()
		second.reload()
		self.assertEqual(first.inventory_sync_status, "Error")
		self.assertEqual(second.inventory_sync_status, "Synced")


class IntegrationTestStaleAndErrorMix(InventoryFixtureMixin, IntegrationTestCase):
	def test_stale_error_alongside_another_user_error_retries_the_whole_chunk(self):
		first = self._one(9, shopify_on_hand=2)
		second = self._one(8, shopify_on_hand=1)
		state = self.shopify.variants[first.external_reference]
		self.shopify.before_set.append(lambda: state.update(on_hand=4))
		self.shopify.extra_errors = [{"code": "INVALID_QUANTITY", "field": ["x"], "message": "Other."}]

		payload = self._sync([first.name, second.name])

		sets = self.shopify.calls_of("set")
		# Stale wins: whole-chunk retry (2 items again), never the per-item fallback.
		self.assertEqual([len(call["input"]["quantities"]) for call in sets], [2, 2])
		self.assertEqual(payload["succeeded"], 2)

	def test_unrecognised_stale_code_falls_back_per_item_and_still_converges(self):
		first = self._one(9, shopify_on_hand=2)
		second = self._one(8, shopify_on_hand=1)
		self.shopify.stale_code = "SOMETHING_ELSE"
		state = self.shopify.variants[first.external_reference]
		self.shopify.before_set.append(lambda: state.update(on_hand=4))

		payload = self._sync([first.name, second.name])

		self.assertEqual(payload["succeeded"], 2)
		self.assertEqual(state["on_hand"], 9)
		self.assertEqual(self.shopify.variants[second.external_reference]["on_hand"], 8)

	def test_partly_applied_request_then_per_item_fallback_converges(self):
		# Unlike the default fake, a userError here still APPLIES the good item.
		self.shopify.partial_apply = True
		good = self._one(6, shopify_on_hand=1)
		bad = self._one(7, shopify_on_hand=1)
		self.shopify.bad_item_ids.add(self.shopify.variants[bad.external_reference]["item_id"])

		payload = self._sync([good.name, bad.name])

		self.assertEqual(self.shopify.variants[good.external_reference]["on_hand"], 6)
		self.assertEqual(self.shopify.variants[bad.external_reference]["on_hand"], 1)
		# The good item was already at target on the fallback's re-read: no re-send.
		self.assertEqual([len(call["input"]["quantities"]) for call in self.shopify.calls_of("set")], [2, 1])
		good.reload()
		bad.reload()
		self.assertEqual(good.inventory_sync_status, "Synced")
		self.assertEqual(bad.inventory_sync_status, "Error")
		self.assertEqual((payload["succeeded"], payload["failed"]), (1, 1))


class IntegrationTestAlreadyChangedErrors(InventoryFixtureMixin, IntegrationTestCase):
	TRACKING_CLAUSE = "tracking was already enabled"
	ACTIVATION_CLAUSE = "already activated"

	def _item_id(self, mapping):
		return self.shopify.variants[mapping.external_reference]["item_id"]

	def test_activate_user_error_after_tracking_says_tracking_was_changed_and_sets_nothing(self):
		mapping = self._one(6, tracked=False, has_level=False, on_hand=None)
		self.shopify.activate_errors.add(self._item_id(mapping))

		payload = self._sync([mapping.name])

		self.assertEqual(self.shopify.kinds(), ["read", "track", "activate"])
		self.assertEqual(payload["failed"], 1)
		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Error")
		self.assertIn("Cannot activate.", mapping.inventory_last_error)
		self.assertIn(self.TRACKING_CLAUSE, mapping.inventory_last_error)
		self.assertNotIn(self.ACTIVATION_CLAUSE, mapping.inventory_last_error)
		self.assertIn(self.TRACKING_CLAUSE, payload["results"][0]["error"])
		self.assertLessEqual(len(mapping.inventory_last_error), inventory_sync.ERROR_MESSAGE_MAX_LENGTH)

	def test_track_user_error_sets_nothing_and_has_no_already_changed_clause(self):
		mapping = self._one(6, tracked=False, has_level=False, on_hand=None)
		self.shopify.track_errors.add(self._item_id(mapping))

		self._sync([mapping.name])

		self.assertEqual(self.shopify.kinds(), ["read", "track"])
		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Error")
		self.assertIn("Cannot track.", mapping.inventory_last_error)
		self.assertNotIn("already", mapping.inventory_last_error)

	def test_track_exception_sets_nothing_and_reports_a_generic_error(self):
		mapping = self._one(6, tracked=False, has_level=False, on_hand=None)
		self.shopify.track_raises.add(self._item_id(mapping))

		self._sync([mapping.name])

		self.assertNotIn("set", self.shopify.kinds())
		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Error")
		self.assertNotIn("track boom", mapping.inventory_last_error)

	def test_activation_then_compare_failed_recovers_via_reread(self):
		# Shopify's new level starts at 5 (not the assumed 0): the first set is
		# CHANGE_FROM_QUANTITY_STALE, the re-read supplies the real value and the retry wins.
		self.shopify.activate_starts_at = 5
		mapping = self._one(6, has_level=False, on_hand=None)

		payload = self._sync([mapping.name])

		self.assertEqual(self.shopify.kinds(), ["read", "activate", "set", "read", "set"])
		compares = [c["input"]["quantities"][0]["changeFromQuantity"] for c in self.shopify.calls_of("set")]
		self.assertEqual(compares, [0, 5])
		self.assertEqual(self.shopify.variants[mapping.external_reference]["on_hand"], 6)
		self.assertEqual(payload["succeeded"], 1)

	def test_stale_exhaustion_after_activation_and_tracking_mentions_both_changes(self):
		mapping = self._one(9, tracked=False, has_level=False, on_hand=None)
		state = self.shopify.variants[mapping.external_reference]
		# Always stale, so retries are exhausted.
		self.shopify.before_set.extend(
			[
				lambda n=n: state.update(on_hand=100 + n)
				for n in range(SHOPIFY_INVENTORY_MAX_STALE_RETRIES + 5)
			]
		)

		self._sync([mapping.name])

		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Error")
		self.assertIn("kept changing", mapping.inventory_last_error)
		self.assertIn(self.TRACKING_CLAUSE, mapping.inventory_last_error)
		self.assertIn(self.ACTIVATION_CLAUSE, mapping.inventory_last_error)
		self.assertLessEqual(len(mapping.inventory_last_error), inventory_sync.ERROR_MESSAGE_MAX_LENGTH)

	def test_set_failure_after_tracking_says_tracking_was_changed(self):
		mapping = self._one(6, shopify_on_hand=1, tracked=False)
		self.shopify.bad_item_ids.add(self._item_id(mapping))

		self._sync([mapping.name])

		mapping.reload()
		self.assertIn("Bad item.", mapping.inventory_last_error)
		self.assertIn(self.TRACKING_CLAUSE, mapping.inventory_last_error)

	def test_long_message_is_truncated_so_the_clause_survives(self):
		note = inventory_sync._with_applied_note("x" * 2000, [inventory_sync.APPLIED_TRACKING])

		self.assertLessEqual(len(note), inventory_sync.ERROR_MESSAGE_MAX_LENGTH)
		self.assertTrue(note.endswith("before this error."))


class IntegrationTestStockingEdgeCases(InventoryFixtureMixin, IntegrationTestCase):
	def test_variant_not_stocked_at_location_is_activated_then_set(self):
		mapping = self._one(6, has_level=False, on_hand=None)

		self._sync([mapping.name])

		self.assertEqual(self.shopify.kinds(), ["read", "activate", "set"])
		activate = self.shopify.calls_of("activate")[0]
		self.assertEqual(activate["locationId"], LOCATION)
		self.assertEqual(self.shopify.calls_of("set")[0]["input"]["quantities"][0]["changeFromQuantity"], 0)
		self.assertEqual(self.shopify.variants[mapping.external_reference]["on_hand"], 6)

	def test_untracked_item_is_tracked_then_activated_if_needed_then_set(self):
		mapping = self._one(6, tracked=False, has_level=False, on_hand=None)

		self._sync([mapping.name])

		self.assertEqual(self.shopify.kinds(), ["read", "track", "activate", "set"])
		track = self.shopify.calls_of("track")[0]
		self.assertEqual(track["input"], {"tracked": True})
		self.assertTrue(self.shopify.variants[mapping.external_reference]["tracked"])

	def test_untracked_item_with_a_level_is_tracked_then_set_without_activate(self):
		mapping = self._one(6, shopify_on_hand=6, tracked=False)

		self._sync([mapping.name])

		# Never skipped as "equal" right after flipping tracking - the compare
		# value read before the flip can't be trusted.
		self.assertEqual(self.shopify.kinds(), ["read", "track", "set"])

	def test_tracked_stocked_variant_uses_neither_prep_mutation(self):
		mapping = self._one(6)

		self._sync([mapping.name])

		self.assertEqual(self.shopify.kinds(), ["read", "set"])

	def test_unreadable_current_quantity_is_an_error_not_a_blind_write(self):
		mapping = self._one(6, on_hand=None)

		self._sync([mapping.name])

		self.assertEqual(self.shopify.kinds(), ["read"])
		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Error")

	def test_variant_missing_in_shopify_is_an_error(self):
		item = self._make_item("Gone", qty=1)
		mapping = self._make_mapping(item)

		self._sync([mapping.name])

		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Error")
		self.assertIn("not found", mapping.inventory_last_error)

	def test_malformed_variant_gid_is_a_local_error_with_no_network_call(self):
		item = self._make_item("BadGid", qty=1)
		mapping = self._make_mapping(item, variant_gid="not-a-gid")

		self._sync([mapping.name])

		self.assertEqual(self.shopify.calls, [])
		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Error")


class IntegrationTestNonStockItems(InventoryFixtureMixin, IntegrationTestCase):
	def test_non_stock_item_is_skipped_with_no_shopify_change(self):
		mapping = self._one(None, is_stock_item=0, tracked=False, has_level=False, on_hand=None)

		payload = self._sync([mapping.name])

		# No read either: nothing was sent, so no mutation and no store change.
		self.assertEqual(self.shopify.calls, [])
		self.assertEqual((payload["skipped_mappings"], payload["failed"], payload["succeeded"]), (1, 0, 0))
		self.assertTrue(payload["results"][0]["skipped"])
		self.assertIn("not a stock Item", payload["results"][0]["error"])
		state = self.shopify.variants[mapping.external_reference]
		self.assertFalse(state["tracked"])
		self.assertFalse(state["has_level"])
		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Not Synced")

	def test_full_reconciliation_skips_non_stock_and_still_syncs_stock_items(self):
		stock = self._one(5, shopify_on_hand=1)
		non_stock = self._one(None, is_stock_item=0, tracked=False, has_level=False, on_hand=None)

		payload = self._sync(None)

		self.assertEqual((payload["succeeded"], payload["skipped_mappings"], payload["failed"]), (1, 1, 0))
		self.assertEqual(self.shopify.calls_of("read")[0]["ids"], [stock.external_reference])
		self.assertEqual(self.shopify.kinds().count("track"), 0)
		self.assertEqual(self.shopify.variants[stock.external_reference]["on_hand"], 5)
		self.assertFalse(self.shopify.variants[non_stock.external_reference]["tracked"])

	def test_stock_item_still_syncs(self):
		mapping = self._one(4, shopify_on_hand=0)

		payload = self._sync([mapping.name])

		self.assertEqual(payload["succeeded"], 1)
		self.assertEqual(self.shopify.variants[mapping.external_reference]["on_hand"], 4)


class IntegrationTestBundleItems(InventoryFixtureMixin, IntegrationTestCase):
	# The inventory sync commits, so the Items, Bins and Product Bundles these tests create would
	# survive the class rollback: every one is removed by a cleanup (which runs after tearDown has
	# deleted the mappings) and committed.
	def _delete_item_records(self, item_code):
		try:
			for bundle in frappe.get_all("Product Bundle", filters={"new_item_code": item_code}, pluck="name"):
				frappe.delete_doc("Product Bundle", bundle, ignore_permissions=True, force=True)
			frappe.db.delete("Bin", {"item_code": item_code})
			if frappe.db.exists("Item", item_code):
				frappe.delete_doc("Item", item_code, ignore_permissions=True, force=True)
			frappe.db.commit()
		except Exception:
			frappe.log_error(title="shopify bundle test cleanup failed", message=frappe.get_traceback())

	def _make_item(self, marker, *args, **kwargs):
		item_code = super()._make_item(marker, *args, **kwargs)
		self.addCleanup(self._delete_item_records, item_code)
		return item_code

	def _bundle_mapping(self, components, **shopify_state):
		"""Non-stock parent Item with a Product Bundle, mapped to a variant."""
		parent = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"Inventory Bundle Parent {random.randint(0, 10**6)} {self.suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		self.addCleanup(self._delete_item_records, parent.name)
		if components is not None:
			# ERPNext rejects a bundle with no rows at insert: build an empty (invalid) bundle with
			# one placeholder component and empty it with a direct DB write.
			rows = components or [(self._make_item(f"BundlePlaceholder{random.randint(0, 10**6)}", qty=1), 1)]
			bundle = frappe.get_doc(
				{
					"doctype": "Product Bundle",
					"new_item_code": parent.name,
					"items": [{"item_code": code, "qty": qty} for code, qty in rows],
				}
			).insert(ignore_permissions=True)
			if not components:
				frappe.db.delete("Product Bundle Item", {"parent": bundle.name})
		mapping = self._make_mapping(parent.name)
		shopify_state.setdefault("on_hand", 0)
		self.shopify.add(mapping.external_reference, **shopify_state)
		return mapping

	def test_bundle_parent_uses_the_bundle_quantity_instead_of_being_skipped(self):
		part = self._make_item(f"BundlePart{random.randint(0, 10**6)}", qty=11)
		mapping = self._bundle_mapping([(part, 2)], on_hand=1)

		payload = self._sync([mapping.name])

		self.assertEqual((payload["succeeded"], payload["skipped_mappings"], payload["failed"]), (1, 0, 0))
		self.assertEqual(self.shopify.variants[mapping.external_reference]["on_hand"], 5)

	def test_bundle_with_no_components_is_an_error_and_not_pushed_as_zero(self):
		mapping = self._bundle_mapping([], on_hand=3)

		payload = self._sync([mapping.name])

		self.assertEqual((payload["succeeded"], payload["failed"]), (0, 1))
		self.assertEqual(self.shopify.calls_of("set"), [])
		self.assertEqual(self.shopify.variants[mapping.external_reference]["on_hand"], 3)
		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Error")
		self.assertIn("no component", mapping.inventory_last_error)

	def test_non_stock_item_without_a_bundle_is_still_skipped(self):
		mapping = self._bundle_mapping(None, tracked=False, has_level=False, on_hand=None)

		payload = self._sync([mapping.name])

		self.assertEqual((payload["skipped_mappings"], payload["failed"]), (1, 0))
		self.assertEqual(self.shopify.calls, [])

	def test_sweep_enqueues_a_bundle_parent_when_a_component_bin_changes(self):
		part = self._make_item(f"SweepPart{random.randint(0, 10**6)}", qty=8)
		mapping = self._bundle_mapping([(part, 1)])
		unrelated = self._one(3)
		account = frappe.get_doc("Marketplace Account", self.account.name)
		with patch.object(
			inventory_sync, "_sweep_since", return_value=add_to_date(now_datetime(), minutes=-5)
		), patch.object(inventory_sync, "_advance_watermark"), patch.object(
			inventory_sync, "enqueue_account_sync", return_value=(True, "job")
		) as enqueue:
			inventory_sync._sweep_account(account, now_datetime())

		names = enqueue.call_args.args[1]
		self.assertIn(mapping.name, names)
		self.assertIn(unrelated.name, names)

	def test_sweep_ignores_a_component_whose_bin_did_not_change(self):
		part = self._make_item(f"SweepOld{random.randint(0, 10**6)}", qty=8)
		mapping = self._bundle_mapping([(part, 1)])
		account = frappe.get_doc("Marketplace Account", self.account.name)
		with patch.object(
			inventory_sync, "_sweep_since", return_value=add_to_date(now_datetime(), minutes=5)
		), patch.object(inventory_sync, "_advance_watermark"), patch.object(
			inventory_sync, "enqueue_account_sync", return_value=(True, "job")
		) as enqueue:
			inventory_sync._sweep_account(account, now_datetime())

		enqueue.assert_not_called()
		self.assertTrue(mapping.name)

	def test_sync_uses_the_shared_resolver(self):
		mapping = self._one(4)
		with patch.object(
			inventory_sync,
			"resolve_stock_quantities",
			return_value=({mapping.item: 9}, {}),
		) as resolver:
			self._sync([mapping.name])

		resolver.assert_called_once()
		self.assertEqual(self.shopify.variants[mapping.external_reference]["on_hand"], 9)


class IntegrationTestTemplateItems(InventoryFixtureMixin, IntegrationTestCase):
	def test_template_item_is_skipped_with_no_shopify_change(self):
		mapping = self._one(None, has_variants=1, tracked=False, has_level=False, on_hand=None)

		payload = self._sync([mapping.name])

		# No read either: nothing was sent, so no mutation and no store change.
		self.assertEqual(self.shopify.calls, [])
		self.assertEqual((payload["skipped_mappings"], payload["failed"], payload["succeeded"]), (1, 0, 0))
		self.assertTrue(payload["results"][0]["skipped"])
		self.assertIn("template", payload["results"][0]["error"])
		state = self.shopify.variants[mapping.external_reference]
		self.assertFalse(state["tracked"])
		self.assertFalse(state["has_level"])
		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Not Synced")

	def test_full_reconciliation_skips_template_and_still_syncs_stock_items(self):
		stock = self._one(5, shopify_on_hand=1)
		template = self._one(None, has_variants=1, tracked=False, has_level=False, on_hand=None)

		payload = self._sync(None)

		self.assertEqual((payload["succeeded"], payload["skipped_mappings"], payload["failed"]), (1, 1, 0))
		self.assertEqual(self.shopify.calls_of("read")[0]["ids"], [stock.external_reference])
		self.assertEqual(self.shopify.kinds().count("track"), 0)
		self.assertEqual(self.shopify.variants[stock.external_reference]["on_hand"], 5)
		self.assertFalse(self.shopify.variants[template.external_reference]["tracked"])


class IntegrationTestJobResilience(InventoryFixtureMixin, IntegrationTestCase):
	def test_mappings_missing_at_run_time_get_an_error_row_so_totals_reconcile(self):
		kept = self._one(5)
		gone = self._one(5)
		gone_name = gone.name
		other_account = self._make_account("Moved")
		moved = self._make_mapping(self._make_item("Moved", qty=1), account=other_account)
		frappe.delete_doc("Marketplace Product Mapping", gone_name, force=True)

		payload = self._sync([kept.name, gone_name, moved.name])

		self.assertEqual(payload["total"], 3)
		self.assertEqual((payload["succeeded"], payload["failed"]), (1, 2))
		by_name = {row["mapping_name"]: row for row in payload["results"]}
		for name in (gone_name, moved.name):
			self.assertIn("no longer exists or was moved", by_name[name]["error"])

	def test_error_log_entries_are_capped_per_job_with_one_summary(self):
		inventory_sync.frappe.flags.shopify_inventory_logged_failures = 0
		inventory_sync.frappe.flags.shopify_inventory_suppressed_failures = 0
		cap = inventory_sync.MAX_ERROR_LOG_ENTRIES_PER_JOB
		with patch.object(inventory_sync.frappe, "log_error") as mocked_log:
			for _n in range(cap + 5):
				inventory_sync._log_failure("read")
			self.assertEqual(mocked_log.call_count, cap)
			inventory_sync._log_suppressed_failures()

		self.assertEqual(mocked_log.call_count, cap + 1)
		self.assertIn("5 more failures suppressed", mocked_log.call_args.kwargs["message"])

	def test_status_writes_do_not_change_modified(self):
		mapping = self._one(5)
		before = frappe.db.get_value("Marketplace Product Mapping", mapping.name, "modified")

		self._sync([mapping.name])

		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Synced")
		self.assertEqual(mapping.modified, before)

	def test_whole_job_setup_failure_marks_every_selected_mapping_as_error(self):
		# validate_inventory_account() (or the Location re-check, or any other
		# whole-job setup step) throwing BEFORE per-mapping processing starts
		# must not leave the mappings' inventory_sync_status untouched/stale -
		# see this module's _record_whole_job_failure(). A ValidationError's
		# own text is user-facing (decision 3), so it survives verbatim.
		first = self._one(5)
		first.db_set("inventory_sync_status", "Synced", update_modified=False)
		second = self._one(6)
		second.db_set("inventory_sync_status", "Synced", update_modified=False)

		with patch.object(
			inventory_sync,
			"validate_inventory_account",
			side_effect=frappe.ValidationError("Location deactivated"),
		):
			with self.assertRaises(frappe.ValidationError):
				inventory_sync.sync_account_inventory(self.account.name, [first.name, second.name])

		for mapping in (first, second):
			mapping.reload()
			self.assertEqual(mapping.inventory_sync_status, "Error")
			self.assertIn("Location deactivated", mapping.inventory_last_error)
		self.assertEqual(self.shopify.calls, [])

	def test_whole_job_setup_failure_during_full_reconciliation_marks_every_enabled_mapping(self):
		enabled = self._one(5)
		disabled = self._one(5)
		disabled.db_set("enabled", 0, update_modified=False)

		with patch.object(
			inventory_sync, "validate_inventory_account", side_effect=frappe.ValidationError("boom")
		):
			with self.assertRaises(frappe.ValidationError):
				inventory_sync.sync_account_inventory(self.account.name, None)

		enabled.reload()
		disabled.reload()
		self.assertEqual(enabled.inventory_sync_status, "Error")
		self.assertIn("boom", enabled.inventory_last_error)
		# A disabled mapping is never touched by the full-account fallback either
		# - matching the ordinary reconciliation's own enabled-only scope.
		self.assertNotEqual(disabled.inventory_sync_status, "Error")

	def test_whole_job_setup_failure_uses_a_generic_message_for_non_validation_errors(self):
		# Anything other than frappe.ValidationError could carry a raw Shopify
		# response fragment, an internal DB error, etc. - never echoed on a
		# mapping row (decision 3). Full detail still reaches the Error Log via
		# the caller's own except block.
		mapping = self._one(5)

		with patch.object(
			inventory_sync, "validate_inventory_account", side_effect=RuntimeError("raw db error detail")
		):
			with self.assertRaises(RuntimeError):
				inventory_sync.sync_account_inventory(self.account.name, [mapping.name])

		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Error")
		self.assertNotIn("raw db error detail", mapping.inventory_last_error)
		self.assertIn("Error Log", mapping.inventory_last_error)

	def test_whole_job_setup_failure_excludes_skip_class_mappings_explicit_selection(self):
		# A skip-class mapping never gets a status write from a normal run
		# (disabled, disabled Item, non-stock Item, template Item) - the
		# whole-job fallback must leave it untouched too, or a stale Error
		# would persist forever since a later successful run silently skips it
		# with no write to clear it.
		stock = self._one(5)
		non_stock = self._one(None, is_stock_item=0, tracked=False, has_level=False, on_hand=None)
		template = self._one(None, has_variants=1, tracked=False, has_level=False, on_hand=None)
		disabled_item = self._make_item("WholeJobDisabledItem", qty=1, disabled=1)
		disabled_item_mapping = self._make_mapping(disabled_item)
		self.shopify.add(disabled_item_mapping.external_reference)

		with patch.object(
			inventory_sync,
			"validate_inventory_account",
			side_effect=frappe.ValidationError("Location deactivated"),
		):
			with self.assertRaises(frappe.ValidationError):
				inventory_sync.sync_account_inventory(
					self.account.name,
					[stock.name, non_stock.name, template.name, disabled_item_mapping.name],
				)

		stock.reload()
		self.assertEqual(stock.inventory_sync_status, "Error")
		for skipped in (non_stock, template, disabled_item_mapping):
			skipped.reload()
			self.assertNotEqual(skipped.inventory_sync_status, "Error")
		self.assertEqual(self.shopify.calls, [])

	def test_whole_job_setup_failure_full_reconciliation_excludes_skip_class_mappings(self):
		enabled_stock = self._one(5)
		non_stock = self._one(None, is_stock_item=0, tracked=False, has_level=False, on_hand=None)
		template = self._one(None, has_variants=1, tracked=False, has_level=False, on_hand=None)
		disabled_mapping = self._one(5)
		disabled_mapping.db_set("enabled", 0, update_modified=False)
		disabled_item = self._make_item("WholeJobReconcileDisabledItem", qty=1, disabled=1)
		disabled_item_mapping = self._make_mapping(disabled_item)

		with patch.object(
			inventory_sync, "validate_inventory_account", side_effect=frappe.ValidationError("boom")
		):
			with self.assertRaises(frappe.ValidationError):
				inventory_sync.sync_account_inventory(self.account.name, None)

		enabled_stock.reload()
		self.assertEqual(enabled_stock.inventory_sync_status, "Error")
		for skipped in (non_stock, template, disabled_mapping, disabled_item_mapping):
			skipped.reload()
			self.assertNotEqual(skipped.inventory_sync_status, "Error")

	def test_whole_job_setup_failure_does_not_touch_a_mapping_moved_to_another_account(self):
		# The explicit-selection path is scoped by account (same as
		# _mappings_for_sync()'s own "no longer exists or was moved" handling)
		# - a mapping since moved to a different account must not be marked
		# Error under this (wrong) account's job.
		mapping = self._one(5)
		other_account = self._make_account("MovedAway")
		other_item = self._make_item("MovedAwayItem", qty=1)
		moved_mapping = self._make_mapping(other_item, account=other_account)
		moved_mapping.db_set("inventory_sync_status", "Synced", update_modified=False)

		with patch.object(
			inventory_sync, "validate_inventory_account", side_effect=frappe.ValidationError("boom")
		):
			with self.assertRaises(frappe.ValidationError):
				inventory_sync.sync_account_inventory(self.account.name, [mapping.name, moved_mapping.name])

		mapping.reload()
		moved_mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Error")
		self.assertEqual(moved_mapping.inventory_sync_status, "Synced")

	def test_a_failure_while_recording_the_whole_job_failure_does_not_mask_the_original_exception(self):
		mapping = self._one(5)

		with (
			patch.object(
				inventory_sync, "validate_inventory_account", side_effect=RuntimeError("original failure")
			),
			patch.object(
				inventory_sync, "_record_whole_job_failure", side_effect=RuntimeError("secondary failure")
			),
			patch.object(inventory_sync.frappe, "log_error") as mocked_log_error,
		):
			with self.assertRaisesRegex(RuntimeError, "original failure"):
				inventory_sync.sync_account_inventory(self.account.name, [mapping.name])

		titles = [call.kwargs.get("title") for call in mocked_log_error.call_args_list]
		self.assertIn("Failed to record whole-job sync failure", titles)

	def test_a_failure_after_per_mapping_processing_started_does_not_overwrite_earlier_outcomes(self):
		# A later chunk's exception must not retroactively blanket-overwrite an
		# earlier chunk's already-persisted (correct) outcome - the whole-job
		# fallback only applies before mapping_processing_started flips True.
		with patch.object(inventory_sync, "SHOPIFY_INVENTORY_SYNC_CHUNK_SIZE", 1):
			first = self._one(5)
			second = self._one(6)
			real_sync_chunk = inventory_sync._sync_chunk

			def flaky_sync_chunk(client, account_doc, location_id, chunk, item_by_name):
				if chunk[0].name == second.name:
					raise RuntimeError("chunk boom")
				return real_sync_chunk(client, account_doc, location_id, chunk, item_by_name)

			with (
				patch(EXECUTE_TARGET, side_effect=self.shopify),
				patch(
					FETCH_LOCATIONS_TARGET,
					return_value={"shop_id": "s", "active_locations": [{"id": LOCATION, "name": "Main"}]},
				),
				patch.object(inventory_sync, "_sync_chunk", side_effect=flaky_sync_chunk),
			):
				with self.assertRaises(RuntimeError):
					inventory_sync.sync_account_inventory(self.account.name, [first.name, second.name])

		first.reload()
		self.assertEqual(first.inventory_sync_status, "Synced")


class IntegrationTestNoMutationWhenDisabled(InventoryFixtureMixin, IntegrationTestCase):
	def test_disabled_mapping_is_skipped_not_error_and_sends_nothing(self):
		mapping = self._one(5)
		mapping.db_set("enabled", 0, update_modified=False)

		payload = self._sync([mapping.name])

		self.assertEqual(self.shopify.calls, [])
		self.assertEqual((payload["skipped_mappings"], payload["failed"], payload["succeeded"]), (1, 0, 0))
		self.assertTrue(payload["results"][0]["skipped"])
		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Not Synced")

	def test_disabled_item_is_skipped_not_error_and_sends_nothing(self):
		item = self._make_item("Disabled", qty=5, disabled=1)
		mapping = self._make_mapping(item)
		self.shopify.add(mapping.external_reference)

		payload = self._sync([mapping.name])

		self.assertEqual(self.shopify.calls, [])
		self.assertEqual(payload["skipped_mappings"], 1)
		self.assertEqual(payload["failed"], 0)

	def test_disabled_account_sends_nothing(self):
		mapping = self._one(5)
		self.account.db_set("enabled", 0, update_modified=False)

		with self.assertRaisesRegex(frappe.ValidationError, "disabled"):
			self._sync([mapping.name])

		self.assertEqual(self.shopify.calls, [])

	def test_disabled_connection_sends_nothing(self):
		mapping = self._one(5)
		frappe.db.set_value("Shopify Connection", self.account.connection, "enabled", 0)

		with self.assertRaisesRegex(frappe.ValidationError, "disabled"):
			self._sync([mapping.name])

		self.assertEqual(self.shopify.calls, [])

	def test_reconciliation_only_touches_enabled_mappings(self):
		enabled = self._one(5)
		disabled = self._one(5)
		disabled.db_set("enabled", 0, update_modified=False)

		payload = self._sync(None)

		self.assertEqual(payload["total"], 1)
		self.assertEqual(self.shopify.calls_of("read")[0]["ids"], [enabled.external_reference])


class IntegrationTestLocationRecheck(InventoryFixtureMixin, IntegrationTestCase):
	def test_location_no_longer_active_fails_the_job_before_any_shopify_call(self):
		mapping = self._one(5)
		other_locations = {
			"shop_id": "s",
			"active_locations": [{"id": "gid://shopify/Location/2", "name": "x"}],
		}

		with (
			patch(EXECUTE_TARGET, side_effect=self.shopify),
			patch(FETCH_LOCATIONS_TARGET, return_value=other_locations),
			self.assertRaisesRegex(frappe.ValidationError, "no longer an active Location"),
		):
			inventory_sync.sync_account_inventory(self.account.name, [mapping.name])

		self.assertEqual(self.shopify.calls, [])

	def test_location_is_rechecked_once_per_job_not_per_chunk(self):
		with patch.object(inventory_sync, "SHOPIFY_INVENTORY_SYNC_CHUNK_SIZE", 1):
			first = self._one(5)
			second = self._one(6)
			locations = {"shop_id": "s", "active_locations": [{"id": LOCATION, "name": "Main"}]}
			with (
				patch(EXECUTE_TARGET, side_effect=self.shopify),
				patch(FETCH_LOCATIONS_TARGET, return_value=locations) as mocked_fetch,
			):
				inventory_sync.sync_account_inventory(self.account.name, [first.name, second.name])

		mocked_fetch.assert_called_once()

	def test_missing_location_or_warehouse_fails_local_validation(self):
		self.account.db_set("shopify_location_gid", None, update_modified=False)
		with self.assertRaisesRegex(frappe.ValidationError, "Shopify Location"):
			inventory_sync._validate_inventory_account_local(self.account.name)

		self.account.db_set("shopify_location_gid", LOCATION, update_modified=False)
		self.account.db_set("warehouse", None, update_modified=False)
		with self.assertRaisesRegex(frappe.ValidationError, "Warehouse"):
			inventory_sync._validate_inventory_account_local(self.account.name)


class IntegrationTestIdempotency(InventoryFixtureMixin, IntegrationTestCase):
	def _keys(self):
		keys = []
		for _kind, variables in self.shopify.calls:
			if "idempotencyKey" in variables:
				keys.append(variables["idempotencyKey"])
		return keys

	def test_every_inventory_mutation_carries_a_fresh_uuid_key(self):
		mapping = self._one(6, has_level=False, on_hand=None)

		self._sync([mapping.name])

		keys = self._keys()
		self.assertEqual(len(keys), 2)  # activate + set
		for key in keys:
			self.assertEqual(str(uuid.UUID(key)), key)
		self.assertEqual(len(set(keys)), 2)

	def test_stale_retry_uses_a_new_key_for_the_new_logical_mutation(self):
		mapping = self._one(9, shopify_on_hand=2)
		state = self.shopify.variants[mapping.external_reference]
		self.shopify.before_set.append(lambda: state.update(on_hand=4))

		self._sync([mapping.name])

		keys = [call["idempotencyKey"] for call in self.shopify.calls_of("set")]
		self.assertEqual(len(keys), 2)
		self.assertNotEqual(keys[0], keys[1])

	def test_same_change_repeated_later_gets_a_different_key(self):
		mapping = self._one(6, shopify_on_hand=1)
		self._sync([mapping.name])
		self.shopify.variants[mapping.external_reference]["on_hand"] = 1
		self._sync([mapping.name])

		keys = [call["idempotencyKey"] for call in self.shopify.calls_of("set")]
		self.assertEqual(len(keys), 2)
		self.assertNotEqual(keys[0], keys[1])


class IntegrationTestJobStatus(InventoryFixtureMixin, IntegrationTestCase):
	def test_status_is_owner_scoped_and_reports_results(self):
		mapping = self._one(5)
		job_id = frappe.generate_hash(length=16)
		inventory_sync._set_status(job_id, {"owner": "Administrator", "status": "queued", "processed": 0})

		self._sync([mapping.name], job_id_for_result=job_id, owner="Administrator")

		status = inventory_sync.get_sync_status(job_id)
		self.assertEqual(status["status"], "completed")
		self.assertEqual(status["result"]["succeeded"], 1)
		self.assertNotIn("owner", status)
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				inventory_sync.get_sync_status(job_id)
		finally:
			frappe.set_user("Administrator")

	def test_failed_job_records_failed_status(self):
		mapping = self._one(5)
		self.account.db_set("enabled", 0, update_modified=False)
		job_id = frappe.generate_hash(length=16)
		inventory_sync._set_status(job_id, {"owner": "Administrator", "status": "queued"})

		with self.assertRaises(frappe.ValidationError):
			self._sync([mapping.name], job_id_for_result=job_id, owner="Administrator")

		self.assertEqual(inventory_sync.get_sync_status(job_id)["status"], "failed")

	def test_results_returned_are_capped_but_counts_are_not(self):
		results = [
			{"mapping_name": f"M{i}", "ok": True}
			for i in range(inventory_sync.MAX_INVENTORY_RESULTS_RETURNED + 3)
		]

		payload = inventory_sync._result_payload(results)

		self.assertEqual(len(payload["results"]), inventory_sync.MAX_INVENTORY_RESULTS_RETURNED)
		self.assertEqual(payload["total"], len(results))
		self.assertEqual(payload["succeeded"], len(results))
		self.assertTrue(payload["truncated"])

	def test_progress_writes_are_throttled(self):
		with patch.object(inventory_sync, "SHOPIFY_INVENTORY_SYNC_CHUNK_SIZE", 1):
			mappings = [self._one(i) for i in range(3)]
			with patch.object(inventory_sync, "_report") as mocked_report:
				self._sync(
					[mapping.name for mapping in mappings], job_id_for_result="j", owner="Administrator"
				)

		statuses = [call.kwargs.get("status") for call in mocked_report.call_args_list]
		# initial running, after-local running, and the final completed - the 3
		# chunks are below INVENTORY_PROGRESS_UPDATE_INTERVAL so no tick between.
		self.assertEqual(statuses, ["running", "running", "completed"])


class IntegrationTestQueueInventorySync(InventoryFixtureMixin, IntegrationTestCase):
	def _queue(self, names, enqueue_return="enqueued"):
		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch(FETCH_LOCATIONS_TARGET) as mocked_fetch,
			patch("frappe.enqueue", return_value=enqueue_return) as mocked_enqueue,
		):
			result = inventory_sync.queue_inventory_sync(names)
		# The Desk request path must never call Shopify.
		mocked_execute.assert_not_called()
		mocked_fetch.assert_not_called()
		return result, mocked_enqueue

	def test_queues_one_job_per_account_with_no_shopify_call(self):
		mapping = self._one(5)

		result, mocked_enqueue = self._queue([mapping.name])

		self.assertEqual(len(result["jobs"]), 1)
		self.assertEqual(result["jobs"][0]["account"], self.account.name)
		self.assertEqual(result["jobs"][0]["mapping_count"], 1)
		self.assertEqual(result["skipped_accounts"], [])
		kwargs = mocked_enqueue.call_args.kwargs
		self.assertEqual(kwargs["account_name"], self.account.name)
		self.assertEqual(kwargs["mapping_names"], [mapping.name])
		self.assertEqual(kwargs["job_id"], f"marketplace-shopify-inventory-sync-{self.account.name}")
		self.assertTrue(kwargs["deduplicate"])
		# Owner-scoped "queued" entry seeded before enqueue.
		state = frappe.cache().get_value(inventory_sync._status_cache_key(kwargs["job_id_for_result"]))
		self.assertEqual(state["owner"], frappe.session.user)
		self.assertEqual(state["status"], "queued")

	def test_dedup_rejection_reports_the_reason_and_clears_the_seeded_entry(self):
		mapping = self._one(5)

		result, mocked_enqueue = self._queue([mapping.name], enqueue_return=None)

		self.assertEqual(result["jobs"], [])
		self.assertEqual(result["skipped_accounts"][0]["account"], self.account.name)
		self.assertIn("already running", result["skipped_accounts"][0]["reason"])
		job_id = mocked_enqueue.call_args.kwargs["job_id_for_result"]
		self.assertIsNone(frappe.cache().get_value(inventory_sync._status_cache_key(job_id)))

	def test_misconfigured_account_is_skipped_with_its_real_reason_while_a_valid_one_queues(self):
		bad_mapping = self._one(5)
		self.account.db_set("shopify_location_gid", None, update_modified=False)
		other_account = self._make_account("Other")
		other_item = self._make_item("OtherAcct", qty=1)
		other_mapping = self._make_mapping(other_item, account=other_account)

		result, mocked_enqueue = self._queue([bad_mapping.name, other_mapping.name])

		self.assertEqual([job["account"] for job in result["jobs"]], [other_account.name])
		self.assertEqual(len(result["skipped_accounts"]), 1)
		self.assertEqual(result["skipped_accounts"][0]["account"], self.account.name)
		self.assertIn("Shopify Location", result["skipped_accounts"][0]["reason"])
		mocked_enqueue.assert_called_once()

	def test_dedup_for_one_account_does_not_block_another(self):
		first = self._one(5)
		other_account = self._make_account("Other2")
		other_mapping = self._make_mapping(self._make_item("Other2", qty=1), account=other_account)

		def enqueue(*args, **kwargs):
			return None if kwargs["account_name"] == self.account.name else object()

		with patch("frappe.enqueue", side_effect=enqueue), patch(EXECUTE_TARGET) as mocked_execute:
			result = inventory_sync.queue_inventory_sync([first.name, other_mapping.name])

		mocked_execute.assert_not_called()
		self.assertEqual([job["account"] for job in result["jobs"]], [other_account.name])
		self.assertEqual([skip["account"] for skip in result["skipped_accounts"]], [self.account.name])

	def test_reconciliation_queues_the_whole_account(self):
		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch(FETCH_LOCATIONS_TARGET) as mocked_fetch,
			patch("frappe.enqueue", return_value=object()) as mocked_enqueue,
		):
			result = inventory_sync.queue_account_reconciliation(self.account.name)

		mocked_execute.assert_not_called()
		mocked_fetch.assert_not_called()
		self.assertEqual(result["jobs"][0]["mapping_count"], None)
		self.assertIsNone(mocked_enqueue.call_args.kwargs["mapping_names"])

	def test_reconciliation_of_a_misconfigured_account_reports_its_reason(self):
		self.account.db_set("warehouse", None, update_modified=False)

		with patch("frappe.enqueue") as mocked_enqueue:
			result = inventory_sync.queue_account_reconciliation(self.account.name)

		mocked_enqueue.assert_not_called()
		self.assertEqual(result["jobs"], [])
		self.assertIn("Warehouse", result["skipped_accounts"][0]["reason"])

	def test_a_talabat_mapping_is_skipped_as_not_a_shopify_account(self):
		talabat_account = self._make_account("Talabat")
		talabat_account.db_set("platform", "Talabat", update_modified=False)
		mapping = self._make_mapping(self._make_item("TalabatMap", qty=1), account=talabat_account)

		result, mocked_enqueue = self._queue([mapping.name])

		mocked_enqueue.assert_not_called()
		self.assertEqual(result["jobs"], [])
		self.assertIn("not a Shopify account", result["skipped_accounts"][0]["reason"])

	def test_user_without_a_platform_role_is_denied(self):
		# #322 (strict): marketplace rows are visible only through a platform role (or System
		# Manager). A user with read/write DocPerms but no platform role is denied.
		mapping = self._one(5)
		email, _role = make_role_user(
			self,
			"Inv No Platform",
			{"Marketplace Account": ["write"], "Marketplace Product Mapping": ["write"]},
		)
		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				inventory_sync.queue_inventory_sync([mapping.name])
			with self.assertRaises(frappe.PermissionError):
				inventory_sync.queue_account_reconciliation(self.account.name)
		finally:
			frappe.set_user("Administrator")

	def test_requires_write_permission_on_the_mapping(self):
		# A mock is legitimate here: every platform role is granted full rights on Marketplace
		# Product Mapping, so a read-only platform user cannot exist (#322). Administrator passes
		# every real check; only WRITE on the mapping doctype/doc is denied, so only the
		# endpoint's own mapping write check can raise.
		mapping = self._one(5)
		real_has_permission = frappe.has_permission
		real_doc_has_permission = frappe.model.document.Document.has_permission

		# Mirrors frappe.has_permission(doctype, ptype, doc, user, throw, *, ...): a denial
		# raises when throw is truthy, otherwise returns False.
		def deny_mapping_write(doctype=None, ptype="read", doc=None, user=None, throw=False, **kwargs):
			if doctype == "Marketplace Product Mapping" and ptype == "write":
				if throw:
					raise frappe.PermissionError
				return False
			return real_has_permission(doctype, ptype, doc, user, throw, **kwargs)

		def deny_mapping_doc_write(doc, permtype="write", *args, **kwargs):
			if doc.doctype == "Marketplace Product Mapping" and permtype == "write":
				return False
			return real_doc_has_permission(doc, permtype, *args, **kwargs)

		with (
			patch("frappe.has_permission", side_effect=deny_mapping_write),
			patch.object(
				frappe.model.document.Document,
				"has_permission",
				autospec=True,
				side_effect=deny_mapping_doc_write,
			),
		):
			with self.assertRaises(frappe.PermissionError):
				inventory_sync.queue_inventory_sync([mapping.name])
			with self.assertRaises(frappe.PermissionError):
				inventory_sync.queue_account_reconciliation(self.account.name)

	def test_rejects_an_empty_selection(self):
		with self.assertRaises(frappe.ValidationError):
			inventory_sync.queue_inventory_sync([])


class IntegrationTestScheduledTriggers(InventoryFixtureMixin, IntegrationTestCase):
	"""#107 PR2: sweep_changed_bins() / reconcile_all_accounts(). Every test
	mocks both ShopifyClient.execute() and frappe.enqueue() - the point of
	these two entry points is that neither ever calls Shopify directly, only
	enqueues."""

	def _bin_name(self, item_code):
		return frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": self.warehouse}, "name")

	def _set_bin_modified(self, item_code, modified):
		# frappe.db.set_value's own `modified` kwarg sets the column to this
		# exact value instead of "now" - lets a test deterministically place a
		# Bin's last-write time relative to the watermark/overlap window
		# without waiting in real time.
		frappe.db.set_value(
			"Bin", self._bin_name(item_code), "actual_qty", 1, modified=modified, update_modified=True
		)

	@staticmethod
	def _calls_for_account_positional(mocked_enqueue, account_name):
		# For a mock of enqueue_account_sync() itself (called positionally:
		# account_name, mapping_names), as opposed to _calls_for_account()
		# below, which filters frappe.enqueue()'s own kwargs.
		return [call for call in mocked_enqueue.call_args_list if call.args and call.args[0] == account_name]

	@staticmethod
	def _calls_for_account(mocked_enqueue, account_name):
		# Other test classes in this module run the real worker, which
		# commits per chunk (see this module's own top docstring) - those
		# fixtures are never rolled back and so accumulate as real enabled
		# Shopify Marketplace Accounts on the test site. sweep_changed_bins()/
		# reconcile_all_accounts() correctly enumerate ALL of them, so
		# assertions here filter to just this test's own account(s) instead
		# of asserting exclusivity/an exact total call count.
		return [call for call in mocked_enqueue.call_args_list if call.kwargs["account_name"] == account_name]

	def test_sweep_only_picks_up_bins_changed_since_the_watermark_with_overlap(self):
		old_item = self._make_item("SweepOld", qty=1)
		self._make_mapping(old_item)
		new_item = self._make_item("SweepNew", qty=1)
		new_mapping = self._make_mapping(new_item)

		tick_time = now_datetime()
		inventory_sync._advance_watermark(self.account.name, tick_time)
		self._set_bin_modified(
			old_item, add_to_date(tick_time, minutes=-(SHOPIFY_INVENTORY_SWEEP_OVERLAP_MINUTES + 5))
		)
		self._set_bin_modified(
			new_item, add_to_date(tick_time, minutes=-(SHOPIFY_INVENTORY_SWEEP_OVERLAP_MINUTES - 2))
		)

		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue") as mocked_enqueue:
			inventory_sync.sweep_changed_bins()

		mocked_execute.assert_not_called()
		calls = self._calls_for_account(mocked_enqueue, self.account.name)
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0].kwargs["mapping_names"], [new_mapping.name])

	def test_sweep_with_no_changed_bins_enqueues_nothing(self):
		item = self._make_item("SweepUnchanged", qty=1)
		self._make_mapping(item)
		tick_time = now_datetime()
		inventory_sync._advance_watermark(self.account.name, tick_time)
		self._set_bin_modified(
			item, add_to_date(tick_time, minutes=-(SHOPIFY_INVENTORY_SWEEP_OVERLAP_MINUTES + 5))
		)

		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue") as mocked_enqueue:
			inventory_sync.sweep_changed_bins()

		mocked_execute.assert_not_called()
		self.assertEqual(self._calls_for_account(mocked_enqueue, self.account.name), [])

	def test_sweep_advances_the_watermark_after_a_successful_tick(self):
		item = self._make_item("SweepAdvance", qty=1)
		self._make_mapping(item)
		self.assertIsNone(frappe.cache().get_value(inventory_sync._watermark_cache_key(self.account.name)))

		before_tick = now_datetime()
		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch("frappe.enqueue"),
			patch.object(inventory_sync, "now_datetime", return_value=before_tick),
		):
			inventory_sync.sweep_changed_bins()

		mocked_execute.assert_not_called()
		after = frappe.cache().get_value(inventory_sync._watermark_cache_key(self.account.name))
		self.assertIsNotNone(after)
		# The new watermark is exactly this tick's own start time, not merely
		# "some non-None value" - proves it was advanced to tick_time itself.
		self.assertEqual(after, before_tick)

	def test_sweep_uses_the_real_cached_watermark_not_just_the_now_fallback(self):
		# Cached watermark set 30 minutes in the PAST (not "now"): distinguishes
		# "the sweep reads the real watermark" from "it always falls back to
		# now-minus-overlap", since a watermark of exactly `now` produces the
		# same `since` as the missing-watermark fallback and the two cases
		# would be indistinguishable otherwise.
		item = self._make_item("SweepRealWatermark", qty=1)
		mapping = self._make_mapping(item)
		tick_time = now_datetime()
		past_watermark = add_to_date(tick_time, minutes=-30)
		inventory_sync._advance_watermark(self.account.name, past_watermark)
		# 25 minutes ago: newer than (watermark - overlap) = 40 minutes ago,
		# so only picked up if the real watermark (not "now") was used.
		self._set_bin_modified(item, add_to_date(tick_time, minutes=-25))

		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue") as mocked_enqueue:
			inventory_sync.sweep_changed_bins()

		mocked_execute.assert_not_called()
		calls = self._calls_for_account(mocked_enqueue, self.account.name)
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0].kwargs["mapping_names"], [mapping.name])

	def test_sweep_failure_in_the_real_bin_query_leaves_the_watermark_unchanged(self):
		# Exercises _sweep_account's REAL Bin-query code path raising (not a
		# fully-replaced _sweep_account mock), so the "no advance on failure"
		# line is actually exercised.
		item = self._make_item("SweepBinQueryFails", qty=1)
		self._make_mapping(item)
		watermark_before = add_to_date(now_datetime(), minutes=-5)
		inventory_sync._advance_watermark(self.account.name, watermark_before)

		real_get_all = frappe.get_all

		def flaky_get_all(doctype, *args, **kwargs):
			if doctype == "Bin":
				raise RuntimeError("bin query boom")
			return real_get_all(doctype, *args, **kwargs)

		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch("frappe.enqueue") as mocked_enqueue,
			patch.object(inventory_sync.frappe, "get_all", side_effect=flaky_get_all),
		):
			inventory_sync.sweep_changed_bins()

		mocked_execute.assert_not_called()
		self.assertEqual(self._calls_for_account(mocked_enqueue, self.account.name), [])
		after = frappe.cache().get_value(inventory_sync._watermark_cache_key(self.account.name))
		self.assertEqual(after, watermark_before)

	def test_bin_change_in_a_different_warehouse_is_not_picked_up(self):
		other_warehouse = frappe.get_all(
			"Warehouse",
			filters={"disabled": 0, "is_group": 0, "name": ["!=", self.warehouse]},
			pluck="name",
			limit=1,
		)
		if not other_warehouse:
			self.skipTest("No second leaf Warehouse available on this test site.")
		item = self._make_item("SweepOtherWarehouse", qty=1)
		self._make_mapping(item)
		tick_time = now_datetime()
		inventory_sync._advance_watermark(self.account.name, tick_time)
		bin_name = frappe.db.get_value("Bin", {"item_code": item, "warehouse": other_warehouse[0]}, "name")
		if not bin_name:
			frappe.get_doc(
				{"doctype": "Bin", "item_code": item, "warehouse": other_warehouse[0], "actual_qty": 1}
			).insert()
			bin_name = frappe.db.get_value(
				"Bin", {"item_code": item, "warehouse": other_warehouse[0]}, "name"
			)
		frappe.db.set_value(
			"Bin",
			bin_name,
			"actual_qty",
			2,
			modified=add_to_date(tick_time, minutes=-1),
			update_modified=True,
		)

		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue") as mocked_enqueue:
			inventory_sync.sweep_changed_bins()

		mocked_execute.assert_not_called()
		self.assertEqual(self._calls_for_account(mocked_enqueue, self.account.name), [])

	def test_bin_change_for_an_unmapped_item_is_not_picked_up(self):
		item = self._make_item("SweepUnmapped", qty=1)
		# Deliberately no mapping created for this item.
		tick_time = now_datetime()
		inventory_sync._advance_watermark(self.account.name, tick_time)
		self._set_bin_modified(item, add_to_date(tick_time, minutes=-1))

		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue") as mocked_enqueue:
			inventory_sync.sweep_changed_bins()

		mocked_execute.assert_not_called()
		self.assertEqual(self._calls_for_account(mocked_enqueue, self.account.name), [])

	def test_dedup_rejected_enqueue_does_not_advance_the_watermark(self):
		item = self._make_item("SweepDedupNoAdvance", qty=1)
		self._make_mapping(item)
		tick_time = now_datetime()
		watermark_before = add_to_date(tick_time, minutes=-5)
		inventory_sync._advance_watermark(self.account.name, watermark_before)
		self._set_bin_modified(item, add_to_date(tick_time, minutes=-1))

		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch.object(
				inventory_sync, "enqueue_account_sync", return_value=(False, None)
			) as mocked_enqueue,
			patch.object(inventory_sync, "now_datetime", return_value=tick_time),
		):
			inventory_sync.sweep_changed_bins()

		mocked_execute.assert_not_called()
		calls = self._calls_for_account_positional(mocked_enqueue, self.account.name)
		self.assertEqual(len(calls), 1)
		after = frappe.cache().get_value(inventory_sync._watermark_cache_key(self.account.name))
		self.assertEqual(after, watermark_before)

	def test_reconcile_dedup_rejection_is_logged(self):
		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch.object(inventory_sync, "enqueue_account_sync", return_value=(False, None)),
			patch.object(inventory_sync.frappe, "log_error") as mocked_log_error,
		):
			inventory_sync.reconcile_all_accounts()

		mocked_execute.assert_not_called()
		mocked_log_error.assert_called()
		titles = [call.kwargs.get("title") for call in mocked_log_error.call_args_list]
		self.assertIn("Shopify daily inventory reconciliation skipped", titles)

	def test_account_with_warehouse_in_a_different_company_is_excluded(self):
		other_company = frappe.get_all(
			"Company", filters={"name": ["!=", frappe.db.get_value("Warehouse", self.warehouse, "company")]}
		)
		if not other_company:
			self.skipTest("No second Company available on this test site.")
		self.account.db_set("erpnext_company", other_company[0].name, update_modified=False)
		item = self._make_item("SweepCompanyMismatch", qty=1)
		self._make_mapping(item)
		tick_time = now_datetime()
		self._set_bin_modified(item, add_to_date(tick_time, minutes=-1))

		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue") as mocked_enqueue:
			inventory_sync.sweep_changed_bins()
			inventory_sync.reconcile_all_accounts()

		mocked_execute.assert_not_called()
		self.assertEqual(self._calls_for_account(mocked_enqueue, self.account.name), [])

	def test_account_with_a_disabled_warehouse_is_excluded(self):
		self.account.db_set("erpnext_company", None, update_modified=False)
		frappe.db.set_value("Warehouse", self.warehouse, "disabled", 1)
		self.addCleanup(lambda: frappe.db.set_value("Warehouse", self.warehouse, "disabled", 0))
		item = self._make_item("SweepDisabledWarehouse", qty=1)
		self._make_mapping(item)
		tick_time = now_datetime()
		self._set_bin_modified(item, add_to_date(tick_time, minutes=-1))

		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue") as mocked_enqueue:
			inventory_sync.sweep_changed_bins()
			inventory_sync.reconcile_all_accounts()

		mocked_execute.assert_not_called()
		self.assertEqual(self._calls_for_account(mocked_enqueue, self.account.name), [])

	def test_missing_watermark_falls_back_to_the_overlap_window_not_skip_everything(self):
		recent_item = self._make_item("FirstTickRecent", qty=1)
		recent_mapping = self._make_mapping(recent_item)
		old_item = self._make_item("FirstTickOld", qty=1)
		self._make_mapping(old_item)

		now = now_datetime()
		self._set_bin_modified(
			old_item, add_to_date(now, minutes=-(SHOPIFY_INVENTORY_SWEEP_OVERLAP_MINUTES + 5))
		)
		self._set_bin_modified(recent_item, add_to_date(now, minutes=-1))
		# No _advance_watermark() call at all - simulates a first-ever tick or
		# a lost/evicted cache entry.
		self.assertIsNone(frappe.cache().get_value(inventory_sync._watermark_cache_key(self.account.name)))

		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue") as mocked_enqueue:
			inventory_sync.sweep_changed_bins()

		mocked_execute.assert_not_called()
		calls = self._calls_for_account(mocked_enqueue, self.account.name)
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0].kwargs["mapping_names"], [recent_mapping.name])
		# The tick still leaves the account with a real watermark afterwards -
		# a missing watermark is a one-tick fallback, not a permanent skip.
		self.assertIsNotNone(frappe.cache().get_value(inventory_sync._watermark_cache_key(self.account.name)))

	def test_disabled_account_is_never_swept(self):
		item = self._make_item("SweepDisabledAcct", qty=1)
		self._make_mapping(item)
		self.account.db_set("enabled", 0, update_modified=False)

		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue") as mocked_enqueue:
			inventory_sync.sweep_changed_bins()

		mocked_execute.assert_not_called()
		self.assertEqual(self._calls_for_account(mocked_enqueue, self.account.name), [])

	def test_disabled_account_is_never_reconciled(self):
		self.account.db_set("enabled", 0, update_modified=False)

		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue") as mocked_enqueue:
			inventory_sync.reconcile_all_accounts()

		mocked_execute.assert_not_called()
		self.assertEqual(self._calls_for_account(mocked_enqueue, self.account.name), [])

	def test_disabled_connection_is_never_swept_or_reconciled(self):
		frappe.db.set_value("Shopify Connection", self.account.connection, "enabled", 0)
		item = self._make_item("SweepDisabledConn", qty=1)
		self._make_mapping(item)

		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue") as mocked_enqueue:
			inventory_sync.sweep_changed_bins()
			inventory_sync.reconcile_all_accounts()

		mocked_execute.assert_not_called()
		self.assertEqual(self._calls_for_account(mocked_enqueue, self.account.name), [])

	def test_reconciliation_enqueues_every_enabled_account_and_skips_disabled(self):
		other_account = self._make_account("ReconOther")
		disabled_account = self._make_account("ReconDisabled")
		disabled_account.db_set("enabled", 0, update_modified=False)

		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue") as mocked_enqueue:
			inventory_sync.reconcile_all_accounts()

		mocked_execute.assert_not_called()
		# assertIn/subset (not assertEqual) - other pre-existing enabled Shopify
		# accounts on this test site are not this test's concern.
		enqueued_accounts = {call.kwargs["account_name"] for call in mocked_enqueue.call_args_list}
		self.assertTrue({self.account.name, other_account.name}.issubset(enqueued_accounts))
		self.assertNotIn(disabled_account.name, enqueued_accounts)
		for call in mocked_enqueue.call_args_list:
			self.assertIsNone(call.kwargs["mapping_names"])

	def test_two_close_sweep_ticks_for_the_same_account_rely_on_the_same_dedup_job_id(self):
		item = self._make_item("SweepDedup", qty=1)
		self._make_mapping(item)

		calls_for_account = []

		def fake_enqueue(*args, **kwargs):
			if kwargs["account_name"] != self.account.name:
				# Other (polluted) accounts on the test site: let them through
				# unremarkably so they don't interfere with this account's own
				# dedup behavior below.
				return object()
			calls_for_account.append(kwargs)
			# Second call simulates RQ's own dedup rejecting an already-queued
			# job for the same job_id - sweep_changed_bins() must tolerate
			# this without raising or double-processing.
			return None if len(calls_for_account) > 1 else object()

		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue", side_effect=fake_enqueue):
			inventory_sync.sweep_changed_bins()
			inventory_sync.sweep_changed_bins()

		mocked_execute.assert_not_called()
		self.assertEqual(len(calls_for_account), 2)
		self.assertEqual(calls_for_account[0]["job_id"], calls_for_account[1]["job_id"])
		self.assertEqual(
			calls_for_account[0]["job_id"], f"marketplace-shopify-inventory-sync-{self.account.name}"
		)

	def test_reconcile_logs_excluded_misconfigured_accounts_once_per_day(self):
		frappe.cache().delete_value(inventory_sync._EXCLUDED_ACCOUNTS_LOG_CACHE_KEY)
		self.account.db_set("erpnext_company", None, update_modified=False)
		frappe.db.set_value("Warehouse", self.warehouse, "disabled", 1)
		self.addCleanup(lambda: frappe.db.set_value("Warehouse", self.warehouse, "disabled", 0))

		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch("frappe.enqueue") as mocked_enqueue,
			patch.object(inventory_sync.frappe, "log_error") as mocked_log_error,
		):
			# A second run on the same day must not log a second time.
			inventory_sync.reconcile_all_accounts()
			inventory_sync.reconcile_all_accounts()

		mocked_execute.assert_not_called()
		self.assertEqual(self._calls_for_account(mocked_enqueue, self.account.name), [])
		titles = [call.kwargs.get("title") for call in mocked_log_error.call_args_list]
		self.assertEqual(
			titles.count("Shopify inventory reconciliation: accounts excluded from automatic sync"), 1
		)

	def test_sweep_does_not_log_excluded_accounts(self):
		# The sweep runs every 2 minutes - logging there would flood the Error
		# Log, so only the daily reconciliation logs exclusions (decision 6).
		frappe.cache().delete_value(inventory_sync._EXCLUDED_ACCOUNTS_LOG_CACHE_KEY)
		self.account.db_set("erpnext_company", None, update_modified=False)
		frappe.db.set_value("Warehouse", self.warehouse, "disabled", 1)
		self.addCleanup(lambda: frappe.db.set_value("Warehouse", self.warehouse, "disabled", 0))

		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch("frappe.enqueue") as mocked_enqueue,
			patch.object(inventory_sync.frappe, "log_error") as mocked_log_error,
		):
			inventory_sync.sweep_changed_bins()

		mocked_execute.assert_not_called()
		self.assertEqual(self._calls_for_account(mocked_enqueue, self.account.name), [])
		mocked_log_error.assert_not_called()

	def test_one_account_failing_does_not_stop_the_sweep_for_another(self):
		item = self._make_item("SweepIsolation", qty=1)
		self._make_mapping(item)
		other_account = self._make_account("SweepIsolationOther")
		other_item = self._make_item("SweepIsolationOther", qty=1)
		self._make_mapping(other_item, account=other_account)

		real_sweep_account = inventory_sync._sweep_account

		def flaky_sweep_account(account, tick_time):
			if account.name == self.account.name:
				raise RuntimeError("boom")
			return real_sweep_account(account, tick_time)

		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch("frappe.enqueue", return_value=object()) as mocked_enqueue,
			patch(
				"marketplace_integrations.integrations.shopify.inventory_sync._sweep_account",
				side_effect=flaky_sweep_account,
			),
		):
			inventory_sync.sweep_changed_bins()

		mocked_execute.assert_not_called()
		enqueued_accounts = {call.kwargs["account_name"] for call in mocked_enqueue.call_args_list}
		self.assertIn(other_account.name, enqueued_accounts)
		self.assertNotIn(self.account.name, enqueued_accounts)


class IntegrationTestPushSwitch(InventoryFixtureMixin, IntegrationTestCase):
	"""#285: the per-account "Push Inventory to Shopify" switch
	(``Marketplace Account.shopify_inventory_push_enabled``)."""

	PAUSED = "Inventory push is paused for this account"

	def _pause(self, account=None):
		(account or self.account).db_set("shopify_inventory_push_enabled", 0, update_modified=False)

	@staticmethod
	def _enqueued_accounts(mocked_enqueue):
		return {call.kwargs["account_name"] for call in mocked_enqueue.call_args_list}

	def test_switch_defaults_on_for_new_accounts_and_in_the_database_column(self):
		self.assertEqual(self.account.shopify_inventory_push_enabled, 1)
		self.assertEqual(
			frappe.get_meta("Marketplace Account").get_field("shopify_inventory_push_enabled").default, "1"
		)
		# Rows that existed before the field was added read as on because the
		# column itself carries the default (no backfill patch needed).
		column = frappe.db.sql(
			"SHOW COLUMNS FROM `tabMarketplace Account` LIKE 'shopify_inventory_push_enabled'", as_dict=True
		)
		self.assertEqual(str(column[0]["Default"]), "1")

	def test_sweep_skips_a_paused_account_but_still_syncs_an_active_one(self):
		other_account = self._make_account("SwitchOn")
		for marker, account in (("SwitchPaused", self.account), ("SwitchOn", other_account)):
			item = self._make_item(marker, qty=1)
			self._make_mapping(item, account=account)
		self._pause()

		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue") as mocked_enqueue:
			inventory_sync.sweep_changed_bins()

		mocked_execute.assert_not_called()
		enqueued = self._enqueued_accounts(mocked_enqueue)
		self.assertIn(other_account.name, enqueued)
		self.assertNotIn(self.account.name, enqueued)

	def test_reconcile_skips_a_paused_account_without_logging_it_as_excluded(self):
		other_account = self._make_account("ReconOn")
		self._pause()

		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch("frappe.enqueue") as mocked_enqueue,
			patch("frappe.log_error") as mocked_log_error,
		):
			usable, excluded = inventory_sync._enabled_shopify_accounts_with_exclusions()
			inventory_sync.reconcile_all_accounts()

		mocked_execute.assert_not_called()
		enqueued = self._enqueued_accounts(mocked_enqueue)
		self.assertIn(other_account.name, enqueued)
		self.assertNotIn(self.account.name, enqueued)
		self.assertNotIn(self.account.name, {account.name for account in usable})
		self.assertNotIn(self.account.name, {name for name, _reason in excluded})
		for call in mocked_log_error.call_args_list:
			self.assertNotIn(self.account.name, call.kwargs.get("message", ""))

	def test_manual_actions_queue_once_while_the_switch_is_off(self):
		mapping = self._one(5)
		self._pause()

		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch(FETCH_LOCATIONS_TARGET) as mocked_fetch,
			patch("frappe.enqueue", return_value=object()) as mocked_enqueue,
		):
			selected = inventory_sync.queue_inventory_sync([mapping.name])
			reconciled = inventory_sync.queue_account_reconciliation(self.account.name)

		for result in (selected, reconciled):
			self.assertEqual(result["skipped_accounts"], [])
			self.assertEqual(result["jobs"][0]["account"], self.account.name)
		# The request path itself makes no Shopify call; the worker is told it is manual.
		mocked_execute.assert_not_called()
		mocked_fetch.assert_not_called()
		self.assertEqual(mocked_enqueue.call_count, 2)
		for call in mocked_enqueue.call_args_list:
			self.assertIs(call.kwargs["manual"], True)
			self.assertEqual(call.kwargs["account_name"], self.account.name)

	def test_manual_action_for_a_disabled_account_is_still_refused(self):
		mapping = self._one(5)
		self._pause()
		self.account.db_set("enabled", 0, update_modified=False)

		with patch("frappe.enqueue") as mocked_enqueue:
			result = inventory_sync.queue_inventory_sync([mapping.name])

		self.assertEqual(result["jobs"], [])
		self.assertIn("disabled", result["skipped_accounts"][0]["reason"])
		mocked_enqueue.assert_not_called()

	def test_a_manual_click_while_a_sync_is_running_is_deduplicated(self):
		mapping = self._one(5)
		self._pause()

		with patch("frappe.enqueue", return_value=None):
			result = inventory_sync.queue_inventory_sync([mapping.name])

		self.assertEqual(result["jobs"], [])
		self.assertIn("already running", result["skipped_accounts"][0]["reason"])

	def test_enqueue_account_sync_refuses_a_paused_account_for_automatic_callers_only(self):
		self._pause()

		with patch("frappe.enqueue", return_value=object()) as mocked_enqueue:
			with self.assertRaises(frappe.ValidationError):
				inventory_sync.enqueue_account_sync(self.account.name, None, "Administrator")
			mocked_enqueue.assert_not_called()
			enqueued, _job_id = inventory_sync.enqueue_account_sync(
				self.account.name, None, "Administrator", manual=True
			)

		self.assertTrue(enqueued)
		self.assertIs(mocked_enqueue.call_args.kwargs["manual"], True)
		frappe.cache().delete_value(inventory_sync._status_cache_key(_job_id))

	def test_a_manual_worker_pushes_with_the_switch_off(self):
		mapping = self._one(5, shopify_on_hand=0)
		self._pause()

		payload = self._sync([mapping.name], manual=True)

		self.assertNotIn("paused", payload)
		self.assertEqual(self.shopify.variants[mapping.external_reference]["on_hand"], 5)
		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Synced")

	def test_a_manual_worker_is_not_stopped_by_the_switch_between_chunks(self):
		with patch.object(inventory_sync, "SHOPIFY_INVENTORY_SYNC_CHUNK_SIZE", 1):
			first = self._one(3)
			second = self._one(4)
			# Switch turned off during chunk 1: an automatic run would stop; manual must finish.
			self.shopify.before_set.append(self._pause)

			payload = self._sync([first.name, second.name], manual=True)

		self.assertNotIn("paused", payload)
		self.assertEqual(self.shopify.kinds().count("set"), 2)
		self.assertEqual(self.shopify.variants[first.external_reference]["on_hand"], 3)
		self.assertEqual(self.shopify.variants[second.external_reference]["on_hand"], 4)

	def test_a_manual_worker_for_a_disabled_account_still_fails(self):
		mapping = self._one(5)
		self._pause()
		self.account.db_set("enabled", 0, update_modified=False)

		with self.assertRaises(frappe.ValidationError):
			self._sync([mapping.name], manual=True)

		self.assertEqual(self.shopify.kinds().count("set"), 0)

	def test_a_job_queued_before_the_switch_was_turned_off_makes_no_shopify_call(self):
		mapping = self._one(5, shopify_on_hand=0)
		self._pause()
		job_id = frappe.generate_hash(length=16)
		inventory_sync._set_status(job_id, {"owner": "Administrator", "status": "queued", "processed": 0})

		payload = self._sync([mapping.name], job_id_for_result=job_id, owner="Administrator")

		self.assertEqual(self.shopify.calls, [])
		self.assertTrue(payload["paused"])
		self.assertEqual(payload["total"], 0)
		state = frappe.cache().get_value(inventory_sync._status_cache_key(job_id))
		self.assertEqual(state["status"], "failed")
		self.assertIn(self.PAUSED, state["error"])
		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Not Synced")
		frappe.cache().delete_value(inventory_sync._status_cache_key(job_id))

	def test_turning_the_switch_back_on_resumes_syncing_without_an_automatic_push(self):
		mapping = self._one(5, shopify_on_hand=0)
		self._pause()

		with patch("frappe.enqueue") as mocked_enqueue:
			self.account.db_set("shopify_inventory_push_enabled", 1, update_modified=False)
		mocked_enqueue.assert_not_called()

		self._sync([mapping.name])
		self.assertEqual(self.shopify.variants[mapping.external_reference]["on_hand"], 5)

	def test_order_import_still_works_for_a_paused_account(self):
		from marketplace_integrations.integrations.shopify import orders
		from marketplace_integrations.integrations.shopify.test_orders import _order_gid, make_order_payload

		self._pause()
		order_gid = _order_gid(frappe.generate_hash(length=8))
		with patch(EXECUTE_TARGET, return_value={"order": make_order_payload(order_gid)}):
			result = orders.import_order(self.account.name, order_gid)

		self.assertEqual(result.doctype, "Marketplace Order")
		self.assertEqual(result.marketplace_account, self.account.name)
		frappe.delete_doc("Marketplace Order", result.name, force=True, ignore_permissions=True)

	def test_switch_turned_off_between_chunks_stops_before_the_next_shopify_write(self):
		with patch.object(inventory_sync, "SHOPIFY_INVENTORY_SYNC_CHUNK_SIZE", 1):
			first = self._one(3)
			second = self._one(4)
			third = self._one(5)
			# Runs inside chunk 1's inventorySetQuantities call (before it is
			# evaluated): chunk 1 is already in flight and finishes, chunk 2
			# must never start.
			self.shopify.before_set.append(self._pause)
			job_id = frappe.generate_hash(length=16)
			inventory_sync._set_status(job_id, {"owner": "Administrator", "status": "queued", "processed": 0})

			payload = self._sync(
				[first.name, second.name, third.name], job_id_for_result=job_id, owner="Administrator"
			)

		self.assertEqual(self.shopify.kinds().count("set"), 1)
		self.assertEqual(self.shopify.kinds().count("read"), 1)
		self.assertEqual(self.shopify.variants[first.external_reference]["on_hand"], 3)
		self.assertEqual(self.shopify.variants[second.external_reference]["on_hand"], 0)
		self.assertEqual(self.shopify.variants[third.external_reference]["on_hand"], 0)
		self.assertTrue(payload["paused"])
		self.assertEqual((payload["total"], payload["succeeded"], payload["failed"]), (1, 1, 0))
		for mapping, status in ((first, "Synced"), (second, "Not Synced"), (third, "Not Synced")):
			mapping.reload()
			self.assertEqual(mapping.inventory_sync_status, status)
		state = frappe.cache().get_value(inventory_sync._status_cache_key(job_id))
		self.assertEqual(state["status"], "failed")  # terminal, never stuck on "running"
		self.assertIn(self.PAUSED, state["error"])
		self.assertEqual(state["result"]["succeeded"], 1)
		frappe.cache().delete_value(inventory_sync._status_cache_key(job_id))

	def test_scheduled_entry_points_make_no_shopify_call_for_a_paused_account(self):
		item = self._make_item("SchedPaused", qty=1)
		self._make_mapping(item)
		self._pause()
		ran_inline = []

		def enqueue_inline(fn, **kwargs):
			# Run the worker for OUR account as if RQ had picked it up; leave
			# other (leftover) accounts of the test site alone.
			if kwargs.get("account_name") != self.account.name:
				return MagicMock()
			ran_inline.append(kwargs)
			fn(
				**{
					key: value
					for key, value in kwargs.items()
					if key not in ("queue", "timeout", "job_id", "deduplicate")
				}
			)
			return MagicMock()

		def fail_on_any_call(query, variables=None):
			raise AssertionError(f"Shopify was called for a paused account: {query[:80]}")

		with (
			patch(EXECUTE_TARGET, side_effect=fail_on_any_call) as mocked_execute,
			patch(FETCH_LOCATIONS_TARGET, side_effect=fail_on_any_call),
			patch("frappe.enqueue", side_effect=enqueue_inline),
		):
			inventory_sync.sweep_changed_bins()
			inventory_sync.reconcile_all_accounts()

		self.assertEqual(ran_inline, [])
		mocked_execute.assert_not_called()

	def test_a_sweep_or_reconcile_racing_the_switch_is_a_quiet_skip(self):
		item = self._make_item("RaceItem", qty=1)
		mapping = self._make_mapping(item)
		self._pause()
		# The account query ran just before the switch was turned off.
		stale = frappe._dict(name=self.account.name, warehouse=self.warehouse)

		with (
			patch.object(inventory_sync, "_enabled_shopify_accounts", return_value=[stale]),
			patch.object(
				inventory_sync, "_enabled_shopify_accounts_with_exclusions", return_value=([stale], [])
			),
			patch("frappe.enqueue") as mocked_enqueue,
			patch("frappe.log_error") as mocked_log_error,
			patch(EXECUTE_TARGET) as mocked_execute,
		):
			inventory_sync.sweep_changed_bins()
			inventory_sync.reconcile_all_accounts()

		mocked_enqueue.assert_not_called()
		mocked_execute.assert_not_called()
		mocked_log_error.assert_not_called()
		mapping.reload()
		self.assertEqual(mapping.inventory_sync_status, "Not Synced")
