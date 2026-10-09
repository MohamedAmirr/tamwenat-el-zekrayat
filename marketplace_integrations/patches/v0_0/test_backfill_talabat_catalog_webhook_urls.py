# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.backfill_talabat_catalog_webhook_urls import execute


class IntegrationTestBackfillTalabatCatalogWebhookUrls(IntegrationTestCase):
	def test_backfills_blank_urls_without_replacing_existing_values(self):
		suffix = frappe.generate_hash(length=8)
		blank = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Blank Catalog Webhook URL {suffix}",
				"client_id": "test-client-id",
				"client_secret": "test-client-secret",
				"chain_id": "test-chain-id",
				"environment": "Sandbox",
				"catalog_webhook_url": "https://previous.example.com/endpoint",
			}
		).insert()
		# Simulate a connection that existed before the catalog webhook feature:
		# both fields blank, exactly as before_insert() never ran for it.
		frappe.db.set_value(
			"Talabat Connection", blank.name, {"catalog_webhook_url": "", "catalog_webhook_secret": ""}
		)

		existing = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Existing Catalog Webhook URL {suffix}",
				"client_id": "test-client-id",
				"client_secret": "test-client-secret",
				"chain_id": "test-chain-id",
				"environment": "Sandbox",
				"catalog_webhook_url": "https://kept.example.com/endpoint",
			}
		).insert()
		existing_secret = existing.get_password("catalog_webhook_secret", raise_exception=False)

		with patch(
			"marketplace_integrations.marketplace_integrations.doctype.talabat_connection.talabat_connection.get_url",
			return_value=(
				"https://current.example.com/api/method/"
				"marketplace_integrations.api.talabat_webhooks.catalog_update_feedback"
			),
		):
			execute()

		blank.reload()
		existing.reload()
		self.assertEqual(
			blank.catalog_webhook_url,
			"https://current.example.com/api/method/"
			"marketplace_integrations.api.talabat_webhooks.catalog_update_feedback",
		)
		self.assertTrue(blank.get_password("catalog_webhook_secret", raise_exception=False))
		self.assertEqual(existing.catalog_webhook_url, "https://kept.example.com/endpoint")
		# A connection whose URL was already set is untouched by this patch's
		# query entirely - its pre-existing secret must survive unchanged.
		self.assertEqual(
			existing.get_password("catalog_webhook_secret", raise_exception=False), existing_secret
		)
