# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Every test here mocks ShopifyClient.execute() directly - never a real
Shopify call - mirroring integrations/shopify/test_products.py's pattern.
"""

from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

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

	def tearDown(self):
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Shopify Connection", self.account.connection, ignore_permissions=True, force=True)

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

	def tearDown(self):
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Shopify Connection", self.account.connection, ignore_permissions=True, force=True)
		# push_catalog() commits per group, so this teardown must commit too
		# or the class-level rollback would resurrect the committed rows.
		frappe.db.commit()

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
				bulk_response={
					"productVariants": [{"id": "gid://shopify/ProductVariant/1"}],
					"userErrors": [],
				}
			),
		):
			catalog_push.push_catalog("job-1", "Administrator", self.account.name, [mapping.name], manual=True)

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
			catalog_push.push_catalog("job-2", "Administrator", self.account.name, [mapping.name], manual=True)

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
			catalog_push.push_catalog("job-3", "Administrator", self.account.name, [mapping.name], manual=True)

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Error")
		self.assertTrue(mapping.last_error)

	def test_malformed_identity_is_a_local_error_and_never_calls_shopify(self):
		self._make_price(25)
		mapping = self._make_mapping("not-a-variant-gid", "gid://shopify/Product/4")

		with patch(EXECUTE_TARGET, side_effect=_currency_only_dispatcher()):
			catalog_push.push_catalog("job-4", "Administrator", self.account.name, [mapping.name], manual=True)

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
			catalog_push.push_catalog("job-5a", "Administrator", self.account.name, [mapping.name], manual=True)
			catalog_push.push_catalog("job-5b", "Administrator", self.account.name, [mapping.name], manual=True)

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
				"job-6", "Administrator", self.account.name, [mapping_a.name, mapping_b.name], manual=True
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
			catalog_push.push_catalog("job-7", "Administrator", self.account.name, [mapping.name], manual=True)

		mapping.reload()
		# Nothing was attempted, so sync_status is untouched (still whatever
		# the freshly-inserted mapping's default was).
		self.assertNotEqual(mapping.sync_status, "Error")
		# Strengthened per PR #119 review: assert the mapping actually shows up
		# in the result's skipped-mappings accounting, not just that its
		# status isn't Error - a regression that silently dropped the mapping
		# entirely would still pass the weaker assertion above but would be
		# invisible in the actual dialog UI.
		status = catalog_push.get_push_status("job-7")
		skipped_rows = [row for row in status["result"]["results"] if row["mapping_name"] == mapping.name]
		self.assertEqual(len(skipped_rows), 1)
		self.assertTrue(skipped_rows[0]["skipped"])
		self.assertEqual(status["result"]["skipped_mappings"], 1)

	def test_mapping_with_disabled_item_is_skipped_not_errored(self):
		"""Same as above, but for Talabat's other half of the active
		semantics: bool(mapping.enabled) and not bool(item.disabled)."""
		self._make_price(15)
		mapping = self._make_mapping("gid://shopify/ProductVariant/9", "gid://shopify/Product/9")
		frappe.db.set_value("Item", self.item, "disabled", 1)

		with patch(EXECUTE_TARGET, side_effect=_currency_only_dispatcher()):
			catalog_push.push_catalog("job-8", "Administrator", self.account.name, [mapping.name], manual=True)

		mapping.reload()
		self.assertNotEqual(mapping.sync_status, "Error")
		status = catalog_push.get_push_status("job-8")
		skipped_rows = [row for row in status["result"]["results"] if row["mapping_name"] == mapping.name]
		self.assertEqual(len(skipped_rows), 1)
		self.assertTrue(skipped_rows[0]["skipped"])
		self.assertEqual(status["result"]["skipped_mappings"], 1)

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
			catalog_push.push_catalog("job-9", "Administrator", self.account.name, mapping_names, manual=True)

		self.assertEqual(len(bulk_update_calls), 2)
		self.assertEqual(len(bulk_update_calls[0]["variants"]), chunk_size)
		self.assertEqual(len(bulk_update_calls[1]["variants"]), 3)
		for call_variables in bulk_update_calls:
			self.assertEqual(call_variables["productId"], "gid://shopify/Product/1000")

		for mapping_name in mapping_names:
			self.assertEqual(
				frappe.db.get_value(catalog_push.MAPPING_DOCTYPE, mapping_name, "sync_status"), "Synced"
			)

	def test_pushed_variables_carry_the_correct_resolved_price_and_allow_partial_updates(self):
		"""Review finding #4: assert the exact price string placed into the
		mutation's own variables, and that allowPartialUpdates: True is
		present - not just sync_status/last_error as the tests above check.
		Includes a distractor Item Price on a DIFFERENT Price List, to prove
		the pushed price is the account's own configured Price List's price,
		not the distractor's."""
		other_price_list = (
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": f"Distractor Price List {self.suffix}",
					"currency": "USD",
					"selling": 1,
					"enabled": 1,
				}
			)
			.insert()
			.name
		)
		# Distractor: same Item, correct uom, WRONG Price List.
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"price_list": other_price_list,
				"item_code": self.item,
				"uom": "Nos",
				"price_list_rate": 999.99,
				"selling": 1,
			}
		).insert()
		# The correct price: this account's own Price List, at the Item's
		# stock UOM ("Nos" - see test_products.py's make_item()).
		self._make_price(19.99)
		mapping = self._make_mapping("gid://shopify/ProductVariant/50", "gid://shopify/Product/50")

		captured_variables = []

		def _side_effect(query, variables=None):
			if "currencyCode" in query:
				return {"shop": {"currencyCode": "USD"}}
			if "productVariantsBulkUpdate" in query:
				captured_variables.append(variables)
				returned = [{"id": variant["id"]} for variant in variables["variants"]]
				return {"productVariantsBulkUpdate": {"productVariants": returned, "userErrors": []}}
			raise AssertionError(f"Unexpected query in test: {query}")

		with patch(EXECUTE_TARGET, side_effect=_side_effect):
			catalog_push.push_catalog("job-price", "Administrator", self.account.name, [mapping.name], manual=True)

		self.assertEqual(len(captured_variables), 1)
		variables = captured_variables[0]
		self.assertIs(variables["allowPartialUpdates"], True)
		self.assertEqual(len(variables["variants"]), 1)
		self.assertEqual(variables["variants"][0]["id"], "gid://shopify/ProductVariant/50")
		self.assertEqual(variables["variants"][0]["price"], "19.99")

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Synced")


class UnitTestFormatMoney(UnitTestCase):
	"""Direct coverage for _format_money() (review finding #4) - see
	docs/frappe-conventions.md's Decimal-formatting-for-external-payloads
	entry for why str(Decimal(...)) is unsafe for a very small/precisely
	scaled value."""

	def test_plain_values_render_as_fixed_point(self):
		self.assertEqual(catalog_push._format_money(Decimal("19.99")), "19.99")
		self.assertEqual(catalog_push._format_money(Decimal("100")), "100")
		self.assertEqual(catalog_push._format_money(Decimal("0")), "0")

	def test_very_small_value_never_renders_as_scientific_notation(self):
		# Decimal("0.0000001")'s default str() form is "1E-7" - exactly the
		# footgun _format_money() exists to avoid.
		formatted = catalog_push._format_money(Decimal("0.0000001"))
		self.assertEqual(formatted, "0.0000001")
		self.assertNotIn("E", formatted.upper())

	def test_large_precisely_scaled_value_never_renders_as_scientific_notation(self):
		formatted = catalog_push._format_money(Decimal("123456789.123456789"))
		self.assertNotIn("E", formatted.upper())
		self.assertEqual(formatted, "123456789.123456789")


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

	def tearDown(self):
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Shopify Connection", self.account.connection, ignore_permissions=True, force=True)

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
		self.assertEqual(result["skipped_accounts"], [])
		mocked_enqueue.assert_called_once()
		self.assertEqual(mocked_enqueue.call_args.kwargs["account_name"], self.account.name)
		self.assertEqual(mocked_enqueue.call_args.kwargs["mapping_names"], [self.mapping.name])
		# #516: the by-mapping Desk path is a manual push.
		self.assertIs(mocked_enqueue.call_args.kwargs["manual"], True)

	def test_dedup_rejection_reports_skipped_and_clears_queued_cache_entry(self):
		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch("frappe.enqueue", return_value=None) as mocked_enqueue,
		):
			result = catalog_push.queue_catalog_push([self.mapping.name])

		mocked_execute.assert_not_called()
		self.assertEqual(result["jobs"], [])
		self.assertEqual(len(result["skipped_accounts"]), 1)
		self.assertEqual(result["skipped_accounts"][0]["account"], self.account.name)

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
		self.assertEqual(len(result["skipped_accounts"]), 1)
		self.assertEqual(result["skipped_accounts"][0]["account"], self.account.name)

	def test_misconfigured_account_is_skipped_but_does_not_block_a_valid_account(self):
		"""Review finding #2: a per-account tolerant validation loop - one
		misconfigured account (here: no Selling Price List) must not prevent
		another, validly configured account in the same request from being
		queued. Previously a single misconfigured account's frappe.throw()
		aborted the whole request, queuing zero accounts."""
		self.account.db_set("erpnext_selling_price_list", None, update_modified=False)

		other_account = make_shopify_account(f"Queue Push Valid Account {self.suffix}")
		other_account.db_set("erpnext_selling_price_list", self.price_list, update_modified=False)
		other_account.reload()
		other_item = make_item(f"Queue Push Valid Item {self.suffix}")
		other_mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": other_account.name,
				"item": other_item,
				"external_reference": "gid://shopify/ProductVariant/300",
				"external_product_id": "gid://shopify/Product/300",
			}
		).insert()

		fake_job = object()
		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch("frappe.enqueue", return_value=fake_job) as mocked_enqueue,
		):
			result = catalog_push.queue_catalog_push([self.mapping.name, other_mapping.name])

		mocked_execute.assert_not_called()
		self.assertEqual(len(result["jobs"]), 1)
		self.assertEqual(result["jobs"][0]["account"], other_account.name)
		self.assertEqual(len(result["skipped_accounts"]), 1)
		self.assertEqual(result["skipped_accounts"][0]["account"], self.account.name)
		self.assertIn("Selling Price List", result["skipped_accounts"][0]["reason"])
		mocked_enqueue.assert_called_once()
		self.assertEqual(mocked_enqueue.call_args.kwargs["account_name"], other_account.name)


class IntegrationTestQueueAccountCatalogPush(IntegrationTestCase):
	"""#168 "Sync All" price leg: the account-level push selects every enabled
	mapping of one account and delegates to queue_catalog_push() unchanged."""

	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.price_list = (
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": f"Account Push Price List {self.suffix}",
					"currency": "USD",
					"selling": 1,
					"enabled": 1,
				}
			)
			.insert()
			.name
		)
		self.account = make_shopify_account(f"Account Push Account {self.suffix}")
		self.account.db_set("erpnext_selling_price_list", self.price_list, update_modified=False)
		self.account.reload()
		self.item = make_item(f"Account Push Item {self.suffix}")

	def tearDown(self):
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Shopify Connection", self.account.connection, ignore_permissions=True, force=True)

	def _make_mapping(self, variant_number, enabled=1):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": self.item,
				"external_reference": f"gid://shopify/ProductVariant/{variant_number}",
				"external_product_id": f"gid://shopify/Product/{variant_number}",
				"enabled": enabled,
			}
		).insert()

	def test_pushes_only_enabled_mappings_of_the_account(self):
		enabled = self._make_mapping(201)
		self._make_mapping(202, enabled=0)

		with patch("frappe.enqueue", return_value=object()) as mocked_enqueue:
			result = catalog_push.queue_account_catalog_push(self.account.name)

		self.assertEqual(len(result["jobs"]), 1)
		self.assertEqual(result["jobs"][0]["mapping_count"], 1)
		self.assertEqual(mocked_enqueue.call_args.kwargs["mapping_names"], [enabled.name])

	def test_account_push_never_loads_mapping_documents_one_by_one(self):
		self._make_mapping(203)
		self._make_mapping(204)
		real_get_doc = frappe.get_doc
		loaded_mappings = []

		def spy_get_doc(*args, **kwargs):
			if args and args[0] == "Marketplace Product Mapping":
				loaded_mappings.append(args)
			return real_get_doc(*args, **kwargs)

		with (
			patch("frappe.enqueue", return_value=object()),
			patch.object(frappe, "get_doc", side_effect=spy_get_doc),
		):
			result = catalog_push.queue_account_catalog_push(self.account.name)

		self.assertEqual(result["jobs"][0]["mapping_count"], 2)
		self.assertEqual(loaded_mappings, [])

	def test_account_without_mappings_queues_nothing(self):
		with patch("frappe.enqueue") as mocked_enqueue:
			result = catalog_push.queue_account_catalog_push(self.account.name)

		self.assertEqual(result, {"jobs": [], "skipped_accounts": []})
		mocked_enqueue.assert_not_called()

	def test_requires_write_permission_on_the_account(self):
		self._make_mapping(205)
		# Both doctypes are System-Manager-only, so any role-less user (Guest)
		# fails the account-level write check before anything is enqueued.
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")

		with patch("frappe.enqueue") as mocked_enqueue, self.assertRaises(frappe.PermissionError):
			catalog_push.queue_account_catalog_push(self.account.name)

		mocked_enqueue.assert_not_called()

	def test_requires_write_permission_on_marketplace_product_mapping(self):
		self._make_mapping(206)
		real_has_permission = frappe.has_permission

		def deny_mapping_write(doctype=None, ptype="read", *args, **kwargs):
			if doctype == "Marketplace Product Mapping" and ptype == "write":
				return False
			return real_has_permission(doctype, ptype, *args, **kwargs)

		# The account's own write check goes through frappe.permissions (not
		# frappe.has_permission), so only the explicit doctype-level mapping
		# check in queue_account_catalog_push() is denied here.
		with (
			patch.object(frappe, "has_permission", side_effect=deny_mapping_write),
			patch("frappe.enqueue") as mocked_enqueue,
			self.assertRaises(frappe.PermissionError),
		):
			catalog_push.queue_account_catalog_push(self.account.name)

		mocked_enqueue.assert_not_called()

	def test_manual_account_push_tells_the_worker_it_is_manual(self):
		self._make_mapping(207)

		with patch("frappe.enqueue", return_value=object()) as mocked_enqueue:
			catalog_push.queue_account_catalog_push(self.account.name)

		self.assertIs(mocked_enqueue.call_args.kwargs["manual"], True)


class IntegrationTestShopifyPricePushSwitch(IntegrationTestCase):
	"""#516: "Push Prices to Shopify" (shopify_price_push_enabled, default OFF) governs only the
	automatic hourly sweep; a manual push still runs. Every Shopify call is mocked."""

	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.price_list = (
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": f"Price Switch Price List {self.suffix}",
					"currency": "USD",
					"selling": 1,
					"enabled": 1,
				}
			)
			.insert()
			.name
		)
		self.account = make_shopify_account(f"Price Switch Account {self.suffix}")
		self.account.db_set("erpnext_selling_price_list", self.price_list, update_modified=False)
		self.account.reload()
		self.item = make_item(f"Price Switch Item {self.suffix}")
		self.item_price = (
			frappe.get_doc(
				{
					"doctype": "Item Price",
					"price_list": self.price_list,
					"item_code": self.item,
					"uom": "Nos",
					"price_list_rate": 25,
					"selling": 1,
				}
			)
			.insert()
			.name
		)

	def tearDown(self):
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Shopify Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.delete_doc("Item Price", self.item_price, ignore_permissions=True, force=True)
		frappe.delete_doc("Item", self.item, ignore_permissions=True, force=True)
		frappe.delete_doc("Price List", self.price_list, ignore_permissions=True, force=True)
		# push_catalog() commits per group, so this teardown must commit too.
		frappe.db.commit()

	def _make_mapping(self, number, enabled=1):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": self.item,
				"external_reference": f"gid://shopify/ProductVariant/{number}",
				"external_product_id": f"gid://shopify/Product/{number}",
				"enabled": enabled,
			}
		).insert()

	def _sweep(self):
		"""Run the sweep with enqueue and Shopify mocked; return this account's enqueue calls."""
		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue", return_value=object()) as enqueue:
			catalog_push.sweep_price_pushes()
		mocked_execute.assert_not_called()  # the sweep itself never calls Shopify
		return [c for c in enqueue.call_args_list if c.kwargs.get("account_name") == self.account.name]

	def test_switch_defaults_to_off_and_the_column_exists(self):
		self.assertEqual(frappe.get_meta("Marketplace Account").get_field("shopify_price_push_enabled").default, "0")
		self.assertEqual(self.account.shopify_price_push_enabled, 0)

	def test_sweep_is_registered_hourly(self):
		cron = frappe.get_hooks("scheduler_events")["cron"]
		self.assertIn(
			"marketplace_integrations.integrations.shopify.catalog_push.sweep_price_pushes", cron["25 * * * *"]
		)

	def test_sweep_skips_an_account_with_the_switch_off(self):
		self._make_mapping(501)
		self.assertEqual(self._sweep(), [])

	def test_sweep_queues_only_enabled_mappings_of_a_switched_on_account_as_automatic(self):
		enabled = self._make_mapping(502)
		self._make_mapping(503, enabled=0)
		self.account.db_set("shopify_price_push_enabled", 1, update_modified=False)

		calls = self._sweep()

		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0].kwargs["mapping_names"], [enabled.name])
		self.assertIs(calls[0].kwargs["manual"], False)

	def test_sweep_skips_a_disabled_account_and_an_account_without_enabled_mappings(self):
		self.account.db_set("shopify_price_push_enabled", 1, update_modified=False)
		self.assertEqual(self._sweep(), [])  # no mappings at all
		self._make_mapping(504, enabled=0)
		self.assertEqual(self._sweep(), [])  # only a disabled mapping
		self._make_mapping(505)
		self.account.db_set("enabled", 0, update_modified=False)
		self.assertEqual(self._sweep(), [])  # disabled account

	def test_sweep_isolates_one_accounts_failure(self):
		self._make_mapping(506)
		self.account.db_set("shopify_price_push_enabled", 1, update_modified=False)
		with (
			patch.object(catalog_push, "_enqueue_account_pushes", side_effect=RuntimeError("boom")),
			patch.object(frappe, "log_error") as log_error,
		):
			catalog_push.sweep_price_pushes()  # must not raise
		log_error.assert_called()

	def test_automatic_job_does_nothing_when_the_switch_is_off(self):
		mapping = self._make_mapping(507)

		with patch(EXECUTE_TARGET) as mocked_execute:
			outcome = catalog_push.push_catalog(
				"job-sw-1", "Administrator", self.account.name, [mapping.name], manual=False
			)

		mocked_execute.assert_not_called()
		self.assertEqual(outcome, {"paused": True})
		mapping.reload()
		self.assertEqual(mapping.sync_status, "Not Synced")
		state = frappe.cache().get_value(catalog_push._push_cache_key("job-sw-1"))
		self.assertEqual(state["status"], "failed")
		self.assertEqual(state["error"], "Price push is paused for this account.")

	def test_manual_job_pushes_with_the_switch_off(self):
		mapping = self._make_mapping(508)
		self.assertEqual(self.account.shopify_price_push_enabled, 0)

		with patch(
			EXECUTE_TARGET,
			side_effect=_execute_dispatcher(
				bulk_response={"productVariants": [{"id": mapping.external_reference}], "userErrors": []}
			),
		):
			catalog_push.push_catalog("job-sw-2", "Administrator", self.account.name, [mapping.name], manual=True)

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Synced")

	def test_automatic_job_pushes_when_the_switch_is_on(self):
		mapping = self._make_mapping(509)
		self.account.db_set("shopify_price_push_enabled", 1, update_modified=False)

		with patch(
			EXECUTE_TARGET,
			side_effect=_execute_dispatcher(
				bulk_response={"productVariants": [{"id": mapping.external_reference}], "userErrors": []}
			),
		):
			catalog_push.push_catalog(
				"job-sw-3", "Administrator", self.account.name, [mapping.name], manual=False
			)

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Synced")

	def test_automatic_job_with_a_currency_mismatch_writes_the_mapping_error_and_does_not_raise(self):
		mapping = self._make_mapping(510)
		self.account.db_set("shopify_price_push_enabled", 1, update_modified=False)

		with patch(EXECUTE_TARGET, side_effect=_currency_only_dispatcher(currency="EGP")):
			outcome = catalog_push.push_catalog(
				"job-sw-4", "Administrator", self.account.name, [mapping.name], manual=False
			)

		self.assertTrue(outcome["failed"])
		mapping.reload()
		self.assertEqual(mapping.sync_status, "Error")
		self.assertIn("currency", mapping.last_error)
		state = frappe.cache().get_value(catalog_push._push_cache_key("job-sw-4"))
		self.assertEqual(state["status"], "failed")
		self.assertIn("currency", state["error"])

	def test_manual_job_with_a_currency_mismatch_still_raises_to_the_user(self):
		mapping = self._make_mapping(513)

		with (
			patch(EXECUTE_TARGET, side_effect=_currency_only_dispatcher(currency="EGP")),
			self.assertRaisesRegex(frappe.ValidationError, "currency"),
		):
			catalog_push.push_catalog(
				"job-sw-6", "Administrator", self.account.name, [mapping.name], manual=True
			)

	def test_sweep_isolates_a_failing_account_and_still_runs_the_second(self):
		self._make_mapping(514)
		self.account.db_set("shopify_price_push_enabled", 1, update_modified=False)
		other = make_shopify_account(f"Price Switch Other Account {self.suffix}")
		other.db_set("erpnext_selling_price_list", self.price_list, update_modified=False)
		other.db_set("shopify_price_push_enabled", 1, update_modified=False)
		other_mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": other.name,
				"item": self.item,
				"external_reference": "gid://shopify/ProductVariant/515",
				"external_product_id": "gid://shopify/Product/515",
			}
		).insert()

		def _cleanup():
			frappe.db.delete("Marketplace Product Mapping", {"name": other_mapping.name})
			frappe.delete_doc("Marketplace Account", other.name, ignore_permissions=True, force=True)
			frappe.delete_doc("Shopify Connection", other.connection, ignore_permissions=True, force=True)

		self.addCleanup(_cleanup)
		seen = []

		def _enqueue(accounts, manual=False):
			name = next(iter(accounts))
			if name == self.account.name:
				raise RuntimeError("boom")
			seen.append((name, manual))
			return {"jobs": [], "skipped_accounts": []}

		with (
			patch.object(catalog_push, "_enqueue_account_pushes", side_effect=_enqueue),
			patch.object(frappe, "log_error") as log_error,
		):
			catalog_push.sweep_price_pushes()

		log_error.assert_called_once()
		self.assertEqual(seen, [(other.name, False)])

	def test_sweep_when_enqueue_is_deduplicated_does_not_raise_or_mark_the_mapping(self):
		mapping = self._make_mapping(516)
		self.account.db_set("shopify_price_push_enabled", 1, update_modified=False)

		with patch(EXECUTE_TARGET) as mocked_execute, patch("frappe.enqueue", return_value=None):
			catalog_push.sweep_price_pushes()  # must not raise

		mocked_execute.assert_not_called()
		mapping.reload()
		self.assertEqual(mapping.sync_status, "Not Synced")

	def test_sweep_skipping_a_misconfigured_account_writes_the_reason_on_its_mappings(self):
		mapping = self._make_mapping(517)
		self.account.db_set("shopify_price_push_enabled", 1, update_modified=False)
		self.account.db_set("erpnext_selling_price_list", None, update_modified=False)

		self.assertEqual(self._sweep(), [])

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Error")
		self.assertIn("Selling Price List", mapping.last_error)

	def test_desk_paths_pass_manual_true_and_the_default_is_automatic(self):
		mapping = self._make_mapping(518)
		with patch("frappe.enqueue", return_value=object()) as enqueue:
			catalog_push.queue_catalog_push([mapping.name])
			catalog_push.queue_account_catalog_push(self.account.name)
		self.assertEqual([c.kwargs["manual"] for c in enqueue.call_args_list], [True, True])

		with patch("frappe.enqueue", return_value=object()) as enqueue:
			catalog_push._enqueue_account_pushes(
				{self.account.name: {"account": self.account, "mapping_names": [mapping.name]}}
			)
		self.assertIs(enqueue.call_args.kwargs["manual"], False)

	def test_switch_turned_off_mid_job_stops_before_the_next_product_group(self):
		first = self._make_mapping(511)
		second = self._make_mapping(512)
		self.account.db_set("shopify_price_push_enabled", 1, update_modified=False)
		real_push_group = catalog_push._push_product_group

		def push_group_then_switch_off(*args, **kwargs):
			outcome = real_push_group(*args, **kwargs)
			frappe.db.set_value("Marketplace Account", self.account.name, "shopify_price_push_enabled", 0)
			return outcome

		with (
			patch(
				EXECUTE_TARGET,
				side_effect=_execute_dispatcher(
					bulk_response={
						"productVariants": [
							{"id": first.external_reference},
							{"id": second.external_reference},
						],
						"userErrors": [],
					}
				),
			),
			patch.object(catalog_push, "_push_product_group", side_effect=push_group_then_switch_off),
		):
			outcome = catalog_push.push_catalog(
				"job-sw-5", "Administrator", self.account.name, [first.name, second.name], manual=False
			)

		self.assertEqual(outcome, {"paused": True})
		first.reload()
		second.reload()
		self.assertEqual(first.sync_status, "Synced")
		self.assertEqual(second.sync_status, "Not Synced")
