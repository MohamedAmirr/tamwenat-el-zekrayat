# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#323: a platform user cannot drive another platform's account, order or mapping through the
whitelisted methods.

A Talabat User calls the Shopify and Noon methods that take an account, order or mapping name,
and a Noon User calls the Talabat ones. Every call must end in ``frappe.PermissionError`` raised by
the document-level check (the platform row filter of #322), not by a later platform or connection
validation. The fixtures are real Shopify, Noon and Talabat accounts so no earlier validation can
mask a missing permission check. The class deletes what it creates in ``tearDown`` because
``make_role_user`` commits.
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify.test_products import make_item, make_shopify_account
from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.test_marketplace_account import (
	STAND_IN_CONNECTION,
	STAND_IN_CONNECTION_DOCTYPE,
)
from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)
from marketplace_integrations.tests.utils import make_role_user

NOON_PEM = "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----"

API = "marketplace_integrations.api"

# (dotted method, kwargs). The kwargs values are fixture keys resolved per test: "SHOPIFY_ACCOUNT",
# "SHOPIFY_ORDER", "SHOPIFY_MAPPING", "NOON_*", "TALABAT_*" and "ITEM"; anything else is literal.
SHOPIFY_AND_NOON_CASES = (
	(f"{API}.shopify_catalog_push.queue_catalog_push", {"mapping_names": ["SHOPIFY_MAPPING"]}),
	(f"{API}.shopify_catalog_push.queue_account_catalog_push", {"account": "SHOPIFY_ACCOUNT"}),
	(f"{API}.shopify_inventory.queue_inventory_sync", {"mapping_names": ["SHOPIFY_MAPPING"]}),
	(f"{API}.shopify_inventory.queue_account_reconciliation", {"account": "SHOPIFY_ACCOUNT"}),
	(
		f"{API}.shopify_orders.queue_order_import",
		{"account": "SHOPIFY_ACCOUNT", "start": "2026-01-01", "end": "2026-01-02"},
	),
	(f"{API}.shopify_orders.refresh_order", {"marketplace_order": "SHOPIFY_ORDER"}),
	(f"{API}.shopify_products.queue_catalog_sweep", {"account": "SHOPIFY_ACCOUNT"}),
	(f"{API}.shopify_products.export_catalog", {"account": "SHOPIFY_ACCOUNT"}),
	(
		f"{API}.shopify_products.upsert_mapping",
		{
			"account": "SHOPIFY_ACCOUNT",
			"external_reference": "gid://shopify/ProductVariant/1",
			"item": "ITEM",
		},
	),
	(f"{API}.marketplace_orders.sync_to_erpnext", {"marketplace_order": "SHOPIFY_ORDER"}),
	(f"{API}.marketplace_orders.get_erpnext_sync_status", {"marketplace_order": "SHOPIFY_ORDER"}),
	(f"{API}.noon_orders.import_orders", {"account": "NOON_ACCOUNT"}),
	(f"{API}.noon_orders.refresh_order", {"marketplace_order": "NOON_ORDER"}),
	(f"{API}.noon_returns.lookup_return", {"account": "NOON_ACCOUNT", "barcode": "barcode-1"}),
	(f"{API}.noon_catalog.queue_account_sync", {"account": "NOON_ACCOUNT"}),
	(f"{API}.noon_catalog.get_unmapped_order_skus", {"account": "NOON_ACCOUNT"}),
	(
		f"{API}.noon_catalog.upsert_mapping",
		{"account": "NOON_ACCOUNT", "external_reference": "noon-sku-1", "item": "ITEM"},
	),
	(f"{API}.noon_pricing.queue_pricing_push", {"account": "NOON_ACCOUNT"}),
	(f"{API}.noon_inventory.queue_stock_push", {"account": "NOON_ACCOUNT"}),
	(f"{API}.noon_awbs.generate_awbs", {"account": "NOON_ACCOUNT", "qty": 1}),
	(f"{API}.noon_awbs.get_available_count", {"account": "NOON_ACCOUNT"}),
	(f"{API}.noon_fulfillment.get_available_actions", {"marketplace_order": "NOON_ORDER"}),
	(f"{API}.noon_fulfillment.release_stuck_oos_reservation", {"marketplace_order": "NOON_ORDER"}),
	(f"{API}.noon_fulfillment.retry_order_invoice", {"marketplace_order": "NOON_ORDER"}),
)

TALABAT_CASES = (
	(f"{API}.talabat_catalog.get_catalog_page", {"marketplace_account": "TALABAT_ACCOUNT"}),
	(f"{API}.talabat_catalog.get_mapped_items", {"marketplace_account": "TALABAT_ACCOUNT"}),
	(
		f"{API}.talabat_catalog.upsert_mapping",
		{"marketplace_account": "TALABAT_ACCOUNT", "external_reference": "talabat-sku-1", "item": "ITEM"},
	),
	(
		f"{API}.talabat_catalog.disable_mapping",
		{"marketplace_account": "TALABAT_ACCOUNT", "external_reference": "talabat-sku-1"},
	),
	(f"{API}.talabat_catalog.queue_catalog_sync", {"account": "TALABAT_ACCOUNT"}),
	(f"{API}.talabat_catalog.queue_mapping_catalog_sync", {"mapping": "TALABAT_MAPPING"}),
	(f"{API}.talabat_catalog.queue_mappings_catalog_sync", {"mapping_names": ["TALABAT_MAPPING"]}),
	(f"{API}.talabat_orders.refresh_order", {"marketplace_order": "TALABAT_ORDER"}),
	(
		f"{API}.talabat_orders.import_vendor_orders",
		{"account": "TALABAT_ACCOUNT", "start": "2026-01-01", "end": "2026-01-02"},
	),
	(f"{API}.talabat_orders.update_order", {"marketplace_order": "TALABAT_ORDER", "status": "ACCEPTED"}),
	(f"{API}.talabat_outlet.refresh_outlet_status", {"account": "TALABAT_ACCOUNT"}),
	(f"{API}.talabat_outlet.set_outlet_status", {"account": "TALABAT_ACCOUNT", "status": "OPEN"}),
	(f"{API}.marketplace_orders.sync_to_erpnext", {"marketplace_order": "TALABAT_ORDER"}),
)


@contextmanager
def as_user(user):
	previous = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(previous)


class IntegrationTestCrossPlatformDenial(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self._created = []
		self.fixtures = {"ITEM": make_item(f"Cross Platform Item {self.suffix}")}
		self._created.append(("Item", self.fixtures["ITEM"]))

		shopify = make_shopify_account(f"Cross Platform Shopify {self.suffix}")
		self._created.append(("Shopify Connection", shopify.connection))
		self._created.append(("Marketplace Account", shopify.name))
		self.fixtures["SHOPIFY_ACCOUNT"] = shopify.name

		noon = self.make_noon_account()
		self.fixtures["NOON_ACCOUNT"] = noon.name

		talabat = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Cross Platform Talabat {self.suffix}",
				"platform": "Talabat",
				"external_account_id": f"cross-platform-talabat-{self.suffix}",
				"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
				"connection": STAND_IN_CONNECTION,
			}
		).insert()
		self._created.append(("Marketplace Account", talabat.name))
		self.fixtures["TALABAT_ACCOUNT"] = talabat.name

		for prefix, account, platform in (
			("SHOPIFY", shopify, "Shopify"),
			("NOON", noon, "Noon"),
			("TALABAT", talabat, "Talabat"),
		):
			order = frappe.get_doc(
				{
					"doctype": "Marketplace Order",
					"marketplace_account": account.name,
					"platform": platform,
					"external_order_id": f"cross-platform-{platform.lower()}-{self.suffix}",
				}
			)
			order.flags.skip_sync_status_update = True
			order.order_identity_key = compute_order_identity_key(account.name, order.external_order_id)
			order.insert()
			self.fixtures[f"{prefix}_ORDER"] = order.name
			mapping = frappe.get_doc(
				{
					"doctype": "Marketplace Product Mapping",
					"marketplace_account": account.name,
					"item": self.fixtures["ITEM"],
					"external_reference": f"cross-platform-{platform.lower()}-{self.suffix}",
				}
			).insert()
			self.fixtures[f"{prefix}_MAPPING"] = mapping.name

	def tearDown(self):
		frappe.set_user("Administrator")
		accounts = [name for key, name in self.fixtures.items() if key.endswith("_ACCOUNT")]
		frappe.db.delete("Marketplace Sync Log", {"marketplace_account": ["in", accounts]})
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": ["in", accounts]})
		frappe.db.delete("Marketplace Order", {"marketplace_account": ["in", accounts]})
		for doctype, name in reversed(self._created):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def make_noon_account(self):
		code = f"cross-platform-noon-{self.suffix}"
		connection = frappe.get_doc(
			{
				"doctype": "Noon Connection",
				"connection_name": code,
				"project_code": code,
				"key_id": "test-key-id",
				"private_key": NOON_PEM,
				"environment": "Static Sandbox",
				"enabled": 1,
			}
		).insert()
		self._created.append(("Noon Connection", connection.name))
		# The warehouse lookup would call Noon; it is not what this test is about.
		with patch("marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"):
			account = frappe.get_doc(
				{
					"doctype": "Marketplace Account",
					"account_name": code,
					"platform": "Noon",
					"external_account_id": code,
					"connection_doctype": "Noon Connection",
					"connection": connection.name,
					"noon_warehouse_code": "NOON-WH-CROSS",
					"noon_country_code": "ae",
				}
			).insert()
		self._created.append(("Marketplace Account", account.name))
		return account

	def make_user(self, label, role):
		email, _role = make_role_user(self, label, {})
		frappe.get_doc("User", email).add_roles(role)
		frappe.clear_cache(user=email)
		return email

	def resolve(self, value):
		if isinstance(value, list):
			return [self.resolve(entry) for entry in value]
		return self.fixtures.get(value, value) if isinstance(value, str) else value

	def assert_denied(self, user, cases):
		# frappe.enqueue is patched so that a regressed check fails this test instead of queueing a
		# real background job against the fixture account.
		with patch("frappe.enqueue") as enqueue:
			for path, kwargs in cases:
				with self.subTest(method=path), as_user(user):
					method = frappe.get_attr(path)
					with self.assertRaises(frappe.PermissionError):
						method(**{key: self.resolve(value) for key, value in kwargs.items()})
		enqueue.assert_not_called()

	def test_talabat_user_cannot_drive_shopify_or_noon_records(self):
		user = self.make_user("Cross Talabat", "Talabat User")
		self.assert_denied(user, SHOPIFY_AND_NOON_CASES)

	def test_noon_user_cannot_drive_talabat_records(self):
		user = self.make_user("Cross Noon", "Noon User")
		self.assert_denied(user, TALABAT_CASES)

	def test_shopify_user_cannot_drive_talabat_records(self):
		user = self.make_user("Cross Shopify", "Shopify User")
		self.assert_denied(user, TALABAT_CASES)
