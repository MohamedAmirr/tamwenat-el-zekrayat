# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from marketplace_integrations.services.marketplace_order_reconciliation import (
	enqueue_nightly_order_reconciliation,
)


class TestNightlyMarketplaceOrderReconciliation(TestCase):
	@patch("marketplace_integrations.integrations.noon.orders.enqueue_order_import")
	@patch("marketplace_integrations.services.marketplace_order_reconciliation.frappe.enqueue")
	@patch("marketplace_integrations.services.marketplace_order_reconciliation.frappe.get_all")
	def test_enqueues_one_deduplicated_job_per_enabled_shopify_and_noon_account(
		self, get_all, enqueue, enqueue_noon
	):
		get_all.return_value = [
			SimpleNamespace(name="Shopify A", platform="Shopify"),
			SimpleNamespace(name="Noon A", platform="Noon"),
		]
		enqueue.return_value = object()
		enqueue_noon.return_value = object()

		result = enqueue_nightly_order_reconciliation()

		self.assertEqual(result, {"queued": 2, "errors": 0})
		enqueue.assert_called_once_with(
			"marketplace_integrations.integrations.shopify.orders.reconcile_all_accounts_orders",
			queue="long",
			timeout=3600,
			job_id="marketplace-shopify-order-reconciliation-Shopify A",
			deduplicate=True,
			account_name="Shopify A",
			catch_up_unlinked=True,
		)
		enqueue_noon.assert_called_once_with(
			"Noon A", trigger_source="Scheduled Reconciliation", catch_up_unlinked=True
		)
