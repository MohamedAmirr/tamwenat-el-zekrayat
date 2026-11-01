# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#323: the Shopify reconciliation services are open to System Manager and Shopify User only,
read through ``frappe.get_list`` and count Shopify data only.

Users are ordinary users built with ``make_role_user`` plus the real platform role. The class
deletes what it creates in ``tearDown`` because ``make_role_user`` commits.
"""

from contextlib import contextmanager

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify.test_products import make_item, make_shopify_account
from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)
from marketplace_integrations.services import shopify_reconciliation as service
from marketplace_integrations.tests.utils import make_role_user

SERVICE_FUNCTIONS = (
	service.get_order_import_failures,
	service.get_catalog_push_error_count,
	service.get_inventory_sync_error_count,
	service.get_order_sync_error_count,
	service.get_webhook_delivery_error_count,
	service.get_webhook_delivery_exhausted_retry_count,
	service.get_orders_received_count_in_range,
	service.get_total_order_value_in_range,
	service.get_sales_order_sync_success_rate_in_range,
	service.get_total_refunded_amount_in_range,
	service.get_webhook_processing_success_rate_in_range,
	service.get_reconciliation_summary,
)


@contextmanager
def as_user(user):
	previous = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(previous)


class IntegrationTestShopifyReconciliationPermissions(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.item = make_item(f"Recon Perm Item {self.suffix}")
		self.shopify = make_shopify_account(f"Recon Perm Shopify {self.suffix}")
		# A second account of another platform: Marketplace Account.platform is what every
		# permission and count keys on (same switch as test_products.IntegrationTestGetAccount).
		self.talabat = make_shopify_account(f"Recon Perm Talabat {self.suffix}")
		self.talabat.db_set("platform", "Talabat", update_modified=False)
		self.accounts = [self.shopify.name, self.talabat.name]

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete("Marketplace Sync Log", {"marketplace_account": ["in", self.accounts]})
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": ["in", self.accounts]})
		frappe.db.delete("Marketplace Order", {"marketplace_account": ["in", self.accounts]})
		for account in (self.shopify, self.talabat):
			frappe.delete_doc("Marketplace Account", account.name, ignore_permissions=True, force=True)
			frappe.delete_doc("Shopify Connection", account.connection, ignore_permissions=True, force=True)
		frappe.db.delete("Item", {"item_code": self.item})
		frappe.db.commit()

	def make_user(self, label, *roles):
		email, _role = make_role_user(self, label, {})
		frappe.get_doc("User", email).add_roles(*roles)
		frappe.clear_cache(user=email)
		return email

	def make_mapping(self, account, tag, **values):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": account.name,
				"item": self.item,
				"external_reference": f"recon-perm-{tag}-{self.suffix}",
				**values,
			}
		).insert()

	def make_order(self, account, platform, tag, **values):
		order = frappe.get_doc(
			{
				"doctype": "Marketplace Order",
				"marketplace_account": account.name,
				"external_order_id": f"recon-perm-{tag}-{self.suffix}",
				"platform": platform,
				**values,
			}
		)
		order.flags.skip_sync_status_update = True
		order.order_identity_key = compute_order_identity_key(account.name, order.external_order_id)
		order.insert()
		order.db_set("erpnext_sync_status", "Sync Error", update_modified=False)
		return order

	def make_failure(self, account):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"marketplace_account": account.name,
				"status": "Error",
				"attempted_at": frappe.utils.now_datetime(),
				"request_data": frappe.as_json(
					{"account": account.name, "order_gid": f"gid://{account.name}"}
				),
				"traceback": "boom",
			}
		).insert()

	def test_non_shopify_roles_are_refused_by_every_service_function(self):
		for label, roles in (
			("Recon Talabat", ("Talabat User",)),
			("Recon Noon", ("Noon User",)),
			("Recon Stock", ("Stock User",)),
			("Recon Plain", ()),
		):
			user = self.make_user(label, *roles)
			for function in SERVICE_FUNCTIONS:
				with self.subTest(user=label, function=function.__name__), as_user(user):
					with self.assertRaises(frappe.PermissionError):
						function()

	def test_shopify_user_reads_only_shopify_data(self):
		user = self.make_user("Recon Shopify", "Shopify User")
		shopify_failure = self.make_failure(self.shopify)
		talabat_failure = self.make_failure(self.talabat)

		with as_user(user):
			names = [row.name for row in service.get_order_import_failures()]
		self.assertIn(shopify_failure.name, names)
		self.assertNotIn(talabat_failure.name, names)

		# The whole summary is readable by a Shopify User.
		with as_user(user):
			self.assertEqual(len(service.get_reconciliation_summary()), 10)

	def test_counts_include_shopify_rows_and_exclude_other_platforms(self):
		user = self.make_user("Recon Shopify", "Shopify User")
		counters = (
			service.get_catalog_push_error_count,
			service.get_inventory_sync_error_count,
			service.get_order_sync_error_count,
			service.get_orders_received_count_in_range,
		)
		with as_user(user):
			before = [counter() for counter in counters]

		# Other platform's rows: no counter moves for the Shopify User or for Administrator.
		self.make_mapping(self.talabat, "t", sync_status="Error", inventory_sync_status="Error")
		self.make_order(self.talabat, "Talabat", "t")
		with as_user(user):
			self.assertEqual([counter() for counter in counters], before)

		self.make_mapping(self.shopify, "s", sync_status="Error", inventory_sync_status="Error")
		self.make_order(self.shopify, "Shopify", "s")
		with as_user(user):
			after = [counter() for counter in counters]
		self.assertEqual(after, [count + 1 for count in before])

	def test_administrator_counts_exclude_other_platform_mappings_and_orders(self):
		before = (
			service.get_catalog_push_error_count(),
			service.get_order_sync_error_count(),
			service.get_orders_received_count_in_range(),
		)
		self.make_mapping(self.talabat, "t", sync_status="Error")
		self.make_order(self.talabat, "Talabat", "t")
		after = (
			service.get_catalog_push_error_count(),
			service.get_order_sync_error_count(),
			service.get_orders_received_count_in_range(),
		)
		self.assertEqual(before, after)
