# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Test-only fixture (#368): a REAL submitted Sales Order linked to a Marketplace Order, with
non-stock Items (so a draft Delivery Note needs no stock) and enabled Marketplace Product Mappings.
Not collected by the test runner (no ``test_`` prefix) and not imported by production code.
"""

import frappe
from frappe.utils import add_days, nowdate

from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.services.sync_status import SYNCED


class SubmittedOrderFixture:
	"""``rows`` is ``[(sku, qty), ...]``; each SKU gets its own Item and enabled mapping on ``account``
	(a SKU listed in ``unmapped`` gets an Item on the Sales Order but no mapping)."""

	def __init__(self, account, rows, platform_order_id=None, unmapped=(), submit=True):
		self.account = account
		self.suffix = frappe.generate_hash(length=8)
		self.items = {}
		self.mappings = []
		self.customer = None
		self.marketplace_order = None
		self.sales_order = None
		self.name = None
		try:
			self._build(rows, platform_order_id, unmapped, submit)
		except BaseException:
			# A half-built fixture would leak: the caller never gets the object to clean up.
			self.cleanup()
			raise

	def _build(self, rows, platform_order_id, unmapped, submit):
		account = self.account
		suffix = self.suffix
		company = frappe.defaults.get_global_default("company")
		price_list = frappe.get_all("Price List", filters={"selling": 1, "enabled": 1}, pluck="name", limit=1)[0]
		warehouse = frappe.get_all(
			"Warehouse", filters={"disabled": 0, "is_group": 0, "company": company}, pluck="name", limit=1
		)[0]
		for sku, _qty in rows:
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": f"TEST-HANDOVER-{sku}-{suffix}",
					"item_group": "Products",
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert()
			self.items[sku] = item
			if sku not in unmapped:
				self.mappings.append(
					frappe.get_doc(
						{
							"doctype": "Marketplace Product Mapping",
							"marketplace_account": account.name,
							"external_reference": sku,
							"external_sku": sku,
							"item": item.name,
							"enabled": 1,
						}
					).insert()
				)
		self.customer = frappe.get_doc(
			{"doctype": "Customer", "customer_name": f"Test Handover {suffix}", "customer_type": "Company"}
		).insert()
		self.marketplace_order = upsert_marketplace_order(
			account.name,
			{
				"external_order_id": platform_order_id or f"ORD-HO-{suffix}",
				"received_on": nowdate(),
				"latest_payload": {"items": []},
				"items": [],
			},
		)
		self.sales_order = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"company": company,
				"customer": self.customer.name,
				"selling_price_list": price_list,
				"transaction_date": nowdate(),
				"delivery_date": add_days(nowdate(), 1),
				"ignore_pricing_rule": 1,
				"custom_marketplace_order": self.marketplace_order.name,
				"items": [
					{
						"item_code": self.items[sku].name,
						"qty": qty,
						"rate": 10,
						"uom": "Nos",
						"warehouse": warehouse,
					}
					for sku, qty in rows
				],
			}
		).insert()
		if submit:
			self.sales_order.submit()
		self.name = self.sales_order.name
		frappe.db.set_value(
			"Marketplace Order",
			self.marketplace_order.name,
			{
				"erpnext_sales_order": self.name,
				"erpnext_sync_status": SYNCED,  # as after a completed sync
				"sync_debounce_until": None,
				"pending_sync_trigger_source": None,
			},
		)
		self.marketplace_order.reload()

	def row_for(self, sku):
		item = self.items[sku].name
		return next(row for row in frappe.get_doc("Sales Order", self.name).items if row.item_code == item)

	def delivery_notes(self):
		"""Names of every Delivery Note that has a row on this Sales Order."""
		return sorted(
			set(
				frappe.get_all(
					"Delivery Note Item",
					filters={"against_sales_order": self.name},
					pluck="parent",
					parent_doctype="Delivery Note",
				)
			)
		)

	def cleanup(self):
		"""Remove everything the fixture created, in dependency order.

		Idempotent and failure-safe: every step is isolated in its own try/except, so a step that
		fails (or an object that is already gone) never stops the later steps and never raises,
		which keeps a cleanup problem from masking the failure of the test itself.  A failing step
		is written to the Error Log under the title "handover fixture cleanup step failed", so a
		leak can be traced.  Submitted Delivery Notes and the submitted Sales Order are cancelled
		before they are deleted.  The final sweep removes anything still tied to this fixture's
		Customer or unique suffix (for example a Sales Order whose build failed half way).  Commits
		at the end: the deletes would otherwise be undone by the test class's rollback.
		"""
		self._step(self._remove_delivery_notes)
		if self.name:
			self._step(
				lambda: frappe.db.set_value(
					"Marketplace Order", {"erpnext_sales_order": self.name}, "erpnext_sales_order", None
				)
			)
			self._step(self._remove_sales_order)
		self._step(self._sweep_by_marker)
		for mapping in self.mappings:
			self._step(lambda mapping=mapping: self._remove("Marketplace Product Mapping", mapping.name))
		if self.customer:
			self._step(lambda: self._remove("Customer", self.customer.name))
		for item in self.items.values():
			self._step(lambda item=item: self._remove("Item", item.name))
		self._step(frappe.db.commit)

	def _sweep_by_marker(self):
		"""Documents of this fixture that were created but never reached ``self.name``."""
		if not self.customer:
			return
		for name in frappe.get_all("Sales Order", filters={"customer": self.customer.name}, pluck="name"):
			self.name = name
			self._step(self._remove_delivery_notes)
			self._step(self._remove_sales_order)

	@staticmethod
	def _step(function):
		try:
			function()
		except Exception:
			frappe.log_error(title="handover fixture cleanup step failed", message=frappe.get_traceback())

	@staticmethod
	def _remove(doctype, name):
		if frappe.db.exists(doctype, name):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)

	def _remove_delivery_notes(self):
		if not self.name:
			return
		for name in self.delivery_notes():
			docstatus = frappe.db.get_value("Delivery Note", name, "docstatus")
			if docstatus == 1:
				note = frappe.get_doc("Delivery Note", name)
				note.flags.ignore_permissions = True
				note.cancel()
			self._remove("Delivery Note", name)

	def _remove_sales_order(self):
		if frappe.db.get_value("Sales Order", self.name, "docstatus") == 1:
			order = frappe.get_doc("Sales Order", self.name)
			order.flags.ignore_permissions = True
			# #376: the Noon cancel guard refuses while a shipment is open; cleanup is not a user cancel.
			syncing = frappe.flags.setdefault("marketplace_syncing_orders", set())
			marker = order.get("custom_marketplace_order")
			added = bool(marker) and marker not in syncing
			if added:
				syncing.add(marker)
			try:
				order.cancel()
			finally:
				if added:
					syncing.discard(marker)
		self._remove("Sales Order", self.name)
