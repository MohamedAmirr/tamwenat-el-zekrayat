# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#103 review finding #6: no test module previously existed for
api/shopify_locations.py. Every test here mocks ShopifyClient.execute()
directly - never a real Shopify call."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.shopify_locations import list_active_locations

EXECUTE_TARGET = "marketplace_integrations.integrations.shopify.client.ShopifyClient.execute"

GRAPHQL_RESULT = {
    "shop": {"id": "gid://shopify/Shop/1"},
    "locations": {
        "edges": [
            {"node": {"id": "gid://shopify/Location/1", "name": "Active", "isActive": True}},
        ]
    },
}


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


class IntegrationTestListActiveLocations(IntegrationTestCase):
    def setUp(self):
        self.connection = make_connection("Test API Locations Connection")

    def test_requires_marketplace_account_write_permission(self):
        # Review finding #7: this now checks write on Marketplace Account
        # (doctype-level, since the picker also needs to work on a brand-new,
        # unsaved Marketplace Account), not read on Shopify Connection.
        with patch("frappe.has_permission", return_value=False) as mocked_has_permission:
            with self.assertRaises(frappe.PermissionError):
                list_active_locations(self.connection.name)

        mocked_has_permission.assert_called_once_with("Marketplace Account", "write")

    def test_returns_active_locations_when_permitted(self):
        with (
            patch("frappe.has_permission", return_value=True),
            patch(EXECUTE_TARGET, return_value=GRAPHQL_RESULT),
        ):
            result = list_active_locations(self.connection.name)

        self.assertEqual(result, [{"id": "gid://shopify/Location/1", "name": "Active"}])

    def test_nonexistent_connection_rejected_with_clean_error(self):
        # Review finding #8: a bad/nonexistent Connection name must surface a
        # clean ValidationError from this API boundary, not an unhandled
        # frappe.DoesNotExistError from deep inside ShopifyClient.
        with patch("frappe.has_permission", return_value=True):
            with self.assertRaisesRegex(frappe.ValidationError, "does not exist"):
                list_active_locations("Some Nonexistent Shopify Connection")

    def test_post_only(self):
        # Review finding #6: list_active_locations is POST-only - assert the
        # whitelisting metadata rather than exercising Frappe's HTTP method
        # dispatch itself (which would need a real request context).
        self.assertEqual(
            frappe.allowed_http_methods_for_whitelisted_func[list_active_locations], ["POST"]
        )
