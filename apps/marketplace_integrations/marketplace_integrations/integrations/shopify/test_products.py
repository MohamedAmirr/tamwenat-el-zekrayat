# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Every test here mocks ShopifyClient.execute() directly - never a real
Shopify call - and mocks fetch_shop_and_active_locations() (used by
Marketplace Account's own validate_shopify_account()) so a Shopify-platform
Marketplace Account fixture never makes a live call either. See
marketplace_account/test_marketplace_account.py for the same pattern."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify import products
from marketplace_integrations.integrations.shopify.constants import SHOPIFY_NODES_LOOKUP_CHUNK_SIZE
from marketplace_integrations.integrations.shopify.products import (
	_is_variant_gid,
	_last_sweep_cache_key,
	_sweep_cache_key,
	get_account,
	get_last_catalog_sweep,
	get_sweep_status,
	queue_catalog_sweep,
	run_catalog_sweep,
	upsert_mapping,
)

EXECUTE_TARGET = "marketplace_integrations.integrations.shopify.client.ShopifyClient.execute"
FETCH_SHOP_AND_ACTIVE_LOCATIONS = (
	"marketplace_integrations.marketplace_integrations.doctype.marketplace_account."
	"marketplace_account.fetch_shop_and_active_locations"
)

def _generate_shop_gid():
	"""Generate a unique shop GID for each test account."""
	return f"gid://shopify/Shop/{frappe.generate_hash(length=16)}"


def mock_shopify_verification(shop_gid):
	return patch(
		FETCH_SHOP_AND_ACTIVE_LOCATIONS,
		return_value={"shop_id": shop_gid, "active_locations": []},
	)


def make_shopify_connection(connection_name):
	return (
		frappe.get_doc(
			{
				"doctype": "Shopify Connection",
				"connection_name": connection_name,
				"shop_domain": f"test-shop-{frappe.generate_hash(length=8)}.myshopify.com",
				"client_id": "test-client-id",
				"client_secret": "test-client-secret",
			}
		)
		.insert()
		.name
	)


def make_shopify_account(account_name, connection=None):
	shop_gid = _generate_shop_gid()
	with mock_shopify_verification(shop_gid):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": account_name,
				"platform": "Shopify",
				"external_account_id": shop_gid,
				"connection": connection or make_shopify_connection(account_name + " Connection"),
			}
		).insert()


def make_item(item_code):
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


class IntegrationTestGetAccount(IntegrationTestCase):
	def test_rejects_non_shopify_account(self):
		account = make_shopify_account("Test Non-Shopify Account")
		account.db_set("platform", "Talabat", update_modified=False)

		with self.assertRaisesRegex(frappe.ValidationError, "not a Shopify account"):
			get_account(account.name)


class IntegrationTestIsVariantGid(IntegrationTestCase):
	def test_valid_gid(self):
		self.assertTrue(_is_variant_gid("gid://shopify/ProductVariant/123"))

	def test_invalid_values(self):
		self.assertFalse(_is_variant_gid(""))
		self.assertFalse(_is_variant_gid(None))
		self.assertFalse(_is_variant_gid("123"))
		self.assertFalse(_is_variant_gid("gid://shopify/Product/123"))
		self.assertFalse(_is_variant_gid("SKU-001"))


class IntegrationTestUpsertMapping(IntegrationTestCase):
	def setUp(self):
		self.account = make_shopify_account("Test Upsert Account")
		self.item = make_item("Test Shopify Upsert Item")

	def test_creates_mapping_with_identity_consistency(self):
		result = upsert_mapping(
			self.account.name,
			"gid://shopify/ProductVariant/1",
			self.item,
			external_sku="SKU-1",
			external_barcode="BAR-1",
			external_product_id="gid://shopify/Product/1",
		)

		mapping = frappe.get_doc("Marketplace Product Mapping", result["name"])
		self.assertEqual(mapping.external_reference, "gid://shopify/ProductVariant/1")
		self.assertEqual(mapping.external_variant_id, "gid://shopify/ProductVariant/1")
		self.assertEqual(mapping.item, self.item)
		self.assertEqual(mapping.external_sku, "SKU-1")
		self.assertTrue(mapping.enabled)

	def test_rejects_non_variant_gid(self):
		with self.assertRaisesRegex(frappe.ValidationError, "not a valid Shopify product variant GID"):
			upsert_mapping(self.account.name, "SKU-1", self.item)

	def test_updates_existing_mapping(self):
		first = upsert_mapping(self.account.name, "gid://shopify/ProductVariant/2", self.item)
		other_item = make_item("Test Shopify Upsert Item 2")

		second = upsert_mapping(self.account.name, "gid://shopify/ProductVariant/2", other_item)

		self.assertEqual(first["name"], second["name"])
		mapping = frappe.get_doc("Marketplace Product Mapping", second["name"])
		self.assertEqual(mapping.item, other_item)


class IntegrationTestRunCatalogSweep(IntegrationTestCase):
	def setUp(self):
		self.account = make_shopify_account(f"Test Sweep Account {frappe.generate_hash(length=8)}")
		self.item = make_item("Test Shopify Sweep Item")

	def tearDown(self):
		# Review finding #2's frappe.db.commit() (added to
		# _refresh_mapping_descriptive_fields()) is reachable from several
		# tests in this class - per docs/frappe-conventions.md's commit/test
		# transaction gotcha, that permanently persists this test's rows past
		# IntegrationTestCase's normal class-scoped rollback. Explicitly clean
		# up here rather than relying on that rollback, which a commit()
		# already defeats for whatever was in the transaction at that point.
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc(
			"Shopify Connection", self.account.connection, ignore_permissions=True, force=True
		)
		# Review finding #7: this class's setUp() creates self.item via
		# make_item(), same gotcha as above - the commit() reachable from
		# _refresh_mapping_descriptive_fields() makes it permanent too, and
		# it's never deleted anywhere else.
		if frappe.db.exists("Item", self.item):
			frappe.delete_doc("Item", self.item, ignore_permissions=True, force=True)
		# The commit() above (if a test reached it) already made this test's
		# account/connection/item permanent - these deletes must be committed
		# too, in the SAME way, or they'd only land in a fresh post-commit
		# transaction that the class-level rollback then undoes right back to
		# "still present", silently defeating this whole cleanup.
		frappe.db.commit()
		# When a test's account insert does NOT reach the commit() above, the
		# insert (and the MKTACC naming-series increment) rolls back with the
		# rest of that test's transaction - so a later test can be handed the
		# exact same account name. This class's own tests write a Redis cache
		# entry keyed by that name (_last_sweep_cache_key) which is NOT
		# transactional and survives the rollback, so a stale entry from one
		# test leaks into whichever later test reuses the same name (caught
		# by IntegrationTestGetLastCatalogSweep.test_returns_none_when_no_sweep_has_run
		# in the same run). Clear it explicitly rather than relying on rollback.
		frappe.cache().delete_value(_last_sweep_cache_key(self.account.name))

	def _run(self, side_effect):
		job_id = frappe.generate_hash(length=8)
		with patch(EXECUTE_TARGET, side_effect=side_effect):
			run_catalog_sweep(job_id, frappe.session.user, self.account.name)
		return frappe.cache().get_value(f"marketplace_integrations:shopify:catalog_sweep_job:{job_id}")

	def test_unmapped_variant_reported(self):
		responses = [
			{
				"productVariants": {
					"pageInfo": {"hasNextPage": False, "endCursor": None},
					"nodes": [
						{
							"id": "gid://shopify/ProductVariant/10",
							"sku": "SKU-10",
							"barcode": None,
							"title": "Default",
							"product": {"id": "gid://shopify/Product/10", "title": "Widget", "status": "ACTIVE"},
						}
					],
				}
			},
			{"nodes": []},
		]
		state = self._run(responses)

		self.assertEqual(state["status"], "completed")
		exceptions = state["result"]["exceptions"]
		self.assertEqual(len(exceptions), 1)
		self.assertEqual(exceptions[0]["category"], "Unmapped")
		self.assertEqual(exceptions[0]["external_reference"], "gid://shopify/ProductVariant/10")

	def test_unmapped_variant_with_disabled_mapping_exposes_mapping_link(self):
		# Review finding #6: a variant whose only mapping row is DISABLED is
		# still "Unmapped" (no ENABLED row) - but that exception's `mapping`
		# field must point at the existing disabled row (not None) so the
		# page's "Open mapping" link renders, warning the operator that
		# setting an Item here re-enables that row rather than creating a
		# new one.
		disabled = upsert_mapping(self.account.name, "gid://shopify/ProductVariant/60", self.item)
		frappe.db.set_value("Marketplace Product Mapping", disabled["name"], "enabled", 0)
		responses = [
			{
				"productVariants": {
					"pageInfo": {"hasNextPage": False, "endCursor": None},
					"nodes": [
						{
							"id": "gid://shopify/ProductVariant/60",
							"sku": "SKU-60",
							"barcode": None,
							"product": {"id": "gid://shopify/Product/60", "title": "Widget", "status": "ACTIVE"},
						}
					],
				}
			},
			{"nodes": [{"id": "gid://shopify/ProductVariant/60", "sku": "SKU-60", "barcode": None, "product": {"id": "gid://shopify/Product/60"}}]},
		]
		state = self._run(responses)

		exceptions = state["result"]["exceptions"]
		unmapped = [row for row in exceptions if row["category"] == "Unmapped"]
		self.assertEqual(len(unmapped), 1)
		self.assertEqual(unmapped[0]["mapping"], disabled["name"])

	def test_mapped_variant_not_reported_as_unmapped(self):
		upsert_mapping(self.account.name, "gid://shopify/ProductVariant/20", self.item)
		responses = [
			{
				"productVariants": {
					"pageInfo": {"hasNextPage": False, "endCursor": None},
					"nodes": [
						{
							"id": "gid://shopify/ProductVariant/20",
							"sku": "SKU-20",
							"barcode": "BAR-20",
							"title": "Default",
							"product": {"id": "gid://shopify/Product/20", "title": "Gadget", "status": "ACTIVE"},
						}
					],
				}
			},
			{
				"nodes": [
					{
						"id": "gid://shopify/ProductVariant/20",
						"sku": "SKU-20",
						"barcode": "BAR-20",
						"product": {"id": "gid://shopify/Product/20"},
					}
				]
			},
		]
		state = self._run(responses)

		categories = [row["category"] for row in state["result"]["exceptions"]]
		self.assertNotIn("Unmapped", categories)
		mapping = frappe.get_doc(
			"Marketplace Product Mapping",
			{"marketplace_account": self.account.name, "external_reference": "gid://shopify/ProductVariant/20"},
		)
		self.assertEqual(mapping.external_sku, "SKU-20")
		self.assertEqual(mapping.external_barcode, "BAR-20")

	def test_deleted_mapping_reported(self):
		upsert_mapping(self.account.name, "gid://shopify/ProductVariant/30", self.item)
		responses = [
			{"productVariants": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}},
			{"nodes": [None]},
		]
		state = self._run(responses)

		exceptions = state["result"]["exceptions"]
		self.assertEqual(len(exceptions), 1)
		self.assertEqual(exceptions[0]["category"], "Deleted")
		self.assertEqual(exceptions[0]["external_reference"], "gid://shopify/ProductVariant/30")

	def test_variant_less_mapping_reported_without_nodes_lookup(self):
		# A malformed external_reference (e.g. written by the generic Excel
		# mapping importer) must never be sent into the nodes(ids:...) lookup -
		# it isn't a real GID at all.
		mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"external_reference": "not-a-gid",
				"item": self.item,
			}
		).insert()

		responses = [
			{"productVariants": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}},
		]
		state = self._run(responses)

		exceptions = state["result"]["exceptions"]
		self.assertEqual(len(exceptions), 1)
		self.assertEqual(exceptions[0]["category"], "Variant-less")
		self.assertEqual(exceptions[0]["mapping"], mapping.name)

	def test_failure_marks_job_failed(self):
		with patch(EXECUTE_TARGET, side_effect=frappe.ValidationError("boom")):
			job_id = frappe.generate_hash(length=8)
			with self.assertRaises(frappe.ValidationError):
				run_catalog_sweep(job_id, frappe.session.user, self.account.name)
			state = frappe.cache().get_value(f"marketplace_integrations:shopify:catalog_sweep_job:{job_id}")
		self.assertEqual(state["status"], "failed")

	def test_completion_writes_account_keyed_last_sweep_cache_entry(self):
		# #139 section 1: a completed sweep must also write the account-keyed
		# "last sweep" cache entry get_last_catalog_sweep() reads - distinct
		# from the per-job_id entry `_run()` already asserts on above.
		responses = [
			{"productVariants": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}},
			{"nodes": []},
		]
		self._run(responses)

		last_sweep = frappe.cache().get_value(
			f"marketplace_integrations:shopify:catalog_sweep_last:{self.account.name}"
		)
		self.assertEqual(last_sweep["status"], "completed")
		self.assertEqual(last_sweep["result"]["total_exceptions"], 0)
		self.assertIsNotNone(last_sweep["completed_at"])

	def test_failure_writes_account_keyed_last_sweep_cache_entry(self):
		with patch(EXECUTE_TARGET, side_effect=frappe.ValidationError("boom")):
			job_id = frappe.generate_hash(length=8)
			with self.assertRaises(frappe.ValidationError):
				run_catalog_sweep(job_id, frappe.session.user, self.account.name)

		last_sweep = frappe.cache().get_value(
			f"marketplace_integrations:shopify:catalog_sweep_last:{self.account.name}"
		)
		self.assertEqual(last_sweep["status"], "failed")
		self.assertIn("boom", last_sweep["error"])

	def test_pagination_walks_second_cursor_page(self):
		# Explicit acceptance criterion for #105: pagination works beyond one
		# GraphQL page. Every other test in this class returns a single page
		# (hasNextPage: False) - this is the only one that exercises the
		# cursor being carried from one call's endCursor into the next call's
		# `after` argument.
		responses = [
			{
				"productVariants": {
					"pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
					"nodes": [
						{
							"id": "gid://shopify/ProductVariant/40",
							"sku": "SKU-40",
							"barcode": None,
							"product": {"id": "gid://shopify/Product/40", "title": "A", "status": "ACTIVE"},
						}
					],
				}
			},
			{
				"productVariants": {
					"pageInfo": {"hasNextPage": False, "endCursor": None},
					"nodes": [
						{
							"id": "gid://shopify/ProductVariant/41",
							"sku": "SKU-41",
							"barcode": None,
							"product": {"id": "gid://shopify/Product/41", "title": "B", "status": "ACTIVE"},
						}
					],
				}
			},
		]
		job_id = frappe.generate_hash(length=8)
		with patch(EXECUTE_TARGET, side_effect=responses) as mocked:
			run_catalog_sweep(job_id, frappe.session.user, self.account.name)
		state = frappe.cache().get_value(f"marketplace_integrations:shopify:catalog_sweep_job:{job_id}")

		exceptions = state["result"]["exceptions"]
		references = {row["external_reference"] for row in exceptions}
		self.assertEqual(
			references, {"gid://shopify/ProductVariant/40", "gid://shopify/ProductVariant/41"}
		)
		self.assertEqual(mocked.call_count, 2)
		first_variables = mocked.call_args_list[0].args[1]
		second_variables = mocked.call_args_list[1].args[1]
		self.assertIsNone(first_variables["after"])
		self.assertEqual(second_variables["after"], "cursor-1")

	def test_multiple_nodes_lookup_chunks_pair_by_id_not_position(self):
		# Explicit acceptance criterion for #105 and review finding #1: more
		# than SHOPIFY_NODES_LOOKUP_CHUNK_SIZE saved mappings forces the
		# nodes(ids:...) lookup into two chunks, and each chunk's response is
		# returned in REVERSED order relative to the request - if pairing were
		# ever done by list position (the pre-fix zip() bug) rather than by
		# each node's own `id`, this would silently swap sku values between
		# rows within a chunk.
		gids = [
			f"gid://shopify/ProductVariant/{2000 + i}" for i in range(SHOPIFY_NODES_LOOKUP_CHUNK_SIZE + 1)
		]
		for gid in gids:
			upsert_mapping(self.account.name, gid, self.item, external_sku=f"OLD-{gid}")

		def _nodes_lookup_reversed(query, variables):
			if "ids" in variables:
				ids = variables["ids"]
				return {
					"nodes": [
						{
							"id": gid,
							"sku": f"NEW-{gid}",
							"barcode": None,
							"product": {"id": f"{gid}-product"},
						}
						for gid in reversed(ids)
					]
				}
			return {"productVariants": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}

		job_id = frappe.generate_hash(length=8)
		with patch(EXECUTE_TARGET, side_effect=_nodes_lookup_reversed) as mocked:
			run_catalog_sweep(job_id, frappe.session.user, self.account.name)
		state = frappe.cache().get_value(f"marketplace_integrations:shopify:catalog_sweep_job:{job_id}")

		# 1 call for the productVariants pass + 2 calls for the two
		# nodes(ids:...) chunks (SHOPIFY_NODES_LOOKUP_CHUNK_SIZE + 1 rows).
		self.assertEqual(mocked.call_count, 3)
		self.assertEqual(state["result"]["exceptions"], [])
		for gid in gids:
			mapping = frappe.get_doc(
				"Marketplace Product Mapping",
				{"marketplace_account": self.account.name, "external_reference": gid},
			)
			self.assertEqual(mapping.external_sku, f"NEW-{gid}")

	def test_nodes_lookup_missing_id_treated_as_deleted(self):
		# Review finding #1: a row whose external_reference isn't present at
		# all in the nodes(ids:...) response (a shorter list than requested)
		# must be classified "Deleted", the same as an explicit null node -
		# not silently dropped or misclassified.
		upsert_mapping(self.account.name, "gid://shopify/ProductVariant/50", self.item)
		responses = [
			{"productVariants": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}},
			{"nodes": []},
		]
		state = self._run(responses)

		exceptions = state["result"]["exceptions"]
		self.assertEqual(len(exceptions), 1)
		self.assertEqual(exceptions[0]["category"], "Deleted")
		self.assertEqual(exceptions[0]["external_reference"], "gid://shopify/ProductVariant/50")


class IntegrationTestQueueCatalogSweep(IntegrationTestCase):
	"""Review finding #2: dedup-rejection path was previously untested."""

	def setUp(self):
		self.account = make_shopify_account(f"Test Queue Account {frappe.generate_hash(length=8)}")

	def tearDown(self):
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc(
			"Shopify Connection", self.account.connection, ignore_permissions=True, force=True
		)

	def test_dedup_rejection_throws_and_clears_queued_cache_entry(self):
		# frappe.enqueue() returns None when it silently skips a deduplicated
		# enqueue (review finding #5 of the earlier round) - mock that shape
		# directly rather than racing two real background jobs.
		with patch("frappe.enqueue", return_value=None) as mocked_enqueue:
			with self.assertRaisesRegex(frappe.ValidationError, "already running"):
				queue_catalog_sweep(self.account.name)
			self.assertEqual(mocked_enqueue.call_count, 1)

		# Review finding #3: a rejected enqueue must not leave the "queued"
		# cache entry seeded before the enqueue call behind - assert against
		# the exact job_id the rejected call used (captured from the mocked
		# frappe.enqueue()'s own kwargs).
		job_id = mocked_enqueue.call_args.kwargs["job_id_for_result"]
		self.assertIsNone(frappe.cache().get_value(_sweep_cache_key(job_id)))

	def test_successful_enqueue_seeds_queued_status_before_returning(self):
		fake_job = object()
		with patch("frappe.enqueue", return_value=fake_job):
			result = queue_catalog_sweep(self.account.name)

		self.assertEqual(result["status"], "queued")
		state = frappe.cache().get_value(_sweep_cache_key(result["job_id"]))
		self.assertEqual(state["status"], "queued")
		self.assertEqual(state["processed"], 0)


class IntegrationTestMaxExceptionsTruncation(IntegrationTestCase):
	"""Review finding #2: MAX_EXCEPTIONS_RETURNED truncation was untested."""

	def setUp(self):
		self.account = make_shopify_account(f"Test Truncate Account {frappe.generate_hash(length=8)}")

	def tearDown(self):
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc(
			"Shopify Connection", self.account.connection, ignore_permissions=True, force=True
		)

	def test_more_than_max_exceptions_are_truncated(self):
		nodes = [
			{
				"id": f"gid://shopify/ProductVariant/{9000 + i}",
				"sku": f"SKU-{i}",
				"barcode": None,
				"product": {"id": f"gid://shopify/Product/{9000 + i}", "title": "Widget"},
			}
			for i in range(3)
		]
		responses = [
			{"productVariants": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}},
			{"nodes": []},
		]
		job_id = frappe.generate_hash(length=8)
		with patch(EXECUTE_TARGET, side_effect=responses), patch.object(products, "MAX_EXCEPTIONS_RETURNED", 2):
			run_catalog_sweep(job_id, frappe.session.user, self.account.name)
		state = frappe.cache().get_value(f"marketplace_integrations:shopify:catalog_sweep_job:{job_id}")

		result = state["result"]
		self.assertEqual(result["total_exceptions"], 3)
		self.assertEqual(len(result["exceptions"]), 2)
		self.assertTrue(result["truncated"])


class IntegrationTestGetSweepStatusOwnerScoping(IntegrationTestCase):
	"""Review finding #2: get_sweep_status()'s owner scoping was untested."""

	def test_rejects_status_lookup_for_a_different_owner(self):
		job_id = frappe.generate_hash(length=8)
		frappe.cache().set_value(
			_sweep_cache_key(job_id),
			{"owner": "someone-else@example.com", "status": "running", "processed": 5},
			expires_in_sec=60,
		)

		with self.assertRaises(frappe.PermissionError):
			get_sweep_status(job_id)

	def test_allows_status_lookup_for_the_owning_user(self):
		job_id = frappe.generate_hash(length=8)
		frappe.cache().set_value(
			_sweep_cache_key(job_id),
			{"owner": frappe.session.user, "status": "running", "processed": 5},
			expires_in_sec=60,
		)

		status = get_sweep_status(job_id)

		self.assertEqual(status["status"], "running")
		self.assertEqual(status["processed"], 5)
		self.assertNotIn("owner", status)


class IntegrationTestGetLastCatalogSweep(IntegrationTestCase):
	"""#139 section 1: get_last_catalog_sweep() reads the account-keyed cache
	entry, permission-checked like get_account()/_require_mapping_permission()
	- NOT owner-scoped, unlike get_sweep_status() (see that function's
	docstring)."""

	def setUp(self):
		self.account = make_shopify_account(f"Test Last Sweep Account {frappe.generate_hash(length=8)}")

	def tearDown(self):
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc(
			"Shopify Connection", self.account.connection, ignore_permissions=True, force=True
		)
		# This class's own account insert isn't behind a commit(), so it rolls
		# back with the rest of the test's transaction, freeing the MKTACC
		# name for reuse by a later test/class - but a Redis cache entry
		# written here (test_returns_cached_state_for_the_account) is not
		# transactional and would otherwise leak into whichever later test
		# gets handed that same reused name.
		frappe.cache().delete_value(_last_sweep_cache_key(self.account.name))

	def test_returns_none_when_no_sweep_has_run(self):
		self.assertIsNone(get_last_catalog_sweep(self.account.name))

	def test_returns_cached_state_for_the_account(self):
		frappe.cache().set_value(
			_last_sweep_cache_key(self.account.name),
			{"job_id": "abc", "status": "completed", "result": {"total_exceptions": 3}},
			expires_in_sec=60,
		)

		state = get_last_catalog_sweep(self.account.name)

		self.assertEqual(state["status"], "completed")
		self.assertEqual(state["result"]["total_exceptions"], 3)

	def test_rejects_non_shopify_account(self):
		account = make_shopify_account(f"Test Non-Shopify Last Sweep {frappe.generate_hash(length=8)}")
		account.db_set("platform", "Talabat", update_modified=False)
		try:
			with self.assertRaisesRegex(frappe.ValidationError, "not a Shopify account"):
				get_last_catalog_sweep(account.name)
		finally:
			frappe.delete_doc("Marketplace Account", account.name, ignore_permissions=True, force=True)
			frappe.delete_doc(
				"Shopify Connection", account.connection, ignore_permissions=True, force=True
			)
