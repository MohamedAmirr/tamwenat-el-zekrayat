# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.backfill_talabat_order_webhook_urls import execute


class IntegrationTestBackfillTalabatOrderWebhookUrls(IntegrationTestCase):
	def test_backfills_blank_urls_without_replacing_existing_values(self):
		suffix = frappe.generate_hash(length=8)
		blank = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Blank Webhook URL {suffix}",
				"client_id": "test-client-id",
				"client_secret": "test-client-secret",
				"chain_id": "test-chain-id",
				"environment": "Sandbox",
				"order_webhook_url": "https://previous.example.com/endpoint",
			}
		).insert()
		frappe.db.set_value("Talabat Connection", blank.name, "order_webhook_url", "")

		existing = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Existing Webhook URL {suffix}",
				"client_id": "test-client-id",
				"client_secret": "test-client-secret",
				"chain_id": "test-chain-id",
				"environment": "Sandbox",
				"order_webhook_url": "https://kept.example.com/endpoint",
			}
		).insert()

		with patch(
			"marketplace_integrations.marketplace_integrations.doctype.talabat_connection.talabat_connection.get_url",
			return_value="https://current.example.com/api/method/marketplace_integrations.api.talabat_webhooks.order_event",
		):
			execute()

		blank.reload()
		existing.reload()
		self.assertEqual(
			blank.order_webhook_url,
			"https://current.example.com/api/method/marketplace_integrations.api.talabat_webhooks.order_event",
		)
		self.assertEqual(existing.order_webhook_url, "https://kept.example.com/endpoint")
