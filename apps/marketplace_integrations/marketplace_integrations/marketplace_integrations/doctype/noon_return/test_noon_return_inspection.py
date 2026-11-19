# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note, make_sales_invoice
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime, nowdate

from marketplace_integrations.services.listing_quantity import get_sellable_qty
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.tests.utils import make_role_user

IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Marketplace Order",
	"Account",
	"User",
	"Mode of Payment",
	"Delivery Note",
	"Sales Invoice",
]


class TestNoonReturnInspection(IntegrationTestCase):
	"""#261: record the inspection outcome; Sellable drafts a Sales Return."""

	def setUp(self):
		super().setUp()
		self.suffix = frappe.generate_hash(length=8)
		self.created = []
		self.company = frappe.defaults.get_global_default("company")
		self.price_list = frappe.get_all(
			"Price List", filters={"selling": 1, "enabled": 1}, pluck="name", limit=1
		)[0]
		warehouses = frappe.get_all(
			"Warehouse",
			filters={"disabled": 0, "is_group": 0, "company": self.company},
			pluck="name",
			limit=1,
		)
		self.warehouse = warehouses[0]
		self.damaged_warehouse = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": f"Noon Damaged {self.suffix}",
				"company": self.company,
			}
		).insert()
		self.track("Warehouse", self.damaged_warehouse.name)
		self.item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"TEST-NOON-RET-{self.suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}
		).insert()
		self.track("Item", self.item.name)
		self.account = self.make_account()
		self.mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": self.item.name,
				"external_reference": f"SKU-{self.suffix}",
				"sales_uom": "Nos",
			}
		).insert()
		self.track("Marketplace Product Mapping", self.mapping.name)

	def tearDown(self):
		for doctype, name in reversed(self.created):
			if not frappe.db.exists(doctype, name):
				continue
			if (
				frappe.get_meta(doctype).is_submittable
				and frappe.db.get_value(doctype, name, "docstatus") == 1
			):
				frappe.get_doc(doctype, name).cancel()
			if doctype == "Warehouse":
				# Cancelled stock documents leave ledger rows that block deleting the warehouse.
				frappe.db.delete("Stock Ledger Entry", {"warehouse": name})
				frappe.db.delete("Bin", {"warehouse": name})
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def track(self, doctype, name):
		self.created.append((doctype, name))

	def make_account(self):
		connection = frappe.get_doc(
			{
				"doctype": "Noon Connection",
				"connection_name": f"Noon Inspect {self.suffix}",
				"project_code": f"test-noon-inspect-{self.suffix}",
				"key_id": "test-key-id",
				"private_key": "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
				"environment": "Static Sandbox",
			}
		).insert()
		self.track("Noon Connection", connection.name)
		with patch("marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"):
			account = frappe.get_doc(
				{
					"doctype": "Marketplace Account",
					"account_name": f"Noon Inspect Account {self.suffix}",
					"platform": "Noon",
					"external_account_id": f"test-noon-inspect-{self.suffix}",
					"connection_doctype": "Noon Connection",
					"connection": connection.name,
					"noon_warehouse_code": "NOON-WH-INSPECT",
					"noon_country_code": "ae",
					"erpnext_company": self.company,
					"erpnext_selling_price_list": self.price_list,
						"noon_damaged_warehouse": self.damaged_warehouse.name,
					"warehouse": self.warehouse,
				}
			).insert()
		self.track("Marketplace Account", account.name)
		return account

	def make_delivered_order(self, qty=2, delivery_warehouse=None, deliver_qty=None):
		"""A submitted Sales Order delivered by a submitted Delivery Note, linked from a Marketplace Order."""
		stock = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [
					{
						"item_code": self.item.name,
						"qty": 10,
						"uom": "Nos",
						"t_warehouse": delivery_warehouse or self.warehouse,
						"basic_rate": 10,
					}
				],
			}
		).insert()
		stock.submit()
		self.track("Stock Entry", stock.name)

		customer = frappe.get_all("Customer", pluck="name", limit=1)[0]
		sales_order = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": customer,
				"company": self.company,
				"transaction_date": nowdate(),
				"delivery_date": add_days(nowdate(), 1),
				"selling_price_list": self.price_list,
				"items": [
					{
						"item_code": self.item.name,
						"qty": qty,
						"rate": 20,
						"uom": "Nos",
						"warehouse": delivery_warehouse or self.warehouse,
						"delivery_date": add_days(nowdate(), 1),
					}
				],
			}
		).insert()
		sales_order.submit()
		self.track("Sales Order", sales_order.name)

		delivery_note = make_delivery_note(sales_order.name)
		if deliver_qty:
			delivery_note.items[0].qty = deliver_qty
		delivery_note.insert()
		delivery_note.submit()
		self.track("Delivery Note", delivery_note.name)

		order = upsert_marketplace_order(
			self.account.name,
			{
				"external_order_id": f"noon-inspect-order-{self.suffix}",
				"received_on": nowdate(),
				"latest_payload": {"fbpi_order_nr": "ORD-INSPECT"},
				"items": [
					{
						"external_item_id": "sku:inspect",
						"external_sku": f"SKU-{self.suffix}",
						"external_status": "CONFIRMED",
						"integration_status": "ACKNOWLEDGED",
						"current_quantity": qty,
						"transaction_amount": 40.0,
					}
				],
			},
		)
		self.track("Marketplace Order", order.name)
		frappe.db.set_value("Marketplace Order", order.name, "erpnext_sales_order", sales_order.name)
		return order, sales_order, delivery_note

	def make_return(self, order=None, acknowledged=True, **values):
		noon_return = frappe.get_doc(
			{
				"doctype": "Noon Return",
				"marketplace_account": self.account.name,
				"purchase_item_nr": f"test-purchase-item-{frappe.generate_hash(length=8)}",
				"partner_sku": f"SKU-{self.suffix}",
				"marketplace_order": order.name if order else None,
				"found_via": "Manual Lookup",
				"first_seen_on": now_datetime(),
				"last_seen_on": now_datetime(),
				**values,
			}
		).insert()
		self.track("Noon Return", noon_return.name)
		if acknowledged:
			noon_return.acknowledge_receipt()
			noon_return.reload()
		return noon_return

	def test_sellable_creates_one_draft_return_row_in_the_account_warehouse(self):
		order, sales_order, delivery_note = self.make_delivered_order(qty=2)
		noon_return = self.make_return(order)

		result = noon_return.record_inspection("Sellable")

		draft = frappe.get_doc("Delivery Note", result["sales_return"])
		self.track("Delivery Note", draft.name)
		self.assertEqual(draft.docstatus, 0)
		self.assertEqual(draft.is_return, 1)
		self.assertEqual(draft.return_against, delivery_note.name)
		self.assertEqual(len(draft.items), 1)
		self.assertEqual(draft.items[0].qty, -1)
		self.assertEqual(draft.items[0].item_code, self.item.name)
		self.assertEqual(draft.items[0].warehouse, self.warehouse)
		self.assertEqual(draft.items[0].against_sales_order, sales_order.name)
		noon_return.reload()
		self.assertEqual(noon_return.inspection_outcome, "Sellable")
		self.assertEqual(noon_return.sales_return, draft.name)

	def test_submitting_the_draft_raises_the_quantity_the_noon_stock_sync_reads(self):
		order, _sales_order, _delivery_note = self.make_delivered_order(qty=2)
		noon_return = self.make_return(order)
		before = get_sellable_qty(self.item.name, self.warehouse, "Nos")

		draft = frappe.get_doc("Delivery Note", noon_return.record_inspection("Sellable")["sales_return"])
		self.track("Delivery Note", draft.name)
		draft.submit()

		self.assertEqual(get_sellable_qty(self.item.name, self.warehouse, "Nos"), before + 1)

	def test_sellable_returns_into_the_account_warehouse_not_the_delivery_row_warehouse(self):
		other = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": f"Noon Inspect Other {self.suffix}",
				"company": self.company,
			}
		).insert()
		self.track("Warehouse", other.name)
		order, _sales_order, _delivery_note = self.make_delivered_order(qty=1, delivery_warehouse=other.name)
		noon_return = self.make_return(order)

		draft = frappe.get_doc("Delivery Note", noon_return.record_inspection("Sellable")["sales_return"])
		self.track("Delivery Note", draft.name)

		self.assertEqual(draft.items[0].warehouse, self.warehouse)

	def test_second_call_returns_the_same_draft_and_a_cancelled_one_is_replaced(self):
		order, _sales_order, _delivery_note = self.make_delivered_order(qty=2)
		noon_return = self.make_return(order)
		first = noon_return.record_inspection("Sellable")
		self.track("Delivery Note", first["sales_return"])

		again = noon_return.record_inspection("Sellable")
		self.assertEqual(again["sales_return"], first["sales_return"])
		self.assertTrue(again["already_created"])

		draft = frappe.get_doc("Delivery Note", first["sales_return"])
		draft.submit()
		draft.cancel()
		noon_return.reload()
		replaced = noon_return.record_inspection("Sellable")
		self.track("Delivery Note", replaced["sales_return"])
		self.assertNotEqual(replaced["sales_return"], first["sales_return"])

	def test_damaged_outcomes_draft_one_return_into_the_damaged_warehouse_only(self):
		for outcome in ("Damaged", "Damaged, Noon paid"):
			order, sales_order, delivery_note = self.make_delivered_order(qty=2)
			noon_return = self.make_return(order)
			before = get_sellable_qty(self.item.name, self.warehouse, "Nos")

			result = noon_return.record_inspection(outcome)

			draft = frappe.get_doc("Delivery Note", result["sales_return"])
			self.track("Delivery Note", draft.name)
			self.assertEqual(draft.docstatus, 0)
			self.assertEqual(draft.is_return, 1)
			self.assertEqual(draft.return_against, delivery_note.name)
			self.assertEqual(len(draft.items), 1)
			self.assertEqual(draft.items[0].qty, -1)
			self.assertEqual(draft.items[0].warehouse, self.damaged_warehouse.name)
			self.assertEqual(draft.items[0].against_sales_order, sales_order.name)
			self.assertNotEqual(draft.items[0].warehouse, self.warehouse)
			noon_return.reload()
			self.assertEqual(noon_return.inspection_outcome, outcome)
			self.assertEqual(noon_return.sales_return, draft.name)
			self.assertEqual(get_sellable_qty(self.item.name, self.warehouse, "Nos"), before)

	def test_account_refuses_an_invalid_damaged_warehouse(self):
		def refused(warehouse):
			account = frappe.get_doc("Marketplace Account", self.account.name)
			account.noon_damaged_warehouse = warehouse
			with patch("marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"):
				with self.assertRaises(frappe.ValidationError):
					account.save()

		refused(self.warehouse)  # same as the account warehouse
		group = frappe.db.get_value("Warehouse", {"company": self.company, "is_group": 1}, "name")
		if group:
			refused(group)
		other = frappe.db.get_value(
			"Warehouse", {"company": ["!=", self.company], "is_group": 0, "disabled": 0}, "name"
		)
		if other:
			refused(other)

	def test_damaged_outcomes_are_refused_without_a_damaged_warehouse(self):
		order, _sales_order, _delivery_note = self.make_delivered_order(qty=2)
		frappe.db.set_value("Marketplace Account", self.account.name, "noon_damaged_warehouse", None)
		returns_before = frappe.db.count("Delivery Note", {"is_return": 1})
		for outcome in ("Damaged", "Damaged, Noon paid"):
			noon_return = self.make_return(order)
			with self.assertRaises(frappe.ValidationError) as raised:
				noon_return.record_inspection(outcome)
			self.assertIn("damaged warehouse", str(raised.exception))
			self.assertFalse(frappe.db.get_value("Noon Return", noon_return.name, "inspection_outcome"))
		self.assertEqual(frappe.db.count("Delivery Note", {"is_return": 1}), returns_before)

	def test_damaged_needs_a_submitted_sales_order(self):
		noon_return = self.make_return(None)
		with self.assertRaises(frappe.ValidationError):
			noon_return.record_inspection("Damaged, Noon paid")

	def test_recorded_outcome_cannot_be_changed(self):
		order, _sales_order, _delivery_note = self.make_delivered_order(qty=2)
		noon_return = self.make_return(order)
		first = noon_return.record_inspection("Damaged")
		self.track("Delivery Note", first["sales_return"])
		for outcome in ("Sellable", "Damaged, Noon paid"):
			with self.assertRaises(frappe.ValidationError):
				noon_return.record_inspection(outcome)

	def test_refusals_create_nothing(self):
		order, _sales_order, _delivery_note = self.make_delivered_order(qty=2)
		returns_before = frappe.db.count("Delivery Note", {"is_return": 1})

		unacknowledged = self.make_return(order, acknowledged=False)
		with self.assertRaises(frappe.ValidationError):
			unacknowledged.record_inspection("Sellable")

		unlinked = self.make_return(None)
		with self.assertRaises(frappe.ValidationError):
			unlinked.record_inspection("Sellable")

		unmapped = self.make_return(order, partner_sku="SKU-NOT-MAPPED")
		with self.assertRaises(frappe.ValidationError):
			unmapped.record_inspection("Sellable")

		with self.assertRaises(frappe.ValidationError):
			unlinked.record_inspection("Broken")

		self.assertEqual(frappe.db.count("Delivery Note", {"is_return": 1}), returns_before)
		self.assertFalse(frappe.db.get_value("Noon Return", unmapped.name, "inspection_outcome"))

	def test_several_candidates_refuse_naming_the_delivery_notes(self):
		order, sales_order, delivery_note = self.make_delivered_order(qty=3, deliver_qty=1)
		second = make_delivery_note(sales_order.name)
		second.insert()
		second.submit()
		self.track("Delivery Note", second.name)
		noon_return = self.make_return(order)

		with self.assertRaises(frappe.ValidationError) as raised:
			noon_return.record_inspection("Sellable")

		message = str(raised.exception)
		self.assertIn(delivery_note.name, message)
		self.assertIn(second.name, message)
		self.assertFalse(frappe.db.get_value("Noon Return", noon_return.name, "inspection_outcome"))

	def test_no_submitted_delivery_note_refuses(self):
		order, _sales_order, delivery_note = self.make_delivered_order(qty=2)
		noon_return = self.make_return(order)
		delivery_note.cancel()

		with self.assertRaises(frappe.ValidationError) as raised:
			noon_return.record_inspection("Sellable")

		self.assertIn("exactly one", str(raised.exception))

	def test_inspection_requires_write_permission_and_creates_no_noon_call(self):
		order, _sales_order, _delivery_note = self.make_delivered_order(qty=2)
		noon_return = self.make_return(order)
		email, _ = make_role_user(self, "Noon Return Reader", {"Noon Return": ["read"]})

		original_user = frappe.session.user
		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				noon_return.record_inspection("Damaged")
		finally:
			frappe.set_user(original_user)

		with patch("marketplace_integrations.integrations.noon.client.NoonClient.request") as request:
			result = noon_return.record_inspection("Damaged")
		self.track("Delivery Note", result["sales_return"])
		request.assert_not_called()

	def make_invoice(self, sales_order, update_stock=0):
		invoice = make_sales_invoice(sales_order.name)
		invoice.update_stock = update_stock
		invoice.insert()
		invoice.submit()
		self.track("Sales Invoice", invoice.name)
		return invoice

	def inspected_return(self, order, outcome="Damaged"):
		noon_return = self.make_return(order)
		result = noon_return.record_inspection(outcome)
		if result.get("sales_return"):
			self.track("Delivery Note", result["sales_return"])
		noon_return.reload()
		return noon_return

	def test_credit_note_creates_one_draft_return_row_for_one_unit(self):
		order, sales_order, _delivery_note = self.make_delivered_order(qty=2)
		invoice = self.make_invoice(sales_order)
		noon_return = self.inspected_return(order)

		result = noon_return.create_credit_note()

		draft = frappe.get_doc("Sales Invoice", result["credit_note"])
		self.track("Sales Invoice", draft.name)
		self.assertEqual(draft.docstatus, 0)
		self.assertEqual(draft.is_return, 1)
		self.assertEqual(draft.return_against, invoice.name)
		self.assertEqual(len(draft.items), 1)
		self.assertEqual(draft.items[0].qty, -1)
		self.assertEqual(draft.items[0].item_code, self.item.name)
		self.assertEqual(draft.items[0].sales_order, sales_order.name)
		self.assertEqual(frappe.db.count("Payment Entry Reference", {"reference_name": draft.name}), 0)
		noon_return.reload()
		self.assertEqual(noon_return.credit_note, draft.name)

	def test_credit_note_needs_an_inspection_outcome_first(self):
		order, sales_order, _delivery_note = self.make_delivered_order(qty=2)
		self.make_invoice(sales_order)
		noon_return = self.make_return(order)
		with self.assertRaises(frappe.ValidationError):
			noon_return.create_credit_note()

	def test_credit_note_refuses_without_an_invoice_or_with_an_update_stock_invoice(self):
		order, sales_order, _delivery_note = self.make_delivered_order(qty=2)
		noon_return = self.inspected_return(order)
		credit_notes_before = frappe.db.count("Sales Invoice", {"is_return": 1})

		with self.assertRaises(frappe.ValidationError) as raised:
			noon_return.create_credit_note()
		self.assertIn("exactly one", str(raised.exception))

		# An invoice that already updates stock would return the stock a second time. The
		# delivery above already consumed the Sales Order quantity, so this one is built
		# by editing the flag on a normal submitted invoice.
		invoice = self.make_invoice(sales_order)
		frappe.db.set_value("Sales Invoice", invoice.name, "update_stock", 1)
		with self.assertRaises(frappe.ValidationError) as raised:
			noon_return.create_credit_note()
		self.assertIn("updates stock", str(raised.exception))
		self.assertEqual(frappe.db.count("Sales Invoice", {"is_return": 1}), credit_notes_before)

	def test_credit_note_second_call_is_idempotent_and_a_cancelled_one_is_replaced(self):
		order, sales_order, _delivery_note = self.make_delivered_order(qty=2)
		self.make_invoice(sales_order)
		noon_return = self.inspected_return(order)
		first = noon_return.create_credit_note()
		self.track("Sales Invoice", first["credit_note"])

		again = noon_return.create_credit_note()
		self.assertEqual(again["credit_note"], first["credit_note"])
		self.assertTrue(again["already_created"])

		draft = frappe.get_doc("Sales Invoice", first["credit_note"])
		draft.submit()
		draft.cancel()
		noon_return.reload()
		replaced = noon_return.create_credit_note()
		self.track("Sales Invoice", replaced["credit_note"])
		self.assertNotEqual(replaced["credit_note"], first["credit_note"])

	def test_credit_note_requires_invoice_create_permission_and_makes_no_noon_call(self):
		order, sales_order, _delivery_note = self.make_delivered_order(qty=2)
		self.make_invoice(sales_order)
		noon_return = self.inspected_return(order)
		email, _ = make_role_user(
			self, "Noon Return Editor", {"Noon Return": ["read", "write"], "Sales Invoice": ["read"]}
		)
		original_user = frappe.session.user
		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				noon_return.create_credit_note()
		finally:
			frappe.set_user(original_user)

		with patch("marketplace_integrations.integrations.noon.client.NoonClient.request") as request:
			result = noon_return.create_credit_note()
		self.track("Sales Invoice", result["credit_note"])
		request.assert_not_called()

	def test_return_evidence_clears_only_after_submitted_documents(self):
		from marketplace_integrations.integrations.noon.order_reconciliation import _has_noon_return

		order, sales_order, _delivery_note = self.make_delivered_order(qty=2)
		self.make_invoice(sales_order)
		noon_return = self.inspected_return(order, "Sellable")
		self.assertTrue(_has_noon_return(order))

		credit = frappe.get_doc("Sales Invoice", noon_return.create_credit_note()["credit_note"])
		self.track("Sales Invoice", credit.name)
		self.assertTrue(_has_noon_return(order))  # drafts do not count

		credit.submit()
		self.assertTrue(_has_noon_return(order))  # Sellable still needs its Sales Return

		frappe.get_doc("Delivery Note", noon_return.sales_return).submit()
		self.assertFalse(_has_noon_return(order))

	def test_damaged_outcomes_clear_only_after_credit_note_and_sales_return_are_submitted(self):
		from marketplace_integrations.integrations.noon.order_reconciliation import _has_noon_return

		order, sales_order, _delivery_note = self.make_delivered_order(qty=2)
		self.make_invoice(sales_order)
		noon_return = self.inspected_return(order, "Damaged, Noon paid")
		self.assertTrue(_has_noon_return(order))

		credit = frappe.get_doc("Sales Invoice", noon_return.create_credit_note()["credit_note"])
		self.track("Sales Invoice", credit.name)
		credit.submit()
		self.assertTrue(_has_noon_return(order))  # the Sales Return is still a draft

		frappe.get_doc("Delivery Note", noon_return.sales_return).submit()
		self.assertFalse(_has_noon_return(order))

	def apply_rows(self, *pairs):
		from marketplace_integrations.integrations.noon.returns import apply_outcomes

		results = apply_outcomes([{"noon_return": name, "outcome": outcome} for name, outcome in pairs])
		for result in results:
			if result["sales_return"]:
				self.track("Delivery Note", result["sales_return"])
		return results

	def test_apply_outcomes_acknowledges_and_records_each_outcome(self):
		order, _sales_order, _delivery_note = self.make_delivered_order(qty=5)
		restock = self.make_return(order, acknowledged=False)
		damaged = self.make_return(order, acknowledged=False)
		paid = self.make_return(order, acknowledged=False)

		with patch("marketplace_integrations.integrations.noon.client.NoonClient.request") as request:
			results = self.apply_rows(
				(restock.name, "Sellable"), (damaged.name, "Damaged"), (paid.name, "Damaged, Noon paid")
			)
		request.assert_not_called()

		self.assertTrue(all(result["ok"] for result in results))
		expected = {
			restock.name: ("Sellable", self.warehouse),
			damaged.name: ("Damaged", self.damaged_warehouse.name),
			paid.name: ("Damaged, Noon paid", self.damaged_warehouse.name),
		}
		for name, (outcome, warehouse) in expected.items():
			row = frappe.get_doc("Noon Return", name)
			self.assertEqual(row.inspection_outcome, outcome)
			self.assertEqual(row.receipt_acknowledgement_note, "Received through New Return")
			self.assertTrue(row.receipt_acknowledged_on)
			draft = frappe.get_doc("Delivery Note", row.sales_return)
			self.assertEqual(draft.docstatus, 0)
			self.assertEqual(draft.items[0].qty, -1)
			self.assertEqual(draft.items[0].warehouse, warehouse)

	def test_apply_outcomes_refused_row_rolls_back_alone(self):
		order, _sales_order, _delivery_note = self.make_delivered_order(qty=5)
		unlinked = self.make_return(None, acknowledged=False)
		recorded = self.make_return(order)
		first = recorded.record_inspection("Damaged")
		self.track("Delivery Note", first["sales_return"])
		good = self.make_return(order, acknowledged=False)
		bad_outcome = self.make_return(order, acknowledged=False)

		results = self.apply_rows(
			(unlinked.name, "Sellable"),
			(recorded.name, "Sellable"),
			(good.name, "Sellable"),
			(bad_outcome.name, "Broken"),
			("NO-SUCH-RETURN", "Sellable"),
		)

		self.assertEqual([result["ok"] for result in results], [False, False, True, False, False])
		for result in (results[0], results[1], results[3], results[4]):
			self.assertTrue(result["message"])
		self.assertTrue(results[2]["sales_return"])
		# A refused row keeps no receipt acknowledgement either.
		for row in (unlinked, bad_outcome):
			values = frappe.db.get_value(
				"Noon Return", row.name, ["receipt_acknowledged_on", "inspection_outcome"], as_dict=True
			)
			self.assertFalse(values.receipt_acknowledged_on)
			self.assertFalse(values.inspection_outcome)
		self.assertEqual(frappe.db.get_value("Noon Return", recorded.name, "inspection_outcome"), "Damaged")

	def test_apply_outcomes_refuses_damaged_without_a_damaged_warehouse_per_row(self):
		order, _sales_order, _delivery_note = self.make_delivered_order(qty=5)
		frappe.db.set_value("Marketplace Account", self.account.name, "noon_damaged_warehouse", None)
		damaged = self.make_return(order, acknowledged=False)
		restock = self.make_return(order, acknowledged=False)

		results = self.apply_rows((damaged.name, "Damaged"), (restock.name, "Sellable"))

		self.assertEqual([result["ok"] for result in results], [False, True])
		self.assertFalse(frappe.db.get_value("Noon Return", damaged.name, "receipt_acknowledged_on"))

	def test_apply_outcomes_message_is_escaped(self):
		results = self.apply_rows(("<img src=x onerror=1>", "Sellable"))
		self.assertNotIn("<img", results[0]["message"])

	def test_apply_outcomes_accepts_at_most_200_rows(self):
		from marketplace_integrations.integrations.noon.returns import apply_outcomes

		with self.assertRaises(frappe.ValidationError):
			apply_outcomes([{"noon_return": "X", "outcome": "Sellable"}] * 201)

	def test_apply_outcomes_permissions_are_checked_per_row(self):
		from marketplace_integrations.api.noon_returns import apply_outcomes

		order, _sales_order, _delivery_note = self.make_delivered_order(qty=5)
		reader_row = self.make_return(order, acknowledged=False)
		reader, _ = make_role_user(self, "Noon Return Reader Only", {"Noon Return": ["read"]})
		with self.set_user(reader):
			results = apply_outcomes([{"noon_return": reader_row.name, "outcome": "Sellable"}])
		self.assertFalse(results[0]["ok"])
		self.assertFalse(frappe.db.get_value("Noon Return", reader_row.name, "receipt_acknowledged_on"))

		# Noon Return and Marketplace Account writers, but no Delivery Note create: Restock is refused.
		writer, _ = make_role_user(
			self,
			"Noon Return Writer",
			{
				"Noon Return": ["read", "write"],
				"Marketplace Account": ["read", "write"],
				"Delivery Note": ["read"],
			},
		)
		no_create = self.make_return(order, acknowledged=False)
		with self.set_user(writer):
			results = apply_outcomes([{"noon_return": no_create.name, "outcome": "Sellable"}])
		self.assertFalse(results[0]["ok"])
		self.assertFalse(frappe.db.get_value("Noon Return", no_create.name, "inspection_outcome"))

		# With Delivery Note create the same user's row applies.
		creator, _ = make_role_user(
			self,
			"Noon Return Full Writer",
			{
				"Noon Return": ["read", "write"],
				"Marketplace Account": ["read", "write"],
				"Delivery Note": ["read", "create"],
				"Customer": ["read"],
				"Item": ["read"],
				"Account": ["read"],
			},
		)
		allowed = self.make_return(order, acknowledged=False)
		with self.set_user(creator):
			results = apply_outcomes([{"noon_return": allowed.name, "outcome": "Sellable"}])
		self.assertTrue(results[0]["ok"], results[0]["message"])
		self.track("Delivery Note", results[0]["sales_return"])
