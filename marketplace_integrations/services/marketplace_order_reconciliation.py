# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Nightly order catch-up enqueue coordinator (#354)."""

import frappe


def enqueue_nightly_order_reconciliation():
	"""Enqueue one deduplicated long job per enabled Shopify/Noon account."""
	frappe.set_user("Administrator")
	queued = 0
	errors = []
	for account in frappe.get_all(
		"Marketplace Account",
		filters={"platform": ["in", ["Shopify", "Noon"]], "enabled": 1},
		fields=["name", "platform"],
	):
		try:
			if account.platform == "Shopify":
				job = frappe.enqueue(
					"marketplace_integrations.integrations.shopify.orders.reconcile_all_accounts_orders",
					queue="long",
					timeout=3600,
					job_id=f"marketplace-shopify-order-reconciliation-{account.name}",
					deduplicate=True,
					account_name=account.name,
					catch_up_unlinked=True,
				)
			else:
				from marketplace_integrations.integrations.noon.orders import enqueue_order_import

				job = enqueue_order_import(account.name, trigger_source="Scheduled Reconciliation")
			queued += bool(job)
		except Exception as exc:
			errors.append({"account": account.name, "error": str(exc)[:300]})
	if errors:
		frappe.log_error(
			title="Nightly marketplace order reconciliation enqueue failures",
			message=frappe.as_json({"queued": queued, "errors": errors}),
		)
	return {"queued": queued, "errors": len(errors)}
