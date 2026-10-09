# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase


def make_connection(connection_name, **kwargs):
	# shop_domain defaults to a call-unique value - it carries a DB-level
	# unique: 1 constraint, and IntegrationTestCase only rolls back once at
	# class teardown (see docs/frappe-conventions.md's Tests section), so a
	# fixed literal default would collide across this class's test methods.
	doc = {
		"doctype": "Shopify Connection",
		"connection_name": connection_name,
		"shop_domain": f"test-shop-{frappe.generate_hash(length=8)}.myshopify.com",
		"client_id": "test-client-id",
		"client_secret": "test-client-secret",
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


def make_delivery(connection, webhook_id, **kwargs):
	doc = {
		"doctype": "Shopify Webhook Delivery",
		"connection": connection.name,
		"shop_domain": connection.shop_domain,
		"topic": "ORDERS_CREATE",
		"webhook_id": webhook_id,
		"received_at": frappe.utils.now_datetime(),
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


class IntegrationTestShopifyWebhookDelivery(IntegrationTestCase):
	def setUp(self):
		self.connection = make_connection(f"Delivery Connection {frappe.generate_hash(length=8)}")

	def test_naming_is_expression_based(self):
		delivery = make_delivery(self.connection, "webhook-id-unique-1")
		self.assertTrue(delivery.name.startswith("SHOPWHD-"))

	def test_webhook_id_is_unique_at_the_database_level(self):
		make_delivery(self.connection, "webhook-id-dup")
		second = frappe.get_doc(
			{
				"doctype": "Shopify Webhook Delivery",
				"connection": self.connection.name,
				"shop_domain": self.connection.shop_domain,
				"topic": "ORDERS_UPDATED",
				"webhook_id": "webhook-id-dup",
				"received_at": frappe.utils.now_datetime(),
			}
		)
		with self.assertRaises(frappe.UniqueValidationError):
			second.insert()
