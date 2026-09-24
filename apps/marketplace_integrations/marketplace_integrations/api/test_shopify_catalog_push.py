# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Thin API-boundary tests for api/shopify_catalog_push.py - the real
business logic and permission checks are exercised by
integrations/shopify/test_catalog_push.py; these just confirm the delegation
and whitelisting metadata, mirroring api/test_shopify_products.py's shape.
"""

from unittest.mock import patch

import frappe

from marketplace_integrations.api.shopify_catalog_push import get_catalog_push_status, queue_catalog_push
from marketplace_integrations.testing_utils import CleanupTestCase


class IntegrationTestShopifyCatalogPushApi(CleanupTestCase):
	def test_queue_catalog_push_delegates(self):
		with patch(
			"marketplace_integrations.integrations.shopify.catalog_push.queue_catalog_push",
			return_value={"jobs": [{"account": "Some Account", "job_id": "abc", "mapping_count": 1}]},
		) as mocked:
			result = queue_catalog_push(["MPM-00001"])

		mocked.assert_called_once_with(["MPM-00001"])
		self.assertEqual(result, {"jobs": [{"account": "Some Account", "job_id": "abc", "mapping_count": 1}]})

	def test_queue_catalog_push_post_only(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[queue_catalog_push], ["POST"])

	def test_get_catalog_push_status_delegates(self):
		with patch(
			"marketplace_integrations.integrations.shopify.catalog_push.get_push_status",
			return_value={"status": "running", "processed": 3},
		) as mocked:
			result = get_catalog_push_status("job-1")

		mocked.assert_called_once_with("job-1")
		self.assertEqual(result, {"status": "running", "processed": 3})
