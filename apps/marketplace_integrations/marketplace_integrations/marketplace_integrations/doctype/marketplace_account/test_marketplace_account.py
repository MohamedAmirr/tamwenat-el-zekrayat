# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.marketplace_account import (
    compute_account_identity_key,
    shipping_account_problem,
)

# #103's Shopify shop-GID / Location cross-checks call
# integrations/shopify/locations.py's fetch_shop_and_active_locations(), which
# is a live (though read-only) Admin GraphQL call - never make that call for
# real from these tests. Every Shopify-platform test below patches this
# module-level import in marketplace_account.py with a canned result instead.
FETCH_SHOP_AND_ACTIVE_LOCATIONS = (
    "marketplace_integrations.marketplace_integrations.doctype.marketplace_account."
    "marketplace_account.fetch_shop_and_active_locations"
)

SHOPIFY_SHOP_GID = "gid://shopify/Shop/1000000001"
SHOPIFY_LOCATION_GID = "gid://shopify/Location/1"
SHOPIFY_LOCATION_NAME = "Test Shopify Warehouse"


def mock_shopify_verification(shop_id=SHOPIFY_SHOP_GID, active_locations=None):
    if active_locations is None:
        active_locations = [{"id": SHOPIFY_LOCATION_GID, "name": SHOPIFY_LOCATION_NAME}]
    return patch(
        FETCH_SHOP_AND_ACTIVE_LOCATIONS,
        return_value={"shop_id": shop_id, "active_locations": active_locations},
    )

# Talabat Connection and Shopify Connection both exist now (#37/#38, #101),
# but these particular Talabat-platform tests predate Talabat Connection and
# deliberately keep using "UOM"/"Item Group" as connection_doctype/connection
# stand-ins - they're testing this controller's own generic
# validate_connection()/mismatch behavior, independent of any one platform's
# real Connection DocType. "UOM" and "Item Group" are plain, always-present
# ERPNext master doctypes with a stable seeded record ("Nos", "Products" -
# both already relied on by talabat_integration's own tests), so they're used
# here purely as structural stand-ins for "a real DocType with a real
# document", not as any endorsement of UOM/Item Group as real connection
# targets.
STAND_IN_CONNECTION_DOCTYPE = "UOM"
STAND_IN_CONNECTION = "Nos"
OTHER_DOCTYPE_WITH_DIFFERENT_RECORD = "Item Group"
OTHER_DOCTYPE_RECORD = "Products"

# Frappe's IntegrationTestCase auto-generates a generic test record for every
# doctype linked from the one under test (Warehouse, plus #61's optional
# Company/Price List/Customer sync configuration). ERPNext's generic
# Company/Fiscal Year test-record generator hits a pre-existing Fiscal Year
# overlap bug on this dev site, unrelated to this doctype (same root cause
# documented in talabat_product_mapping's test file for its own Item
# dependency). These tests construct only their required Warehouse with the
# site's real configured company, so skip generic generation for every
# optional ERPNext Link target.
IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Warehouse",
	"Company",
	"Price List",
	"Customer",
	"Account",
	"Mode of Payment",
]


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

    def make_group_warehouse(self, warehouse_name):
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
                    "is_group": 1,
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

    def make_talabat_connection(self, connection_name="Test Platform Change Talabat Connection"):
        existing = frappe.db.exists("Talabat Connection", {"connection_name": connection_name})
        if existing:
            return existing

        return (
            frappe.get_doc(
                {
                    "doctype": "Talabat Connection",
                    "connection_name": connection_name,
                    "client_id": "test-client-id",
                    "client_secret": "test-client-secret",
                    "chain_id": "test-chain-id",
                    "environment": "Sandbox",
                }
            )
            .insert()
            .name
        )

    def make_shopify_connection(self, connection_name="Test Shopify Connection"):
        existing = frappe.db.exists("Shopify Connection", {"connection_name": connection_name})
        if existing:
            return existing

        # shop_domain carries a DB-level unique: 1 constraint - derive it
        # from connection_name (rather than a fixed literal) so two distinct
        # connection_name values used in the same test class never collide.
        # This stays deterministic/idempotent with the existing-lookup above:
        # the same connection_name always maps to the same shop_domain.
        domain_slug = connection_name.lower().replace(" ", "-")
        return (
            frappe.get_doc(
                {
                    "doctype": "Shopify Connection",
                    "connection_name": connection_name,
                    "shop_domain": f"{domain_slug}.myshopify.com",
                    "client_id": "test-client-id",
                    "client_secret": "test-client-secret",
                }
            )
            .insert()
            .name
        )

    def make_shopify_account(self, account_name, external_account_id=SHOPIFY_SHOP_GID, connection=None, **kwargs):
        """Caller is responsible for wrapping this in mock_shopify_verification()
        (or a differently-parameterized patch of the same target) - insert()
        always runs validate_shopify_account(), which calls the (mocked)
        fetch_shop_and_active_locations()."""
        doc = {
            "doctype": "Marketplace Account",
            "account_name": account_name,
            "platform": "Shopify",
            "external_account_id": external_account_id,
            "connection": connection or self.make_shopify_connection(),
        }
        doc.update(kwargs)
        return frappe.get_doc(doc).insert()

    def make_noon_connection(self, project_code):
        return (
            frappe.get_doc(
                {
                    "doctype": "Noon Connection",
                    "connection_name": f"Test {project_code}",
                    "project_code": project_code,
                    "key_id": f"test-key-{project_code}",
                    "private_key": "-----BEGIN PRIVATE KEY-----\ntest-only\n-----END PRIVATE KEY-----\n",
                    "environment": "Static Sandbox",
                    "enabled": 1,
                }
            )
            .insert()
            .name
        )

    def make_noon_account(self, account_name, external_account_id, connection):
        return frappe.get_doc(
            {
                "doctype": "Marketplace Account",
                "account_name": account_name,
                "platform": "Noon",
                "external_account_id": external_account_id,
                "connection": connection,
            }
        ).insert()

    def test_noon_platform_option_and_connection_doctype_default(self):
        meta = frappe.get_meta("Marketplace Account")
        self.assertIn("Noon", meta.get_field("platform").options.split("\n"))
        self.assertEqual(
            meta.get_field("external_account_id").mandatory_depends_on,
            'eval:doc.platform != "Shopify"',
        )
        account = frappe.get_doc({"doctype": "Marketplace Account", "platform": "Noon"})
        self.assertEqual(account.connection_doctype, "Noon Connection")

    def test_noon_account_saves_only_with_matching_project_code_and_without_network(self):
        project_code = "noon-account-match-143"
        connection = self.make_noon_connection(project_code)
        with patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected HTTP request")):
            account = self.make_noon_account("Test Noon Matching Project", project_code, connection)
        account.reload()
        self.assertEqual(account.connection_doctype, "Noon Connection")
        self.assertEqual(account.connection, connection)
        self.assertEqual(account.external_account_id, project_code)
        self.assertEqual(account.account_identity_key, compute_account_identity_key("Noon", project_code))

    def test_noon_account_rejects_wrong_or_blank_project_code(self):
        connection = self.make_noon_connection("noon-account-mismatch-143")
        for external_account_id in ("another-project", "", " noon-account-mismatch-143 "):
            with self.subTest(external_account_id=external_account_id):
                with self.assertRaisesRegex(frappe.ValidationError, "exactly match"):
                    self.make_noon_account("Test Noon Wrong Project", external_account_id, connection)

    def test_noon_account_rejects_wrong_connection_doctype(self):
        with self.assertRaisesRegex(frappe.ValidationError, "must use a Noon Connection"):
            frappe.get_doc(
                {
                    "doctype": "Marketplace Account",
                    "account_name": "Test Noon Wrong Connection Type",
                    "platform": "Noon",
                    "external_account_id": "noon-account-wrong-type-143",
                    "connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
                    "connection": STAND_IN_CONNECTION,
                }
            ).insert()

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

    def test_group_warehouse_is_rejected(self):
        group_warehouse = self.make_group_warehouse("Test Group Catalog Warehouse")

        with self.assertRaisesRegex(frappe.ValidationError, "leaf Warehouse"):
            self.make_account("Test Group Warehouse", "ext-account-group-warehouse", warehouse=group_warehouse)

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

    # --- #103: Shopify Marketplace Account / inventory Location binding ---

    def test_shopify_platform_option_exists(self):
        # Regression guard for the Select options change itself - a bare
        # assertion that "Shopify" is now a valid platform, independent of any
        # other Shopify-specific behavior below.
        meta = frappe.get_meta("Marketplace Account")
        platform_options = meta.get_field("platform").options.split("\n")
        self.assertIn("Talabat", platform_options)
        self.assertIn("Shopify", platform_options)

    def test_shopify_connection_doctype_defaults_when_blank(self):
        # Every Shopify test below uses its own distinct external_account_id
        # (paired with a matching mocked shop_id, when one is needed) -
        # IntegrationTestCase runs every test method in this class inside one
        # shared, not-yet-rolled-back transaction (see the "Test Records
        # created" line printed once for the whole class), so two test
        # methods reusing the same identity would collide with each other,
        # the same way two real accounts for the same shop would.
        external_account_id = "gid://shopify/Shop/2000000001"
        with mock_shopify_verification(shop_id=external_account_id):
            account = self.make_shopify_account(
                "Test Shopify Default Conn Doctype", external_account_id=external_account_id
            )
        account.reload()

        self.assertEqual(account.connection_doctype, "Shopify Connection")

    def test_shopify_account_shop_gid_self_heals_to_connections_own_shop(self):
        # Review finding #2: external_account_id is no longer trusted as a
        # typed/provided value at all for Shopify - it always ends up equal
        # to the live-verified shop GID, self-healing over whatever was
        # passed in rather than throwing a mismatch error.
        live_shop_id = "gid://shopify/Shop/999999"
        with mock_shopify_verification(shop_id=live_shop_id):
            account = self.make_shopify_account(
                "Test Shopify Shop Self Heal", external_account_id="gid://shopify/Shop/2000000002"
            )
        account.reload()

        self.assertEqual(account.external_account_id, live_shop_id)

    def test_shopify_account_matching_shop_gid_saves(self):
        external_account_id = "gid://shopify/Shop/2000000003"
        with mock_shopify_verification(shop_id=external_account_id):
            account = self.make_shopify_account(
                "Test Shopify Shop Match", external_account_id=external_account_id
            )
        account.reload()

        self.assertEqual(account.external_account_id, external_account_id)

    def test_shopify_location_optional(self):
        external_account_id = "gid://shopify/Shop/2000000004"
        with mock_shopify_verification(shop_id=external_account_id):
            account = self.make_shopify_account(
                "Test Shopify No Location", external_account_id=external_account_id
            )
        account.reload()

        self.assertFalse(account.shopify_location_gid)
        self.assertFalse(account.shopify_location_name)

    def test_shopify_inactive_location_rejected(self):
        external_account_id = "gid://shopify/Shop/2000000005"
        with mock_shopify_verification(
            shop_id=external_account_id,
            active_locations=[{"id": SHOPIFY_LOCATION_GID, "name": "Active One"}],
        ):
            with self.assertRaisesRegex(frappe.ValidationError, "not an active Location"):
                self.make_shopify_account(
                    "Test Shopify Inactive Location",
                    external_account_id=external_account_id,
                    shopify_location_gid="gid://shopify/Location/does-not-exist",
                )

    def test_shopify_active_location_sets_display_name(self):
        external_account_id = "gid://shopify/Shop/2000000006"
        with mock_shopify_verification(shop_id=external_account_id):
            account = self.make_shopify_account(
                "Test Shopify Active Location",
                external_account_id=external_account_id,
                shopify_location_gid=SHOPIFY_LOCATION_GID,
            )
        account.reload()

        self.assertEqual(account.shopify_location_gid, SHOPIFY_LOCATION_GID)
        self.assertEqual(account.shopify_location_name, SHOPIFY_LOCATION_NAME)

    def test_shopify_group_warehouse_still_rejected(self):
        # Same generic leaf-Warehouse-only rule as Talabat - #103 reuses
        # validate_warehouse() as-is, no Shopify-specific duplicate.
        group_warehouse = self.make_group_warehouse("Test Shopify Group Catalog Warehouse")
        external_account_id = "gid://shopify/Shop/2000000007"

        with mock_shopify_verification(shop_id=external_account_id):
            with self.assertRaisesRegex(frappe.ValidationError, "leaf Warehouse"):
                self.make_shopify_account(
                    "Test Shopify Group Warehouse",
                    external_account_id=external_account_id,
                    warehouse=group_warehouse,
                )

    def test_second_enabled_shopify_account_same_shop_rejected(self):
        # One enabled Shopify Marketplace Account per shop in v1: the existing
        # (platform, external_account_id) identity uniqueness already
        # enforces this generically (it rejects ANY duplicate, not just an
        # enabled one) - this test proves that generic mechanism actually
        # covers the Shopify case end-to-end, not just that it exists.
        external_account_id = "gid://shopify/Shop/2000000008"
        with mock_shopify_verification(shop_id=external_account_id):
            self.make_shopify_account("Test Shopify Shop Dup 1", external_account_id=external_account_id)

            with self.assertRaisesRegex(frappe.ValidationError, "already exists"):
                self.make_shopify_account("Test Shopify Shop Dup 2", external_account_id=external_account_id)

    def test_shopify_wrong_connection_doctype_rejected(self):
        # connection_doctype is only ever auto-defaulted when blank
        # (set_connection_doctype()) - an explicitly mismatched value (e.g. a
        # Shopify account mistakenly pointed at a Talabat Connection-shaped
        # doctype) must still be rejected by validate_shopify_account() itself.
        with self.assertRaisesRegex(frappe.ValidationError, "must use a Shopify Connection"):
            frappe.get_doc(
                {
                    "doctype": "Marketplace Account",
                    "account_name": "Test Shopify Wrong Connection Doctype",
                    "platform": "Shopify",
                    "external_account_id": "gid://shopify/Shop/1000000004",
                    "connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
                    "connection": STAND_IN_CONNECTION,
                }
            ).insert()

    def test_talabat_account_unaffected_by_shopify_addition(self):
        # Explicit regression proof for the acceptance criterion "existing
        # Talabat accounts must save unchanged after the platform options
        # change" - a Talabat account never calls
        # fetch_shop_and_active_locations() (unpatched here - a real call
        # would fail loudly if this ever regressed) and keeps its own
        # connection_doctype untouched.
        account = self.make_account("Test Talabat Unaffected", "ext-account-shopify-regression")
        account.reload()

        self.assertEqual(account.platform, "Talabat")
        self.assertEqual(account.connection_doctype, STAND_IN_CONNECTION_DOCTYPE)
        self.assertFalse(account.shopify_location_gid)
        self.assertFalse(account.shopify_location_name)

    # --- #103 review findings ---

    def test_shopify_external_account_id_auto_populated_when_blank(self):
        # UX change: external_account_id is no longer manually typed for a
        # Shopify account - when blank, it's set from the live-confirmed shop
        # GID instead of throwing a mismatch.
        live_shop_id = "gid://shopify/Shop/2000000101"
        with mock_shopify_verification(shop_id=live_shop_id):
            account = self.make_shopify_account(
                "Test Shopify Auto Populate", external_account_id="", connection=self.make_shopify_connection()
            )
        account.reload()

        self.assertEqual(account.external_account_id, live_shop_id)

    def test_shopify_mismatched_external_account_id_self_heals_to_live_value(self):
        # Review finding #2: a disagreeing (non-blank) external_account_id
        # used to throw an unrecoverable "does not match" error - since the
        # field is read-only on the form for Shopify accounts, there was no
        # way for a user to fix it (e.g. repointing an existing account at a
        # different Connection, or converting a Talabat account to Shopify
        # while carrying over its old vendor ID). It must now self-heal to
        # the live-verified value instead of throwing.
        live_shop_id = "gid://shopify/Shop/999999999"
        with mock_shopify_verification(shop_id=live_shop_id):
            account = self.make_shopify_account(
                "Test Shopify Mismatch Self Heals",
                external_account_id="gid://shopify/Shop/2000000102",
            )
        account.reload()

        self.assertEqual(account.external_account_id, live_shop_id)

    def test_shopify_blank_live_shop_id_rejected_with_clear_error(self):
        # Review finding #6: the blank-external_account_id branch must guard
        # against a falsy live_shop_id too (the non-blank/mismatch branch
        # already did) - otherwise external_account_id silently ends up
        # None/blank and the failure only surfaces later as a confusing
        # generic "Platform and External Account ID are required" error from
        # the identity-key computation, instead of a clear Shopify-specific
        # one right here.
        with mock_shopify_verification(shop_id=None):
            with self.assertRaisesRegex(frappe.ValidationError, "Could not verify Shopify Connection"):
                self.make_shopify_account(
                    "Test Shopify Blank Live Shop Id", external_account_id=""
                )

    def test_new_shopify_account_saves_from_desk_with_blank_external_account_id(self):
        # Review finding #1: external_account_id is `reqd` in the doctype
        # JSON and is also made read-only client-side for Shopify accounts on
        # the form (marketplace_account.js). Frappe's client-side
        # check_mandatory (frappe's own save.js) blocks the Save button on
        # any `reqd` field with no value regardless of read_only - so a plain
        # `reqd: 1` would make a NEW Shopify Marketplace Account impossible to
        # save from Desk at all. The JSON field now uses
        # `mandatory_depends_on: eval:doc.platform != "Shopify"` instead, so
        # it's still required for Talabat but not for Shopify (where the
        # server fills it in here during validate()).
        #
        # Known gap: this test still goes through frappe.get_doc(...).insert()
        # like every other test in this file - i.e. straight through the
        # server ORM - so it does NOT exercise Desk's client-side
        # check_mandatory() itself. That client-vs-server gap is inherent to
        # every Python test in this file (none of them run a browser), not
        # something this specific test can close; what it does prove is that
        # nothing server-side treats a blank external_account_id as an error
        # for a brand-new Shopify account, which is the other half of the bug
        # (a plain `reqd: 1` field is also enforced server-side by core's own
        # mandatory check, so if that half were still broken this insert()
        # would fail too, just with a different error).
        meta = frappe.get_meta("Marketplace Account")
        external_account_id_field = meta.get_field("external_account_id")
        self.assertFalse(external_account_id_field.reqd)
        self.assertEqual(
            external_account_id_field.mandatory_depends_on, 'eval:doc.platform != "Shopify"'
        )

        live_shop_id = "gid://shopify/Shop/2000000110"
        with mock_shopify_verification(shop_id=live_shop_id):
            account = self.make_shopify_account(
                "Test New Shopify Account Blank External Id From Desk", external_account_id=""
            )
        account.reload()

        self.assertEqual(account.external_account_id, live_shop_id)

    def test_shopify_save_skips_live_check_when_unrelated_field_changes(self):
        # Review finding #1: a save that only touches Warehouse/Price
        # List/Customer/enabled on an already-verified Shopify account must
        # not re-trigger the live Shopify call.
        external_account_id = "gid://shopify/Shop/2000000103"
        warehouse = self.make_warehouse("Test Shopify Skip Live Check Warehouse")
        with mock_shopify_verification(shop_id=external_account_id):
            account = self.make_shopify_account(
                "Test Shopify Skip Live Check", external_account_id=external_account_id
            )

        with patch(FETCH_SHOP_AND_ACTIVE_LOCATIONS) as mocked_fetch:
            account.warehouse = warehouse
            account.enabled = 0
            account.save()

        mocked_fetch.assert_not_called()
        account.reload()
        self.assertEqual(account.warehouse, warehouse)
        self.assertEqual(account.enabled, 0)

    def test_shopify_save_reruns_live_check_when_connection_changes(self):
        # Companion to the above: changing `connection` on an existing
        # Shopify account IS Shopify-relevant and must still re-trigger the
        # live check.
        external_account_id = "gid://shopify/Shop/2000000104"
        with mock_shopify_verification(shop_id=external_account_id):
            account = self.make_shopify_account(
                "Test Shopify Rerun On Connection Change", external_account_id=external_account_id
            )

        other_connection = self.make_shopify_connection("Test Shopify Connection Other")
        with mock_shopify_verification(shop_id=external_account_id) as mocked_fetch:
            account.connection = other_connection
            account.save()

        mocked_fetch.assert_called_once()
        account.reload()
        self.assertEqual(account.connection, other_connection)

    def test_shopify_save_reruns_live_check_when_location_gid_changes(self):
        external_account_id = "gid://shopify/Shop/2000000105"
        with mock_shopify_verification(shop_id=external_account_id):
            account = self.make_shopify_account(
                "Test Shopify Rerun On Location Change", external_account_id=external_account_id
            )

        with mock_shopify_verification(shop_id=external_account_id) as mocked_fetch:
            account.shopify_location_gid = SHOPIFY_LOCATION_GID
            account.save()

        mocked_fetch.assert_called_once()
        account.reload()
        self.assertEqual(account.shopify_location_gid, SHOPIFY_LOCATION_GID)
        self.assertEqual(account.shopify_location_name, SHOPIFY_LOCATION_NAME)

    def test_sales_order_sync_customer_creation_does_not_trigger_live_shopify_check(self):
        # Review finding #2: services/sales_order_sync.py's
        # _get_or_create_customer() calls account.save() after only setting
        # erpnext_customer - with finding #1's fix in place, that save must
        # not re-trigger a live Shopify call. This proves that end-to-end
        # through the same code path that caller actually uses, rather than
        # only through this controller's own save().
        from marketplace_integrations.services.sales_order_sync import _get_or_create_customer

        external_account_id = "gid://shopify/Shop/2000000106"
        with mock_shopify_verification(shop_id=external_account_id):
            account = self.make_shopify_account(
                "Test Shopify Sales Order Sync Customer", external_account_id=external_account_id
            )

        with patch(FETCH_SHOP_AND_ACTIVE_LOCATIONS) as mocked_fetch:
            customer_name = _get_or_create_customer(account)

        mocked_fetch.assert_not_called()
        account.reload()
        self.assertEqual(account.erpnext_customer, customer_name)
        self.assertTrue(frappe.db.exists("Customer", customer_name))

    def test_platform_change_to_shopify_updates_connection_doctype(self):
        # Review finding #4: switching platform on an existing account must
        # not strand it on the old connection_doctype.
        #
        # connection_doctype is set explicitly here, alongside platform and
        # connection, rather than left for update_connection_doctype_on_
        # platform_change() to derive during validate(): core's own
        # _validate_links() validates the `connection` Dynamic Link against
        # whatever `connection_doctype` already holds BEFORE validate() ever
        # runs (docs/frappe-conventions.md's _validate_links()-ordering
        # gotcha) - so setting a new-platform `connection` in the same save
        # would otherwise get checked against the still-old connection_doctype
        # and fail before update_connection_doctype_on_platform_change() gets
        # a chance to re-derive it. In real Desk usage the client script sets
        # connection_doctype the moment platform changes, before the save
        # round-trip even starts; a Python caller doing the equivalent must do
        # the same (the same caller-assists-construction gap already
        # documented on set_connection_doctype()/__init__ for the "still
        # blank" case applies here too, for the "changed platform on an
        # existing document" case).
        account = self.make_account("Test Platform Change To Shopify", "ext-account-platform-change-1")
        shopify_connection = self.make_shopify_connection()

        with mock_shopify_verification(shop_id="gid://shopify/Shop/2000000107"):
            account.platform = "Shopify"
            account.connection_doctype = "Shopify Connection"
            account.connection = shopify_connection
            account.external_account_id = "gid://shopify/Shop/2000000107"
            account.save()
        account.reload()

        self.assertEqual(account.platform, "Shopify")
        self.assertEqual(account.connection_doctype, "Shopify Connection")

    def test_platform_change_to_talabat_updates_connection_doctype(self):
        # connection_doctype must switch to "Talabat Connection" automatically
        # (update_connection_doctype_on_platform_change() overrides the old
        # Shopify Connection value on this platform change) - a real Talabat
        # Connection is used here (rather than the STAND_IN_CONNECTION_DOCTYPE
        # stand-in used elsewhere in this file) precisely so `connection`
        # resolves against whatever connection_doctype actually ends up set,
        # proving the automatic override rather than a value this test forced.
        # See test_platform_change_to_shopify_updates_connection_doctype()'s
        # comment: connection_doctype must be set explicitly here too, for the
        # same _validate_links()-ordering reason - update_connection_doctype_
        # on_platform_change() only runs (via validate()) after core has
        # already validated `connection` against whatever connection_doctype
        # held before this save.
        external_account_id = "gid://shopify/Shop/2000000108"
        with mock_shopify_verification(shop_id=external_account_id):
            account = self.make_shopify_account(
                "Test Platform Change To Talabat", external_account_id=external_account_id
            )

        account.platform = "Talabat"
        account.connection_doctype = "Talabat Connection"
        account.connection = self.make_talabat_connection()
        account.external_account_id = "ext-account-platform-change-2"
        account.save()
        account.reload()

        self.assertEqual(account.platform, "Talabat")
        self.assertEqual(account.connection_doctype, "Talabat Connection")

    def test_new_doc_attribute_assignment_construction_still_needs_explicit_connection_doctype(self):
        # Review finding #8: frappe.new_doc() + plain attribute assignment
        # reaches __init__ with `platform` still blank (assigned only
        # afterwards), so set_connection_doctype() has nothing to default
        # from at that point. Core's own _validate_links() then throws on
        # the still-blank connection_doctype before validate()'s own
        # belt-and-suspenders re-default ever gets a chance to run. This
        # documents that residual gap rather than claiming it's fixed - see
        # set_connection_doctype()'s docstring and
        # docs/frappe-conventions.md's ordering-gotcha bullet.
        doc = frappe.new_doc("Marketplace Account")
        doc.account_name = "Test New Doc Construction Gap"
        doc.platform = "Shopify"
        doc.external_account_id = "gid://shopify/Shop/2000000109"
        doc.connection = self.make_shopify_connection()

        with self.assertRaises(frappe.ValidationError):
            doc.insert()

    def test_platform_connection_doctypes_python_js_parity(self):
        # Review finding #5: PLATFORM_CONNECTION_DOCTYPES is duplicated in
        # Python and JS (documented, unavoidable duplication - see
        # marketplace_account.js's own comment on why). Parse the JS
        # object literal with a simple regex (no JS runtime needed) and
        # assert it matches the Python dict exactly, so any future drift
        # fails a test immediately.
        import re

        from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.marketplace_account import (
            PLATFORM_CONNECTION_DOCTYPES,
        )

        js_path = frappe.get_app_path(
            "marketplace_integrations",
            "marketplace_integrations",
            "doctype",
            "marketplace_account",
            "marketplace_account.js",
        )
        with open(js_path) as f:
            js_source = f.read()

        match = re.search(
            r"const PLATFORM_CONNECTION_DOCTYPES\s*=\s*\{(?P<body>[^}]*)\}", js_source
        )
        self.assertIsNotNone(match, "PLATFORM_CONNECTION_DOCTYPES object literal not found in JS file")

        entries = re.findall(r"(\w+)\s*:\s*\"([^\"]*)\"", match.group("body"))
        js_mapping = dict(entries)

        self.assertEqual(js_mapping, PLATFORM_CONNECTION_DOCTYPES)

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

    def _shipping_account_candidates(self):
        company = frappe.defaults.get_global_default("company")
        income = frappe.get_all(
            "Account",
            filters={"company": company, "is_group": 0, "disabled": 0, "root_type": "Income"},
            pluck="name",
            limit=1,
        )[0]
        group = frappe.get_all(
            "Account", filters={"company": company, "is_group": 1, "root_type": "Income"}, pluck="name", limit=1
        )[0]
        wrong_type = frappe.get_all(
            "Account",
            filters={"company": company, "is_group": 0, "account_type": "Cost of Goods Sold"},
            pluck="name",
            limit=1,
        )[0]
        return company, income, group, wrong_type

    def test_shopify_shipping_account_accepts_an_income_account(self):
        company, income, _group, _wrong = self._shipping_account_candidates()
        external_account_id = "gid://shopify/Shop/9000000101"
        with mock_shopify_verification(shop_id=external_account_id):
            account = self.make_shopify_account(
                "Test Shopify Shipping Account",
                external_account_id=external_account_id,
                erpnext_company=company,
                erpnext_shipping_account=income,
            )
        account.reload()
        self.assertEqual(account.erpnext_shipping_account, income)

    def test_shopify_shipping_account_rejects_group_disabled_and_wrong_type_accounts(self):
        company, _income, group, wrong_type = self._shipping_account_candidates()
        disabled = frappe.get_doc(
            {
                "doctype": "Account",
                "account_name": "Test Disabled Shipping Income",
                "parent_account": group,
                "company": company,
                "disabled": 1,
            }
        ).insert()
        cases = {"is a group account": group, "is disabled": disabled.name, "must be an Income": wrong_type}
        for index, (message, shipping_account) in enumerate(cases.items()):
            external_account_id = f"gid://shopify/Shop/90000002{index:02d}"
            with self.subTest(case=message), mock_shopify_verification(shop_id=external_account_id):
                with self.assertRaisesRegex(frappe.ValidationError, message):
                    self.make_shopify_account(
                        f"Test Shopify Bad Shipping {index}",
                        external_account_id=external_account_id,
                        erpnext_company=company,
                        erpnext_shipping_account=shipping_account,
                    )

    def test_shipping_account_of_another_company_or_missing_company_is_a_problem(self):
        company, income, _group, _wrong = self._shipping_account_candidates()
        self.assertIsNone(shipping_account_problem(income, company))
        self.assertIn("does not belong to Company", shipping_account_problem(income, "Another Company"))
        self.assertIn("Set the ERPNext Company", shipping_account_problem(income, None))
        self.assertIn("does not exist", shipping_account_problem("No Such Account", company))
