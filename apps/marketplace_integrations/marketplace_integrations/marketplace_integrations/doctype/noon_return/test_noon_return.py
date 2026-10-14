# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from marketplace_integrations.tests.utils import make_role_user

IGNORE_TEST_RECORD_DEPENDENCIES = ["Marketplace Order", "Account", "User", "Mode of Payment"]


class TestNoonReturn(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		suffix = frappe.generate_hash(length=8)
		self.connection = frappe.get_doc(
			{
				"doctype": "Noon Connection",
				"connection_name": f"Noon Return Test Connection {suffix}",
				"project_code": f"test-noon-{suffix}",
				"key_id": f"test-key-id-{suffix}",
				"private_key": "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
				"environment": "Static Sandbox",
			}
		).insert()
		self.account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Noon Return Test Account {suffix}",
				"platform": "Noon",
				"external_account_id": f"test-noon-{suffix}",
				"connection_doctype": "Noon Connection",
				"connection": self.connection.name,
			}
		).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.db.delete("Noon Return", {"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, force=True)
		frappe.delete_doc("Noon Connection", self.connection.name, force=True)
		frappe.db.commit()
		super().tearDown()

	def make_return(self, **values):
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "Noon Return",
				"marketplace_account": self.account.name,
				"purchase_item_nr": f"test-purchase-item-{suffix}",
				"found_via": "Manual Lookup",
				"first_seen_on": now_datetime(),
				"last_seen_on": now_datetime(),
				**values,
			}
		).insert()

	def test_doctype_fields_defaults_and_permissions_match_contract(self):
		meta = frappe.get_meta("Noon Return")
		fields = {field.fieldname: field for field in meta.fields if field.fieldname}

		self.assertEqual(fields["marketplace_account"].fieldtype, "Link")
		self.assertEqual(fields["marketplace_account"].options, "Marketplace Account")
		self.assertTrue(fields["marketplace_account"].reqd)

		self.assertEqual(fields["purchase_item_nr"].fieldtype, "Data")
		self.assertTrue(fields["purchase_item_nr"].reqd)
		self.assertTrue(fields["purchase_item_nr"].ignore_xss_filter)

		self.assertEqual(fields["return_identity_key"].fieldtype, "Data")
		self.assertTrue(fields["return_identity_key"].hidden)
		self.assertTrue(fields["return_identity_key"].unique)
		self.assertEqual(fields["return_identity_key"].length, 64)

		self.assertEqual(fields["marketplace_order"].fieldtype, "Link")
		self.assertEqual(fields["marketplace_order"].options, "Marketplace Order")
		self.assertFalse(fields["marketplace_order"].reqd)

		self.assertEqual(fields["found_via"].fieldtype, "Select")
		self.assertEqual(fields["found_via"].options.splitlines(), ["Manual Lookup", "AWB Scan"])
		self.assertTrue(fields["found_via"].reqd)

		self.assertEqual(fields["references"].fieldtype, "JSON")
		self.assertTrue(fields["references"].ignore_xss_filter)

		self.assertTrue(fields["first_seen_on"].reqd)
		self.assertTrue(fields["last_seen_on"].reqd)
		self.assertEqual(fields["receipt_acknowledged_on"].fieldtype, "Datetime")
		self.assertEqual(fields["receipt_acknowledged_by"].options, "User")
		self.assertEqual(fields["receipt_acknowledgement_note"].fieldtype, "Small Text")

		for fieldname in (
			"marketplace_account",
			"purchase_item_nr",
			"return_identity_key",
			"partner_sku",
			"order_nr",
			"marketplace_order",
			"mp_code",
			"merchant_code",
			"references",
			"found_via",
			"first_seen_on",
			"last_seen_on",
			"receipt_acknowledged_on",
			"receipt_acknowledged_by",
			"receipt_acknowledgement_note",
		):
			self.assertTrue(fields[fieldname].read_only, fieldname)

		permissions = {perm.role: perm for perm in meta.permissions}
		self.assertEqual(set(permissions), {"System Manager"})
		self.assertTrue(permissions["System Manager"].read)
		self.assertTrue(permissions["System Manager"].create)
		self.assertTrue(permissions["System Manager"].write)
		self.assertTrue(permissions["System Manager"].delete)

	def test_insert_requires_marketplace_account(self):
		with self.assertRaises(frappe.MandatoryError):
			frappe.get_doc(
				{
					"doctype": "Noon Return",
					"purchase_item_nr": "test-purchase-item-missing-account",
					"found_via": "Manual Lookup",
					"first_seen_on": now_datetime(),
					"last_seen_on": now_datetime(),
				}
			).insert()

	def test_unlinked_return_has_blank_marketplace_order(self):
		doc = self.make_return()
		self.assertFalse(doc.marketplace_order)

	def test_acknowledge_receipt_records_internal_actor_time_and_note_without_remote_call(self):
		doc = self.make_return()
		with patch("requests.sessions.Session.request") as request:
			result = doc.acknowledge_receipt("  Parcel received at warehouse  ")

		self.assertTrue(result["acknowledged"])
		self.assertFalse(result["already_acknowledged"])
		request.assert_not_called()
		doc.reload()
		self.assertEqual(doc.receipt_acknowledged_by, "Administrator")
		self.assertTrue(doc.receipt_acknowledged_on)
		self.assertEqual(doc.receipt_acknowledgement_note, "Parcel received at warehouse")

	def test_acknowledge_receipt_preserves_the_original_acknowledgement(self):
		doc = self.make_return()
		first = doc.acknowledge_receipt("First note")
		second = doc.acknowledge_receipt("Changed note")

		self.assertFalse(first["already_acknowledged"])
		self.assertTrue(second["already_acknowledged"])
		doc.reload()
		self.assertEqual(doc.receipt_acknowledgement_note, "First note")
		self.assertEqual(doc.receipt_acknowledged_by, "Administrator")

	def test_acknowledge_receipt_requires_write_permission(self):
		doc = self.make_return()
		email, _ = make_role_user(self, "Noon Return Receipt", {"Noon Return": ["read"]})

		with self.set_user(email), self.assertRaises(frappe.PermissionError):
			doc.acknowledge_receipt("Not permitted")

		doc.reload()
		self.assertFalse(doc.receipt_acknowledged_on)
