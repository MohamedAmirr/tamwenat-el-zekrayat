# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.marketplace_order_presentation import get_item_presentation
from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.services.order_price_presentation import not_mapped_reason


class IntegrationTestMarketplaceOrderPresentation(IntegrationTestCase):
    def setUp(self):
        self.suffix = frappe.generate_hash(length=8)
        self.warehouse = frappe.get_all(
            "Warehouse", filters={"disabled": 0, "is_group": 0}, pluck="name", limit=1
        )[0]
        self.connection = frappe.get_doc(
            {
                "doctype": "Talabat Connection",
                "connection_name": f"Presentation Connection {self.suffix}",
                "client_id": "test-client-id",
                "client_secret": "test-client-secret",
                "chain_id": f"test-chain-{self.suffix}",
                "environment": "Sandbox",
                "enabled": 1,
            }
        ).insert()

    def _make_price_list(self, marker):
        return frappe.get_doc(
            {
                "doctype": "Price List",
                "price_list_name": f"Presentation Price List {marker} {self.suffix}",
                "selling": 1,
                "enabled": 1,
            }
        ).insert()

    def _make_account(self, marker, price_list=None, **kwargs):
        doc = {
            "doctype": "Marketplace Account",
            "account_name": f"Presentation Account {marker} {self.suffix}",
            "platform": "Talabat",
            "external_account_id": f"presentation-vendor-{marker}-{self.suffix}",
            "connection_doctype": "Talabat Connection",
            "connection": self.connection.name,
            "warehouse": self.warehouse,
            "erpnext_selling_price_list": price_list.name if price_list else None,
            "enabled": 1,
        }
        doc.update(kwargs)
        return frappe.get_doc(doc).insert()

    def _make_item(self, marker, **kwargs):
        doc = {
            "doctype": "Item",
            "item_code": f"TEST-PRESENTATION-{marker}-{self.suffix}",
            "item_group": "Products",
            "stock_uom": "Nos",
            "is_stock_item": 1,
        }
        doc.update(kwargs)
        return frappe.get_doc(doc).insert()

    def _make_mapping(self, account, item, sku):
        return frappe.get_doc(
            {
                "doctype": "Marketplace Product Mapping",
                "marketplace_account": account.name,
                "item": item.name,
                "external_reference": sku,
                "sales_uom": item.stock_uom,
                "enabled": 1,
            }
        ).insert()

    def _make_price(self, price_list, item, rate):
        return frappe.get_doc(
            {
                "doctype": "Item Price",
                "price_list": price_list.name,
                "item_code": item.name,
                "uom": item.stock_uom,
                "price_list_rate": rate,
                "selling": 1,
            }
        ).insert()

    def _make_order(self, account, items, promotion_status=None):
        return upsert_marketplace_order(
            account.name,
            {
                "external_order_id": f"presentation-order-{account.name}-{frappe.generate_hash(length=6)}",
                "latest_payload": {},
                "promotion_status": promotion_status,
                "items": items,
            },
        )

    def test_returns_live_mapping_status_and_catalog_price_per_item(self):
        item = self._make_item("mixed")
        price_list = self._make_price_list("mixed")
        account = self._make_account("mixed", price_list=price_list)
        self._make_mapping(account, item, "mapped-sku")
        self._make_price(price_list, item, 42)
        order = self._make_order(
            account,
            [
                {"external_item_id": "line-1", "external_sku": "mapped-sku"},
                {"external_item_id": "line-2", "external_sku": "unmapped-sku"},
            ],
        )

        result = get_item_presentation(order.name)

        self.assertNotIn("price_comparison_available", result)
        self.assertEqual(
            result["items"],
            [
                {
                    "external_item_id": "line-1",
                    "external_sku": "mapped-sku",
                    "mapped_item": item.name,
                    "current_catalog_price": 42.0,
                    "catalog_price_unavailable_reason": None,
                },
                {
                    "external_item_id": "line-2",
                    "external_sku": "unmapped-sku",
                    "mapped_item": None,
                    "current_catalog_price": None,
                    "catalog_price_unavailable_reason": not_mapped_reason(),
                },
            ],
        )

    def test_requires_read_permission_on_the_specific_order(self):
        price_list = self._make_price_list("perm")
        account = self._make_account("perm", price_list=price_list)
        order = self._make_order(account, [{"external_item_id": "line-1", "external_sku": "sku-1"}])
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": f"presentation-{frappe.generate_hash(length=8)}@example.com",
                "first_name": "Presentation Reader",
                "send_welcome_email": 0,
                "roles": [{"role": "All"}],
            }
        ).insert()

        with self.set_user(user.name), self.assertRaises(frappe.PermissionError):
            get_item_presentation(order.name)

    @patch(
        "marketplace_integrations.api.marketplace_order_presentation.frappe.has_permission",
        return_value=False,
    )
    def test_requires_mapping_read_permission(self, has_permission):
        price_list = self._make_price_list("perm-mapping")
        account = self._make_account("perm-mapping", price_list=price_list)
        order = self._make_order(account, [{"external_item_id": "line-1", "external_sku": "sku-1"}])

        with self.assertRaises(frappe.PermissionError):
            get_item_presentation(order.name)
        has_permission.assert_called_once_with("Marketplace Product Mapping", ptype="read")

    @patch("marketplace_integrations.api.marketplace_order_presentation.frappe.has_permission")
    def test_requires_item_price_read_permission(self, has_permission):
        # Marketplace Product Mapping read is allowed but Item Price read is
        # not - the response also carries Item Price/Price List rate data via
        # current_catalog_price(), so both checks must gate the endpoint.
        has_permission.side_effect = lambda doctype, ptype=None: doctype != "Item Price"
        price_list = self._make_price_list("perm-item-price")
        account = self._make_account("perm-item-price", price_list=price_list)
        order = self._make_order(account, [{"external_item_id": "line-1", "external_sku": "sku-1"}])

        with self.assertRaises(frappe.PermissionError):
            get_item_presentation(order.name)
        has_permission.assert_any_call("Item Price", ptype="read")

    @patch("marketplace_integrations.api.marketplace_order_presentation.resolve_catalog_sync_account")
    @patch(
        "marketplace_integrations.api.marketplace_order_presentation.resolve_mapping_and_item",
        return_value=None,
    )
    def test_account_resolution_runs_once_per_order_not_once_per_line(
        self, resolve_mapping_and_item, resolve_catalog_sync_account
    ):
        # #84's N+1 bug: current_catalog_price() used to re-run
        # validate_catalog_sync_account() (Marketplace Account, Talabat
        # Connection with password decryption, Price List, a Warehouse lookup)
        # once per order line. resolve_catalog_sync_account() must now be
        # called exactly once per order regardless of line count.
        resolve_catalog_sync_account.return_value = (object(), None)
        price_list = self._make_price_list("hoist")
        account = self._make_account("hoist", price_list=price_list)
        order = self._make_order(
            account,
            [
                {"external_item_id": "line-1", "external_sku": "sku-1"},
                {"external_item_id": "line-2", "external_sku": "sku-2"},
                {"external_item_id": "line-3", "external_sku": "sku-3"},
            ],
        )

        get_item_presentation(order.name)

        resolve_catalog_sync_account.assert_called_once_with(account.name)

    @patch("marketplace_integrations.api.marketplace_order_presentation.resolve_catalog_sync_account")
    def test_a_misconfigured_account_reports_the_same_reason_for_every_line_without_crashing(
        self, resolve_catalog_sync_account
    ):
        resolve_catalog_sync_account.return_value = (None, "Marketplace Account X is disabled.")
        price_list = self._make_price_list("broken")
        account = self._make_account("broken", price_list=price_list)
        order = self._make_order(
            account,
            [
                {"external_item_id": "line-1", "external_sku": "sku-1"},
                {"external_item_id": "line-2", "external_sku": "sku-2"},
            ],
        )

        result = get_item_presentation(order.name)

        for item in result["items"]:
            self.assertIsNone(item["current_catalog_price"])
            self.assertEqual(item["catalog_price_unavailable_reason"], "Marketplace Account X is disabled.")

    def test_two_accounts_different_selling_price_lists_show_different_catalog_prices(
        self,
    ):
        item = self._make_item("shared")
        price_list_a = self._make_price_list("A")
        price_list_b = self._make_price_list("B")
        account_a = self._make_account("A", price_list=price_list_a)
        account_b = self._make_account("B", price_list=price_list_b)
        self._make_mapping(account_a, item, "shared-sku")
        self._make_mapping(account_b, item, "shared-sku")
        self._make_price(price_list_a, item, 10)
        self._make_price(price_list_b, item, 25)
        order_a = self._make_order(account_a, [{"external_item_id": "line-1", "external_sku": "shared-sku"}])
        order_b = self._make_order(account_b, [{"external_item_id": "line-1", "external_sku": "shared-sku"}])

        result_a = get_item_presentation(order_a.name)
        result_b = get_item_presentation(order_b.name)

        self.assertEqual(result_a["items"][0]["current_catalog_price"], 10.0)
        self.assertEqual(result_b["items"][0]["current_catalog_price"], 25.0)
        self.assertIsNone(result_a["items"][0]["catalog_price_unavailable_reason"])
        self.assertIsNone(result_b["items"][0]["catalog_price_unavailable_reason"])

    def test_changing_item_price_changes_current_reference_only_stored_order_values_unchanged(self):
        item = self._make_item("changing")
        price_list = self._make_price_list("changing")
        account = self._make_account("changing", price_list=price_list)
        self._make_mapping(account, item, "changing-sku")
        item_price = self._make_price(price_list, item, 15)
        order = self._make_order(
            account,
            [
                {
                    "external_item_id": "line-1",
                    "external_sku": "changing-sku",
                    "original_list_price": 30.0,
                    "fulfilled_unit_price": 28.0,
                    "fulfilled_total_price": 28.0,
                }
            ],
        )

        before = get_item_presentation(order.name)
        self.assertEqual(before["items"][0]["current_catalog_price"], 15.0)

        item_price.price_list_rate = 40
        item_price.save()

        after = get_item_presentation(order.name)
        self.assertEqual(after["items"][0]["current_catalog_price"], 40.0)

        # The catalog price change above must never mutate the order's own
        # stored Talabat facts - those remain the historical order truth.
        order.reload()
        self.assertEqual(order.items[0].original_list_price, 30.0)
        self.assertEqual(order.items[0].fulfilled_unit_price, 28.0)
        self.assertEqual(order.items[0].fulfilled_total_price, 28.0)

    def test_missing_catalog_price_has_no_fallback(self):
        # standard_rate is deliberately set to a value that would be wrong if
        # it were ever used as a fallback - #84 requires "no fallback to
        # standard_rate/another Price List" when there is no current Item
        # Price in the account's configured Selling Price List.
        item = self._make_item("missing-price", standard_rate=999)
        price_list = self._make_price_list("missing-price")
        account = self._make_account("missing-price", price_list=price_list)
        self._make_mapping(account, item, "missing-price-sku")
        order = self._make_order(
            account, [{"external_item_id": "line-1", "external_sku": "missing-price-sku"}]
        )

        result = get_item_presentation(order.name)

        item_result = result["items"][0]
        self.assertIsNone(item_result["current_catalog_price"])
        self.assertIsNotNone(item_result["catalog_price_unavailable_reason"])
        self.assertNotIn("999", item_result["catalog_price_unavailable_reason"])

    def test_a_misconfigured_account_does_not_leak_a_stacked_message_log_entry(self):
        # validate_catalog_sync_account()'s underlying frappe.throw() (e.g.
        # "Marketplace Account X is disabled.") must not leave a stacked red
        # toast behind - only the returned reason should reach the client.
        price_list = self._make_price_list("no-toast")
        account = self._make_account("no-toast", price_list=price_list, enabled=0)
        order = self._make_order(account, [{"external_item_id": "line-1", "external_sku": "no-toast-sku"}])

        frappe.clear_messages()
        before = len(frappe.local.message_log)

        result = get_item_presentation(order.name)

        self.assertIsNotNone(result["items"][0]["catalog_price_unavailable_reason"])
        self.assertEqual(len(frappe.local.message_log), before)

    def test_promotion_status_is_not_duplicated_into_the_presentation_response(self):
        # promotion_status already lives on frm.doc.promotion_status - a
        # separately-returned copy here risks a second, potentially stale
        # source of truth client-side after a partial frm.reload_doc().
        price_list = self._make_price_list("promo")
        account = self._make_account("promo", price_list=price_list)
        order = self._make_order(
            account,
            [{"external_item_id": "line-1", "external_sku": "promo-sku"}],
            promotion_status="UNAVAILABLE",
        )

        result = get_item_presentation(order.name)

        self.assertNotIn("promotion_status", result)
        # This module reports facts only - no settlement/accounting keys
        # (e.g. paid/settled/collected) are ever added to the response.
        for forbidden_key in ("paid", "settled", "cash_received", "settlement_status"):
            self.assertNotIn(forbidden_key, result)
            self.assertNotIn(forbidden_key, result["items"][0])


class IntegrationTestShopifyItemPresentation(IntegrationTestCase):
    """#109 decision 9's Shopify branch of get_item_presentation()
    (_resolve_shopify_item()) - previously had zero test coverage."""

    def setUp(self):
        self.suffix = frappe.generate_hash(length=8)
        self.account = make_shopify_account(f"Shopify Presentation {self.suffix}")

    def _make_item(self, marker):
        return frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": f"TEST-SHOPIFY-PRESENTATION-{marker}-{self.suffix}",
                "item_group": "Products",
                "stock_uom": "Nos",
                "is_stock_item": 1,
            }
        ).insert()

    def _make_mapping(self, account, item, variant_gid):
        return frappe.get_doc(
            {
                "doctype": "Marketplace Product Mapping",
                "marketplace_account": account.name,
                "item": item.name,
                "external_reference": variant_gid,
                "sales_uom": item.stock_uom,
                "enabled": 1,
            }
        ).insert()

    def _make_order(self, account, items):
        return upsert_marketplace_order(
            account.name,
            {
                "external_order_id": f"shopify-presentation-order-{frappe.generate_hash(length=6)}",
                "latest_payload": {},
                "items": items,
            },
        )

    def test_mapped_variant_resolves_to_its_item(self):
        item = self._make_item("mapped")
        variant_gid = f"gid://shopify/ProductVariant/{self.suffix}"
        self._make_mapping(self.account, item, variant_gid)
        order = self._make_order(
            self.account,
            [{"external_item_id": "line-1", "external_variant_id": variant_gid}],
        )

        result = get_item_presentation(order.name)

        self.assertEqual(
            result["items"],
            [
                {
                    "external_item_id": "line-1",
                    "external_variant_id": variant_gid,
                    "mapped_item": item.name,
                }
            ],
        )

    def test_unmapped_variant_reports_none_not_an_error(self):
        variant_gid = f"gid://shopify/ProductVariant/{self.suffix}-unmapped"
        order = self._make_order(
            self.account,
            [{"external_item_id": "line-1", "external_variant_id": variant_gid}],
        )

        result = get_item_presentation(order.name)

        self.assertEqual(result["items"][0]["mapped_item"], None)

    def test_mapping_with_disabled_marketplace_account_is_treated_as_no_mapping(self):
        # Round-1 finding #5: resolve_item() must fail closed when the
        # mapping's OWN Marketplace Account is disabled, not just when the
        # mapping row itself is disabled - a hand-rolled {"enabled": 1}
        # filter on the mapping alone would miss this.
        item = self._make_item("disabled-account")
        variant_gid = f"gid://shopify/ProductVariant/{self.suffix}-disabled"
        mapping_account = make_shopify_account(f"Shopify Presentation Disabled {self.suffix}")
        self._make_mapping(mapping_account, item, variant_gid)
        mapping_account.enabled = 0
        mapping_account.save()

        # The order itself belongs to the now-disabled account, so
        # get_item_presentation() resolves against the exact
        # (marketplace_account, external_reference) pair whose account is
        # disabled.
        order = self._make_order(
            mapping_account,
            [{"external_item_id": "line-1", "external_variant_id": variant_gid}],
        )

        result = get_item_presentation(order.name)

        self.assertIsNone(result["items"][0]["mapped_item"])

    def test_custom_line_with_null_variant_is_distinct_from_unmapped(self):
        # Round-1 finding #8: a variant: null custom line must render the
        # distinct "No Shopify variant" case client-side - here that means
        # external_variant_id comes back blank/None, never concatenated with
        # an "Unmapped" mapped_item lookup attempt.
        order = self._make_order(
            self.account,
            [{"external_item_id": "line-1", "external_variant_id": None}],
        )

        result = get_item_presentation(order.name)

        self.assertIsNone(result["items"][0]["external_variant_id"])
        self.assertIsNone(result["items"][0]["mapped_item"])
