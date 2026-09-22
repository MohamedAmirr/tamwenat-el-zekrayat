# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Every test here mocks ShopifyClient.execute() directly - never a real
Shopify call - mirroring integrations/shopify/test_products.py's pattern.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify import catalog_push
from marketplace_integrations.integrations.shopify.test_products import make_item, make_shopify_account

EXECUTE_TARGET = "marketplace_integrations.integrations.shopify.client.ShopifyClient.execute"


def _execute_dispatcher(currency="USD", bulk_response=None):
	"""Return a side_effect function for ShopifyClient.execute() that answers
	the shop-currency query and the productVariantsBulkUpdate mutation
	differently, keyed off the query text itself (no real network call)."""

	def _side_effect(query, variables=None):
		if "currencyCode" in query:
			return {"shop": {"currencyCode": currency}}
		if "productVariantsBulkUpdate" in query:
			return {"productVariantsBulkUpdate": bulk_response or {}}
		raise AssertionError(f"Unexpected query in test: {query}")

	return _side_effect


def _currency_only_dispatcher(currency="USD"):
	"""A side_effect that answers the shop-currency query but raises if the
	productVariantsBulkUpdate mutation is ever attempted - used to assert
	Shopify is never actually asked to update anything."""

	def _side_effect(query, variables=None):
		if "currencyCode" in query:
			return {"shop": {"currencyCode": currency}}
		raise AssertionError("productVariantsBulkUpdate must not be called in this scenario")

	return _side_effect


class IntegrationTestValidatePushAccount(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.price_list = (
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": f"Push Account Price List {self.suffix}",
					"currency": "USD",
					"selling": 1,
					"enabled": 1,
				}
			)
			.insert()
			.name
		)
		self.account = make_shopify_account(f"Push Validate Account {self.suffix}")
		self.account.db_set("erpnext_selling_price_list", self.price_list, update_modified=False)
		self.account.reload()

	def test_rejects_non_shopify_account(self):
		self.account.db_set("platform", "Talabat", update_modified=False)

		with self.assertRaisesRegex(frappe.ValidationError, "not a Shopify account"):
			catalog_push.validate_push_account(self.account.name)

	def test_requires_a_price_list(self):
		self.account.db_set("erpnext_selling_price_list", None, update_modified=False)

		with self.assertRaisesRegex(frappe.ValidationError, "Selling Price List"):
			catalog_push.validate_push_account(self.account.name)

	def test_rejects_disabled_price_list(self):
		frappe.db.set_value("Price List", self.price_list, "enabled", 0)

		with self.assertRaisesRegex(frappe.ValidationError, "disabled"):
			catalog_push.validate_push_account(self.account.name)

	def test_rejects_currency_mismatch(self):
		with patch(EXECUTE_TARGET, side_effect=_execute_dispatcher(currency="EGP")):
			with self.assertRaisesRegex(frappe.ValidationError, "currency"):
				catalog_push.validate_push_account(self.account.name)

	def test_accepts_matching_currency(self):
		with patch(EXECUTE_TARGET, side_effect=_execute_dispatcher(currency="USD")):
			account_doc, _connection, price_list = catalog_push.validate_push_account(self.account.name)

		self.assertEqual(account_doc.name, self.account.name)
		self.assertEqual(price_list.name, self.price_list)


class UnitTestGroupByProduct(IntegrationTestCase):
	def test_splits_valid_and_invalid_identities(self):
		valid = frappe._dict(
			name="MPM-1",
			external_reference="gid://shopify/ProductVariant/1",
			external_product_id="gid://shopify/Product/1",
		)
		bad_variant = frappe._dict(
			name="MPM-2", external_reference="not-a-gid", external_product_id="gid://shopify/Product/1"
		)
		bad_product = frappe._dict(
			name="MPM-3", external_reference="gid://shopify/ProductVariant/3", external_product_id=""
		)

		groups, local_errors = catalog_push.group_by_product([valid, bad_variant, bad_product])

		self.assertEqual(list(groups.keys()), ["gid://shopify/Product/1"])
		self.assertEqual(groups["gid://shopify/Product/1"], [valid])
		self.assertEqual({row[0].name for row in local_errors}, {"MPM-2", "MPM-3"})


class IntegrationTestPushCatalog(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.price_list = (
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": f"Push Catalog Price List {self.suffix}",
					"currency": "USD",
					"selling": 1,
					"enabled": 1,
				}
			)
			.insert()
			.name
		)
		self.account = make_shopify_account(f"Push Catalog Account {self.suffix}")
		self.account.db_set("erpnext_selling_price_list", self.price_list, update_modified=False)
		self.account.reload()
		self.item = make_item(f"Push Catalog Item {self.suffix}")

	def _make_price(self, rate):
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"price_list": self.price_list,
				"item_code": self.item,
				"uom": "Nos",
				"price_list_rate": rate,
				"selling": 1,
			}
		).insert()

	def _make_mapping(self, variant_id, product_id):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": self.item,
				"external_reference": variant_id,
				"external_variant_id": variant_id,
				"external_product_id": product_id,
			}
		).insert()

	def test_confirmed_variant_is_marked_synced_with_current_price(self):
		self._make_price(25)
		mapping = self._make_mapping("gid://shopify/ProductVariant/1", "gid://shopify/Product/1")

		with patch(
			EXECUTE_TARGET,
			side_effect=_execute_dispatcher(
				bulk_response={"productVariants": [{"id": "gid://shopify/ProductVariant/1"}], "userErrors": []}
			),
		):
			catalog_push.push_catalog("job-1", "Administrator", self.account.name, [mapping.name])

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Synced")
		# last_sync_reference is deliberately left untouched - the product GID
		# already lives on external_product_id, writing it again here would
		# just duplicate data already on the row (review finding #5).
		self.assertFalse(mapping.last_sync_reference)
		self.assertFalse(mapping.last_error)

	def test_variant_missing_from_response_is_marked_error_with_user_error_message(self):
		self._make_price(25)
		mapping = self._make_mapping("gid://shopify/ProductVariant/2", "gid://shopify/Product/2")

		with patch(
			EXECUTE_TARGET,
			side_effect=_execute_dispatcher(
				bulk_response={
					"productVariants": [],
					"userErrors": [{"field": ["variants", 0, "price"], "message": "Price is invalid."}],
				}
			),
		):
			catalog_push.push_catalog("job-2", "Administrator", self.account.name, [mapping.name])

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Error")
		self.assertIn("Price is invalid.", mapping.last_error)

	def test_missing_item_price_is_a_local_error_and_never_calls_shopify(self):
		mapping = self._make_mapping("gid://shopify/ProductVariant/3", "gid://shopify/Product/3")

		# The account-level currency check (a read-only Shopify call, made
		# during validate_push_account()) is unavoidable and allowed - what
		# this test actually proves is that the productVariantsBulkUpdate
		# mutation itself is never reached for a mapping with no resolvable
		# price. _currency_only_dispatcher() raises if anything else is asked
		# of ShopifyClient.execute().
		with patch(EXECUTE_TARGET, side_effect=_currency_only_dispatcher()):
			catalog_push.push_catalog("job-3", "Administrator", self.account.name, [mapping.name])

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Error")
		self.assertTrue(mapping.last_error)

	def test_malformed_identity_is_a_local_error_and_never_calls_shopify(self):
		self._make_price(25)
		mapping = self._make_mapping("not-a-variant-gid", "gid://shopify/Product/4")

		with patch(EXECUTE_TARGET, side_effect=_currency_only_dispatcher()):
			catalog_push.push_catalog("job-4", "Administrator", self.account.name, [mapping.name])

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Error")
		self.assertIn("valid Shopify product variant GID", mapping.last_error)

	def test_retrying_a_confirmed_success_stays_an_update_never_a_create(self):
		"""#106's whole idempotency mechanism: every push is
		productVariantsBulkUpdate keyed on an existing GID, never a create
		call - re-running a successful push must remain safe and identical in
		shape."""
		self._make_price(30)
		mapping = self._make_mapping("gid://shopify/ProductVariant/5", "gid://shopify/Product/5")
		dispatcher = _execute_dispatcher(
			bulk_response={"productVariants": [{"id": "gid://shopify/ProductVariant/5"}], "userErrors": []}
		)

		with patch(EXECUTE_TARGET, side_effect=dispatcher) as execute:
			catalog_push.push_catalog("job-5a", "Administrator", self.account.name, [mapping.name])
			catalog_push.push_catalog("job-5b", "Administrator", self.account.name, [mapping.name])

		for call in execute.call_args_list:
			query = call.args[0]
			self.assertNotIn("productCreate", query)
			self.assertNotIn("productSet", query)

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Synced")

	def test_transport_failure_marks_the_whole_group_error(self):
		self._make_price(10)
		mapping_a = self._make_mapping("gid://shopify/ProductVariant/6", "gid://shopify/Product/6")
		mapping_b = self._make_mapping("gid://shopify/ProductVariant/7", "gid://shopify/Product/6")

		def _raise_on_bulk_update(query, variables=None):
			if "currencyCode" in query:
				return {"shop": {"currencyCode": "USD"}}
			raise frappe.ValidationError("Shopify GraphQL request was throttled")

		with patch(EXECUTE_TARGET, side_effect=_raise_on_bulk_update):
			catalog_push.push_catalog(
				"job-6", "Administrator", self.account.name, [mapping_a.name, mapping_b.name]
			)

		mapping_a.reload()
		mapping_b.reload()
		self.assertEqual(mapping_a.sync_status, "Error")
		self.assertEqual(mapping_b.sync_status, "Error")

	def test_disabled_mapping_is_skipped_not_errored_and_never_calls_shopify(self):
		"""Review finding #4: a disabled mapping is excluded from the push
		entirely - reported as skipped, not attempted, not Error."""
		self._make_price(15)
		mapping = self._make_mapping("gid://shopify/ProductVariant/8", "gid://shopify/Product/8")
		mapping.db_set("enabled", 0, update_modified=False)

		with patch(EXECUTE_TARGET, side_effect=_currency_only_dispatcher()):
			catalog_push.push_catalog("job-7", "Administrator", self.account.name, [mapping.name])

		mapping.reload()
		# Nothing was attempted, so sync_status is untouched (still whatever
		# the freshly-inserted mapping's default was).
		self.assertNotEqual(mapping.sync_status, "Error")

	def test_mapping_with_disabled_item_is_skipped_not_errored(self):
		"""Same as above, but for Talabat's other half of the active
		semantics: bool(mapping.enabled) and not bool(item.disabled)."""
		self._make_price(15)
		mapping = self._make_mapping("gid://shopify/ProductVariant/9", "gid://shopify/Product/9")
		frappe.db.set_value("Item", self.item, "disabled", 1)

		with patch(EXECUTE_TARGET, side_effect=_currency_only_dispatcher()):
			catalog_push.push_catalog("job-8", "Administrator", self.account.name, [mapping.name])

		mapping.reload()
		self.assertNotEqual(mapping.sync_status, "Error")

	def test_group_with_more_variants_than_chunk_size_issues_multiple_mutation_calls(self):
		"""Review finding #6: a product group larger than
		SHOPIFY_CATALOG_PUSH_VARIANT_CHUNK_SIZE is split into multiple
		productVariantsBulkUpdate calls, all against the same product."""
		self._make_price(20)
		chunk_size = catalog_push.SHOPIFY_CATALOG_PUSH_VARIANT_CHUNK_SIZE
		variant_count = chunk_size + 3
		mapping_names = []
		for index in range(variant_count):
			mapping = self._make_mapping(
				f"gid://shopify/ProductVariant/{1000 + index}", "gid://shopify/Product/1000"
			)
			mapping_names.append(mapping.name)

		bulk_update_calls = []

		def _side_effect(query, variables=None):
			if "currencyCode" in query:
				return {"shop": {"currencyCode": "USD"}}
			if "productVariantsBulkUpdate" in query:
				bulk_update_calls.append(variables)
				returned = [{"id": variant["id"]} for variant in variables["variants"]]
				return {"productVariantsBulkUpdate": {"productVariants": returned, "userErrors": []}}
			raise AssertionError(f"Unexpected query in test: {query}")

		with patch(EXECUTE_TARGET, side_effect=_side_effect):
			catalog_push.push_catalog("job-9", "Administrator", self.account.name, mapping_names)

		self.assertEqual(len(bulk_update_calls), 2)
		self.assertEqual(len(bulk_update_calls[0]["variants"]), chunk_size)
		self.assertEqual(len(bulk_update_calls[1]["variants"]), 3)
		for call_variables in bulk_update_calls:
			self.assertEqual(call_variables["productId"], "gid://shopify/Product/1000")

		for mapping_name in mapping_names:
			self.assertEqual(frappe.db.get_value(catalog_push.MAPPING_DOCTYPE, mapping_name, "sync_status"), "Synced")


class IntegrationTestQueueCatalogPush(IntegrationTestCase):
	"""Mirrors integrations/shopify/test_products.py's
	IntegrationTestQueueCatalogSweep - same dedup-rejection shape, adapted for
	one job PER ACCOUNT rather than a single account-scoped sweep job."""

	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.price_list = (
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": f"Queue Push Price List {self.suffix}",
					"currency": "USD",
					"selling": 1,
					"enabled": 1,
				}
			)
			.insert()
			.name
		)
		self.account = make_shopify_account(f"Queue Push Account {self.suffix}")
		self.account.db_set("erpnext_selling_price_list", self.price_list, update_modified=False)
		self.account.reload()
		self.item = make_item(f"Queue Push Item {self.suffix}")
		self.mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": self.item,
				"external_reference": "gid://shopify/ProductVariant/100",
				"external_product_id": "gid://shopify/Product/100",
			}
		).insert()

	def test_groups_by_account_and_seeds_one_job_per_account(self):
		fake_job = object()
		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch("frappe.enqueue", return_value=fake_job) as mocked_enqueue,
		):
			result = catalog_push.queue_catalog_push([self.mapping.name])

		# The Desk request path must never call Shopify - only local
		# (non-network) account/connection/Price List checks run here
		# (review finding #1). The shop-currency check happens only inside
		# the worker.
		mocked_execute.assert_not_called()
		self.assertEqual(len(result["jobs"]), 1)
		self.assertEqual(result["jobs"][0]["account"], self.account.name)
		self.assertEqual(result["jobs"][0]["mapping_count"], 1)
		self.assertEqual(result["skipped"], [])
		mocked_enqueue.assert_called_once()
		self.assertEqual(mocked_enqueue.call_args.kwargs["account_name"], self.account.name)
		self.assertEqual(mocked_enqueue.call_args.kwargs["mapping_names"], [self.mapping.name])

	def test_dedup_rejection_reports_skipped_and_clears_queued_cache_entry(self):
		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch("frappe.enqueue", return_value=None) as mocked_enqueue,
		):
			result = catalog_push.queue_catalog_push([self.mapping.name])

		mocked_execute.assert_not_called()
		self.assertEqual(result["jobs"], [])
		self.assertEqual(len(result["skipped"]), 1)
		self.assertEqual(result["skipped"][0]["account"], self.account.name)

		job_id = mocked_enqueue.call_args.kwargs["job_id_for_result"]
		self.assertIsNone(frappe.cache().get_value(catalog_push._push_cache_key(job_id)))

	def test_dedup_rejection_for_one_account_does_not_block_another(self):
		"""Review finding #2: a mid-loop dedup rejection for one account must
		not discard the job id already enqueued for a different account in
		the same request."""
		other_account = make_shopify_account(f"Queue Push Other Account {self.suffix}")
		other_account.db_set("erpnext_selling_price_list", self.price_list, update_modified=False)
		other_account.reload()
		other_item = make_item(f"Queue Push Other Item {self.suffix}")
		other_mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": other_account.name,
				"item": other_item,
				"external_reference": "gid://shopify/ProductVariant/200",
				"external_product_id": "gid://shopify/Product/200",
			}
		).insert()

		fake_job = object()

		def _enqueue_side_effect(*args, **kwargs):
			if kwargs.get("account_name") == self.account.name:
				return None
			return fake_job

		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch("frappe.enqueue", side_effect=_enqueue_side_effect),
		):
			result = catalog_push.queue_catalog_push([self.mapping.name, other_mapping.name])

		mocked_execute.assert_not_called()
		self.assertEqual(len(result["jobs"]), 1)
		self.assertEqual(result["jobs"][0]["account"], other_account.name)
		self.assertEqual(len(result["skipped"]), 1)
		self.assertEqual(result["skipped"][0]["account"], self.account.name)
