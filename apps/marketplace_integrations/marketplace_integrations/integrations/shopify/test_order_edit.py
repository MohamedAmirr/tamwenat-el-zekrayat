# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.shopify.order_edit import (
	OrderEditOutcomeUnknown,
	OrderEditRejected,
	commit_order_edit,
)

CALCULATED_ORDER = "gid://shopify/CalculatedOrder/1"


def _begin(lines=None, has_next_page=False):
	nodes = [
		{"id": line_id, "quantity": quantity, "variant": {"id": "gid://shopify/ProductVariant/9"}}
		for line_id, quantity in (
			lines if lines is not None else [("gid://shopify/CalculatedLineItem/99", 2)]
		)
	]
	return {
		"orderEditBegin": {
			"calculatedOrder": {
				"id": CALCULATED_ORDER,
				"lineItems": {"nodes": nodes, "pageInfo": {"hasNextPage": has_next_page}},
			},
			"userErrors": [],
		}
	}


def _ok(field):
	return {field: {"calculatedOrder": {"id": CALCULATED_ORDER}, "userErrors": []}}


COMMITTED = {
	"orderEditCommit": {
		"order": {"id": "gid://shopify/Order/1"},
		"successMessages": ["Order updated"],
		"userErrors": [],
	}
}
SET_LINE = {
	"kind": "set_quantity",
	"external_item_id": "gid://shopify/LineItem/99",
	"quantity": 0.0,
	"old_quantity": 2.0,
}
ADD_VARIANT = {
	"kind": "add_variant",
	"external_variant_id": "gid://shopify/ProductVariant/2",
	"quantity": 1.0,
}


def _account(**overrides):
	fields = {"platform": "Shopify", "connection_doctype": "Shopify Connection", "connection": "SHOP-1"}
	return MagicMock(**{**fields, **overrides})


class UnitTestShopifyOrderEdit(UnitTestCase):
	def _run(self, side_effect, changes, account=None):
		with patch("marketplace_integrations.integrations.shopify.order_edit.ShopifyClient") as client_class:
			client = MagicMock()
			client_class.return_value = client
			client.execute.side_effect = side_effect
			try:
				return commit_order_edit(
					account or _account(), "gid://shopify/Order/1", changes, "ERPNext note"
				), client
			except Exception:
				self.client = client
				raise

	def test_commits_calculated_line_changes_without_restocking_or_notifying(self):
		result, client = self._run(
			[_begin(), _ok("orderEditSetQuantity"), _ok("orderEditAddVariant"), COMMITTED],
			[SET_LINE, ADD_VARIANT],
		)

		self.assertEqual(result, {"order_id": "gid://shopify/Order/1", "success_messages": ["Order updated"]})
		calls = client.execute.call_args_list
		self.assertEqual(calls[1].args[1]["lineItemId"], "gid://shopify/CalculatedLineItem/99")
		self.assertFalse(calls[1].args[1]["restock"])
		self.assertFalse(calls[3].args[1]["notifyCustomer"])

	def test_quantities_are_cast_to_int_only_when_building_the_variables(self):
		_result, client = self._run(
			[_begin(), _ok("orderEditSetQuantity"), _ok("orderEditAddVariant"), COMMITTED],
			[{**SET_LINE, "quantity": 3.0}, ADD_VARIANT],
		)

		for index in (1, 2):
			quantity = client.execute.call_args_list[index].args[1]["quantity"]
			self.assertIs(type(quantity), int)
		self.assertEqual(client.execute.call_args_list[1].args[1]["quantity"], 3)

	def test_does_not_continue_after_a_shopify_user_error(self):
		with self.assertRaisesRegex(OrderEditRejected, "Rejected"):
			self._run(
				[
					_begin(),
					{
						"orderEditSetQuantity": {
							"calculatedOrder": {"id": CALCULATED_ORDER},
							"userErrors": [{"message": "Rejected"}],
						}
					},
				],
				[{**SET_LINE, "quantity": 1.0}],
			)
		self.assertEqual(self.client.execute.call_count, 2)

	def test_fails_closed_before_any_mutation_when_a_rewritten_line_id_is_missing_or_has_another_quantity(
		self,
	):
		cases = {
			"missing": _begin(lines=[("gid://shopify/CalculatedLineItem/1", 2)]),
			"other quantity": _begin(lines=[("gid://shopify/CalculatedLineItem/99", 5)]),
			"no lines": _begin(lines=[]),
			"too many lines": _begin(has_next_page=True),
		}
		for name, begin in cases.items():
			with self.subTest(name), self.assertRaises(frappe.ValidationError):
				self._run([begin], [SET_LINE])
			# Only orderEditBegin was sent.
			self.assertEqual(self.client.execute.call_count, 1)

	def test_added_variants_need_no_existing_line(self):
		_result, client = self._run([_begin(lines=[]), _ok("orderEditAddVariant"), COMMITTED], [ADD_VARIANT])
		self.assertEqual(client.execute.call_count, 3)

	def test_a_transport_failure_on_the_commit_call_is_an_unknown_outcome(self):
		with self.assertRaises(OrderEditOutcomeUnknown):
			self._run([_begin(), _ok("orderEditSetQuantity"), RuntimeError("timeout")], [SET_LINE])

	def test_a_commit_user_error_is_a_definite_rejection(self):
		rejected = {"orderEditCommit": {"order": None, "userErrors": [{"message": "Nope"}]}}
		with self.assertRaisesRegex(OrderEditRejected, "Nope"):
			self._run([_begin(), _ok("orderEditSetQuantity"), rejected], [SET_LINE])

	def test_a_failure_before_the_commit_call_propagates_unchanged(self):
		with self.assertRaises(RuntimeError):
			self._run([RuntimeError("timeout")], [SET_LINE])

	def test_a_non_shopify_account_is_rejected_before_any_call(self):
		for overrides in ({"platform": "Talabat"}, {"connection_doctype": "Talabat Connection"}):
			with self.subTest(overrides), self.assertRaisesRegex(OrderEditRejected, "not a Shopify account"):
				self._run([_begin()], [SET_LINE], account=_account(**overrides))
			self.client.execute.assert_not_called()
