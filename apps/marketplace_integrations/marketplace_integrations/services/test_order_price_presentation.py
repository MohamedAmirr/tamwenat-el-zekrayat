# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.services.listing_price import CatalogBuildError
from marketplace_integrations.services.order_price_presentation import (
    current_catalog_price,
    not_mapped_reason,
    resolve_catalog_sync_account,
)


class UnitTestOrderPricePresentation(UnitTestCase):
    def test_no_mapped_item_fails_closed_without_touching_the_database(self):
        price, reason = current_catalog_price("Any Account", None, None)

        self.assertIsNone(price)
        self.assertEqual(reason, not_mapped_reason())

    def test_no_mapping_name_fails_closed_the_same_way_as_no_mapped_item(self):
        # Defensive: the caller is expected to always resolve mapping_name and
        # mapped_item together (resolve_mapping_and_item()), but this proves
        # the service still fails closed (rather than crashing on a missing
        # mapping_name) if that invariant is ever broken.
        price, reason = current_catalog_price("Any Account", None, "Some Item")

        self.assertIsNone(price)
        self.assertEqual(reason, not_mapped_reason())

    @patch("marketplace_integrations.services.order_price_presentation.get_listing_price")
    def test_a_price_build_failure_becomes_a_sanitized_reason_not_an_unhandled_exception(
        self, get_listing_price
    ):
        get_listing_price.side_effect = CatalogBuildError("Item X has no current Item Price.")

        price, reason = current_catalog_price("Any Account", "MPM-00001", "Some Item")

        self.assertIsNone(price)
        self.assertEqual(reason, "Item X has no current Item Price.")
        get_listing_price.assert_called_once_with("Any Account", "MPM-00001", "Some Item")

    @patch("marketplace_integrations.services.order_price_presentation.get_listing_price")
    def test_a_price_build_failure_with_no_message_gets_a_curated_fallback_not_a_blank_reason(
        self, get_listing_price
    ):
        get_listing_price.side_effect = CatalogBuildError()

        price, reason = current_catalog_price("Any Account", "MPM-00001", "Some Item")

        self.assertIsNone(price)
        self.assertTrue(reason)
        self.assertIn("MPM-00001", reason)

    @patch("marketplace_integrations.services.order_price_presentation.get_listing_price")
    def test_a_missing_item_or_mapping_becomes_a_sanitized_reason(self, get_listing_price):
        get_listing_price.side_effect = frappe.DoesNotExistError

        price, reason = current_catalog_price("Any Account", "MPM-00001", "Some Item")

        self.assertIsNone(price)
        self.assertIsNotNone(reason)

    @patch("marketplace_integrations.services.order_price_presentation.get_listing_price")
    def test_an_unrelated_validation_error_is_not_swallowed(self, get_listing_price):
        # A renamed doctype or broken link surfaces as a real exception rather
        # than silently becoming "Catalog Price Unavailable: <bug>" - only
        # CatalogBuildError/DoesNotExistError are caught here, not the full
        # frappe.ValidationError hierarchy.
        get_listing_price.side_effect = frappe.ValidationError("Some unrelated bug")

        with self.assertRaises(frappe.ValidationError):
            current_catalog_price("Any Account", "MPM-00001", "Some Item")

    @patch("marketplace_integrations.services.order_price_presentation.get_listing_price", return_value=12.5)
    def test_a_resolved_price_is_returned_as_a_float_with_no_reason(self, get_listing_price):
        price, reason = current_catalog_price("Any Account", "MPM-00002", "Some Item")

        self.assertEqual(price, 12.5)
        self.assertIsInstance(price, float)
        self.assertIsNone(reason)

    def test_a_nonexistent_account_or_mapping_does_not_leak_a_stacked_message_log_entry(self):
        # get_listing_price() -> validate_catalog_sync_account()/_as_doc() ->
        # frappe.get_doc() raises frappe.DoesNotExistError via a real
        # frappe.throw() call (confirmed directly in frappe's own
        # model/document.py) when the referenced document doesn't exist -
        # this is the real, unmocked code path the except block must clear.
        frappe.clear_messages()
        before = len(frappe.local.message_log)

        price, reason = current_catalog_price("Account Does Not Exist", "MPM Does Not Exist", "Some Item")

        self.assertIsNone(price)
        self.assertIsNotNone(reason)
        self.assertEqual(len(frappe.local.message_log), before)

    @patch("marketplace_integrations.services.order_price_presentation.get_listing_price")
    def test_price_resolution_failure_does_not_leak_a_stacked_message_log_entry(self, get_listing_price):
        # get_listing_price() itself raises CatalogBuildError via frappe.throw(),
        # which appends to frappe.message_log before raising - the except
        # block must clear that entry so only the returned reason reaches the
        # client, not a stacked red toast per order line. The mock calls the
        # real frappe.throw() so this exercises that exact mechanism rather
        # than only asserting on a bare, never-msgprint'd exception instance.
        def _raise(*args, **kwargs):
            frappe.throw("Item X has no current Item Price.", exc=CatalogBuildError)

        get_listing_price.side_effect = _raise
        frappe.clear_messages()
        before = len(frappe.local.message_log)

        price, reason = current_catalog_price("Any Account", "MPM-00001", "Some Item")

        self.assertIsNone(price)
        self.assertEqual(reason, "Item X has no current Item Price.")
        self.assertEqual(len(frappe.local.message_log), before)


class UnitTestResolveCatalogSyncAccount(UnitTestCase):
    @patch("marketplace_integrations.services.order_price_presentation.validate_catalog_sync_account")
    def test_returns_the_validated_account_doc_with_no_error(self, validate_catalog_sync_account):
        account_doc = object()
        validate_catalog_sync_account.return_value = (account_doc, object(), object())

        account, error = resolve_catalog_sync_account("Some Account")

        self.assertIs(account, account_doc)
        self.assertIsNone(error)

    @patch("marketplace_integrations.services.order_price_presentation.validate_catalog_sync_account")
    def test_a_misconfigured_account_fails_closed_with_the_validation_message(
        self, validate_catalog_sync_account
    ):
        validate_catalog_sync_account.side_effect = frappe.ValidationError(
            "Marketplace Account X is disabled."
        )

        account, error = resolve_catalog_sync_account("Some Account")

        self.assertIsNone(account)
        self.assertEqual(error, "Marketplace Account X is disabled.")

    @patch("marketplace_integrations.services.order_price_presentation.validate_catalog_sync_account")
    def test_a_blank_validation_message_gets_a_curated_fallback(self, validate_catalog_sync_account):
        validate_catalog_sync_account.side_effect = frappe.ValidationError()

        account, error = resolve_catalog_sync_account("Some Account")

        self.assertIsNone(account)
        self.assertTrue(error)
        self.assertIn("Some Account", error)

    @patch("marketplace_integrations.services.order_price_presentation.validate_catalog_sync_account")
    def test_account_validation_failure_does_not_leak_a_stacked_message_log_entry(
        self, validate_catalog_sync_account
    ):
        # validate_catalog_sync_account() itself raises via frappe.throw(),
        # which appends to frappe.message_log before raising - the except
        # block must clear that entry so only the returned reason reaches the
        # client, not a stacked red toast for every line of the order.
        def _raise(*args, **kwargs):
            frappe.throw("Marketplace Account X is disabled.")

        validate_catalog_sync_account.side_effect = _raise
        frappe.clear_messages()
        before = len(frappe.local.message_log)

        account, error = resolve_catalog_sync_account("Some Account")

        self.assertIsNone(account)
        self.assertEqual(error, "Marketplace Account X is disabled.")
        self.assertEqual(len(frappe.local.message_log), before)
