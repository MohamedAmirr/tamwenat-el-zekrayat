# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#103 review finding #6: no test module previously existed for
fetch_shop_and_active_locations() itself. Every test here mocks
ShopifyClient.execute() directly - never a real Shopify call - since this
module's own docstring establishes it's a live (though read-only) Admin
GraphQL call."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify.locations import fetch_shop_and_active_locations

EXECUTE_TARGET = "marketplace_integrations.integrations.shopify.client.ShopifyClient.execute"


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


class IntegrationTestFetchShopAndActiveLocations(IntegrationTestCase):
    def setUp(self):
        self.connection = make_connection("Test Locations Connection")

    def test_null_shop(self):
        with patch(EXECUTE_TARGET, return_value={"shop": None, "locations": {"edges": []}}):
            result = fetch_shop_and_active_locations(self.connection.name)

        self.assertIsNone(result["shop_id"])
        self.assertEqual(result["active_locations"], [])

    def test_null_edges(self):
        with patch(EXECUTE_TARGET, return_value={"shop": {"id": "gid://shopify/Shop/1"}, "locations": {"edges": None}}):
            result = fetch_shop_and_active_locations(self.connection.name)

        self.assertEqual(result["shop_id"], "gid://shopify/Shop/1")
        self.assertEqual(result["active_locations"], [])

    def test_edge_with_null_node(self):
        with patch(
            EXECUTE_TARGET,
            return_value={
                "shop": {"id": "gid://shopify/Shop/1"},
                "locations": {"edges": [{"node": None}]},
            },
        ):
            result = fetch_shop_and_active_locations(self.connection.name)

        self.assertEqual(result["active_locations"], [])

    def test_location_missing_is_active_excluded(self):
        with patch(
            EXECUTE_TARGET,
            return_value={
                "shop": {"id": "gid://shopify/Shop/1"},
                "locations": {
                    "edges": [
                        {"node": {"id": "gid://shopify/Location/1", "name": "No isActive key"}},
                    ]
                },
            },
        ):
            result = fetch_shop_and_active_locations(self.connection.name)

        self.assertEqual(result["active_locations"], [])

    def test_only_active_locations_returned(self):
        with patch(
            EXECUTE_TARGET,
            return_value={
                "shop": {"id": "gid://shopify/Shop/1"},
                "locations": {
                    "edges": [
                        {"node": {"id": "gid://shopify/Location/1", "name": "Active", "isActive": True}},
                        {"node": {"id": "gid://shopify/Location/2", "name": "Inactive", "isActive": False}},
                    ]
                },
            },
        ):
            result = fetch_shop_and_active_locations(self.connection.name)

        self.assertEqual(result["shop_id"], "gid://shopify/Shop/1")
        self.assertEqual(result["active_locations"], [{"id": "gid://shopify/Location/1", "name": "Active"}])
