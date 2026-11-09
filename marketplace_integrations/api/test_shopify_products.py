# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Thin API-boundary tests for api/shopify_products.py - the real business
logic and permission checks are exercised by
integrations/shopify/test_products.py; these just confirm the delegation and
whitelisting metadata.

Review finding #7's API-layer gap: the tests below (permission-rejection
class) call the REAL api.shopify_products.queue_catalog_sweep() (not a
patched products.queue_catalog_sweep) so an actually-unauthorized user is
actually rejected through this whitelisted boundary - the delegation tests
above only prove args are forwarded/whitelisting metadata is correct, never
that permission enforcement itself is reachable from here."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.shopify_products import (
	get_catalog_sweep_status,
	get_product_create_status,
	list_product_create_accounts,
	queue_catalog_sweep,
	queue_product_create,
	upsert_mapping,
)
from marketplace_integrations.integrations.shopify.test_products import make_shopify_account


class IntegrationTestShopifyProductsApi(IntegrationTestCase):
	def test_queue_catalog_sweep_delegates(self):
		with patch(
			"marketplace_integrations.integrations.shopify.products.queue_catalog_sweep",
			return_value={"job_id": "abc", "status": "queued"},
		) as mocked:
			result = queue_catalog_sweep("Some Account")

		mocked.assert_called_once_with("Some Account")
		self.assertEqual(result, {"job_id": "abc", "status": "queued"})

	def test_queue_catalog_sweep_post_only(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[queue_catalog_sweep], ["POST"])

	def test_upsert_mapping_post_only(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[upsert_mapping], ["POST"])

	def test_get_catalog_sweep_status_delegates(self):
		with patch(
			"marketplace_integrations.integrations.shopify.products.get_sweep_status",
			return_value={"status": "running", "processed": 5},
		) as mocked:
			result = get_catalog_sweep_status("job-1")

		mocked.assert_called_once_with("job-1")
		self.assertEqual(result, {"status": "running", "processed": 5})

	def test_upsert_mapping_delegates(self):
		with patch(
			"marketplace_integrations.integrations.shopify.products.upsert_mapping",
			return_value={"name": "MPM-00001"},
		) as mocked:
			result = upsert_mapping(
				"Some Account",
				"gid://shopify/ProductVariant/1",
				"Some Item",
				external_product_id="gid://shopify/Product/1",
				external_sku="SKU-1",
				external_barcode="BAR-1",
			)

		mocked.assert_called_once_with(
			"Some Account",
			"gid://shopify/ProductVariant/1",
			"Some Item",
			external_product_id="gid://shopify/Product/1",
			external_sku="SKU-1",
			external_barcode="BAR-1",
		)
		self.assertEqual(result, {"name": "MPM-00001"})

	def test_queue_product_create_delegates(self):
		with patch(
			"marketplace_integrations.integrations.shopify.product_create.queue_product_create",
			return_value={"job_id": "abc", "status": "queued"},
		) as mocked:
			result = queue_product_create("Some Account", "Some Item")

		mocked.assert_called_once_with("Some Account", "Some Item")
		self.assertEqual(result, {"job_id": "abc", "status": "queued"})

	def test_queue_product_create_post_only(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[queue_product_create], ["POST"])

	def test_list_product_create_accounts_delegates(self):
		with patch(
			"marketplace_integrations.integrations.shopify.product_create.list_product_create_accounts",
			return_value=[{"name": "A"}],
		) as mocked:
			result = list_product_create_accounts()

		mocked.assert_called_once_with()
		self.assertEqual(result, [{"name": "A"}])

	def test_get_product_create_status_delegates(self):
		with patch(
			"marketplace_integrations.integrations.shopify.product_create.get_product_create_status",
			return_value={"status": "running"},
		) as mocked:
			result = get_product_create_status("job-1")

		mocked.assert_called_once_with("job-1")
		self.assertEqual(result, {"status": "running"})


def _make_role_only_user(email_prefix):
	return frappe.get_doc(
		{
			"doctype": "User",
			"email": f"{email_prefix}-{frappe.generate_hash(length=10)}@example.com",
			"first_name": "Shopify Catalog Permission Test User",
			"send_welcome_email": 0,
			"roles": [{"role": "All"}],
		}
	).insert()


class IntegrationTestShopifyProductsApiPermissions(IntegrationTestCase):
	"""Exercises actual permission rejection through the real (unmocked)
	api.shopify_products.queue_catalog_sweep() boundary - see this module's
	docstring."""

	def setUp(self):
		self.account = make_shopify_account(f"Perm Test Account {frappe.generate_hash(length=8)}")

	def test_rejects_user_without_account_read_permission(self):
		# No share/role grants this user anything on the account - get_account()'s
		# per-document `read` check must reject before any enqueue happens.
		user = _make_role_only_user("shopify-sweep-no-read")

		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				queue_catalog_sweep(self.account.name)

	def test_rejects_user_without_mapping_write_permission(self):
		# Read on the account AND its Shopify Connection (both get_account()
		# checks satisfied), but no grant at all on Marketplace Product
		# Mapping - proves the SECOND check (doctype-level `write` on
		# Marketplace Product Mapping) is independently enforced, not just
		# get_account()'s read checks.
		user = _make_role_only_user("shopify-sweep-no-write")
		frappe.share.add("Marketplace Account", self.account.name, user.name, read=1)
		frappe.share.add("Shopify Connection", self.account.connection, user.name, read=1)

		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				queue_catalog_sweep(self.account.name)


class IntegrationTestExportCatalogApi(IntegrationTestCase):
	def test_export_catalog_delegates(self):
		from marketplace_integrations.api.shopify_products import export_catalog

		with patch("marketplace_integrations.integrations.shopify.products.export_catalog") as mocked:
			export_catalog("Some Account")

		mocked.assert_called_once_with("Some Account")
