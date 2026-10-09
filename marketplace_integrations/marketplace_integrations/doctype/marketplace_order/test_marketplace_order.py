# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.test_marketplace_account import (
    mock_shopify_verification,
)
from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
    compute_order_identity_key,
)
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order

# Marketplace Account has a required Dynamic Link that generic test-record
# generation cannot satisfy. Sales Order is intentionally only a future Link
# target; this issue must never create one.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Marketplace Account", "Sales Order"]


class IntegrationTestMarketplaceOrder(IntegrationTestCase):
    def make_account(self):
        suffix = frappe.generate_hash(length=8)
        connection = frappe.get_doc(
            {
                "doctype": "Talabat Connection",
                "connection_name": f"Order Test Connection {suffix}",
                "client_id": f"order-test-client-{suffix}",
                "client_secret": "order-test-secret",
                "chain_id": f"order-test-chain-{suffix}",
            }
        ).insert()
        return frappe.get_doc(
            {
                "doctype": "Marketplace Account",
                "account_name": f"Order Test Account {suffix}",
                "platform": "Talabat",
                "external_account_id": f"order-test-vendor-{suffix}",
                "connection_doctype": "Talabat Connection",
                "connection": connection.name,
            }
        ).insert()

    def sample_order(self, external_order_id="order-001"):
        payload = {
            "order_id": external_order_id,
            "comment": "Preserve <literal> Talabat content",
            "customer": {"_id": "customer-001", "delivery_address": {"city": "Cairo"}},
        }
        return {
            "external_order_id": external_order_id,
            "platform_order_reference": "platform-order-001",
            "order_code": "PICKUP-001",
            "external_status": "RECEIVED",
            "order_type": "DELIVERY",
            "transport_type": "LOGISTICS_DELIVERY",
            "promotion_status": "AVAILABLE",
            "is_preorder": 0,
            "accepted_for": "2026-08-31 13:00:00",
            "promised_for": "2026-08-31 13:30:00",
            "received_on": "2026-08-31 12:00:00",
            "last_platform_update_on": "2026-08-31 12:05:00",
            "cancelled_by": None,
            "cancellation_reason": None,
            "post_picked_up": 0,
            "customer_external_id": "customer-001",
            "customer_first_name": "Masked",
            "customer_last_name": "Customer",
            "customer_phone_number": "***",
            "customer_formatted_address": "Masked address",
            "customer_city": "Cairo",
            "customer_latitude": 30.0444,
            "customer_longitude": 31.2357,
            "payment_type": "PAID",
            "sub_total": 100.0,
            "discount": -5.0,
            "order_total": 110.0,
            "total_taxes": 2.0,
            "delivery_fee": 10.0,
            "service_fee": 3.0,
            "container_charge": 0.0,
            "difference_to_minimum": 0.0,
            "collect_at_pickup": None,
            "discounts": [{"name": "<Promotion>", "value": -5.0}],
            "additional_fees": {"holiday_fee": 2.0},
            "latest_payload": payload,
            "items": [
                {
                    "external_item_id": "line-unit-001",
                    "external_sku": "SKU-UNIT-001",
                    "product_name": "Unit Item",
                    "instructions": "No substitutions",
                    "barcodes": ["123"],
                    "external_status": "IN_CART",
                    "replaced_external_item_id": None,
                    "pricing_type": "UNIT",
                    "original_quantity": 2,
                    "original_weight": None,
                    "original_weighted_pieces": None,
                    "original_unit_price": 20.0,
                    "original_list_price": 25.0,
                    "original_total_price": 40.0,
                    "fulfilled_quantity": 2,
                    "fulfilled_weight": None,
                    "fulfilled_weighted_pieces": None,
                    "fulfilled_unit_price": 20.0,
                    "fulfilled_total_price": 40.0,
                    "container_deposit": 0.25,
                    "promotions": [
                        {
                            "name": "Drinks 0.5l and 1l on sale",
                            "type": "STRIKETHROUGH",
                            "discount_amount": 2.25,
                            "sponsorships": [{"sponsor": "VENDOR", "amount": 2.25}],
                        }
                    ],
                },
                {
                    "external_item_id": "line-kg-001",
                    "external_sku": "SKU-KG-001",
                    "product_name": "KG Item",
                    "instructions": None,
                    "barcodes": ["456"],
                    "external_status": "IN_CART",
                    "replaced_external_item_id": None,
                    "pricing_type": "KG",
                    "original_quantity": 1,
                    "original_weight": 1.25,
                    "original_weighted_pieces": 3,
                    "original_unit_price": 40.0,
                    "original_list_price": 45.0,
                    "original_total_price": 50.0,
                    "fulfilled_quantity": 1,
                    "fulfilled_weight": 1.2,
                    "fulfilled_weighted_pieces": 3,
                    "fulfilled_unit_price": 40.0,
                    "fulfilled_total_price": 48.0,
                },
            ],
        }

    def test_upsert_preserves_operational_order_data_without_creating_sales_order(self):
        account = self.make_account()
        order = upsert_marketplace_order(account.name, self.sample_order())
        order.reload()

        self.assertEqual(order.marketplace_account, account.name)
        self.assertEqual(order.order_identity_key, compute_order_identity_key(account.name, "order-001"))
        self.assertTrue(order.name.startswith("MKTORD-"))
        self.assertEqual(order.customer_external_id, "customer-001")
        self.assertEqual(order.customer_city, "Cairo")
        self.assertEqual(frappe.parse_json(order.latest_payload)["order_id"], "order-001")
        self.assertEqual(
            frappe.parse_json(order.latest_payload)["comment"], "Preserve <literal> Talabat content"
        )
        self.assertEqual(frappe.parse_json(order.discounts)[0]["name"], "<Promotion>")
        self.assertEqual(frappe.parse_json(order.discounts)[0]["value"], -5.0)
        self.assertEqual(frappe.parse_json(order.additional_fees)["holiday_fee"], 2.0)
        self.assertEqual(order.promotion_status, "AVAILABLE")
        self.assertIsNone(order.erpnext_sales_order)
        self.assertEqual(order.erpnext_sync_status, "Not Synced")

        unit_line = next(row for row in order.items if row.external_item_id == "line-unit-001")
        kg_line = next(row for row in order.items if row.external_item_id == "line-kg-001")
        self.assertEqual(unit_line.original_quantity, 2)
        self.assertEqual(unit_line.container_deposit, 0.25)
        promotion = frappe.parse_json(unit_line.promotions)[0]
        self.assertEqual(promotion["name"], "Drinks 0.5l and 1l on sale")
        self.assertEqual(promotion["type"], "STRIKETHROUGH")
        self.assertEqual(promotion["discount_amount"], 2.25)
        self.assertEqual(promotion["sponsorships"], [{"sponsor": "VENDOR", "amount": 2.25}])
        # Frappe persists an empty Float as 0.0 on reload; the separate
        # pricing_type still tells the later UI whether quantity or weight is
        # the meaningful fulfillment dimension.
        self.assertEqual(unit_line.original_weight, 0.0)
        self.assertEqual(kg_line.original_weight, 1.25)
        self.assertEqual(kg_line.fulfilled_weight, 1.2)
        self.assertEqual(kg_line.fulfilled_weighted_pieces, 3)

    def test_reprocessing_updates_existing_lines_by_external_item_id_without_duplicates(self):
        account = self.make_account()
        first = upsert_marketplace_order(account.name, self.sample_order())
        unit_line_name = next(row.name for row in first.items if row.external_item_id == "line-unit-001")

        updated = self.sample_order()
        updated["external_status"] = "READY_FOR_PICKUP"
        updated["promotion_status"] = "UNAVAILABLE"
        updated["items"][0]["fulfilled_quantity"] = 1
        updated["items"][0]["container_deposit"] = 0.5
        updated["items"][0]["promotions"] = []
        updated["items"].append(
            {
                "external_item_id": "line-addition-001",
                "external_sku": "SKU-ADDITION-001",
                "product_name": "Added Item",
                "instructions": None,
                "barcodes": [],
                "external_status": "ADDITION",
                "replaced_external_item_id": None,
                "pricing_type": "UNIT",
                "original_quantity": None,
                "original_weight": None,
                "original_weighted_pieces": None,
                "original_unit_price": None,
                "original_list_price": None,
                "original_total_price": None,
                "fulfilled_quantity": 1,
                "fulfilled_weight": None,
                "fulfilled_weighted_pieces": None,
                "fulfilled_unit_price": 10.0,
                "fulfilled_total_price": 10.0,
            }
        )
        second = upsert_marketplace_order(account.name, updated)
        second.reload()

        self.assertEqual(first.name, second.name)
        self.assertEqual(len(second.items), 3)
        unit_line = next(row for row in second.items if row.external_item_id == "line-unit-001")
        self.assertEqual(unit_line.name, unit_line_name)
        self.assertEqual(unit_line.fulfilled_quantity, 1)
        self.assertEqual(second.external_status, "READY_FOR_PICKUP")
        self.assertEqual(second.promotion_status, "UNAVAILABLE")
        self.assertEqual(unit_line.container_deposit, 0.5)
        self.assertEqual(frappe.parse_json(unit_line.promotions), [])
        self.assertEqual(
            frappe.db.count("Marketplace Order", {"order_identity_key": second.order_identity_key}), 1
        )

    def test_replacement_relationship_uses_external_item_ids_not_sku(self):
        account = self.make_account()
        order_values = self.sample_order()
        order_values["items"][0]["external_status"] = "REPLACED"
        order_values["items"].append(
            {
                **order_values["items"][0],
                "external_item_id": "line-replacement-001",
                "external_sku": "SKU-REPLACEMENT-001",
                "external_status": "IN_CART",
                "replaced_external_item_id": "line-unit-001",
            }
        )

        order = upsert_marketplace_order(account.name, order_values)
        replacement = next(row for row in order.items if row.external_item_id == "line-replacement-001")
        self.assertEqual(replacement.replaced_external_item_id, "line-unit-001")
        self.assertEqual(replacement.external_sku, "SKU-REPLACEMENT-001")

    def test_duplicate_account_and_external_order_id_is_rejected_by_controller(self):
        account = self.make_account()
        upsert_marketplace_order(account.name, self.sample_order())

        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc(
                {
                    "doctype": "Marketplace Order",
                    "marketplace_account": account.name,
                    "external_order_id": " order-001 ",
                }
            ).insert()

    def test_order_identity_key_has_a_database_unique_constraint(self):
        account = self.make_account()
        first = upsert_marketplace_order(account.name, self.sample_order())
        second = frappe.get_doc(
            {
                "doctype": "Marketplace Order",
                "marketplace_account": account.name,
                "external_order_id": "order-002",
            }
        )
        second.order_identity_key = first.order_identity_key
        second.flags.ignore_validate = True

        with self.assertRaises(frappe.UniqueValidationError):
            second.save()

    def test_upsert_requires_the_full_latest_talabat_payload(self):
        account = self.make_account()
        order = self.sample_order()
        order.pop("latest_payload")

        with self.assertRaises(frappe.ValidationError):
            upsert_marketplace_order(account.name, order)

    def test_upsert_persists_unavailable_promotion_status_with_empty_promotions(self):
        """UNAVAILABLE promotion_status persists alongside empty item
        promotions, while payment totals remain intact - this service never
        derives or adjusts them based on promotion availability."""
        account = self.make_account()
        order_values = self.sample_order()
        order_values["promotion_status"] = "UNAVAILABLE"
        order_values["items"][0]["promotions"] = []

        order = upsert_marketplace_order(account.name, order_values)
        order.reload()

        self.assertEqual(order.promotion_status, "UNAVAILABLE")
        self.assertEqual(order.sub_total, 100.0)
        self.assertEqual(order.order_total, 110.0)
        unit_line = next(row for row in order.items if row.external_item_id == "line-unit-001")
        self.assertEqual(frappe.parse_json(unit_line.promotions), [])

    def test_upsert_persists_an_unrecognized_promotion_status_without_raising(self):
        """promotion_status is a Data field, not a closed Select: a value
        Talabat hasn't documented yet (a future third status) must persist
        as-is rather than failing the whole order's persistence."""
        account = self.make_account()
        order_values = self.sample_order()
        order_values["promotion_status"] = "PARTIALLY_AVAILABLE"

        order = upsert_marketplace_order(account.name, order_values)
        order.reload()

        self.assertEqual(order.promotion_status, "PARTIALLY_AVAILABLE")

    def test_payment_field_labels_do_not_claim_settlement_or_receipt(self):
        """#84 wording requirements: `payment_type` and `collect_at_pickup`
        must use the agreed non-committal labels, never a settlement/payment
        status word like Paid/Settled/Cash Received, since #75 has not yet
        provided actual settlement evidence."""
        meta = frappe.get_meta("Marketplace Order")

        payment_type_label = meta.get_field("payment_type").label
        self.assertEqual(payment_type_label, "Talabat Payment Type")

        collect_at_pickup_label = meta.get_field("collect_at_pickup").label
        self.assertEqual(collect_at_pickup_label, "Cash Talabat says should be collected at delivery")
        for forbidden_word in ("Paid", "Settled", "Received"):
            self.assertNotIn(forbidden_word, collect_at_pickup_label)
            self.assertNotIn(forbidden_word, payment_type_label)

    def test_platform_is_fetched_from_the_linked_marketplace_account(self):
        """#108: platform is never set by a normalized payload/adapter - it
        is always derived from the linked Marketplace Account via
        fetch_from, on every save."""
        account = self.make_account()
        order = upsert_marketplace_order(account.name, self.sample_order())
        order.reload()

        self.assertEqual(order.platform, "Talabat")

    def test_external_status_is_a_generic_data_field_not_a_closed_select(self):
        """#108 decision 2: external_status converts from Select to Data at
        the order level so an unrecognized future value from any platform
        persists rather than failing validation. Talabat's own 5 existing
        values must still round-trip byte-for-byte."""
        meta = frappe.get_meta("Marketplace Order")
        external_status_field = meta.get_field("external_status")
        self.assertEqual(external_status_field.fieldtype, "Data")
        self.assertEqual(external_status_field.label, "External Status")
        self.assertFalse(external_status_field.options)

        account = self.make_account()
        order_values = self.sample_order()
        order_values["external_status"] = "SOME_FUTURE_TALABAT_STATUS"
        order = upsert_marketplace_order(account.name, order_values)
        order.reload()
        self.assertEqual(order.external_status, "SOME_FUTURE_TALABAT_STATUS")

    def test_item_external_status_stays_a_select_relabeled_generically(self):
        """#108 decision 2: only the label changes for the item-level field -
        it stays a Select, since sales_order_sync.py relies on its exact
        closed set of values. Expanded in #145 to include Noon statuses."""
        meta = frappe.get_meta("Marketplace Order Item")
        item_status_field = meta.get_field("external_status")
        self.assertEqual(item_status_field.fieldtype, "Select")
        self.assertEqual(item_status_field.label, "External Item Status")
        self.assertEqual(item_status_field.options, "NOT_PROCESSED\nIN_CART\nNOT_FOUND\nREPLACED\nADDITION\nCONFIRMED\nCANCELLED\nMP_ITEM_STATUS_UNSPECIFIED")

    def test_relabeled_fields_keep_their_original_fieldnames(self):
        """#108 decision 3: latest_payload/talabat_order_presentation are
        relabeled only - renaming the fieldname would break every existing
        reference/patch, so this must never happen."""
        order_meta = frappe.get_meta("Marketplace Order")
        self.assertEqual(order_meta.get_field("latest_payload").label, "Latest Platform Payload")
        self.assertEqual(order_meta.get_field("talabat_order_presentation").label, "Item Presentation")
        self.assertIsNotNone(order_meta.get_field("latest_payload"))
        self.assertIsNotNone(order_meta.get_field("talabat_order_presentation"))

    def test_upsert_persists_new_evidence_fields_and_leaves_them_blank_for_talabat(self):
        """#108 decision 10: the Talabat normalizer never emits
        customer_email/shop_currency/presentment_currency/billing_address/
        shipping_address/payment_transactions/refunds/external_variant_id -
        they must persist as None on every Talabat upsert without breaking
        anything, exactly like discounts/additional_fees already do when
        Talabat omits them."""
        account = self.make_account()
        order = upsert_marketplace_order(account.name, self.sample_order())
        order.reload()

        self.assertFalse(order.customer_email)
        self.assertFalse(order.shop_currency)
        self.assertFalse(order.presentment_currency)
        self.assertFalse(order.billing_address)
        self.assertFalse(order.shipping_address)
        self.assertFalse(order.payment_transactions)
        self.assertFalse(order.refunds)
        unit_line = next(row for row in order.items if row.external_item_id == "line-unit-001")
        self.assertFalse(unit_line.external_variant_id)

    def test_full_talabat_round_trip_after_schema_change(self):
        """End-to-end proof that #108's schema change did not alter any
        Talabat-observable behavior: the same sample order upserted twice
        still creates exactly one row, keyed the same way, with every
        pre-existing field intact, plus platform auto-populated and every
        new field left blank."""
        account = self.make_account()
        first = upsert_marketplace_order(account.name, self.sample_order())

        updated = self.sample_order()
        updated["external_status"] = "DISPATCHED"
        second = upsert_marketplace_order(account.name, updated)
        second.reload()

        self.assertEqual(first.name, second.name)
        self.assertEqual(frappe.db.count("Marketplace Order", {"marketplace_account": account.name}), 1)
        self.assertEqual(second.platform, "Talabat")
        self.assertEqual(second.external_status, "DISPATCHED")
        self.assertEqual(second.customer_city, "Cairo")
        self.assertEqual(frappe.parse_json(second.discounts)[0]["name"], "<Promotion>")
        self.assertFalse(second.billing_address)
        self.assertFalse(second.shop_currency)
        self.assertEqual(len(second.items), 2)

    def make_shopify_account(self):
        """Marketplace Account.validate() runs a live (read-only) Shopify
        shop/Location verification call for a Shopify-platform account
        (#103) - mock it the same way test_marketplace_account.py does,
        rather than ever making a real Admin GraphQL call from this test."""
        suffix = frappe.generate_hash(length=8)
        shop_gid = f"gid://shopify/Shop/{suffix}"
        connection = frappe.get_doc(
            {
                "doctype": "Shopify Connection",
                "connection_name": f"Order Test Shopify Connection {suffix}",
                "shop_domain": f"order-test-{suffix}.myshopify.com",
                "client_id": "shopify-order-test-client",
                "client_secret": "shopify-order-test-secret",
            }
        ).insert()
        with mock_shopify_verification(shop_id=shop_gid, active_locations=[]):
            return frappe.get_doc(
                {
                    "doctype": "Marketplace Account",
                    "account_name": f"Shopify Order Test Account {suffix}",
                    "platform": "Shopify",
                    "external_account_id": shop_gid,
                    "connection_doctype": "Shopify Connection",
                    "connection": connection.name,
                }
            ).insert()

    def test_upsert_persists_shopify_shaped_order_with_gid_identifiers_and_html_like_json(self):
        """New Shopify-shaped upsert coverage (#108): GID-style identifiers,
        JSON evidence fields containing HTML-like content, idempotent
        repeat, and lines keyed by a LineItem-GID-style external_item_id -
        all through the exact same upsert path Talabat uses, with no
        platform-specific branching in the service itself."""
        account = self.make_shopify_account()

        external_order_id = f"gid://shopify/Order/{frappe.generate_hash(length=8)}"
        normalized_order = {
            "external_order_id": external_order_id,
            "order_code": "#1001",
            "customer_email": "buyer@example.com",
            "shop_currency": "EGP",
            "presentment_currency": "USD",
            "billing_address": {"name": "<script>alert(1)</script>", "city": "Cairo"},
            "shipping_address": {"name": "Same <Person>", "city": "Cairo"},
            "payment_transactions": [{"id": "gid://shopify/OrderTransaction/1", "amount": "10.00"}],
            "refunds": [],
            "latest_payload": {"id": external_order_id, "note": "Handle <carefully>"},
            "items": [
                {
                    "external_item_id": "gid://shopify/LineItem/111",
                    "external_sku": "SHOP-SKU-1",
                    "external_variant_id": "gid://shopify/ProductVariant/222",
                    "product_name": "Shopify Item",
                }
            ],
        }

        order = upsert_marketplace_order(account.name, normalized_order)
        order.reload()

        self.assertEqual(order.platform, "Shopify")
        self.assertEqual(order.external_order_id, external_order_id)
        self.assertEqual(order.customer_email, "buyer@example.com")
        self.assertEqual(order.shop_currency, "EGP")
        self.assertEqual(order.presentment_currency, "USD")
        self.assertEqual(frappe.parse_json(order.billing_address)["name"], "<script>alert(1)</script>")
        self.assertEqual(frappe.parse_json(order.shipping_address)["city"], "Cairo")
        self.assertEqual(
            frappe.parse_json(order.payment_transactions)[0]["id"], "gid://shopify/OrderTransaction/1"
        )
        self.assertEqual(frappe.parse_json(order.refunds), [])
        self.assertEqual(frappe.parse_json(order.latest_payload)["note"], "Handle <carefully>")
        line = order.items[0]
        self.assertEqual(line.external_item_id, "gid://shopify/LineItem/111")
        self.assertEqual(line.external_variant_id, "gid://shopify/ProductVariant/222")

        # Idempotent repeat: same external_order_id, same lines - no
        # duplicate parent, no duplicate line.
        repeat = upsert_marketplace_order(account.name, normalized_order)
        self.assertEqual(order.name, repeat.name)
        self.assertEqual(
            frappe.db.count("Marketplace Order", {"order_identity_key": order.order_identity_key}), 1
        )
        repeat.reload()
        self.assertEqual(len(repeat.items), 1)

    def test_upsert_preserves_promotion_name_with_html_like_characters(self):
        """ignore_xss_filter on the promotions JSON field must actually
        protect it: a promotion name containing HTML-like characters must
        survive insert()/reload completely unmodified, the same way the
        discounts field's <Promotion> content already proves for itself."""
        account = self.make_account()
        order_values = self.sample_order()
        order_values["items"][0]["promotions"][0]["name"] = "Buy 2 <get 1 free>"

        order = upsert_marketplace_order(account.name, order_values)
        order.reload()

        unit_line = next(row for row in order.items if row.external_item_id == "line-unit-001")
        self.assertEqual(frappe.parse_json(unit_line.promotions)[0]["name"], "Buy 2 <get 1 free>")

    def test_a_stale_copy_saved_after_the_marker_was_set_keeps_the_marker(self):
        account = self.make_account()
        order = upsert_marketplace_order(account.name, self.sample_order())
        stale = frappe.get_doc("Marketplace Order", order.name)
        marker = "2026-09-01 10:00:00"
        frappe.db.set_value("Marketplace Order", order.name, "outbound_cancelled_on", marker, update_modified=False)

        stale.save()

        self.assertEqual(
            frappe.utils.get_datetime(frappe.db.get_value("Marketplace Order", order.name, "outbound_cancelled_on")),
            frappe.utils.get_datetime(marker),
        )

    def test_an_order_that_never_had_the_marker_is_unaffected(self):
        account = self.make_account()
        order = upsert_marketplace_order(account.name, self.sample_order())
        self.assertIsNone(order.outbound_cancelled_on)

        order.save()

        self.assertIsNone(frappe.db.get_value("Marketplace Order", order.name, "outbound_cancelled_on"))

    def test_the_marker_can_still_be_set_by_a_save_and_the_helper_clears_it(self):
        from marketplace_integrations.services.marketplace_outbound import clear_outbound_cancelled_marker

        account = self.make_account()
        order = upsert_marketplace_order(account.name, self.sample_order())
        order.outbound_cancelled_on = "2026-09-01 10:00:00"
        order.save()
        self.assertIsNotNone(frappe.db.get_value("Marketplace Order", order.name, "outbound_cancelled_on"))

        clear_outbound_cancelled_marker(order.name)

        self.assertIsNone(frappe.db.get_value("Marketplace Order", order.name, "outbound_cancelled_on"))
