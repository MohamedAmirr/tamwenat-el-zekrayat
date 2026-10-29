# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Refuse cancelling a Sales Order while its Noon shipments are not Cancelled or Discarded (#376).

"Wait for Noon" (``docs/noon-integration/sales-order-cancel.md``): the user cancels the Created
shipments on Noon first (``fulfillment.cancel_created_shipments``), then cancels the Sales Order. This
``before_cancel`` guard is the server-side gate; the rules (and the messages) live in
``fulfillment.sales_order_cancel_blockers``. Close is not guarded (ERPNext fires no event for it).

The app's own cancel passes: ``sync_sales_order`` registers the Marketplace Order in
``frappe.flags.marketplace_syncing_orders`` and the reconciliation planner, which cancels a Sales
Order when the customer cancelled every unit on Noon, only runs inside it.
"""

import frappe


def block_cancel_while_shipments_are_open(doc, method=None):
	"""``before_cancel`` on Sales Order: refuse while a Noon shipment is Pending, Created, Cancel Pending or Error."""
	marketplace_order = doc.get("custom_marketplace_order")
	if not marketplace_order:
		return
	if marketplace_order in (frappe.flags.get("marketplace_syncing_orders") or ()):
		return
	if frappe.db.get_value("Marketplace Order", marketplace_order, "platform") != "Noon":
		return
	# Imported here so loading the hook does not import the whole fulfillment module.
	from marketplace_integrations.integrations.noon.fulfillment import sales_order_cancel_blockers

	blockers = sales_order_cancel_blockers(frappe.get_doc("Marketplace Order", marketplace_order))
	if blockers:
		frappe.throw(
			"<br>".join(blockers),
			title=frappe._("Cancel the Noon shipments first"),
		)
