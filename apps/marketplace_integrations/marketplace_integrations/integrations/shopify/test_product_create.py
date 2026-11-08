# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for integrations/shopify/product_create.py (#494).

Every test mocks ShopifyClient.execute() (never a real Shopify call) and
frappe.enqueue() (no background job is queued). The worker is exercised by
calling create_product() directly with the arguments the queue captured.
"""

import random
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify import product_create, products
from marketplace_integrations.integrations.shopify.test_products import make_item, make_shopify_account
from marketplace_integrations.tests.utils import make_role_user

EXECUTE_TARGET = "marketplace_integrations.integrations.shopify.client.ShopifyClient.execute"
ENQUEUE_TARGET = "frappe.enqueue"
UPSERT_MAPPING_TARGET = "marketplace_integrations.integrations.shopify.products.upsert_mapping"
ERROR_LOG_METHOD = "Shopify product create failed"


def _gid(kind, number):
	return f"gid://shopify/{kind}/{number}"


def _random_number():
	return random.randint(10**6, 10**9)


def _create_response(product_gid, variant_gid, user_errors=None):
	return {
		"productCreate": {
			"product": {"id": product_gid, "variants": {"nodes": [{"id": variant_gid}]}},
			"userErrors": user_errors or [],
		}
	}


def _sku_response(variant_gid, user_errors=None):
	return {
		"productVariantsBulkUpdate": {
			"productVariants": [{"id": variant_gid}],
			"userErrors": user_errors or [],
		}
	}


def _dispatcher(product_gid, variant_gid, create=None, sku=None):
	"""side_effect for ShopifyClient.execute(): answers the productCreate and
	productVariantsBulkUpdate mutations by query text. ``create``/``sku`` may
	be a response dict or an Exception instance to raise; None means the
	successful response for these GIDs."""

	def _side_effect(query, variables=None):
		if "productVariantsBulkUpdate" in query:
			if isinstance(sku, Exception):
				raise sku
			return sku if sku is not None else _sku_response(variant_gid)
		if "productCreate(" in query:
			if isinstance(create, Exception):
				raise create
			return create if create is not None else _create_response(product_gid, variant_gid)
		raise AssertionError(f"Unexpected query in test: {query}")

	return _side_effect


def _queue(account, item):
	"""Queue through the real queue_product_create() with frappe.enqueue mocked.
	Returns (result, enqueue kwargs) so a test can run the worker itself."""
	with patch(ENQUEUE_TARGET, return_value=object()) as mocked_enqueue:
		result = product_create.queue_product_create(account, item)
	return result, mocked_enqueue.call_args.kwargs


def _make_staff(testcase, label, roles):
	"""A staff user with Item read (so the Item check is not what refuses them)
	plus exactly ``roles``. Cleanup is registered by make_role_user()."""
	email, _role_name = make_role_user(testcase, label, {"Item": ["read"]})
	frappe.get_doc("User", email).add_roles(*roles)
	return email


class _ProductCreateFixtures(IntegrationTestCase):
	"""Shared setup/teardown: one Shopify account and one stock Item per test."""

	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.bundle_codes = []
		self.extra_items = []
		self.account = make_shopify_account(f"Product Create Account {self.suffix}")
		self.item = self._new_item("Product Create Item")

	def tearDown(self):
		for parent in self.bundle_codes:
			frappe.delete_doc("Product Bundle", parent, ignore_permissions=True, force=True)
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Shopify Connection", self.account.connection, ignore_permissions=True, force=True)
		for code in [self.item, *self.extra_items]:
			frappe.delete_doc("Item", code, ignore_permissions=True, force=True)

	def _new_item(self, label, stock=True):
		code = f"{label} {self.suffix}"
		make_item(code)
		if stock:
			frappe.db.set_value("Item", code, "is_stock_item", 1)
		self.extra_items.append(code)
		return code


class IntegrationTestQueueProductCreate(_ProductCreateFixtures):
	def test_queue_makes_no_shopify_call(self):
		with patch(EXECUTE_TARGET) as mocked_execute:
			result, _kwargs = _queue(self.account.name, self.item)

		mocked_execute.assert_not_called()
		self.assertEqual(result["status"], "queued")

	def test_enqueue_uses_fixed_rq_job_id_and_random_status_id(self):
		result, kwargs = _queue(self.account.name, self.item)

		self.assertEqual(kwargs["job_id"], product_create.job_id_for(self.account.name, self.item))
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["queue"], "long")
		self.assertEqual(result["job_id"], kwargs["job_id_for_result"])
		self.assertNotEqual(result["job_id"], kwargs["job_id"])

	def test_queued_status_is_seeded_for_owner_without_owner_key(self):
		result, kwargs = _queue(self.account.name, self.item)

		state = product_create.get_product_create_status(result["job_id"])

		self.assertEqual(state["status"], "queued")
		self.assertNotIn("owner", state)
		raw = frappe.cache().get_value(product_create._cache_key(result["job_id"]))
		self.assertEqual(raw["owner"], kwargs["owner"])

	def test_duplicate_queue_rejected_and_keeps_running_job_status(self):
		first, first_kwargs = _queue(self.account.name, self.item)
		product_create._update_status(first["job_id"], first_kwargs["owner"], status="running")
		staff = _make_staff(self, "Product Create Duplicate Staff", ["Shopify User"])

		with self.set_user(staff):
			with patch(ENQUEUE_TARGET, return_value=None) as mocked_enqueue:
				with self.assertRaisesRegex(frappe.ValidationError, "already running"):
					product_create.queue_product_create(self.account.name, self.item)

		second_kwargs = mocked_enqueue.call_args.kwargs
		# Same fixed RQ job id (that is what dedups), but a fresh random status id.
		self.assertEqual(second_kwargs["job_id"], first_kwargs["job_id"])
		self.assertNotEqual(second_kwargs["job_id_for_result"], first["job_id"])
		# The rejected call's own status entry is cleared, not left as "queued".
		self.assertIsNone(
			frappe.cache().get_value(product_create._cache_key(second_kwargs["job_id_for_result"]))
		)
		# The running job's status and owner are untouched.
		raw = frappe.cache().get_value(product_create._cache_key(first["job_id"]))
		self.assertEqual(raw["status"], "running")
		self.assertEqual(raw["owner"], first_kwargs["owner"])

	def test_status_is_owner_scoped(self):
		result, _kwargs = _queue(self.account.name, self.item)
		staff = _make_staff(self, "Product Create Status Staff", ["Shopify User"])

		with self.set_user(staff):
			with self.assertRaises(frappe.PermissionError):
				product_create.get_product_create_status(result["job_id"])

	def test_unknown_status_job_id_is_rejected(self):
		with self.assertRaises(frappe.PermissionError):
			product_create.get_product_create_status(frappe.generate_hash(length=16))

	def _assert_refused(self, account, item, message, exc=frappe.ValidationError):
		with patch(ENQUEUE_TARGET) as mocked_enqueue, patch(EXECUTE_TARGET) as mocked_execute:
			with self.assertRaisesRegex(exc, message):
				product_create.queue_product_create(account, item)
		mocked_enqueue.assert_not_called()
		mocked_execute.assert_not_called()

	def test_refuses_empty_item(self):
		self._assert_refused(self.account.name, "   ", "Item is required.")

	def test_refuses_item_that_does_not_exist(self):
		self._assert_refused(self.account.name, f"Missing Item {self.suffix}", "", frappe.DoesNotExistError)

	def test_refuses_template_item(self):
		item = self._new_item("Product Create Template")
		frappe.db.set_value("Item", item, "has_variants", 1)

		self._assert_refused(self.account.name, item, "is a template")

	def test_refuses_erpnext_variant(self):
		template = self._new_item("Product Create Variant Template")
		variant = self._new_item("Product Create Variant")
		frappe.db.set_value("Item", variant, "variant_of", template)

		self._assert_refused(self.account.name, variant, "is an Item variant")

	def test_refuses_product_bundle(self):
		parent = self._new_item("Product Create Bundle", stock=False)
		child = self._new_item("Product Create Bundle Child")
		frappe.get_doc(
			{
				"doctype": "Product Bundle",
				"new_item_code": parent,
				"items": [{"doctype": "Product Bundle Item", "item_code": child, "qty": 1}],
			}
		).insert()
		self.bundle_codes.append(parent)

		self._assert_refused(self.account.name, parent, "is a Product Bundle")

	def test_refuses_non_stock_item(self):
		item = self._new_item("Product Create Non Stock", stock=False)

		self._assert_refused(self.account.name, item, "is not a stock Item")

	def test_refuses_disabled_item(self):
		frappe.db.set_value("Item", self.item, "disabled", 1)

		self._assert_refused(self.account.name, self.item, "is disabled")

	def test_refuses_non_shopify_account(self):
		self.account.db_set("platform", "Talabat", update_modified=False)

		self._assert_refused(self.account.name, self.item, "not a Shopify account")

	def test_refuses_disabled_account(self):
		frappe.db.set_value("Marketplace Account", self.account.name, "enabled", 0)

		self._assert_refused(self.account.name, self.item, "Marketplace Account .* is disabled")

	def test_refuses_disabled_connection(self):
		frappe.db.set_value("Shopify Connection", self.account.connection, "enabled", 0)

		self._assert_refused(self.account.name, self.item, "Shopify Connection .* is disabled")

	def test_refuses_already_mapped_item(self):
		products.upsert_mapping(
			self.account.name,
			_gid("ProductVariant", _random_number()),
			self.item,
		)

		self._assert_refused(self.account.name, self.item, "already mapped")


class IntegrationTestProductCreatePermissions(_ProductCreateFixtures):
	def test_shopify_user_may_queue(self):
		staff = _make_staff(self, "Product Create Shopify Staff", ["Shopify User"])

		with self.set_user(staff):
			with patch(ENQUEUE_TARGET, return_value=object()) as mocked_enqueue:
				with patch(EXECUTE_TARGET) as mocked_execute:
					result = product_create.queue_product_create(self.account.name, self.item)

		mocked_enqueue.assert_called_once()
		mocked_execute.assert_not_called()
		self.assertEqual(result["status"], "queued")

	def test_talabat_user_without_shopify_role_is_refused(self):
		staff = _make_staff(self, "Product Create Talabat Staff", ["Talabat User"])

		with self.set_user(staff):
			with patch(ENQUEUE_TARGET) as mocked_enqueue:
				with self.assertRaises(frappe.PermissionError):
					product_create.queue_product_create(self.account.name, self.item)

		mocked_enqueue.assert_not_called()

	def test_item_manager_without_platform_role_is_refused(self):
		staff = _make_staff(self, "Product Create Item Manager Staff", ["Item Manager"])

		with self.set_user(staff):
			with patch(ENQUEUE_TARGET) as mocked_enqueue:
				with self.assertRaises(frappe.PermissionError):
					product_create.queue_product_create(self.account.name, self.item)

		mocked_enqueue.assert_not_called()


class IntegrationTestCreateProductWorker(IntegrationTestCase):
	"""create_product() run directly. It commits (the mapping, then the status
	write), so the fixtures are committed here too and torn down by hand."""

	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.account = make_shopify_account(f"Product Create Worker Account {self.suffix}")
		self.item = f"Product Create Worker Item {self.suffix}"
		make_item(self.item)
		frappe.db.set_value("Item", self.item, "is_stock_item", 1)
		self.product_gid = _gid("Product", _random_number())
		self.variant_gid = _gid("ProductVariant", _random_number())
		# A mapping-write failure rolls the transaction back; committed fixtures survive it.
		frappe.db.commit()

	def tearDown(self):
		for name in frappe.get_all(
			"Error Log",
			filters={"method": ERROR_LOG_METHOD, "error": ["like", f"%{self.item}%"]},
			pluck="name",
		):
			frappe.delete_doc("Error Log", name, ignore_permissions=True, force=True)
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Shopify Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.delete_doc("Item", self.item, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _run(self, kwargs):
		product_create.create_product(
			job_id_for_result=kwargs["job_id_for_result"],
			owner=kwargs["owner"],
			account=kwargs["account"],
			item=kwargs["item"],
		)

	def _queue_and_run_with(self, execute_side_effect):
		result, kwargs = _queue(self.account.name, self.item)
		with patch(EXECUTE_TARGET, side_effect=execute_side_effect) as mocked_execute:
			self._run(kwargs)
		return product_create.get_product_create_status(result["job_id"]), mocked_execute

	def _mapping_rows(self):
		return frappe.get_all(
			"Marketplace Product Mapping",
			filters={"marketplace_account": self.account.name, "item": self.item},
			fields=[
				"name",
				"enabled",
				"external_reference",
				"external_variant_id",
				"external_product_id",
				"external_sku",
			],
		)

	def _error_log_texts(self):
		return frappe.get_all(
			"Error Log",
			filters={"method": ERROR_LOG_METHOD, "error": ["like", f"%{self.item}%"]},
			pluck="error",
		)

	def _responses(self, create=None, sku=None):
		return _dispatcher(self.product_gid, self.variant_gid, create=create, sku=sku)

	def test_success_creates_one_enabled_mapping_and_sets_sku(self):
		state, mocked_execute = self._queue_and_run_with(self._responses())

		self.assertEqual(state["status"], "completed")
		rows = self._mapping_rows()
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row.enabled, 1)
		self.assertEqual(row.external_reference, self.variant_gid)
		self.assertEqual(row.external_variant_id, self.variant_gid)
		self.assertEqual(row.external_product_id, self.product_gid)
		self.assertEqual(row.external_sku, self.item)
		self.assertEqual(state["result"]["mapping"], row.name)
		self.assertEqual(state["result"]["product_id"], self.product_gid)
		self.assertEqual(state["result"]["variant_id"], self.variant_gid)

		# Create first (DRAFT, title = item name), then the SKU mutation with inventoryItem.sku = item_code.
		create_call, sku_call = mocked_execute.call_args_list
		self.assertIn("productCreate(", create_call.args[0])
		self.assertEqual(create_call.args[1], {"product": {"title": self.item, "status": "DRAFT"}})
		self.assertIn("productVariantsBulkUpdate", sku_call.args[0])
		self.assertEqual(
			sku_call.args[1],
			{
				"productId": self.product_gid,
				"variants": [{"id": self.variant_gid, "inventoryItem": {"sku": self.item}}],
			},
		)

	def test_product_create_user_errors_leave_no_mapping_and_show_message(self):
		create = {
			"productCreate": {
				"product": None,
				"userErrors": [{"field": ["title"], "message": "Title is too long"}],
			}
		}

		state, mocked_execute = self._queue_and_run_with(self._responses(create=create))

		self.assertEqual(state["status"], "failed")
		self.assertIn("Title is too long", state["error"])
		self.assertEqual(self._mapping_rows(), [])
		self.assertEqual(mocked_execute.call_count, 1)
		logs = self._error_log_texts()
		self.assertEqual(len(logs), 1)
		self.assertIn("Title is too long", logs[0])

	def test_product_create_exception_leaves_no_mapping_and_shows_message(self):
		state, mocked_execute = self._queue_and_run_with(self._responses(create=Exception("socket timeout")))

		self.assertEqual(state["status"], "failed")
		self.assertIn("socket timeout", state["error"])
		self.assertIn("check Shopify", state["error"])
		self.assertEqual(self._mapping_rows(), [])
		self.assertEqual(mocked_execute.call_count, 1)
		self.assertEqual(len(self._error_log_texts()), 1)

	def test_missing_ids_in_response_leaves_no_mapping(self):
		create = {
			"productCreate": {
				"product": {"id": self.product_gid, "variants": {"nodes": []}},
				"userErrors": [],
			}
		}

		state, mocked_execute = self._queue_and_run_with(self._responses(create=create))

		self.assertEqual(state["status"], "failed")
		self.assertIn("did not include the product and variant ids", state["error"])
		self.assertEqual(self._mapping_rows(), [])
		self.assertEqual(mocked_execute.call_count, 1)

	def test_sku_user_errors_report_both_gids_and_log(self):
		sku = {
			"productVariantsBulkUpdate": {
				"productVariants": [],
				"userErrors": [{"field": ["variants"], "message": "SKU already taken"}],
			}
		}

		state, _mocked_execute = self._queue_and_run_with(self._responses(sku=sku))

		self.assertEqual(state["status"], "failed")
		self.assertIn(self.product_gid, state["error"])
		self.assertIn(self.variant_gid, state["error"])
		self.assertIn("SKU already taken", state["error"])
		self.assertEqual(self._mapping_rows(), [])
		logs = self._error_log_texts()
		self.assertEqual(len(logs), 1)
		self.assertIn(self.product_gid, logs[0])
		self.assertIn(self.variant_gid, logs[0])

	def test_sku_exception_reports_both_gids_and_logs(self):
		state, _mocked_execute = self._queue_and_run_with(self._responses(sku=Exception("sku boom")))

		self.assertEqual(state["status"], "failed")
		self.assertIn(self.product_gid, state["error"])
		self.assertIn(self.variant_gid, state["error"])
		self.assertIn("sku boom", state["error"])
		self.assertEqual(self._mapping_rows(), [])
		logs = self._error_log_texts()
		self.assertEqual(len(logs), 1)
		self.assertIn(self.variant_gid, logs[0])

	def test_sku_not_confirmed_when_variant_missing_from_response(self):
		sku = {"productVariantsBulkUpdate": {"productVariants": [], "userErrors": []}}

		state, _mocked_execute = self._queue_and_run_with(self._responses(sku=sku))

		self.assertEqual(state["status"], "failed")
		self.assertIn("did not confirm the SKU update", state["error"])
		self.assertIn(self.product_gid, state["error"])
		self.assertEqual(self._mapping_rows(), [])

	def test_mapping_write_failure_reports_both_gids_and_logs(self):
		result, kwargs = _queue(self.account.name, self.item)

		with patch(EXECUTE_TARGET, side_effect=self._responses()):
			with patch(UPSERT_MAPPING_TARGET, side_effect=frappe.ValidationError("Duplicate entry")):
				self._run(kwargs)

		state = product_create.get_product_create_status(result["job_id"])
		self.assertEqual(state["status"], "failed")
		self.assertIn("saving the mapping failed", state["error"])
		self.assertIn(self.product_gid, state["error"])
		self.assertIn(self.variant_gid, state["error"])
		self.assertEqual(self._mapping_rows(), [])
		logs = self._error_log_texts()
		self.assertEqual(len(logs), 1)
		self.assertIn(self.product_gid, logs[0])
		self.assertIn(self.variant_gid, logs[0])

	def test_worker_refuses_when_item_mapped_after_queue(self):
		result, kwargs = _queue(self.account.name, self.item)
		products.upsert_mapping(self.account.name, _gid("ProductVariant", _random_number()), self.item)

		with patch(EXECUTE_TARGET) as mocked_execute:
			self._run(kwargs)

		state = product_create.get_product_create_status(result["job_id"])
		self.assertEqual(state["status"], "failed")
		self.assertIn("already mapped", state["error"])
		mocked_execute.assert_not_called()

	def test_worker_refuses_when_account_disabled_after_queue(self):
		result, kwargs = _queue(self.account.name, self.item)
		frappe.db.set_value("Marketplace Account", self.account.name, "enabled", 0)

		with patch(EXECUTE_TARGET) as mocked_execute:
			self._run(kwargs)

		state = product_create.get_product_create_status(result["job_id"])
		self.assertEqual(state["status"], "failed")
		self.assertIn("is disabled", state["error"])
		mocked_execute.assert_not_called()
