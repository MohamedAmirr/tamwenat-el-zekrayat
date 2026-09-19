# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import time

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify.client import get_access_token_cache_key


def make_connection(connection_name, **kwargs):
	# shop_domain defaults to a call-unique value - it carries a DB-level
	# unique: 1 constraint (see shopify_connection.json), and
	# IntegrationTestCase only rolls back once at class teardown (see
	# docs/frappe-conventions.md's Tests section), so a fixed literal default
	# would collide the moment two test methods in the same class both call
	# this helper without overriding shop_domain themselves.
	doc = {
		"doctype": "Shopify Connection",
		"connection_name": connection_name,
		"shop_domain": f"test-shop-{frappe.generate_hash(length=8)}.myshopify.com",
		"client_id": "test-client-id",
		"client_secret": "test-client-secret",
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


class IntegrationTestShopifyConnection(IntegrationTestCase):
	def test_fields_persist_and_naming_is_expression_based(self):
		connection = make_connection("Test Connection 1", shop_domain="test-connection-1.myshopify.com")
		connection.reload()

		self.assertEqual(connection.connection_name, "Test Connection 1")
		self.assertEqual(connection.shop_domain, "test-connection-1.myshopify.com")
		self.assertEqual(connection.client_id, "test-client-id")
		self.assertEqual(connection.enabled, 1)
		self.assertTrue(connection.name.startswith("SHOPCONN-"))
		self.assertNotIn("Test Connection 1", connection.name)

	def test_two_connections_are_independent_documents(self):
		connection_a = make_connection("Test Independent A", shop_domain="shop-a.myshopify.com")
		connection_b = make_connection("Test Independent B", shop_domain="shop-b.myshopify.com")

		self.assertNotEqual(connection_a.name, connection_b.name)
		self.assertEqual(connection_a.shop_domain, "shop-a.myshopify.com")
		self.assertEqual(connection_b.shop_domain, "shop-b.myshopify.com")

	def test_client_secret_round_trips_without_xss_corruption(self):
		tricky_secret = "s3cr3t-<p<+X>-value"
		connection = make_connection("Test XSS Secret", client_secret=tricky_secret)
		connection.reload()

		self.assertEqual(connection.get_password("client_secret"), tricky_secret)

	def test_client_secret_not_returned_in_plain_document(self):
		tricky_secret = "test-client-secret"
		connection = make_connection("Test Secret Hidden", client_secret=tricky_secret)
		connection.reload()

		as_dict = connection.as_dict()
		for fieldname, value in as_dict.items():
			self.assertNotEqual(
				value,
				tricky_secret,
				f"plaintext client_secret leaked via field {fieldname!r}",
			)

	def test_get_store_base_url(self):
		connection = make_connection("Test Base URL", shop_domain="base-url-shop.myshopify.com")
		self.assertEqual(connection.get_store_base_url(), "https://base-url-shop.myshopify.com")

	def test_shop_domain_uniqueness_is_enforced_by_the_db(self):
		make_connection("Test Unique Domain A", shop_domain="duplicate-shop.myshopify.com")
		with self.assertRaises(frappe.UniqueValidationError):
			make_connection("Test Unique Domain B", shop_domain="duplicate-shop.myshopify.com")

	def test_blank_shop_domain_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			make_connection("Test Blank Domain", shop_domain="")

	def test_shop_domain_with_scheme_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			make_connection("Test Scheme Domain", shop_domain="https://test-shop.myshopify.com")

	def test_shop_domain_with_path_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			make_connection("Test Path Domain", shop_domain="test-shop.myshopify.com/admin")

	def test_shop_domain_with_userinfo_at_sign_is_rejected(self):
		# Guards against userinfo/host confusion in get_store_base_url()'s
		# plain "https://" + shop_domain string prefix: "a@b" resolves to
		# host "b", not "a" - an unrejected "@" would let a connection's
		# configured domain silently send the client secret to an
		# attacker-controlled host.
		with self.assertRaises(frappe.ValidationError):
			make_connection(
				"Test Userinfo Domain", shop_domain="test-shop.myshopify.com@attacker.example"
			)

	def test_shop_domain_with_query_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			make_connection("Test Query Domain", shop_domain="test-shop.myshopify.com?x=1")

	def test_shop_domain_with_fragment_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			make_connection("Test Fragment Domain", shop_domain="test-shop.myshopify.com#x")

	def test_shop_domain_with_port_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			make_connection("Test Port Domain", shop_domain="test-shop.myshopify.com:8080")

	def test_shop_domain_with_whitespace_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			make_connection("Test Whitespace Domain", shop_domain="test shop.myshopify.com")

	def test_shop_domain_with_leading_or_trailing_whitespace_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			make_connection("Test Padded Domain", shop_domain=" test-shop.myshopify.com ")

	def test_bare_shop_domain_saves(self):
		connection = make_connection("Test Bare Domain", shop_domain="another-shop.myshopify.com")
		connection.reload()
		self.assertEqual(connection.shop_domain, "another-shop.myshopify.com")

	def test_on_update_clears_only_this_connections_cached_token(self):
		connection_a = make_connection("Test Cache Clear A", shop_domain="cache-a.myshopify.com")
		connection_b = make_connection("Test Cache Clear B", shop_domain="cache-b.myshopify.com")

		cache_key_a = get_access_token_cache_key(connection_a.name)
		cache_key_b = get_access_token_cache_key(connection_b.name)
		token_payload = {"access_token": "cached-token", "expires_at": time.time() + 3600}
		frappe.cache().set_value(cache_key_a, token_payload, expires_in_sec=3600)
		frappe.cache().set_value(cache_key_b, token_payload, expires_in_sec=3600)

		connection_a.enabled = 0
		connection_a.save()

		self.assertIsNone(frappe.cache().get_value(cache_key_a))
		self.assertEqual(frappe.cache().get_value(cache_key_b), token_payload)

	def test_on_trash_clears_only_this_connections_cached_token(self):
		connection_a = make_connection("Test Cache Trash A", shop_domain="cache-trash-a.myshopify.com")
		connection_b = make_connection("Test Cache Trash B", shop_domain="cache-trash-b.myshopify.com")

		cache_key_a = get_access_token_cache_key(connection_a.name)
		cache_key_b = get_access_token_cache_key(connection_b.name)
		token_payload = {"access_token": "cached-token", "expires_at": time.time() + 3600}
		frappe.cache().set_value(cache_key_a, token_payload, expires_in_sec=3600)
		frappe.cache().set_value(cache_key_b, token_payload, expires_in_sec=3600)

		connection_a.delete()

		self.assertIsNone(frappe.cache().get_value(cache_key_a))
		self.assertEqual(frappe.cache().get_value(cache_key_b), token_payload)
