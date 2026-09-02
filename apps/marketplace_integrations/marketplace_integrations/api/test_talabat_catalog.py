# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.talabat_catalog import queue_catalog_sync


class IntegrationTestTalabatCatalogApi(IntegrationTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		warehouse = frappe.get_all("Warehouse", filters={"disabled": 0}, pluck="name", limit=1)[0]
		price_list = frappe.get_all(
			"Price List", filters={"selling": 1, "enabled": 1}, pluck="name", limit=1
		)[0]
		connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Catalog API Connection {suffix}",
				"client_id": "test-client-id",
				"client_secret": "test-client-secret",
				"chain_id": f"test-chain-{suffix}",
			}
		).insert()
		self.account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Catalog API Account {suffix}",
				"platform": "Talabat",
				"external_account_id": f"test-vendor-{suffix}",
				"connection_doctype": "Talabat Connection",
				"connection": connection.name,
				"warehouse": warehouse,
				"erpnext_selling_price_list": price_list,
			}
		).insert()

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_validates_then_enqueues_without_calling_talabat(self, enqueue):
		result = queue_catalog_sync(self.account.name)

		self.assertEqual(result, {"queued": True})
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.args[0].__name__, "sync_catalog")
		self.assertEqual(enqueue.call_args.kwargs["account"], self.account.name)
		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_requires_account_write_permission(self, enqueue):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"catalog-sync-{frappe.generate_hash(length=10)}@example.com",
				"first_name": "Catalog Sync Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()
		enqueue.reset_mock()

		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				queue_catalog_sync(self.account.name)
		enqueue.assert_not_called()
