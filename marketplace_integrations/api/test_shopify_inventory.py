# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Thin API-boundary tests for api/shopify_inventory.py - the real business
logic and permission checks are exercised by
integrations/shopify/test_inventory_sync.py; these confirm delegation and
whitelisting metadata, mirroring api/test_shopify_catalog_push.py."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.shopify_inventory import (
	get_inventory_sync_status,
	queue_account_reconciliation,
	queue_inventory_sync,
)


class IntegrationTestShopifyInventoryApi(IntegrationTestCase):
	def test_queue_inventory_sync_delegates(self):
		payload = {"jobs": [{"account": "A", "job_id": "abc", "mapping_count": 1}], "skipped_accounts": []}
		with patch(
			"marketplace_integrations.integrations.shopify.inventory_sync.queue_inventory_sync",
			return_value=payload,
		) as mocked:
			result = queue_inventory_sync(["MPM-00001"])

		mocked.assert_called_once_with(["MPM-00001"])
		self.assertEqual(result, payload)

	def test_queue_account_reconciliation_delegates(self):
		with patch(
			"marketplace_integrations.integrations.shopify.inventory_sync.queue_account_reconciliation",
			return_value={"jobs": [], "skipped_accounts": []},
		) as mocked:
			queue_account_reconciliation("MKTACC-00001")

		mocked.assert_called_once_with("MKTACC-00001")

	def test_queue_endpoints_are_post_only(self):
		methods = frappe.allowed_http_methods_for_whitelisted_func
		self.assertEqual(methods[queue_inventory_sync], ["POST"])
		self.assertEqual(methods[queue_account_reconciliation], ["POST"])

	def test_get_inventory_sync_status_delegates(self):
		with patch(
			"marketplace_integrations.integrations.shopify.inventory_sync.get_sync_status",
			return_value={"status": "running", "processed": 3},
		) as mocked:
			result = get_inventory_sync_status("job-1")

		mocked.assert_called_once_with("job-1")
		self.assertEqual(result, {"status": "running", "processed": 3})
