# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.marketplace_integrations.doctype.marketplace_product_mapping.marketplace_product_mapping import (
	compute_mapping_identity_key,
)
from marketplace_integrations.services.mappings import resolve_item, resolve_mapping

# No platform-specific "Connection" DocType exists yet (Talabat Connection
# lands under #37/#38) - same stand-in rationale as
# test_marketplace_account.py: "UOM"/"Nos" are plain, always-present ERPNext
# master doctype/record used purely as a structural stand-in for "a real
# DocType with a real document" for Marketplace Account's Dynamic Link
# connection fields, not any endorsement of UOM as a real connection target.
STAND_IN_CONNECTION_DOCTYPE = "UOM"
STAND_IN_CONNECTION = "Nos"

# Frappe's IntegrationTestCase auto-generates a generic test record for every
# doctype linked from the one under test's meta:
# - "Item" hits ERPNext's pre-existing Company/Fiscal Year overlap bug on this
#   dev site, unrelated to this doctype (same root cause documented in
#   talabat_product_mapping's and marketplace_account's own test files) - we
#   build our own minimal Item below instead.
# - "Marketplace Account" can't be safely auto-generated generically either:
#   it has its own mandatory Dynamic Link (`connection`) whose valid values
#   depend entirely on whatever `connection_doctype` happens to be set to,
#   which a generic field-filler has no way to satisfy correctly. We build
#   our own Marketplace Account below (mirroring marketplace_account.py's own
#   make_account() helper) instead of relying on that auto-generator.
# - "UOM" (via `sales_uom`) doesn't itself trigger any Company/Fiscal Year
#   creation - the actual failure mode is that Frappe's test-dependency
#   walker, while resolving UOM as a transitive Link target, imports UOM
#   Category's test module as a side effect (to check whether IT has its own
#   test-record requirements), and erpnext/tests/utils.py's
#   ERPNextTestSuite/BootStrapTestData() runs AT MODULE IMPORT TIME (not
#   record-creation time) and tries to create a Fiscal Year that collides
#   with this dev site's real 2026-2027 fiscal year. "UOM" is already fully
#   available on the site (e.g. "Nos"), so skipping its auto-generation here
#   avoids ever triggering that import walk in the first place. This is a
#   general hazard, not specific to UOM: ANY transitive Link-field hop (not
#   just a field's direct target) to a doctype whose test module imports
#   erpnext.tests.utils can trigger this, so this list may need a new entry
#   whenever a future Link field on this doctype introduces a fresh
#   transitive path - see docs/frappe-conventions.md.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Item", "Marketplace Account", "UOM"]

TEST_ITEM = "TEST-MPM-ITEM"
TEST_ITEM_2 = "TEST-MPM-ITEM-2"


class IntegrationTestMarketplaceProductMapping(IntegrationTestCase):
	def setUp(self):
		for item_code in (TEST_ITEM, TEST_ITEM_2):
			if not frappe.db.exists("Item", item_code):
				frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": item_code,
						"item_group": "Products",
						"stock_uom": "Nos",
						"is_stock_item": 0,
					}
				).insert()

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

	def make_mapping(self, marketplace_account, item, external_reference, **kwargs):
		doc = {
			"doctype": "Marketplace Product Mapping",
			"marketplace_account": marketplace_account,
			"item": item,
			"external_reference": external_reference,
		}
		doc.update(kwargs)
		return frappe.get_doc(doc).insert()

	def test_fields_persist_and_identity_key_computed(self):
		account = self.make_account("Test MPM Account 1", "ext-acct-mpm-001")

		mapping = self.make_mapping(
			account.name,
			TEST_ITEM,
			"ext-ref-001",
			external_product_id="prod-001",
			external_sku="sku-001",
			external_barcode="barcode-001",
			external_variant_id="variant-001",
			sales_uom="Nos",
		)
		mapping.reload()

		self.assertEqual(mapping.marketplace_account, account.name)
		self.assertEqual(mapping.item, TEST_ITEM)
		self.assertEqual(mapping.external_reference, "ext-ref-001")
		self.assertEqual(mapping.external_product_id, "prod-001")
		self.assertEqual(mapping.external_sku, "sku-001")
		self.assertEqual(mapping.external_barcode, "barcode-001")
		self.assertEqual(mapping.external_variant_id, "variant-001")
		self.assertEqual(mapping.sales_uom, "Nos")
		self.assertEqual(mapping.enabled, 1)
		self.assertEqual(mapping.sync_status, "Not Synced")
		self.assertEqual(
			mapping.mapping_identity_key,
			compute_mapping_identity_key(account.name, "ext-ref-001"),
		)
		# Naming is expression/random (MPM-.#####), independent of any
		# external identifier - the document name must not be derived from
		# external_reference (or any other external_* field).
		self.assertNotIn("ext-ref-001", mapping.name)
		self.assertTrue(mapping.name.startswith("MPM-"))

	def test_one_item_two_mappings_same_account(self):
		# Acceptance: one Item can have two different mappings under the same
		# Marketplace Account (e.g. a bundle listing and a single-unit
		# listing of the same product) - explicitly NOT unique on
		# (marketplace_account, item).
		account = self.make_account("Test MPM Account Bundle", "ext-acct-mpm-bundle")

		bundle_mapping = self.make_mapping(account.name, TEST_ITEM, "ext-ref-bundle")
		single_mapping = self.make_mapping(account.name, TEST_ITEM, "ext-ref-single")

		self.assertEqual(bundle_mapping.item, TEST_ITEM)
		self.assertEqual(single_mapping.item, TEST_ITEM)
		self.assertEqual(bundle_mapping.marketplace_account, account.name)
		self.assertEqual(single_mapping.marketplace_account, account.name)
		self.assertNotEqual(bundle_mapping.name, single_mapping.name)

	def test_duplicate_external_reference_same_account_rejected_by_precheck(self):
		# Normal insert flow: validate_unique_mapping_identity()'s friendly
		# check runs before before_save() ever writes mapping_identity_key,
		# so this is expected to surface as a plain ValidationError.
		account = self.make_account("Test MPM Account Dup", "ext-acct-mpm-dup")
		self.make_mapping(account.name, TEST_ITEM, "ext-ref-dup-001")

		with self.assertRaises(frappe.ValidationError):
			self.make_mapping(account.name, TEST_ITEM_2, "ext-ref-dup-001")

	def test_duplicate_mapping_identity_key_rejected_at_db_level(self):
		# Prove the uniqueness is a real DB-level constraint, not merely the
		# validate()-time precheck above. Bypass this controller's own
		# validate()/before_save() entirely (flags.ignore_validate) and force
		# a raw db_update() with a manually-collided mapping_identity_key -
		# MariaDB's own unique index on the field must still reject it.
		account = self.make_account("Test MPM Account DB Dup", "ext-acct-mpm-dbdup")
		mapping_1 = self.make_mapping(account.name, TEST_ITEM, "ext-ref-dbdup-001")
		mapping_2 = self.make_mapping(account.name, TEST_ITEM_2, "ext-ref-dbdup-002")

		mapping_2.mapping_identity_key = mapping_1.mapping_identity_key
		mapping_2.flags.ignore_validate = True

		with self.assertRaises(frappe.UniqueValidationError):
			mapping_2.save()

	def test_duplicate_rejected_when_external_reference_differs_only_by_whitespace(self):
		# external_reference is a free-text Data field - unlike Marketplace
		# Account's `platform` (a Select field, normalized by Frappe's own
		# _validate_selects() before it's stored), Frappe applies NO
		# normalization to Data field values at all by default. This
		# doctype's own before_save() strips external_reference itself before
		# it's persisted (mirroring compute_mapping_identity_key()'s internal
		# normalization), so " ext-ref-ws-001 " is stored as the canonical
		# "ext-ref-ws-001" - not with its whitespace intact - and a second
		# insert of the same value (whitespace or not) hashes to the
		# identical key and is correctly rejected as a duplicate.
		account = self.make_account("Test MPM Account WS", "ext-acct-mpm-ws")
		first = self.make_mapping(account.name, TEST_ITEM, "ext-ref-ws-001")
		first.reload()

		# The stored value itself must be canonicalized (trimmed), not merely
		# "hashes the same as the trimmed value" - otherwise Desk list-view
		# filtering, frappe.db.get_value(..., {"external_reference": x})
		# lookups, and export/edit/re-import round trips could all silently
		# miss this row despite resolve_item() still resolving it correctly.
		self.assertEqual(first.external_reference, "ext-ref-ws-001")

		with self.assertRaisesRegex(frappe.ValidationError, "already exists"):
			self.make_mapping(account.name, TEST_ITEM_2, " ext-ref-ws-001 ")

		second_untrimmed = self.make_mapping(account.name, TEST_ITEM_2, " ext-ref-ws-002 ")
		second_untrimmed.reload()
		self.assertEqual(second_untrimmed.external_reference, "ext-ref-ws-002")

	def test_same_external_reference_different_accounts_allowed(self):
		account_a = self.make_account("Test MPM Account A", "ext-acct-mpm-a")
		account_b = self.make_account("Test MPM Account B", "ext-acct-mpm-b")

		mapping_a = self.make_mapping(account_a.name, TEST_ITEM, "ext-ref-shared")
		mapping_b = self.make_mapping(account_b.name, TEST_ITEM, "ext-ref-shared")

		self.assertEqual(mapping_a.external_reference, "ext-ref-shared")
		self.assertEqual(mapping_b.external_reference, "ext-ref-shared")
		self.assertNotEqual(mapping_a.marketplace_account, mapping_b.marketplace_account)
		self.assertNotEqual(
			compute_mapping_identity_key(mapping_a.marketplace_account, "ext-ref-shared"),
			compute_mapping_identity_key(mapping_b.marketplace_account, "ext-ref-shared"),
		)

	def test_resolve_item_exact_match_only(self):
		account = self.make_account("Test MPM Account Resolve", "ext-acct-mpm-resolve")
		self.make_mapping(account.name, TEST_ITEM, "ext-ref-resolve-001", external_sku="sku-resolve-001")

		self.assertEqual(resolve_item(account.name, "ext-ref-resolve-001"), TEST_ITEM)

		# No fuzzy/fallback matching: a value that only matches external_sku
		# (not external_reference) must not resolve, even though it's stored
		# on the same mapping row.
		self.assertIsNone(resolve_item(account.name, "sku-resolve-001"))

		# Unrelated external_reference under the same account: no match.
		self.assertIsNone(resolve_item(account.name, "ext-ref-resolve-nonexistent"))

	def test_resolve_item_returns_none_for_disabled_mapping(self):
		account = self.make_account("Test MPM Account Disabled", "ext-acct-mpm-disabled")
		self.make_mapping(account.name, TEST_ITEM, "ext-ref-disabled-001", enabled=0)

		# A disabled mapping must resolve to nothing, the same as no mapping
		# at all - callers fail closed rather than syncing/fulfilling against
		# a mapping an operator has intentionally turned off.
		self.assertIsNone(resolve_item(account.name, "ext-ref-disabled-001"))

	def test_resolve_item_returns_none_for_disabled_account(self):
		# The mapping itself is enabled - only its Marketplace Account is
		# disabled. resolve_item() must still fail closed: an operator
		# disabling a Marketplace Account (the whole point of that field)
		# must not leave every mapping under it silently still resolving.
		account = self.make_account(
			"Test MPM Account Disabled Account", "ext-acct-mpm-disabled-account", enabled=0
		)
		mapping = self.make_mapping(account.name, TEST_ITEM, "ext-ref-disabled-account-001")

		self.assertEqual(mapping.enabled, 1)
		self.assertEqual(account.enabled, 0)
		self.assertIsNone(resolve_item(account.name, "ext-ref-disabled-account-001"))

	def test_resolve_mapping_returns_the_mapping_document_name(self):
		# resolve_mapping() must have exactly the same resolution semantics as
		# resolve_item() above (same identity key, same enabled-mapping AND
		# enabled-account requirement) - it only returns a different field of
		# the same resolved row, for callers that need the mapping document
		# itself (e.g. #84's catalog price comparison needs the mapping's
		# sales_uom for #6's get_listing_price()).
		account = self.make_account("Test MPM Account Resolve Mapping", "ext-acct-mpm-resolve-mapping")
		mapping = self.make_mapping(account.name, TEST_ITEM, "ext-ref-resolve-mapping-001")

		self.assertEqual(resolve_mapping(account.name, "ext-ref-resolve-mapping-001"), mapping.name)
		self.assertIsNone(resolve_mapping(account.name, "ext-ref-resolve-mapping-nonexistent"))

	def test_resolve_mapping_returns_none_for_disabled_mapping_or_account(self):
		disabled_mapping_account = self.make_account(
			"Test MPM Account Resolve Mapping Disabled", "ext-acct-mpm-resolve-mapping-disabled"
		)
		self.make_mapping(
			disabled_mapping_account.name, TEST_ITEM, "ext-ref-resolve-mapping-disabled-001", enabled=0
		)
		self.assertIsNone(
			resolve_mapping(disabled_mapping_account.name, "ext-ref-resolve-mapping-disabled-001")
		)

		disabled_account = self.make_account(
			"Test MPM Account Resolve Mapping Disabled Account",
			"ext-acct-mpm-resolve-mapping-disabled-account",
			enabled=0,
		)
		self.make_mapping(disabled_account.name, TEST_ITEM, "ext-ref-resolve-mapping-disabled-account-001")
		self.assertIsNone(
			resolve_mapping(disabled_account.name, "ext-ref-resolve-mapping-disabled-account-001")
		)

	def test_resolve_mapping_returns_none_for_blank_input(self):
		self.assertIsNone(resolve_mapping(None, "ext-ref-resolve-mapping-blank"))
		self.assertIsNone(resolve_mapping("Test MPM Account Resolve Mapping", None))

	def test_resolve_item_returns_none_for_blank_input(self):
		# resolve_item()'s documented contract is "Item or None" - a blank/
		# missing marketplace_account or external_reference (e.g. a future
		# webhook handler processing an order with a missing product
		# reference) must get a clean None, not an unhandled ValidationError
		# propagated up from compute_mapping_identity_key()'s own
		# frappe.throw() (which is correct behavior for that function's other
		# caller, before_save() - just not for this read-only resolver).
		account = self.make_account("Test MPM Account Blank Input", "ext-acct-mpm-blank-input")
		self.make_mapping(account.name, TEST_ITEM, "ext-ref-blank-input-001")

		self.assertIsNone(resolve_item("", "ext-ref-blank-input-001"))
		self.assertIsNone(resolve_item(None, "ext-ref-blank-input-001"))
		self.assertIsNone(resolve_item(account.name, ""))
		self.assertIsNone(resolve_item(account.name, None))
		self.assertIsNone(resolve_item("", ""))
		self.assertIsNone(resolve_item(None, None))
		# Whitespace-only input normalizes to empty too.
		self.assertIsNone(resolve_item("   ", "ext-ref-blank-input-001"))

	def test_resolve_item_scoped_to_marketplace_account(self):
		account_a = self.make_account("Test MPM Account Resolve A", "ext-acct-mpm-resolve-a")
		account_b = self.make_account("Test MPM Account Resolve B", "ext-acct-mpm-resolve-b")
		self.make_mapping(account_a.name, TEST_ITEM, "ext-ref-scoped")

		self.assertEqual(resolve_item(account_a.name, "ext-ref-scoped"), TEST_ITEM)
		# Same external_reference string, but under a different Marketplace
		# Account with no mapping of its own - must not resolve.
		self.assertIsNone(resolve_item(account_b.name, "ext-ref-scoped"))

	def test_opaque_fields_survive_insert_with_xss_lookalike_characters(self):
		# ignore_xss_filter must preserve exact bytes on every opaque field,
		# including characters that would otherwise be stripped/mangled by
		# Document.save()'s HTML/XSS sanitizer - same footgun documented in
		# frappe-conventions.md and previously hit for real on Talabat
		# Product Mapping's talabat_sku / Talabat Connection's client_secret.
		account = self.make_account("Test MPM Account Opaque Insert", "ext-acct-mpm-opaque-insert")
		tricky = "<p<+X>-001"

		mapping = self.make_mapping(
			account.name,
			TEST_ITEM,
			f"ext-ref-opaque{tricky}",
			external_product_id=f"prod-opaque{tricky}",
			external_sku=f"sku-opaque{tricky}",
			external_barcode=f"barcode-opaque{tricky}",
			external_variant_id=f"variant-opaque{tricky}",
		)
		mapping.reload()

		self.assertEqual(mapping.external_reference, f"ext-ref-opaque{tricky}")
		self.assertEqual(mapping.external_product_id, f"prod-opaque{tricky}")
		self.assertEqual(mapping.external_sku, f"sku-opaque{tricky}")
		self.assertEqual(mapping.external_barcode, f"barcode-opaque{tricky}")
		self.assertEqual(mapping.external_variant_id, f"variant-opaque{tricky}")
		self.assertNotIn(tricky, mapping.name)

	def test_opaque_fields_survive_update_with_xss_lookalike_characters(self):
		account = self.make_account("Test MPM Account Opaque Update", "ext-acct-mpm-opaque-update")
		mapping = self.make_mapping(account.name, TEST_ITEM, "ext-ref-opaque-update-clean")

		tricky = "<p<+X>-002"
		mapping.external_sku = f"sku-opaque{tricky}"
		mapping.external_barcode = f"barcode-opaque{tricky}"
		mapping.external_variant_id = f"variant-opaque{tricky}"
		mapping.external_product_id = f"prod-opaque{tricky}"
		mapping.last_sync_reference = f"sync-ref-opaque{tricky}"
		mapping.save()
		mapping.reload()

		self.assertEqual(mapping.external_sku, f"sku-opaque{tricky}")
		self.assertEqual(mapping.external_barcode, f"barcode-opaque{tricky}")
		self.assertEqual(mapping.external_variant_id, f"variant-opaque{tricky}")
		self.assertEqual(mapping.external_product_id, f"prod-opaque{tricky}")
		self.assertEqual(mapping.last_sync_reference, f"sync-ref-opaque{tricky}")

	def test_optional_external_fields_may_be_blank(self):
		# Only marketplace_account, item, and external_reference are
		# required - external_product_id/external_sku/external_barcode/
		# external_variant_id/sales_uom must persist as blank when omitted,
		# not silently required or defaulted.
		account = self.make_account("Test MPM Account Minimal", "ext-acct-mpm-minimal")
		mapping = self.make_mapping(account.name, TEST_ITEM, "ext-ref-minimal-001")
		mapping.reload()

		self.assertFalse(mapping.external_product_id)
		self.assertFalse(mapping.external_sku)
		self.assertFalse(mapping.external_barcode)
		self.assertFalse(mapping.external_variant_id)
		self.assertFalse(mapping.sales_uom)

	def test_changing_item_on_existing_mapping_persists(self):
		# Naming is expression-based (MPM-.#####), fully decoupled from item -
		# changing item on an existing mapping must actually persist, not be
		# silently reverted the way a field-based autoname would (the #19
		# bug class this whole design deliberately avoids).
		account = self.make_account("Test MPM Account Change Item", "ext-acct-mpm-change-item")
		mapping = self.make_mapping(account.name, TEST_ITEM, "ext-ref-change-item-001")

		mapping.item = TEST_ITEM_2
		mapping.save()
		mapping.reload()

		self.assertEqual(mapping.item, TEST_ITEM_2)

	def test_changing_external_reference_recomputes_identity_key_and_frees_old_key(self):
		# before_save() recomputes mapping_identity_key on every save, not
		# just insert - changing external_reference on an EXISTING row must
		# update the key to match the new value, and the OLD key must become
		# free for a brand new mapping to reuse (proving the old row's
		# occupancy of that key was actually released, not merely shadowed).
		account = self.make_account("Test MPM Account Recompute", "ext-acct-mpm-recompute")
		mapping = self.make_mapping(account.name, TEST_ITEM, "ext-ref-recompute-old")
		old_key = mapping.mapping_identity_key
		self.assertEqual(old_key, compute_mapping_identity_key(account.name, "ext-ref-recompute-old"))

		mapping.external_reference = "ext-ref-recompute-new"
		mapping.save()
		mapping.reload()

		self.assertEqual(
			mapping.mapping_identity_key,
			compute_mapping_identity_key(account.name, "ext-ref-recompute-new"),
		)
		self.assertNotEqual(mapping.mapping_identity_key, old_key)

		# The old key is genuinely free now - a new mapping reusing the old
		# external_reference under the same account must succeed, not be
		# rejected as a phantom duplicate of the row that moved away from it.
		reused = self.make_mapping(account.name, TEST_ITEM_2, "ext-ref-recompute-old")
		self.assertEqual(reused.mapping_identity_key, old_key)

	def test_update_path_collision_rejected(self):
		# Companion to test_duplicate_external_reference_same_account_rejected_
		# by_precheck (insert path only): prove the update-path branch of the
		# precheck (`"name": ["!=", self.name or ""]` in
		# validate_unique_mapping_identity()) is actually exercised with a
		# real, non-empty self.name - i.e. that updating an EXISTING row's
		# external_reference to collide with a DIFFERENT existing row is
		# rejected too, not just a fresh insert colliding with an existing row.
		account = self.make_account("Test MPM Account Update Collision", "ext-acct-mpm-update-collision")
		first = self.make_mapping(account.name, TEST_ITEM, "ext-ref-update-collision-001")
		second = self.make_mapping(account.name, TEST_ITEM_2, "ext-ref-update-collision-002")

		second.external_reference = "ext-ref-update-collision-001"
		with self.assertRaisesRegex(frappe.ValidationError, "already exists"):
			second.save()

		# The rejected update must not have partially persisted.
		second.reload()
		self.assertEqual(second.external_reference, "ext-ref-update-collision-002")
		self.assertEqual(
			first.mapping_identity_key,
			compute_mapping_identity_key(account.name, "ext-ref-update-collision-001"),
		)
