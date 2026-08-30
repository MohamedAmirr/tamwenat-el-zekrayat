# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.marketplace_account import (
	compute_account_identity_key,
)

# No platform-specific "Connection" DocType exists yet (Talabat Connection
# lands under #37/#38). connection_doctype/connection (Dynamic Link) need
# *some* real DocType + document to point at to exercise Frappe's own Link
# existence checks and this controller's validate_connection(). "UOM" and
# "Item Group" are plain, always-present ERPNext master doctypes with a
# stable seeded record ("Nos", "Products" - both already relied on by
# talabat_integration's own tests), so they're used here purely as
# structural stand-ins for "a real DocType with a real document", not as
# any endorsement of UOM/Item Group as real connection targets.
STAND_IN_CONNECTION_DOCTYPE = "UOM"
STAND_IN_CONNECTION = "Nos"
OTHER_DOCTYPE_WITH_DIFFERENT_RECORD = "Item Group"
OTHER_DOCTYPE_RECORD = "Products"

# Frappe's IntegrationTestCase auto-generates a generic test record for every
# doctype linked from the one under test (here: Warehouse, via the
# `warehouse` field). ERPNext's generic Company/Fiscal Year test-record
# generator hits a pre-existing Fiscal Year overlap bug on this dev site,
# unrelated to this doctype (same root cause documented in
# talabat_product_mapping's test file for its own Item dependency). We build
# our own Warehouse below instead, using the site's real configured company,
# so skip just the auto-generation step for Warehouse.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Warehouse"]


class IntegrationTestMarketplaceAccount(IntegrationTestCase):
	def make_warehouse(self, warehouse_name):
		# Use the site's actual configured company rather than a hardcoded
		# "_Test Company" - this dev site was bootstrapped with its own real
		# company ("Tamwenat El Zekrayat"), not ERPNext's generic test
		# fixture company.
		company = frappe.defaults.get_global_default("company")
		abbr = frappe.db.get_value("Company", company, "abbr")
		existing_name = f"{warehouse_name} - {abbr}"
		if frappe.db.exists("Warehouse", existing_name):
			return existing_name

		return (
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": warehouse_name,
					"company": company,
				}
			)
			.insert()
			.name
		)

	def make_account(self, account_name, external_account_id, **kwargs):
		doc = {
			"doctype": "Marketplace Account",
			"account_name": account_name,
			"platform": "Talabat",
			"external_account_id": external_account_id,
			"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
			"connection": STAND_IN_CONNECTION,
		}
		doc.update(kwargs)
		return frappe.get_doc(doc).insert()

	def test_fields_persist_and_identity_key_computed(self):
		account = self.make_account("Test Account 1", "ext-account-001")
		account.reload()

		self.assertEqual(account.account_name, "Test Account 1")
		self.assertEqual(account.platform, "Talabat")
		self.assertEqual(account.external_account_id, "ext-account-001")
		self.assertEqual(account.connection_doctype, STAND_IN_CONNECTION_DOCTYPE)
		self.assertEqual(account.connection, STAND_IN_CONNECTION)
		self.assertEqual(
			account.account_identity_key,
			compute_account_identity_key("Talabat", "ext-account-001"),
		)
		# Naming is expression/random (MKTACC-.#####), independent of
		# external_account_id - the document name must not be derived from it.
		self.assertNotIn("ext-account-001", account.name)
		self.assertTrue(account.name.startswith("MKTACC-"))

	def test_warehouse_optional(self):
		# No stock fallback is introduced by this doctype - an account with no
		# warehouse configured must simply persist with an empty warehouse.
		account = self.make_account("Test Account No Warehouse", "ext-account-002")
		account.reload()

		self.assertFalse(account.warehouse)

	def test_two_accounts_may_share_one_warehouse(self):
		shared_warehouse = self.make_warehouse("Test Shared Warehouse")

		account_a = self.make_account("Test Shared A", "ext-account-shared-a", warehouse=shared_warehouse)
		account_b = self.make_account("Test Shared B", "ext-account-shared-b", warehouse=shared_warehouse)

		self.assertEqual(account_a.warehouse, shared_warehouse)
		self.assertEqual(account_b.warehouse, shared_warehouse)

	def test_two_accounts_may_use_different_warehouses(self):
		warehouse_a = self.make_warehouse("Test Partition Warehouse A")
		warehouse_b = self.make_warehouse("Test Partition Warehouse B")

		account_a = self.make_account("Test Partition A", "ext-account-partition-a", warehouse=warehouse_a)
		account_b = self.make_account("Test Partition B", "ext-account-partition-b", warehouse=warehouse_b)

		self.assertEqual(account_a.warehouse, warehouse_a)
		self.assertEqual(account_b.warehouse, warehouse_b)
		self.assertNotEqual(account_a.warehouse, account_b.warehouse)

	def test_duplicate_platform_external_account_id_rejected_by_precheck(self):
		# Normal insert flow: validate_unique_account_identity()'s friendly
		# check runs before before_save() ever writes account_identity_key,
		# so this is expected to surface as a plain ValidationError.
		self.make_account("Test Dup 1", "ext-account-dup-001")

		with self.assertRaises(frappe.ValidationError):
			self.make_account("Test Dup 2", "ext-account-dup-001")

	def test_duplicate_account_identity_key_rejected_at_db_level(self):
		# Prove the uniqueness is a real DB-level constraint, not merely the
		# validate()-time precheck above. Bypass this controller's own
		# validate()/before_save() entirely (flags.ignore_validate) and force
		# a raw db_update() with a manually-collided account_identity_key -
		# MariaDB's own unique index on the field must still reject it.
		account_1 = self.make_account("Test DB Dup 1", "ext-account-dbdup-001")
		account_2 = self.make_account("Test DB Dup 2", "ext-account-dbdup-002")

		account_2.account_identity_key = account_1.account_identity_key
		account_2.flags.ignore_validate = True

		with self.assertRaises(frappe.UniqueValidationError):
			account_2.save()

	def test_duplicate_rejected_when_platform_differs_only_by_whitespace(self):
		# Frappe's own Select normalization (_validate_selects()) strips
		# leading/trailing whitespace and only THEN checks membership against
		# the field's options - so platform=" Talabat " passes validation and
		# is persisted as the bare "Talabat" string, identical to a
		# platform="Talabat" row. That normalization happens in _validate(),
		# which runs AFTER before_save() (where account_identity_key is
		# computed) - so if compute_account_identity_key() itself didn't
		# normalize platform, this second insert would be hashed against the
		# untrimmed " Talabat " value, produce a *different*
		# account_identity_key than the first row's, sail past both the
		# friendly precheck and the DB unique index, and leave two rows with
		# an identical stored (platform, external_account_id) pair.
		self.make_account("Test Whitespace Platform 1", "ext-account-ws-001")

		with self.assertRaisesRegex(frappe.ValidationError, "already exists"):
			self.make_account(
				"Test Whitespace Platform 2",
				"ext-account-ws-001",
				platform=" Talabat ",
			)

	def test_same_external_account_id_different_platform_allowed(self):
		# Identity is the (platform, external_account_id) pair - a shared
		# external_account_id under a different platform must not collide.
		frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": "Test Cross Platform 1",
				"platform": "Talabat",
				"external_account_id": "ext-account-shared-id",
				"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
				"connection": STAND_IN_CONNECTION,
			}
		).insert()

		# There is only one Select option ("Talabat") today, so this proves
		# the key genuinely incorporates both fields rather than relying on a
		# second real platform value being available yet.
		self.assertEqual(
			compute_account_identity_key("Talabat", "ext-account-shared-id"),
			compute_account_identity_key("Talabat", "ext-account-shared-id"),
		)
		self.assertNotEqual(
			compute_account_identity_key("Talabat", "ext-account-shared-id"),
			compute_account_identity_key("Other Platform", "ext-account-shared-id"),
		)

	def test_missing_connection_rejected(self):
		# Frappe's own core mandatory-field check (_validate_mandatory(), in
		# document._validate()) runs AFTER run_before_save_methods() - i.e.
		# after this controller's validate()/validate_connection() has
		# already had a chance to run. So on this normal (non-bypassed) path,
		# the controller's own "Connection is required." throw always wins
		# the race and core's MandatoryError is never reached. Assert on the
		# controller's specific message (not just frappe.ValidationError,
		# which core's own MandatoryError is also a subclass of) so this test
		# can't silently start passing for the wrong reason if that race
		# order ever changes.
		with self.assertRaisesRegex(frappe.ValidationError, "Connection is required"):
			frappe.get_doc(
				{
					"doctype": "Marketplace Account",
					"account_name": "Test No Connection",
					"platform": "Talabat",
					"external_account_id": "ext-account-no-conn",
					"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
				}
			).insert()

		# Defense-in-depth: prove the controller's own check is not merely
		# "whatever happens to run first" by bypassing core's mandatory-field
		# check entirely (ignore_mandatory=True, the same flag a future bulk
		# migration patch might legitimately pass) and confirming
		# validate_connection() still rejects the missing connection on its
		# own, rather than silently letting a connection-less row through.
		with self.assertRaisesRegex(frappe.ValidationError, "Connection is required"):
			frappe.get_doc(
				{
					"doctype": "Marketplace Account",
					"account_name": "Test No Connection Bypassed Mandatory",
					"platform": "Talabat",
					"external_account_id": "ext-account-no-conn-bypass",
					"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
				}
			).insert(ignore_mandatory=True)

	def test_connection_doctype_mismatch_rejected(self):
		# connection_doctype says "UOM", but the referenced document only
		# exists as an "Item Group" record, not a "UOM" record.
		#
		# On the normal (non-bypassed) path, Frappe's own core link check
		# (_validate_links(), in document.insert()) runs BEFORE
		# run_before_save_methods() - i.e. before this controller's
		# validate()/validate_connection() ever runs - and raises its own
		# frappe.LinkValidationError (a ValidationError subclass) first. That
		# means a plain `assertRaises(frappe.ValidationError)` here would
		# stay green even if validate_connection()'s own frappe.db.exists
		# branch were deleted outright - it never gets reached on this path.
		#
		# Bypass core's link check with ignore_links=True (mirroring the
		# insert()-level flag a caller can legitimately pass) so this test
		# actually exercises - and proves the independent value of - the
		# controller's own check.
		with self.assertRaisesRegex(frappe.ValidationError, "does not exist as a"):
			frappe.get_doc(
				{
					"doctype": "Marketplace Account",
					"account_name": "Test Mismatched Connection",
					"platform": "Talabat",
					"external_account_id": "ext-account-mismatch",
					"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
					"connection": OTHER_DOCTYPE_RECORD,
				}
			).insert(ignore_links=True)

		# Sanity check confirming the fixture data this test depends on is
		# what we think it is - OTHER_DOCTYPE_RECORD really is absent from
		# STAND_IN_CONNECTION_DOCTYPE's table.
		self.assertTrue(frappe.db.exists(OTHER_DOCTYPE_WITH_DIFFERENT_RECORD, OTHER_DOCTYPE_RECORD))
		self.assertFalse(frappe.db.exists(STAND_IN_CONNECTION_DOCTYPE, OTHER_DOCTYPE_RECORD))

	def test_connection_doctype_mismatch_caught_by_core_link_validation_too(self):
		# Companion to test_connection_doctype_mismatch_rejected above: on the
		# normal (non-bypassed) path - the one every real Desk/API caller
		# actually takes - Frappe's own core _validate_links() catches this
		# same mismatch first, before the controller's validate() ever runs.
		# This documents that behavior explicitly (via the core-specific
		# exception type) rather than leaving it as an implicit assumption.
		with self.assertRaises(frappe.LinkValidationError):
			frappe.get_doc(
				{
					"doctype": "Marketplace Account",
					"account_name": "Test Mismatched Connection Core Path",
					"platform": "Talabat",
					"external_account_id": "ext-account-mismatch-core",
					"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
					"connection": OTHER_DOCTYPE_RECORD,
				}
			).insert()

	def test_opaque_external_account_id_survives_insert(self):
		# Unlike Talabat Product Mapping's talabat_sku (which feeds a custom
		# autoname() and so gets rejected outright at insert by Frappe's own
		# validate_name(), regardless of ignore_xss_filter), external_account_id
		# never becomes part of `name` here - naming is independent
		# (MKTACC-.#####) by design. So a value with angle brackets must
		# survive insert too, not just update.
		tricky_value = "ext-id-<p<+X>-insert-001"
		account = self.make_account("Test Opaque Insert", tricky_value)
		account.reload()

		self.assertEqual(account.external_account_id, tricky_value)
		self.assertNotIn(tricky_value, account.name)

	def test_opaque_external_account_id_survives_update(self):
		# ignore_xss_filter on external_account_id must preserve exact bytes,
		# including characters that would otherwise be stripped/mangled by
		# Document.save()'s HTML/XSS sanitizer - same footgun documented in
		# frappe-conventions.md and previously hit for real on
		# Talabat Product Mapping's talabat_sku / Talabat Connection's
		# client_secret.
		account = self.make_account("Test Opaque 1", "clean-ext-id-001")

		tricky_value = "ext-id-<p<+X>-001"
		account.external_account_id = tricky_value
		account.save()
		account.reload()

		self.assertEqual(account.external_account_id, tricky_value)
