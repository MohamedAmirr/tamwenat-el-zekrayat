# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for #169's services/marketplace_attention.py - one case per problem
source, platform-correct unmapped detection, permission scoping, the Shopify
webhook-retry boundary, and the Order Import drop-off.

Every fixture is deleted in tearDown (children first, Marketplace Orders via
``frappe.delete_doc(force=True)`` so their child rows go too). Nothing here
commits and no platform call is made.
"""

from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from marketplace_integrations.integrations.shopify.test_products import (
	make_shopify_account,
)
from marketplace_integrations.integrations.shopify.webhooks import MAX_WEBHOOK_DELIVERY_ATTEMPTS
from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.test_marketplace_account import (
	STAND_IN_CONNECTION,
	STAND_IN_CONNECTION_DOCTYPE,
)
from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)
from marketplace_integrations.services import marketplace_attention
from marketplace_integrations.services.marketplace_attention import (
	FULFILLMENT_ERROR,
	NEEDS_ACTION,
	NOON_FULFILLMENT,
	NOON_RETURN_UNLINKED,
	ORDER_IMPORT_ERROR,
	PAYMENT_ENTRY_CANCELLED,
	PAYMENT_ERROR,
	PAYMENT_EXCESS,
	RETIRED_FULFILLMENT_ACTIONS,
	SYNC_ERROR,
	UNMAPPED_ITEMS,
	WEBHOOK_RETRIES_EXHAUSTED,
	get_attention_counts,
	get_attention_items,
)
from marketplace_integrations.services.marketplace_outbound import platform_instruction

NOON_PEM = "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----"


class IntegrationTestMarketplaceAttention(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self._created = []
		# get_attention_items() returns _()-translated merchant-readable text, and
		# several tests below assert on literal English substrings of it. Pin the
		# language so those assertions hold regardless of the site's default
		# language (#172).
		self._original_lang = frappe.local.lang
		frappe.local.lang = "en"

	def tearDown(self):
		frappe.local.lang = self._original_lang
		for doctype, name in reversed(self._created):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)

	def test_payment_error_is_hidden_by_later_success_for_same_order_and_transaction(self):
		stamp = now_datetime()
		account = frappe._dict(name="MKTACC-SHOPIFY", platform="Shopify")
		failure = frappe._dict(
			name="LOG-FAIL", marketplace_account=account.name, marketplace_order="MKTORD-1",
			request_data=frappe.as_json({"transaction_id": "TX-1"}),
			response_data=frappe.as_json({"message": "Gateway is not mapped"}),
			attempted_at=stamp, creation=stamp, status="Invalid",
		)
		order_accounts = {"MKTORD-1": account.name}
		accounts = {account.name: account}
		# Reads: failures by stored account, failures of rows without an account, Success rows.
		with patch.object(
			marketplace_attention.frappe, "get_list", side_effect=[[failure], [], []]
		):
			items = marketplace_attention._collect_payment_errors(accounts, order_accounts)
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0]["problem_type"], PAYMENT_ERROR)
		self.assertIn("TX-1", items[0]["explanation"])
		success = frappe._dict(
			name="LOG-OK", marketplace_account=account.name, marketplace_order="MKTORD-1",
			request_data=frappe.as_json({"transaction_id": "TX-1"}),
			attempted_at=stamp + timedelta(seconds=1), creation=stamp + timedelta(seconds=1),
		)
		with patch.object(
			marketplace_attention.frappe, "get_list", side_effect=[[failure], [], [success]]
		):
			self.assertEqual(marketplace_attention._collect_payment_errors(accounts, order_accounts), [])

	def test_payment_error_of_a_log_without_an_account_is_found_by_its_order(self):
		stamp = now_datetime()
		account = frappe._dict(name="MKTACC-SHOPIFY", platform="Shopify")
		old_row = frappe._dict(
			name="LOG-OLD", marketplace_account=None, marketplace_order="MKTORD-1",
			request_data=frappe.as_json({"transaction_id": "TX-9"}),
			response_data=frappe.as_json({"message": "old"}),
			attempted_at=stamp, creation=stamp, status="Error",
		)
		with patch.object(
			marketplace_attention.frappe, "get_list", side_effect=[[], [old_row], []]
		):
			items = marketplace_attention._collect_payment_errors(
				{account.name: account}, {"MKTORD-1": account.name}
			)
		self.assertEqual([item["reference_name"] for item in items], ["MKTORD-1"])

	def test_payment_excess_and_cancelled_entry_are_listed_live(self):
		account = frappe._dict(name="MKTACC-SHOPIFY", platform="Shopify")
		with patch.object(
			marketplace_attention.frappe,
			"get_list",
			side_effect=[
				[
					frappe._dict(
						name="PE-EXCESS", custom_marketplace_order="MKTORD-1", unallocated_amount=12.5,
						modified=now_datetime(),
					)
				],
			],
		):
			excess = marketplace_attention._collect_payment_excess(
				{account.name: account}, {"MKTORD-1": account.name}
			)
		self.assertEqual(excess[0]["problem_type"], PAYMENT_EXCESS)
		self.assertIn("12.5", excess[0]["explanation"])

		cancelled_entry = frappe._dict(
			name="PE-CANCELLED", custom_marketplace_order="MKTORD-1",
			custom_marketplace_transaction_id="TX-1", paid_amount=100, total_allocated_amount=100,
			modified=now_datetime(),
		)
		accounts, order_accounts = {account.name: account}, {"MKTORD-1": account.name}
		order_row = frappe._dict(name="MKTORD-1", erpnext_sales_order="SO-1")
		# Reads, in order: the cancelled entries, the orders' Sales Orders, then any submitted amendments.
		with patch.object(
			marketplace_attention.frappe, "get_list", side_effect=[[cancelled_entry], [], []]
		):
			cancelled = marketplace_attention._collect_cancelled_payment_entries(accounts, order_accounts)
		self.assertEqual(cancelled[0]["problem_type"], PAYMENT_ENTRY_CANCELLED)
		self.assertIn("record the payment manually", cancelled[0]["explanation"])

		# Submitting the amendment clears the row (#231).
		with patch.object(
			marketplace_attention.frappe,
			"get_list",
			side_effect=[[cancelled_entry], [], [frappe._dict(amended_from="PE-CANCELLED")]],
		):
			self.assertEqual(
				marketplace_attention._collect_cancelled_payment_entries(accounts, order_accounts), []
			)

		# Another submitted Payment Entry covering the order for at least the cancelled amount clears
		# the row (#425); a smaller one does not.
		for covered, listed in ((100, 0), (150, 0), (99.99, 1)):
			with (
				patch.object(
					marketplace_attention.frappe, "get_list", side_effect=[[cancelled_entry], [order_row], []]
				),
				patch.object(marketplace_attention, "manual_payment_totals", return_value={"SO-1": covered}),
			):
				cancelled = marketplace_attention._collect_cancelled_payment_entries(accounts, order_accounts)
			self.assertEqual(len(cancelled), listed, covered)

		# Split payment: two cancelled entries (100 and 395) need 495 together. A manual 395 clears
		# neither row; 495 clears both. An amended one no longer counts toward the sum.
		small_entry = frappe._dict(
			name="PE-SMALL", custom_marketplace_order="MKTORD-1",
			custom_marketplace_transaction_id="TX-A", paid_amount=100, total_allocated_amount=100,
			modified=now_datetime(),
		)
		big_entry = frappe._dict(
			name="PE-BIG", custom_marketplace_order="MKTORD-1",
			custom_marketplace_transaction_id="TX-B", paid_amount=395, total_allocated_amount=395,
			modified=now_datetime(),
		)
		for covered, listed in ((395, 2), (494.99, 2), (495, 0)):
			with (
				patch.object(
					marketplace_attention.frappe, "get_list", side_effect=[[small_entry, big_entry], [order_row], []]
				),
				patch.object(marketplace_attention, "manual_payment_totals", return_value={"SO-1": covered}),
			):
				cancelled = marketplace_attention._collect_cancelled_payment_entries(accounts, order_accounts)
			self.assertEqual(len(cancelled), listed, covered)
		with (
			patch.object(
				marketplace_attention.frappe,
				"get_list",
				side_effect=[
					[small_entry, big_entry],
					[order_row],
					[frappe._dict(amended_from="PE-SMALL")],
				],
			),
			patch.object(marketplace_attention, "manual_payment_totals", return_value={"SO-1": 395}),
		):
			cancelled = marketplace_attention._collect_cancelled_payment_entries(accounts, order_accounts)
		self.assertEqual(cancelled, [])

		# A rounded Sales Order: the cancelled entry paid 494.21 but allocated 494, so a manual entry of
		# 494 clears it; the comparison uses the allocation, not paid_amount.
		rounded_entry = frappe._dict(
			name="PE-ROUNDED", custom_marketplace_order="MKTORD-1",
			custom_marketplace_transaction_id="TX-2", paid_amount=494.21, total_allocated_amount=494,
			modified=now_datetime(),
		)
		for covered, listed in ((494, 0), (493.99, 1)):
			with (
				patch.object(
					marketplace_attention.frappe, "get_list", side_effect=[[rounded_entry], [order_row], []]
				),
				patch.object(marketplace_attention, "manual_payment_totals", return_value={"SO-1": covered}),
			):
				cancelled = marketplace_attention._collect_cancelled_payment_entries(accounts, order_accounts)
			self.assertEqual(len(cancelled), listed, covered)

	def test_a_duplicate_guard_row_is_listed_only_while_the_guard_still_blocks(self):
		account = frappe._dict(name="MKTACC-SHOPIFY", platform="Shopify")
		stamp = now_datetime()
		guard_row = frappe._dict(
			name="LOG-GUARD", marketplace_account=account.name, marketplace_order="MKTORD-1",
			request_data=frappe.as_json({"transaction_id": "TX-9"}),
			response_data=frappe.as_json(
				{"message": "covered", "guard_sales_order": "SO-1", "guard_required": 494}
			),
			attempted_at=stamp, creation=stamp, status="Invalid",
		)
		for covered, listed in (({"SO-1": 494}, 1), ({"SO-1": 493.99}, 0), ({}, 0)):
			with (
				patch.object(marketplace_attention.frappe, "get_list", side_effect=[[guard_row], [], []]),
				patch.object(marketplace_attention, "manual_payment_totals", return_value=covered),
			):
				items = marketplace_attention._collect_payment_errors(
					{account.name: account}, {"MKTORD-1": account.name}
				)
			self.assertEqual(len(items), listed, covered)

	def test_shopify_order_accounts_are_read_once_for_all_payment_collectors(self):
		account = frappe._dict(name="MKTACC-ONCE", platform="Shopify")
		with (
			patch.object(marketplace_attention, "_get_accounts", return_value={account.name: account}),
			patch.object(
				marketplace_attention, "_shopify_order_accounts", return_value={}
			) as order_accounts,
		):
			marketplace_attention.get_attention_items()
		self.assertEqual(order_accounts.call_count, 1)

	def test_all_three_payment_attention_types_are_counted(self):
		account_name = f"MKTACC-{self.suffix}"
		items = [
			{"account": account_name, "problem_type": problem_type, "reference_doctype": "Payment Entry"}
			for problem_type in (PAYMENT_ERROR, PAYMENT_EXCESS, PAYMENT_ENTRY_CANCELLED)
		]
		with patch.object(marketplace_attention, "get_attention_items", return_value=items):
			counts = get_attention_counts([account_name])
		self.assertEqual(counts[account_name]["payment_errors"], 3)

	# -- fixtures ---------------------------------------------------------

	def _track(self, doc):
		self._created.append((doc.doctype, doc.name))
		return doc

	def make_talabat_account(self, **kwargs):
		doc = {
			"doctype": "Marketplace Account",
			"account_name": f"Attention Talabat {frappe.generate_hash(length=6)}",
			"platform": "Talabat",
			"external_account_id": f"attention-talabat-{frappe.generate_hash(length=8)}",
			"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
			"connection": STAND_IN_CONNECTION,
		}
		doc.update(kwargs)
		return self._track(frappe.get_doc(doc).insert())

	def make_shopify_account(self):
		account = make_shopify_account(f"Attention Shopify {frappe.generate_hash(length=6)}")
		self._created.append(("Shopify Connection", account.connection))
		return self._track(account)

	def make_noon_account(self):
		code = f"attention-noon-{frappe.generate_hash(length=8)}"
		connection = frappe.get_doc(
			{
				"doctype": "Noon Connection",
				"connection_name": code,
				"project_code": code,
				"key_id": "test-key-id",
				"private_key": NOON_PEM,
				"environment": "Static Sandbox",
				"enabled": 1,
			}
		).insert()
		self._track(connection)
		with patch("marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"):
			account = frappe.get_doc(
				{
					"doctype": "Marketplace Account",
					"account_name": f"Attention {code}",
					"platform": "Noon",
					"external_account_id": code,
					"connection_doctype": "Noon Connection",
					"connection": connection.name,
					"noon_warehouse_code": "NOON-WH-ATTENTION",
					"noon_country_code": "ae",
				}
			).insert()
		return self._track(account)

	def make_item(self):
		code = f"Attention Item {frappe.generate_hash(length=6)}"
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": code,
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		return self._track(item)

	def make_mapping(self, account, reference, **overrides):
		item = getattr(self, "_item", None) or self.make_item()
		self._item = item
		values = {
			"doctype": "Marketplace Product Mapping",
			"marketplace_account": account.name,
			"item": item.name,
			"external_reference": reference,
			**overrides,
		}
		return self._track(frappe.get_doc(values).insert())

	def make_order(self, account, items=None, **overrides):
		order_id = overrides.pop("external_order_id", f"order-{frappe.generate_hash(length=8)}")
		values = {
			"doctype": "Marketplace Order",
			"marketplace_account": account.name,
			"platform": account.platform,
			"external_order_id": order_id,
			"items": items or [],
			**overrides,
		}
		order = frappe.get_doc(values)
		order.flags.skip_sync_status_update = True
		order.order_identity_key = compute_order_identity_key(account.name, order_id)
		order.insert()
		return self._track(order)

	def make_log(self, account, **overrides):
		now = now_datetime()
		values = {
			"doctype": "Marketplace Sync Log",
			"marketplace_account": account.name,
			"sync_type": "Order Import",
			"status": "Error",
			"attempted_at": now,
			"traceback": "boom",
			**overrides,
		}
		return self._track(frappe.get_doc(values).insert())

	def items_for(self, account, problem_type=None):
		filters = {"account": account.name}
		if problem_type:
			filters["problem_type"] = problem_type
		return get_attention_items(filters)

	def talabat_item(self, sku, status="IN_CART"):
		return {
			"external_item_id": f"t-{frappe.generate_hash(length=6)}",
			"external_sku": sku,
			"external_status": status,
		}

	# -- order status -----------------------------------------------------

	def test_sync_error_and_modified_after_sync_orders_are_listed_with_next_actions(self):
		account = self.make_talabat_account()
		errored = self.make_order(account, erpnext_sync_status="Sync Error", last_sync_error="No warehouse")
		modified = self.make_order(
			account,
			erpnext_sync_status="Needs Action",
			last_sync_error="Requested quantity is below the quantity already delivered in ERPNext.",
		)
		self.make_order(account, erpnext_sync_status="Synced")

		rows = {row["reference_name"]: row for row in self.items_for(account)}

		self.assertEqual(set(rows), {errored.name, modified.name})
		self.assertEqual(rows[errored.name]["problem_type"], SYNC_ERROR)
		self.assertEqual(rows[errored.name]["next_action"], "Retry")
		self.assertEqual(rows[errored.name]["explanation"], "No warehouse")
		self.assertEqual(rows[errored.name]["reference_doctype"], "Marketplace Order")
		self.assertEqual(rows[errored.name]["platform"], "Talabat")
		self.assertEqual(rows[modified.name]["problem_type"], NEEDS_ACTION)
		self.assertEqual(rows[modified.name]["next_action"], "Open Order")
		self.assertEqual(
			rows[modified.name]["explanation"],
			"Requested quantity is below the quantity already delivered in ERPNext.",
		)
		self.assertTrue(rows[modified.name]["detected_at"])

	def test_a_cancelled_order_is_never_listed_as_unmapped_or_needing_action(self):
		account = self.make_talabat_account()
		order = self.make_order(
			account, items=[self.talabat_item("SKU-NOT-MAPPED")], erpnext_sync_status="Cancelled"
		)
		self.assertNotIn(order.name, {row["reference_name"] for row in self.items_for(account)})

	def test_needs_action_row_carries_the_action_state_and_the_linked_document(self):
		account = self.make_talabat_account()
		waiting = self.make_order(
			account,
			erpnext_sync_status="Needs Action",
			last_sync_error="Confirm the goods were received.",
			sync_action_state="Awaiting Receipt",
		)
		prepared = self.make_order(
			account,
			erpnext_sync_status="Needs Action",
			sync_action_state="Draft Prepared",
		)
		# Set directly: the named documents need not exist for the report to link them.
		frappe.db.set_value(
			"Marketplace Order",
			prepared.name,
			{
				"sync_blocking_doctype": "Payment Entry",
				"sync_blocking_document": "PE-BLOCKER",
				"sync_prepared_doctype": "Delivery Note",
				"sync_prepared_document": "MAT-DN-DRAFT",
			},
		)
		rows = {row["reference_name"]: row for row in self.items_for(account, NEEDS_ACTION)}

		self.assertEqual(rows[waiting.name]["action_state"], "Awaiting Receipt")
		self.assertIsNone(rows[waiting.name]["linked_document"])
		self.assertEqual(rows[prepared.name]["action_state"], "Draft Prepared")
		# The prepared draft wins over the blocker.
		self.assertEqual(rows[prepared.name]["linked_doctype"], "Delivery Note")
		self.assertEqual(rows[prepared.name]["linked_document"], "MAT-DN-DRAFT")

	def test_cancelled_sales_order_with_an_active_platform_order_shows_the_platform_instruction(self):
		"""#181: no new problem type; the planner message (with the platform instruction) is the
		explanation of a Needs Action row, in full and not truncated."""
		account = self.make_talabat_account()
		base = "The linked ERPNext Sales Order is no longer an active submitted document."
		instruction = platform_instruction(
			frappe._dict(platform="Talabat", external_status="RECEIVED", outbound_cancelled_on=None)
		)
		order = self.make_order(
			account, erpnext_sync_status="Needs Action", last_sync_error=f"{base} {instruction}"
		)

		rows = {row["reference_name"]: row for row in self.items_for(account)}

		self.assertEqual(rows[order.name]["problem_type"], NEEDS_ACTION)
		self.assertEqual(rows[order.name]["next_action"], "Open Order")
		self.assertEqual(rows[order.name]["explanation"], f"{base} {instruction}")
		self.assertIn("Cancel the order on Talabat", rows[order.name]["explanation"])

	def test_a_long_sync_message_is_shown_up_to_the_stored_cap(self):
		account = self.make_talabat_account()
		message = ("The marketplace refunded this order, but it was already delivered. " * 7).strip()
		self.assertGreater(len(message), 300)
		order = self.make_order(account, erpnext_sync_status="Needs Action", last_sync_error=message)

		rows = {row["reference_name"]: row for row in self.items_for(account)}

		self.assertEqual(rows[order.name]["explanation"], message)

	# -- unmapped items ---------------------------------------------------

	def test_talabat_unmapped_items_use_external_sku_and_final_item_statuses_only(self):
		account = self.make_talabat_account()
		self.make_mapping(account, "SKU-MAPPED")
		order = self.make_order(
			account,
			items=[
				self.talabat_item("SKU-MAPPED"),
				self.talabat_item("SKU-MISSING"),
				self.talabat_item("SKU-CANCELLED", status="CANCELLED"),
			],
		)
		synced = self.make_order(
			account, items=[self.talabat_item("SKU-MISSING")], erpnext_sync_status="Synced"
		)

		rows = self.items_for(account, UNMAPPED_ITEMS)

		self.assertEqual([row["reference_name"] for row in rows], [order.name])
		self.assertNotIn(synced.name, [row["reference_name"] for row in rows])
		self.assertEqual(rows[0]["next_action"], "Map Item")
		self.assertEqual(rows[0]["next_action_reference"], "SKU-MISSING")
		self.assertIn("SKU-MISSING", rows[0]["explanation"])
		self.assertNotIn("SKU-CANCELLED", rows[0]["explanation"])

	def test_a_disabled_mapping_counts_as_unmapped(self):
		account = self.make_talabat_account()
		mapping = self.make_mapping(account, "SKU-OFF", enabled=0)
		order = self.make_order(account, items=[self.talabat_item("SKU-OFF")])

		rows = self.items_for(account, UNMAPPED_ITEMS)

		self.assertEqual([row["reference_name"] for row in rows], [order.name])
		self.assertEqual(rows[0]["next_action_mapping"], mapping.name)

	def test_a_missing_mapping_carries_no_mapping_name(self):
		account = self.make_talabat_account()
		self.make_order(account, items=[self.talabat_item("SKU-NONE")])

		rows = self.items_for(account, UNMAPPED_ITEMS)

		self.assertIsNone(rows[0]["next_action_mapping"])

	def test_shopify_unmapped_detection_uses_the_variant_id_not_the_sku(self):
		account = self.make_shopify_account()
		# A mapping keyed by the line's SKU must NOT satisfy a Shopify line.
		self.make_mapping(account, "SKU-1")
		self.make_mapping(account, "gid://shopify/ProductVariant/2")
		order = self.make_order(
			account,
			items=[
				{
					"external_item_id": "li-1",
					"external_sku": "SKU-1",
					"external_variant_id": "gid://shopify/ProductVariant/9",
					"current_quantity": 1,
				},
				{
					"external_item_id": "li-2",
					"external_variant_id": "gid://shopify/ProductVariant/2",
					"current_quantity": 1,
				},
			],
		)

		rows = self.items_for(account, UNMAPPED_ITEMS)

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["reference_name"], order.name)
		self.assertEqual(rows[0]["next_action_reference"], "gid://shopify/ProductVariant/9")
		self.assertEqual(rows[0]["platform"], "Shopify")

	def test_noon_unmapped_detection_uses_sku_for_eligible_units_only(self):
		account = self.make_noon_account()
		order = self.make_order(
			account,
			items=[
				{
					"external_item_id": "n-1",
					"external_sku": "NOON-MISSING",
					"external_status": "CONFIRMED",
					"integration_status": "ACKNOWLEDGED",
					"current_quantity": 1,
				},
				{
					"external_item_id": "n-2",
					"external_sku": "NOON-OOS",
					"external_status": "CONFIRMED",
					"integration_status": "OUT_OF_STOCK",
					"current_quantity": 1,
				},
				{
					"external_item_id": "n-3",
					"external_sku": "NOON-CANCELLED",
					"external_status": "CANCELLED",
					"integration_status": "ACKNOWLEDGED",
					"current_quantity": 1,
				},
			],
		)

		rows = self.items_for(account, UNMAPPED_ITEMS)

		self.assertEqual([row["reference_name"] for row in rows], [order.name])
		self.assertEqual(rows[0]["next_action_reference"], "NOON-MISSING")
		self.assertNotIn("NOON-OOS", rows[0]["explanation"])
		self.assertNotIn("NOON-CANCELLED", rows[0]["explanation"])

	def shopify_item(self, variant, quantity=1):
		return {
			"external_item_id": f"li-{frappe.generate_hash(length=6)}",
			"external_variant_id": variant,
			"current_quantity": quantity,
		}

	def test_shopify_unmapped_variant_raises_no_row_for_synced_cancelled_or_zero_quantity(self):
		account = self.make_shopify_account()
		variant = "gid://shopify/ProductVariant/404"
		self.make_order(
			account, erpnext_sync_status="Synced", items=[self.shopify_item(variant)]
		)
		self.make_order(account, cancelled_at=now_datetime(), items=[self.shopify_item(variant)])
		self.make_order(account, items=[self.shopify_item(variant, quantity=0)])

		self.assertEqual(self.items_for(account, UNMAPPED_ITEMS), [])

		# Control: the same variant on a live, unsynced order is flagged.
		live = self.make_order(account, items=[self.shopify_item(variant)])
		self.assertEqual(
			[row["reference_name"] for row in self.items_for(account, UNMAPPED_ITEMS)], [live.name]
		)

	def test_more_than_five_unmapped_skus_truncate_the_explanation_after_five(self):
		account = self.make_shopify_account()
		# Short references: the explanation itself is capped at
		# EXPLANATION_MAX_LENGTH, so full-length Shopify GIDs would cut the list earlier.
		variants = [f"variant-{index}" for index in range(1, 8)]
		self.make_order(account, items=[self.shopify_item(variant) for variant in variants])

		rows = self.items_for(account, UNMAPPED_ITEMS)

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["next_action_reference"], variants[0])
		self.assertTrue(rows[0]["explanation"].startswith("7 item(s) have no enabled product mapping: "))
		listed = rows[0]["explanation"].split(": ", 1)[1]
		self.assertEqual(listed, ", ".join(variants[:5]) + ", ...")

	def test_a_mapping_on_another_account_does_not_resolve(self):
		account = self.make_talabat_account()
		other = self.make_talabat_account()
		self.make_mapping(other, "SKU-SHARED")
		order = self.make_order(account, items=[self.talabat_item("SKU-SHARED")])

		self.assertEqual([r["reference_name"] for r in self.items_for(account, UNMAPPED_ITEMS)], [order.name])

	# -- order import errors ----------------------------------------------

	def test_order_import_error_is_listed_and_drops_off_once_the_order_exists(self):
		for account, key, order_id in (
			(self.make_talabat_account(), "order_id", "talabat-order-1"),
			(self.make_shopify_account(), "order_gid", "gid://shopify/Order/1"),
			(self.make_noon_account(), "fbpi_order_nr", "NOON-ORDER-1"),
		):
			log = self.make_log(
				account, request_data=frappe.as_json({"account": account.name, key: order_id})
			)

			rows = self.items_for(account, ORDER_IMPORT_ERROR)
			self.assertEqual([row["reference_name"] for row in rows], [log.name], account.platform)
			self.assertEqual(rows[0]["next_action"], "View Error")
			self.assertEqual(rows[0]["reference_doctype"], "Marketplace Sync Log")

			self.make_order(
				account,
				external_order_id=order_id,
				last_platform_update_on=now_datetime() + timedelta(minutes=1),
			)
			self.assertEqual(self.items_for(account, ORDER_IMPORT_ERROR), [], account.platform)

	def test_failed_update_of_an_existing_order_stays_visible(self):
		account = self.make_talabat_account()
		self.make_order(
			account,
			external_order_id="old-order",
			last_platform_update_on=now_datetime() - timedelta(hours=1),
		)
		log = self.make_log(account, request_data=frappe.as_json({"order_id": "old-order"}))

		rows = self.items_for(account, ORDER_IMPORT_ERROR)

		self.assertEqual([row["reference_name"] for row in rows], [log.name])

	def test_order_without_platform_update_time_falls_back_to_modified(self):
		account = self.make_talabat_account()
		self.make_log(
			account,
			request_data=frappe.as_json({"order_id": "fallback-order"}),
			attempted_at=now_datetime() - timedelta(minutes=5),
		)
		# modified is "now", i.e. later than the failed attempt.
		self.make_order(account, external_order_id="fallback-order")

		self.assertEqual(self.items_for(account, ORDER_IMPORT_ERROR), [])

	def test_order_created_after_the_failure_resolves_it_despite_an_older_platform_time(self):
		# Shopify's last_platform_update_on is the platform's updatedAt, which
		# can predate the failed attempt even though the order was imported
		# (created here) only afterwards.
		account = self.make_shopify_account()
		self.make_log(
			account,
			request_data=frappe.as_json({"account": account.name, "order_gid": "gid://shopify/Order/9"}),
			attempted_at=now_datetime() - timedelta(minutes=1),
		)
		self.make_order(
			account,
			external_order_id="gid://shopify/Order/9",
			last_platform_update_on=now_datetime() - timedelta(hours=1),
		)

		self.assertEqual(self.items_for(account, ORDER_IMPORT_ERROR), [])

	def test_order_import_error_for_the_same_order_is_listed_once(self):
		account = self.make_talabat_account()
		data = frappe.as_json({"account": account.name, "order_id": "dup-order"})
		self.make_log(account, request_data=data, attempted_at=now_datetime() - timedelta(hours=1))
		latest = self.make_log(account, request_data=data)

		rows = self.items_for(account, ORDER_IMPORT_ERROR)

		self.assertEqual([row["reference_name"] for row in rows], [latest.name])

	def test_order_import_error_with_an_unrecognized_shape_stays_visible(self):
		account = self.make_talabat_account()
		self.make_order(
			account, external_order_id="whatever", last_platform_update_on=now_datetime() + timedelta(hours=1)
		)
		unparseable = self.make_log(account, request_data="not json")
		unknown_keys = self.make_log(account, request_data=frappe.as_json({"something": "whatever"}))

		names = {row["reference_name"] for row in self.items_for(account, ORDER_IMPORT_ERROR)}

		self.assertEqual(names, {unparseable.name, unknown_keys.name})

	def test_order_import_log_that_is_not_an_error_is_ignored(self):
		account = self.make_talabat_account()
		self.make_log(account, status="Success")
		self.make_log(account, status="Invalid")

		self.assertEqual(self.items_for(account, ORDER_IMPORT_ERROR), [])

	# -- fulfillment errors -----------------------------------------------

	def test_fulfillment_error_is_listed_until_a_later_success_of_the_same_action(self):
		account = self.make_noon_account()
		order = self.make_order(account)
		error = self.make_log(
			account,
			sync_type="Fulfillment",
			marketplace_order=order.name,
			attempted_at=now_datetime() - timedelta(minutes=10),
			request_data=frappe.as_json({"action": "UpdateOrder"}),
		)

		rows = self.items_for(account, FULFILLMENT_ERROR)
		self.assertEqual([row["reference_name"] for row in rows], [error.name])
		self.assertEqual(rows[0]["next_action"], "View Error")

		# A later success of a DIFFERENT action does not resolve it.
		self.make_log(
			account,
			sync_type="Fulfillment",
			status="Success",
			marketplace_order=order.name,
			request_data=frappe.as_json({"action": "UploadOrderInvoice"}),
		)
		self.assertEqual(len(self.items_for(account, FULFILLMENT_ERROR)), 1)

		self.make_log(
			account,
			sync_type="Fulfillment",
			status="Success",
			marketplace_order=order.name,
			request_data=frappe.as_json({"action": "UpdateOrder"}),
		)
		self.assertEqual(self.items_for(account, FULFILLMENT_ERROR), [])

	def make_fulfillment_log(self, account, order, action, status="Error", minutes_ago=0, **request_data):
		return self.make_log(
			account,
			sync_type="Fulfillment",
			status=status,
			marketplace_order=order.name,
			attempted_at=now_datetime() - timedelta(minutes=minutes_ago),
			request_data=frappe.as_json({"action": action, **request_data}),
		)

	def test_fulfillment_errors_are_tracked_per_order_and_action(self):
		account = self.make_noon_account()
		order = self.make_order(account)
		other_order = self.make_order(account)
		failed = self.make_fulfillment_log(account, order, "UpdateOrder", minutes_ago=10)
		resolved = self.make_fulfillment_log(account, other_order, "UpdateOrder", minutes_ago=10)
		self.make_fulfillment_log(account, other_order, "UpdateOrder", "Success")

		rows = self.items_for(account, FULFILLMENT_ERROR)

		self.assertEqual([row["reference_name"] for row in rows], [failed.name])
		self.assertNotIn(resolved.name, [row["reference_name"] for row in rows])

	def test_error_rows_of_the_retired_shipment_actions_are_never_listed(self):
		# The old Marketplace Order shipment flow is gone (#415): nothing is left to retry or discard.
		account = self.make_noon_account()
		order = self.make_order(account)
		for action in RETIRED_FULFILLMENT_ACTIONS:
			self.make_fulfillment_log(account, order, action, integration_shipment_nr="S-1")

		self.assertEqual(self.items_for(account, FULFILLMENT_ERROR), [])

	def test_fulfillment_explanation_uses_the_message_never_the_traceback(self):
		account = self.make_noon_account()
		order = self.make_order(account)
		with_message = self.make_log(
			account,
			sync_type="Fulfillment",
			marketplace_order=order.name,
			request_data=frappe.as_json({"action": "UpdateOrder"}),
			response_data=frappe.as_json("Noon rejected the update."),
			traceback="Traceback (most recent call last): secret",
		)
		traceback_only = self.make_log(
			account,
			sync_type="Fulfillment",
			marketplace_order=order.name,
			request_data=frappe.as_json({"action": "UploadOrderInvoice"}),
			traceback="Traceback (most recent call last): secret",
		)

		rows = {row["reference_name"]: row for row in self.items_for(account, FULFILLMENT_ERROR)}

		self.assertIn("Noon rejected the update.", rows[with_message.name]["explanation"])
		self.assertIn("Upload order invoice failed", rows[traceback_only.name]["explanation"])
		for row in rows.values():
			self.assertNotIn("Traceback", row["explanation"])
			self.assertNotIn("secret", row["explanation"])

	def test_fulfillment_explanation_uses_plain_verbs_and_the_platform_order_reference(self):
		account = self.make_noon_account()
		order = self.make_order(account, platform_order_reference="21873645")
		no_reference = self.make_order(account)
		# Marketplace Order.before_save refills an empty reference with external_order_id, so blank it in the DB.
		frappe.db.set_value("Marketplace Order", no_reference.name, "platform_order_reference", "", update_modified=False)
		verbs = {
			"UpdateOrder": "Mark units Out of Stock",
			"UploadOrderInvoice": "Upload order invoice",
			"ReleaseStuckOosReservation": "Release Out of Stock reservation",
			"SomethingNew": "SomethingNew",
		}
		logs = {action: self.make_fulfillment_log(account, order, action) for action in verbs}
		fallback = self.make_fulfillment_log(account, no_reference, "UpdateOrder")

		rows = {row["reference_name"]: row for row in self.items_for(account, FULFILLMENT_ERROR)}

		for action, verb in verbs.items():
			explanation = rows[logs[action].name]["explanation"]
			self.assertTrue(explanation.startswith(f"{verb} failed on order 21873645: "), explanation)
			self.assertNotIn(order.name, explanation)
		# Without a platform reference the order name is still shown.
		self.assertIn(f"on order {no_reference.name}:", rows[fallback.name]["explanation"])

	# -- Shopify webhook retries ------------------------------------------

	def make_delivery(self, account, attempt_count, status="Error"):
		connection = frappe.get_doc("Shopify Connection", account.connection)
		return self._track(
			frappe.get_doc(
				{
					"doctype": "Shopify Webhook Delivery",
					"connection": connection.name,
					"shop_domain": connection.shop_domain,
					"topic": "ORDERS_UPDATED",
					"webhook_id": f"wh-{frappe.generate_hash(length=10)}",
					"received_at": now_datetime(),
					"status": status,
					"attempt_count": attempt_count,
					"error": "handler failed",
				}
			).insert()
		)

	def test_webhook_retries_are_flagged_only_at_the_max_attempt_boundary(self):
		account = self.make_shopify_account()
		self.make_delivery(account, MAX_WEBHOOK_DELIVERY_ATTEMPTS - 1)
		self.make_delivery(account, MAX_WEBHOOK_DELIVERY_ATTEMPTS, status="Processed")
		exhausted = self.make_delivery(account, MAX_WEBHOOK_DELIVERY_ATTEMPTS)

		rows = self.items_for(account, WEBHOOK_RETRIES_EXHAUSTED)

		self.assertEqual([row["reference_name"] for row in rows], [exhausted.name])
		self.assertEqual(rows[0]["reference_doctype"], "Shopify Webhook Delivery")
		self.assertEqual(rows[0]["next_action"], "View Error")
		self.assertEqual(rows[0]["platform"], "Shopify")

	# -- Noon rollup -------------------------------------------------------

	def test_noon_pending_out_of_stock_units_are_one_row_per_order(self):
		account = self.make_noon_account()
		order = self.make_order(account, noon_pending_oos_units=frappe.as_json(["unit-1", "unit-2"]))
		self.make_order(account, noon_pending_oos_units=frappe.as_json([]))

		rows = self.items_for(account, NOON_FULFILLMENT)

		self.assertEqual([row["reference_name"] for row in rows], [order.name])
		self.assertEqual(rows[0]["next_action"], "Open Order")
		self.assertEqual(rows[0]["explanation"], "2 unit(s) pending Out of Stock")

	def test_noon_pending_oos_alone_is_flagged(self):
		account = self.make_noon_account()
		order = self.make_order(account, noon_pending_oos_units=frappe.as_json(["unit-1"]))

		rows = self.items_for(account, NOON_FULFILLMENT)

		self.assertEqual([row["reference_name"] for row in rows], [order.name])

	# -- Noon unlinked returns (#164) ----------------------------------------------------------

	def test_a_clean_noon_order_produces_no_finding(self):
		account = self.make_noon_account()
		self.make_order(account)

		self.assertEqual(get_attention_items({"account": account.name}), [])

	def make_noon_return(self, account, purchase_item_nr, **overrides):
		values = {
			"doctype": "Noon Return",
			"marketplace_account": account.name,
			"purchase_item_nr": purchase_item_nr,
			"order_nr": "ORD-NONE",
			"found_via": "Manual Lookup",
			"first_seen_on": now_datetime(),
			"last_seen_on": now_datetime(),
			**overrides,
		}
		return self._track(frappe.get_doc(values).insert())

	def test_an_unlinked_noon_return_is_one_finding(self):
		account = self.make_noon_account()
		order = self.make_order(account)
		unlinked = self.make_noon_return(account, f"PI-U-{self.suffix}")
		self.make_noon_return(account, f"PI-L-{self.suffix}", marketplace_order=order.name)

		rows = self.items_for(account, NOON_RETURN_UNLINKED)

		self.assertEqual([row["reference_name"] for row in rows], [unlinked.name])
		self.assertEqual(rows[0]["reference_doctype"], "Noon Return")
		self.assertEqual(rows[0]["next_action"], "Open Return")
		# Counted once, and not as an order, import error or fulfillment error.
		self.assertEqual(len(get_attention_items({"account": account.name})), 1)
		self.assertEqual(
			get_attention_counts([account.name])[account.name],
			{
				"orders_needing_attention": 0,
				"import_errors": 0,
				"fulfillment_errors": 0,
				"payment_errors": 0,
			},
		)

	def test_unlinked_returns_are_scoped_to_the_account(self):
		account = self.make_noon_account()
		other = self.make_noon_account()
		self.make_noon_return(other, f"PI-O-{self.suffix}")

		self.assertEqual(self.items_for(account, NOON_RETURN_UNLINKED), [])

	def test_new_findings_are_permission_scoped_through_get_list(self):
		account = self.make_noon_account()
		self.make_order(account, noon_pending_oos_units=frappe.as_json(["unit-1"]))
		self.make_noon_return(account, f"PI-P-{self.suffix}")
		real_get_list = frappe.get_list
		doctypes = []

		def spy(doctype, *args, **kwargs):
			doctypes.append(doctype)
			return real_get_list(doctype, *args, **kwargs)

		with patch(
			"marketplace_integrations.services.marketplace_attention.frappe.get_list", side_effect=spy
		):
			get_attention_items({"account": account.name})

		self.assertIn("Noon Return", doctypes)
		self.assertIn("Marketplace Order", doctypes)

	# -- filters, counts, permissions ---------------------------------------

	def test_filters_scope_by_platform_account_and_problem_type(self):
		talabat = self.make_talabat_account()
		noon = self.make_noon_account()
		self.make_order(talabat, erpnext_sync_status="Sync Error")
		self.make_order(noon, erpnext_sync_status="Needs Action")

		self.assertEqual({r["platform"] for r in get_attention_items({"account": talabat.name})}, {"Talabat"})
		noon_rows = [r for r in get_attention_items({"platform": "Noon"}) if r["account"] == noon.name]
		self.assertEqual([r["problem_type"] for r in noon_rows], [NEEDS_ACTION])
		self.assertEqual(
			get_attention_items({"account": talabat.name, "problem_type": NEEDS_ACTION}), []
		)

	def test_counts_group_orders_and_non_order_problems_per_account(self):
		account = self.make_noon_account()
		other = self.make_talabat_account()
		order = self.make_order(account, erpnext_sync_status="Sync Error", items=[self.talabat_item("SKU-X")])
		self.make_log(account)
		self.make_fulfillment_log(account, order, "UpdateOrder")
		self.make_order(other, erpnext_sync_status="Synced")

		counts = get_attention_counts([account.name, other.name])

		# One order with two problems (Sync Error + Unmapped Items) counts once;
		# import and fulfillment errors are counted separately.
		self.assertEqual(
			counts[account.name],
			{
				"orders_needing_attention": 1,
				"import_errors": 1,
				"fulfillment_errors": 1,
				"payment_errors": 0,
			},
		)
		self.assertEqual(
			counts[other.name],
			{
				"orders_needing_attention": 0,
				"import_errors": 0,
				"fulfillment_errors": 0,
				"payment_errors": 0,
			},
		)

	def test_exhausted_webhook_deliveries_are_listed_but_not_counted(self):
		account = self.make_shopify_account()
		self.make_delivery(account, MAX_WEBHOOK_DELIVERY_ATTEMPTS)

		self.assertEqual(len(self.items_for(account, WEBHOOK_RETRIES_EXHAUSTED)), 1)
		self.assertEqual(
			get_attention_counts([account.name])[account.name],
			{
				"orders_needing_attention": 0,
				"import_errors": 0,
				"fulfillment_errors": 0,
				"payment_errors": 0,
			},
		)

	def test_counts_for_no_accounts_is_empty(self):
		self.assertEqual(get_attention_counts([]), {})

	def test_unreadable_data_is_denied_not_leaked(self):
		self.make_order(self.make_talabat_account(), erpnext_sync_status="Sync Error")

		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				get_attention_items()
		finally:
			frappe.set_user("Administrator")
