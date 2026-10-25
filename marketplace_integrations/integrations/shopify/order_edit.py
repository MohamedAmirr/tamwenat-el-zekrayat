# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Shopify's explicit, synchronous order-edit session workflow (#356)."""

import frappe
from frappe import _
from frappe.utils import cstr, flt

from marketplace_integrations.integrations.shopify.client import ShopifyClient


class OrderEditRejected(frappe.ValidationError):
	"""A definitive rejection (userErrors or a failed pre-check): the edit was not applied."""


class OrderEditOutcomeUnknown(frappe.ValidationError):
	"""The commit mutation failed without a definitive answer; Shopify may have applied it."""


_BEGIN = """
mutation BeginOrderEdit($id: ID!) {
  orderEditBegin(id: $id) {
    calculatedOrder {
      id
      lineItems(first: 250) {
        nodes { id quantity variant { id } }
        pageInfo { hasNextPage }
      }
    }
    userErrors { field message }
  }
}
"""

_SET_QUANTITY = """
mutation SetOrderEditQuantity($id: ID!, $lineItemId: ID!, $quantity: Int!, $restock: Boolean) {
  orderEditSetQuantity(id: $id, lineItemId: $lineItemId, quantity: $quantity, restock: $restock) {
    calculatedOrder { id }
    userErrors { field message }
  }
}
"""

_ADD_VARIANT = """
mutation AddOrderEditVariant($id: ID!, $variantId: ID!, $quantity: Int!) {
  orderEditAddVariant(id: $id, variantId: $variantId, quantity: $quantity) {
    calculatedOrder { id }
    userErrors { field message }
  }
}
"""

_COMMIT = """
mutation CommitOrderEdit($id: ID!, $notifyCustomer: Boolean, $staffNote: String) {
  orderEditCommit(id: $id, notifyCustomer: $notifyCustomer, staffNote: $staffNote) {
    order { id }
    successMessages
    userErrors { field message }
  }
}
"""


def commit_order_edit(account, order_gid, changes, staff_note):
	"""Apply a prevalidated change list and return Shopify's committed order ID.

	This function makes no local writes.  The caller records its durable action
	log and refreshes the Marketplace Order only after Shopify accepts commit.

	``CalculatedLineItem`` IDs belong to the edit session.  The app stores only the
	original ``LineItem`` GIDs, so each one is rewritten to a CalculatedLineItem GID
	by swapping the type segment.  That relies on the numeric ID being the same in
	both types, which Shopify's docs do not guarantee: it is to be verified on the
	dev store.  So, before any mutation, every rewritten ID must exist in the
	session's own ``calculatedOrder.lineItems`` with ``quantity == old_quantity``;
	otherwise nothing is changed and the edit fails closed.

	Anything raised before the commit mutation leaves Shopify unchanged (an
	uncommitted edit session applies nothing).  ``OrderEditOutcomeUnknown`` is raised
	only when the commit call itself fails without a definitive answer.  The first
	page of lines (250, Shopify's maximum for a connection) must hold every line.
	"""
	if account.platform != "Shopify" or account.connection_doctype != "Shopify Connection":
		raise OrderEditRejected(_("Marketplace Account {0} is not a Shopify account.").format(account.name))
	client = ShopifyClient(account.connection)
	begin = _mutation(client, _BEGIN, {"id": order_gid}, "orderEditBegin")
	calculated_id = begin["calculatedOrder"]["id"]
	_verify_calculated_lines(begin["calculatedOrder"], changes)
	for change in changes:
		if change["kind"] == "set_quantity":
			result = _mutation(
				client,
				_SET_QUANTITY,
				{
					"id": calculated_id,
					"lineItemId": _calculated_line_item_id(change["external_item_id"]),
					"quantity": int(change["quantity"]),
					"restock": False,
				},
				"orderEditSetQuantity",
			)
		else:
			result = _mutation(
				client,
				_ADD_VARIANT,
				{
					"id": calculated_id,
					"variantId": change["external_variant_id"],
					"quantity": int(change["quantity"]),
				},
				"orderEditAddVariant",
			)
		calculated_id = result["calculatedOrder"]["id"]
	try:
		commit = _mutation(
			client,
			_COMMIT,
			{"id": calculated_id, "notifyCustomer": False, "staffNote": staff_note},
			"orderEditCommit",
		)
	except OrderEditRejected:
		raise
	except Exception as exc:
		raise OrderEditOutcomeUnknown(
			_("Shopify did not confirm the item changes; check the order in Shopify before retrying.")
		) from exc
	return {"order_id": commit["order"]["id"], "success_messages": commit.get("successMessages") or []}


def _verify_calculated_lines(calculated_order, changes):
	lines = calculated_order.get("lineItems") or {}
	if (lines.get("pageInfo") or {}).get("hasNextPage"):
		raise OrderEditRejected(_("The Shopify order has too many lines to edit from ERPNext."))
	quantities = {node.get("id"): node.get("quantity") for node in lines.get("nodes") or [] if node}
	for change in changes:
		if change["kind"] != "set_quantity":
			continue
		line_id = _calculated_line_item_id(change["external_item_id"])
		if line_id not in quantities or flt(quantities[line_id]) != flt(change["old_quantity"]):
			raise OrderEditRejected(
				_(
					"Shopify's edit session does not contain line {0} with quantity {1}; refresh the order and try again."
				).format(change["external_item_id"], change["old_quantity"])
			)


def _mutation(client, query, variables, field):
	result = (client.execute(query, variables) or {}).get(field) or {}
	errors = result.get("userErrors") or []
	if errors:
		messages = [cstr(error.get("message")).strip() for error in errors if isinstance(error, dict)]
		raise OrderEditRejected(
			"; ".join(message for message in messages if message) or _("Shopify rejected the order edit.")
		)
	if not result.get("calculatedOrder") and field != "orderEditCommit":
		raise OrderEditRejected(_("Shopify returned no calculated order."))
	if field == "orderEditCommit" and not result.get("order"):
		raise frappe.ValidationError(_("Shopify returned no committed order."))
	return result


def _calculated_line_item_id(line_item_id):
	return line_item_id.replace("/LineItem/", "/CalculatedLineItem/", 1)
