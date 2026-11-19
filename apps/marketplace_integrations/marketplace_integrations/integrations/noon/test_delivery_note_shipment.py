# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The Noon adapter of the neutral carrier contract (#415): create on Submit through the neutral job,
GetShipment before every CreateShipment, the AWB step (one call, reused on retry), unit picking, the
duplicate AWB refusal, the locked fields and the cancel. Noon HTTP is mocked, never live."""

import json
from unittest.mock import MagicMock, patch

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

from marketplace_integrations.integrations.noon import delivery_note_shipment as noon
from marketplace_integrations.integrations.noon import fulfillment
from marketplace_integrations.integrations.noon.test_fulfillment import (
	IntegrationTestNoonFulfillmentBase,
	_confirmed_acknowledged,
)
from marketplace_integrations.patches.v0_0 import create_noon_transporter_supplier
from marketplace_integrations.services import carrier_log, carrier_shipments
from marketplace_integrations.services import delivery_note_workflow as lifecycle
from marketplace_integrations.tests.handover_fixture import SubmittedOrderFixture

_CLIENT = "marketplace_integrations.integrations.noon.fulfillment.NoonClient"
_GET_ORDER = "marketplace_integrations.integrations.noon.fulfillment.get_fbpi_order"
_CANCELLED = "MP_ITEM_STATUS_CANCELLED"


def _response(status=200, body=None, message=None):
	response = MagicMock(status_code=status)
	response.json.return_value = body if body is not None else {}
	response.message = message
	return response


def _not_found():
	return _response(400, {"error": {"code": "INVALID_ARGUMENT", "message": "No shipment found with number"}})


def _awb(awb="NOON-AWB-1"):
	return _response(200, {"awb_nrs": [awb]})


def _paths(client):
	return [call.args[1].rsplit("/", 1)[-1] for call in client.request.call_args_list]


class IntegrationTestNoonDeliveryNoteShipment(IntegrationTestNoonFulfillmentBase):
	def setUp(self):
		super().setUp()
		self.fixtures = []
		self._stock_items = []
		self._invoices = []
		self._bundles = []
		self._extra_items = []
		self._fbpi_ready_account()
		self.created_supplier = False
		if not noon.transporter_name():
			create_noon_transporter_supplier.execute()
			self.created_supplier = True
			self.created_supplier_name = noon.transporter_name()

	def tearDown(self):
		frappe.set_user("Administrator")
		vouchers = []
		self._drop_invoices()
		for fixture in self.fixtures:
			# the neutral cancel/delete guard refuses a note with a live shipment: close them first
			for name in fixture.delivery_notes():
				vouchers.append(name)
				frappe.db.set_value("Delivery Note", name, "custom_integration_status", "Cancelled", update_modified=False)
				# a held note (#527) refuses the native cancel: the fixture cleanup must be able to cancel it
				if frappe.db.get_value("Delivery Note", name, "custom_shipping_status") == noon.SHIPPING_AWAITING_RETURN:
					frappe.db.set_value("Delivery Note", name, "custom_shipping_status", "Cancelled", update_modified=False)
				if self._stock_items and frappe.db.get_value("Delivery Note", name, "docstatus") == 1:
					# cancel while the stock rows exist; they are purged BEFORE the fixture deletes its Items
					note = frappe.get_doc("Delivery Note", name)
					note.flags.ignore_permissions = True
					note.cancel()
		frappe.db.commit()
		if self._bundles:
			# a Product Bundle is linked to the Sales Orders / notes that sold it: remove those first
			for fixture in self.fixtures:
				fixture._step(fixture._remove_delivery_notes)
				fixture._step(fixture._remove_sales_order)
			frappe.db.commit()
		for bundle in self._bundles:
			frappe.delete_doc("Product Bundle", bundle, force=True, ignore_permissions=True)
		self._purge_stock(vouchers)
		for fixture in self.fixtures:
			fixture.cleanup()
		for item in self._extra_items:
			frappe.delete_doc("Item", item, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()
		if self.created_supplier:
			frappe.delete_doc("Supplier", self.created_supplier_name, force=True, ignore_permissions=True)
			frappe.db.commit()

	# ---- helpers -------------------------------------------------------------------------

	def _fbpi_ready_account(self, **overrides):
		"""The account as after Check Noon Warehouse (#416)."""
		values = {
			"noon_fulfillment_system_code": "fbpi",
			"noon_warehouse_active": 1,
			"noon_warehouse_checked_on": frappe.utils.now_datetime(),
		}
		values.update(overrides)
		frappe.db.set_value("Marketplace Account", self.account.name, values, update_modified=False)
		frappe.db.commit()

	def _order(self, rows, payload_skus):
		"""A submitted Noon Sales Order of ``rows`` ([(sku, qty)]) whose Noon payload holds one CONFIRMED +
		ACKNOWLEDGED unit per entry of ``payload_skus`` (mp_item_nr 1, 2, ...)."""
		fixture = SubmittedOrderFixture(self.account, rows)
		self.fixtures.append(fixture)
		items = [_confirmed_acknowledged(index, sku) for index, sku in enumerate(payload_skus, start=1)]
		frappe.db.set_value(
			"Marketplace Order",
			fixture.marketplace_order.name,
			"latest_payload",
			frappe.as_json({"fbpi_order_nr": fixture.marketplace_order.external_order_id, "items": items}),
		)
		frappe.db.commit()
		return fixture

	def _draft_note(self, fixture, lr_no=None, generate_awb=0):
		"""Save a draft Delivery Note from the Sales Order the way Create > Delivery Note does (it stays a draft; tests
		that need a submitted note flip the docstatus flags with ``_submit_flags``)."""
		note = make_delivery_note(fixture.name)
		note.lr_no = lr_no
		note.custom_noon_generate_awb = generate_awb
		note.insert()
		frappe.db.commit()
		# Deleting the newest note gives its name back to the series, so a later test (or run) gets the same
		# name: the name-keyed cache flags (invoice upload running, refresh again) and the Integration Request
		# log rows (a "Cancel / Success" row hides a Carrier Cancelled item) must not carry over.
		logs = {"reference_doctype": "Delivery Note", "reference_docname": note.name}
		frappe.db.delete("Integration Request", logs)
		self.addCleanup(frappe.db.delete, "Integration Request", logs)
		for flag in (noon._INVOICE_INFLIGHT, noon._REFRESH_AGAIN):
			self.addCleanup(frappe.cache().delete_value, flag.format(note.name))
		return note

	def _note(self, name):
		return frappe.get_doc("Delivery Note", name)

	def _found(self, fixture, awbs=None, units=("1",), order_nr=None):
		"""A GetShipment 200 for ``fixture``'s order (``fbpi_order_nr`` is part of the answer)."""
		return _response(
			200,
			{
				"fbpi_order_nr": order_nr or fixture.marketplace_order.external_order_id,
				"awbs": awbs or [],
				"items": [{"mp_item_nr": unit} for unit in units],
			},
		)

	def _creating(self, note):
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Creating", "custom_carrier_reference": note.name},
			update_modified=False,
		)
		frappe.db.commit()

	def _run(self, note, *responses, lookup_first=True):
		with patch(_CLIENT) as client_class:
			client = client_class.return_value
			client.request.side_effect = list(responses)
			client.error_message.side_effect = lambda response: response.message
			carrier_shipments.run_create(note.name, log=None, lookup_first=lookup_first)
		return client

	def _created_note(self, fixture, units=("1",)):
		"""A draft note whose shipment is Created with the given units in its snapshot."""
		note = self._draft_note(fixture, lr_no=f"AWB-{frappe.generate_hash(length=6)}")
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{
				"custom_integration_status": "Created",
				"custom_carrier_reference": note.name,
				"custom_carrier_request_snapshot": json.dumps({"mp_item_nrs": list(units)}),
			},
			update_modified=False,
		)
		frappe.db.commit()
		return note

	def _cancel(self, note, *responses, order_items=None):
		items = order_items if order_items is not None else [{"mp_item_nr": "1", "mp_status": "MP_ITEM_STATUS_CONFIRMED"}]
		with patch(_CLIENT) as client_class, patch(_GET_ORDER, return_value={"items": items}) as get_order:
			client = client_class.return_value
			client.request.side_effect = list(responses)
			client.error_message.side_effect = lambda response: response.message
			result = carrier_shipments.cancel(note.name)
		return result, client, get_order

	# ---- real stock (Confirm Returned, #527) ---------------------------------------------------

	def _purge_stock(self, vouchers=()):
		"""Posted stock rows cannot be removed through the ORM: delete what these tests posted (test site only).
		``vouchers`` are the Delivery Notes of this test: only their GL rows (and the Stock Entries') go."""
		if not self._stock_items:
			return
		entries = frappe.get_all("Stock Entry Detail", filters={"item_code": ["in", self._stock_items]}, pluck="parent")
		for entry in set(entries):
			for doctype in ("GL Entry", "Stock Ledger Entry"):
				frappe.db.delete(doctype, {"voucher_no": entry})
			frappe.db.delete("Stock Entry Detail", {"parent": entry})
			frappe.db.delete("Stock Entry", {"name": entry})
			frappe.db.delete("Version", {"ref_doctype": "Stock Entry", "docname": entry})
		for voucher in vouchers:
			frappe.db.delete("GL Entry", {"voucher_type": "Delivery Note", "voucher_no": voucher})
		for item in self._stock_items:
			frappe.db.delete("Stock Ledger Entry", {"item_code": item})
			frappe.db.delete("Repost Item Valuation", {"item_code": item})
			frappe.db.delete("Bin", {"item_code": item})
		frappe.db.commit()

	def _drop_invoices(self):
		"""Cancel and delete the Sales Invoices a test made (they block cancelling and deleting the note)."""
		for name in self._invoices:
			if not frappe.db.exists("Sales Invoice", name):
				continue
			if frappe.db.get_value("Sales Invoice", name, "docstatus") == 1:
				frappe.get_doc("Sales Invoice", name).cancel()
			frappe.delete_doc("Sales Invoice", name, force=True, ignore_permissions=True, delete_permanently=True)
			for doctype in ("GL Entry", "Payment Ledger Entry"):
				frappe.db.delete(doctype, {"voucher_no": name})
			frappe.db.delete("Version", {"ref_doctype": "Sales Invoice", "docname": name})
			frappe.db.delete("Comment", {"reference_doctype": "Sales Invoice", "reference_name": name})
		frappe.db.commit()

	def _held_note(self, qty=3, stock=10, stage=lifecycle.PICKED_UP, sku="SKU-A"):
		"""A REALLY submitted Noon note of ``qty`` units of a stock Item (``stock`` received), at ``stage``, that Noon
		then cancelled: held (Cancelled + Awaiting Return). Returns ``(fixture, note_name, item, warehouse)``."""
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		fx = self._order([(sku, qty)], [sku] * qty)  # the mapping (account + SKU) is unique: a second note needs its own SKU
		item = fx.items[sku].name
		frappe.db.set_value("Item", item, "is_stock_item", 1)
		self._stock_items.append(item)
		warehouse = fx.row_for(sku).warehouse
		make_stock_entry(item_code=item, qty=stock, to_warehouse=warehouse, company=fx.sales_order.company, rate=10)
		note = self._created_note(fx, units=tuple(str(n) for n in range(1, qty + 1)))
		doc = self._note(note.name)
		doc.submit()
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{
				"custom_integration_status": "Cancelled",
				"custom_shipping_status": noon.SHIPPING_AWAITING_RETURN,
				lifecycle.FIELD: lifecycle.RETURNED_FAILED,
			},
			update_modified=False,
		)
		frappe.db.commit()
		return fx, note.name, item, warehouse

	@staticmethod
	def _bin_qty(item, warehouse):
		return frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty") or 0

	def _lines(self, note, returned=None):
		"""The dialog's lines for ``note`` with ``returned`` ([qty per line]) or every unit back."""
		lines = noon.get_return_lines(note)
		return [
			{"name": line["name"], "returned_qty": line["shipped_qty"] if returned is None else returned[index]}
			for index, line in enumerate(lines)
		]

	def test_confirm_returned_with_everything_back_cancels_the_note_and_puts_the_stock_back(self):
		_fx, note, item, warehouse = self._held_note(qty=3, stock=10)
		self.assertEqual(self._bin_qty(item, warehouse), 7)
		lines = noon.get_return_lines(note)
		self.assertEqual([(row["item_code"], row["shipped_qty"]) for row in lines], [(item, 3)])
		with patch("frappe.enqueue") as enqueue:
			result = noon.confirm_returned(note, self._lines(note), 0)
		self.assertEqual(result, {"stock_entry": None})
		self.assertEqual(frappe.db.get_value("Delivery Note", note, "docstatus"), 2)
		self.assertEqual(self._bin_qty(item, warehouse), 10)
		# the Sales Order cancel is queued once, always as the system
		jobs = [c for c in enqueue.call_args_list if "run_noon_order_cancel" in str(c)]
		self.assertEqual(len(jobs), 1)
		self.assertEqual(jobs[0].kwargs["requested_by"], "Administrator")
		# a second confirm is refused and nothing else changes
		with self.assertRaises(frappe.ValidationError):
			noon.confirm_returned(note, self._lines(note), 0)

	def test_confirm_returned_after_our_own_cancel_does_not_queue_the_sales_order_cancel(self):
		# #552: only a cancel Noon started may cancel the order; a person's Cancel (or the cascade) never does.
		_fx, note, _item, _warehouse = self._held_note(qty=2, stock=10)
		frappe.db.set_value(
			"Delivery Note",
			note,
			"custom_carrier_request_snapshot",
			json.dumps({"mp_item_nrs": ["1", "2"], noon.CANCELLED_BY_KEY: noon.CANCELLED_BY_US}),
			update_modified=False,
		)
		frappe.db.commit()
		with patch("frappe.enqueue") as enqueue:
			noon.confirm_returned(note, self._lines(note), 0)
		self.assertEqual(frappe.db.get_value("Delivery Note", note, "docstatus"), 2)
		self.assertEqual([c for c in enqueue.call_args_list if "run_noon_order_cancel" in str(c)], [])

	def test_confirm_returned_with_a_write_off_posts_one_material_issue_on_the_company_accounts(self):
		fx, note, item, warehouse = self._held_note(qty=3, stock=10)
		company = fx.sales_order.company
		accounts = frappe.db.get_value("Company", company, ["stock_adjustment_account", "cost_center"], as_dict=True)
		if not accounts.stock_adjustment_account or not accounts.cost_center:
			self.skipTest("The test Company has no Stock Adjustment Account or default cost center.")
		with patch("frappe.enqueue"):
			result = noon.confirm_returned(note, self._lines(note, returned=[1]), 1)
		self.assertTrue(result["stock_entry"])
		self.assertEqual(frappe.db.get_value("Delivery Note", note, "docstatus"), 2)
		entry = frappe.get_doc("Stock Entry", result["stock_entry"])
		self.assertEqual((entry.docstatus, entry.purpose), (1, "Material Issue"))
		self.assertIn(note, entry.remarks)
		self.assertEqual(len(entry.items), 1)
		row = entry.items[0]
		self.assertEqual((row.item_code, row.s_warehouse, row.qty), (item, warehouse, 2))
		self.assertEqual(row.expense_account, accounts.stock_adjustment_account)
		self.assertEqual(row.cost_center, accounts.cost_center)
		# 10 received, 3 delivered then back, 2 written off
		self.assertEqual(self._bin_qty(item, warehouse), 8)
		comments = frappe.get_all(
			"Comment", filters={"reference_doctype": "Delivery Note", "reference_name": note, "comment_type": "Comment"}, pluck="content"
		)
		self.assertTrue(any(result["stock_entry"] in text for text in comments))

	def test_confirm_returned_refuses_bad_quantities_and_changes_nothing(self):
		_fx, note, item, warehouse = self._held_note(qty=3, stock=10)
		line = noon.get_return_lines(note)[0]["name"]
		cases = [
			([{"name": line, "returned_qty": -1}], 1),  # below 0
			([{"name": line, "returned_qty": 4}], 1),  # above shipped
			([{"name": line, "returned_qty": 2}], 0),  # a shortfall without the write-off
			([{"name": line, "returned_qty": None}], 1),  # a blank is not zero
			([{"name": "no-such-row", "returned_qty": 3}], 0),  # a line the note does not have
			([], 0),  # a missing line
			([{"name": line, "returned_qty": "nan"}], 1),  # not finite
			([{"name": line, "returned_qty": float("inf")}], 1),
			({"name": line, "returned_qty": 3}, 0),  # not a list
			("not json", 0),
		]
		for lines, write_off in cases:
			with self.subTest(lines=lines, write_off=write_off):
				with self.assertRaises(frappe.ValidationError):
					noon.confirm_returned(note, lines, write_off)
				self.assertEqual(frappe.db.get_value("Delivery Note", note, "docstatus"), 1)
				self.assertTrue(noon.is_held(self._note(note)))
				self.assertEqual(self._bin_qty(item, warehouse), 7)

	def test_confirm_returned_refuses_a_write_off_of_a_serial_or_batch_line_and_a_company_without_accounts(self):
		serial = frappe._dict(name="r1", item_code="I", qty=3, precision=3, serial_batch=True)
		plain = frappe._dict(name="r1", item_code="I", qty=3, precision=3, serial_batch=False)
		doc = frappe._dict(company="Some Company")
		with patch.object(noon, "_stock_lines", return_value=[serial]):
			with self.assertRaises(frappe.ValidationError):
				noon._check_returned(doc, [{"name": "r1", "returned_qty": 1}], True)
		no_accounts = frappe._dict(stock_adjustment_account=None, cost_center="CC")
		with patch.object(noon, "_stock_lines", return_value=[plain]), patch.object(frappe.db, "get_value", return_value=no_accounts):
			with self.assertRaises(frappe.ValidationError):
				noon._check_returned(doc, [{"name": "r1", "returned_qty": 1}], True)

	def test_a_failed_material_issue_rolls_the_note_cancel_back_and_the_note_stays_held(self):
		_fx, note, item, warehouse = self._held_note(qty=3, stock=10)
		with patch.object(noon, "_write_off", side_effect=frappe.ValidationError("<b>no account</b>")):
			with self.assertRaises(frappe.ValidationError) as caught:
				noon.confirm_returned(note, self._lines(note, returned=[1]), 1)
		self.assertIn("no account", str(caught.exception))
		self.assertNotIn("<b>", str(caught.exception))  # the reason is escaped
		self.assertEqual(frappe.db.get_value("Delivery Note", note, "docstatus"), 1)
		self.assertTrue(noon.is_held(self._note(note)))
		self.assertEqual(self._bin_qty(item, warehouse), 7)
		self.assertEqual(frappe.get_all("Stock Entry", filters={"remarks": ["like", f"%{note}%"]}), [])

	def test_a_failed_material_issue_also_rolls_back_the_cancel_of_the_unpaid_sales_invoice(self):
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

		_fx, note, item, warehouse = self._held_note(qty=3, stock=10)
		invoice = make_sales_invoice(note)
		invoice.insert()
		invoice.submit()
		frappe.db.commit()
		self._invoices.append(invoice.name)
		with patch.object(noon, "_write_off", side_effect=frappe.ValidationError("boom")):
			with self.assertRaises(frappe.ValidationError):
				noon.confirm_returned(note, self._lines(note, returned=[1]), 1)
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Delivery Note", note, "docstatus"), 1)
		self.assertTrue(noon.is_held(self._note(note)))
		self.assertEqual(self._bin_qty(item, warehouse), 7)
		# and a good confirm cancels the unpaid invoice with the note
		with patch("frappe.enqueue"):
			noon.confirm_returned(note, self._lines(note), 0)
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Delivery Note", note, "docstatus"), 2)

	def test_confirm_returned_on_a_product_bundle_note_works_on_its_packed_items(self):
		accounts = frappe.db.get_value(
			"Company", frappe.defaults.get_global_default("company"), ["stock_adjustment_account", "cost_center"], as_dict=True
		)
		if not accounts.stock_adjustment_account or not accounts.cost_center:
			self.skipTest("The test Company has no Stock Adjustment Account or default cost center.")
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		fx = self._order([("SKU-A", 2)], ["SKU-A", "SKU-A"])
		parent = fx.items["SKU-A"].name
		part = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"TEST-BUNDLE-PART-{frappe.generate_hash(length=6)}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}
		).insert()
		self._extra_items.append(part.name)
		self._stock_items.append(part.name)
		bundle = frappe.get_doc(
			{"doctype": "Product Bundle", "new_item_code": parent, "items": [{"item_code": part.name, "qty": 2, "uom": "Nos"}]}
		).insert()
		self._bundles.append(bundle.name)
		warehouse = fx.row_for("SKU-A").warehouse
		make_stock_entry(item_code=part.name, qty=20, to_warehouse=warehouse, company=fx.sales_order.company, rate=10)
		note = self._created_note(fx, units=("1", "2"))
		self._note(note.name).submit()
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{
				"custom_integration_status": "Cancelled",
				"custom_shipping_status": noon.SHIPPING_AWAITING_RETURN,
				lifecycle.FIELD: lifecycle.RETURNED_FAILED,
			},
			update_modified=False,
		)
		frappe.db.commit()
		lines = noon.get_return_lines(note.name)
		# 2 bundles x 2 parts = 4 units of the part (the bundle item itself posts nothing)
		self.assertEqual([(row["item_code"], row["shipped_qty"]) for row in lines], [(part.name, 4)])
		self.assertEqual(self._bin_qty(part.name, warehouse), 16)
		with patch("frappe.enqueue"):
			result = noon.confirm_returned(note.name, self._lines(note.name, returned=[3]), 1)
		entry = frappe.get_doc("Stock Entry", result["stock_entry"])
		self.assertEqual([(row.item_code, row.qty, row.conversion_factor) for row in entry.items], [(part.name, 1, 1)])
		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "docstatus"), 2)
		self.assertEqual(self._bin_qty(part.name, warehouse), 19)

	def test_a_noon_user_with_only_read_confirms_returned_through_the_endpoint_with_and_without_a_write_off(self):
		from marketplace_integrations.api import noon_fulfillment
		from marketplace_integrations.tests.utils import make_role_user

		reader, _role = make_role_user(self, "Noon Return Confirmer", {"Delivery Note": ["read"]})
		frappe.get_doc("User", reader).add_roles("Noon User")
		frappe.clear_cache(user=reader)
		_fx, full, item, warehouse = self._held_note(qty=3, stock=10)
		_fx2, partial, item2, warehouse2 = self._held_note(qty=3, stock=10, sku="SKU-B")
		with self.set_user(reader), patch("frappe.enqueue"):
			self.assertFalse(frappe.has_permission("Delivery Note", "write", doc=full))
			rows = noon_fulfillment.get_return_lines(full)
			self.assertEqual(rows[0]["shipped_qty"], 3)
			result = noon_fulfillment.confirm_returned(full, frappe.as_json(self._lines(full)), 0)
			self.assertEqual(result, {"stock_entry": None})
			result = noon_fulfillment.confirm_returned(partial, frappe.as_json(self._lines(partial, returned=[1])), 1)
		self.assertTrue(result["stock_entry"])
		self.assertEqual(frappe.db.get_value("Delivery Note", full, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Delivery Note", partial, "docstatus"), 2)
		self.assertEqual(self._bin_qty(item, warehouse), 10)
		self.assertEqual(self._bin_qty(item2, warehouse2), 8)

	def test_the_sales_order_cancel_refuses_a_held_note_up_front_and_cancels_nothing_at_noon(self):
		from marketplace_integrations.services import order_cancel_cascade

		fx = self._order([("SKU-A", 2)], ["SKU-A", "SKU-A"])
		ready = self._created_note(fx, units=("1",))
		held = self._created_note(fx, units=("2",))
		for name in (ready.name, held.name):
			self._submit_flags(self._note(name))
		frappe.db.set_value(
			"Delivery Note",
			held.name,
			{
				"custom_shipping_status": noon.SHIPPING_AWAITING_RETURN,
				"custom_integration_status": "Cancelled",
				lifecycle.FIELD: lifecycle.RETURNED_FAILED,
			},
			update_modified=False,
		)
		frappe.db.set_value("Delivery Note", ready.name, lifecycle.FIELD, lifecycle.READY_FOR_PICKUP, update_modified=False)
		frappe.db.commit()
		with patch(_CLIENT) as client_class:
			with self.assertRaises(frappe.ValidationError) as caught:
				order_cancel_cascade._check_note(held.name, fx.name, True)
			self.assertIn("Confirm Returned", str(caught.exception))
			self.assertIn("Nothing was cancelled", str(caught.exception))
			# the ready note alone passes the check; nothing was sent to Noon either way
			order_cancel_cascade._check_note(ready.name, fx.name, True)
			client_class.return_value.request.assert_not_called()
		for name in (ready.name, held.name):
			self._unflag(self._note(name))

	def test_the_confirm_returned_endpoints_need_noon_user_or_system_manager_and_read_on_the_note(self):
		from marketplace_integrations.api import noon_fulfillment
		from marketplace_integrations.tests.utils import make_role_user

		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="TYPED-CR")
		reader, _role = make_role_user(self, "Noon Return Reader", {"Delivery Note": ["read"]})
		with (
			patch.object(noon, "confirm_returned", return_value={"stock_entry": None}) as confirm,
			patch.object(noon, "get_return_lines", return_value=[]) as lines,
		):
			with self.set_user(reader):
				for call in (
					lambda: noon_fulfillment.get_return_lines(note.name),
					lambda: noon_fulfillment.confirm_returned(note.name, "[]", 0),
				):
					with self.assertRaises(frappe.PermissionError):
						call()
			confirm.assert_not_called()
			lines.assert_not_called()
			# Noon User with Delivery Note read only (no write) passes both
			frappe.get_doc("User", reader).add_roles("Noon User")
			frappe.clear_cache(user=reader)
			with self.set_user(reader):
				self.assertFalse(frappe.has_permission("Delivery Note", "write", doc=note.name))
				self.assertEqual(noon_fulfillment.get_return_lines(note.name), [])
				self.assertEqual(noon_fulfillment.confirm_returned(note.name, "[]", 1), {"stock_entry": None})
			confirm.assert_called_once_with(note.name, "[]", 1)

	# ---- contract ------------------------------------------------------------------------

	def test_noon_is_a_registered_carrier_and_claims_its_notes(self):
		self.assertIs(carrier_shipments.CARRIERS["Noon"], noon)
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="TYPED-1")
		self.assertIs(carrier_shipments.adapter_for(note), noon)
		# the Needs Attention collector and the amend cleanup judge a dict by its transporter
		self.assertTrue(noon.applies_to({"is_return": 0, "transporter": noon.transporter_name()}))
		self.assertFalse(noon.applies_to({"is_return": 1, "transporter": noon.transporter_name()}))
		self.assertFalse(noon.applies_to({"is_return": 0, "transporter": None}))
		self.assertEqual(
			noon.problem_states(), {"Carrier Exception": [noon.SHIPPING_MIXED, noon.SHIPPING_CANCELLED]}
		)
		self.assertEqual(noon.terminal_states(), ["Cancelled"])

	def test_the_first_save_sets_the_carrier_and_not_created(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="TYPED-1")
		saved = self._note(note.name)
		self.assertEqual((saved.transporter, saved.transporter_name), (noon.transporter_name(), "Noon"))
		self.assertEqual(saved.custom_integration_status, "Not Created")

	# ---- request (the neutral entry) ------------------------------------------------------------

	def test_request_create_runs_one_deduplicated_job_that_looks_up_first(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="TYPED-1")
		with patch("frappe.enqueue") as enqueue, patch(_CLIENT) as client_class:
			result = carrier_shipments.request_create(note.name)
			again = carrier_shipments.request_create(note.name)
		enqueue.assert_called_once()
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["job_id"], f"noon-create|{note.name}")
		self.assertTrue(kwargs["deduplicate"])
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertTrue(kwargs["lookup_first"])  # even the first attempt asks Noon first
		self.assertEqual((result["status"], result["queued"]), ("Creating", True))
		self.assertFalse(again["queued"])
		client_class.return_value.request.assert_not_called()
		saved = self._note(note.name)
		self.assertEqual((saved.custom_integration_status, saved.custom_carrier_reference), ("Creating", note.name))

	def test_submit_is_refused_until_the_shipment_is_created(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="TYPED-1")
		with self.assertRaises(frappe.ValidationError):
			self._note(note.name).submit()

	def test_validate_ready_refuses_what_noon_cannot_take(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx)  # no AWB, checkbox off
		with self.assertRaises(frappe.ValidationError):
			noon.validate_ready(self._note(note.name))
		frappe.db.set_value("Delivery Note", note.name, "lr_no", "TYPED-1")
		noon.validate_ready(self._note(note.name))  # now ready (nothing is sent)
		frappe.db.set_value("Delivery Note Item", {"parent": note.name}, "qty", 1.5)
		with self.assertRaises(frappe.ValidationError):
			noon.validate_ready(self._note(note.name))

	def test_validate_ready_refuses_a_warehouse_that_is_not_fbpi_ready(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="TYPED-1")
		cases = (
			({"noon_fulfillment_system_code": "", "noon_warehouse_checked_on": None}, "never checked"),
			({"noon_fulfillment_system_code": "OTHER"}, "OTHER, not FBPI"),
			({"noon_warehouse_active": 0}, "not active"),
		)
		for overrides, reason in cases:
			self._fbpi_ready_account(**overrides)
			with self.assertRaisesRegex(frappe.ValidationError, reason):
				noon.validate_ready(self._note(note.name))
			with patch(_CLIENT) as client_class:
				with self.assertRaises(frappe.ValidationError):
					carrier_shipments.request_create(note.name)
			client_class.return_value.request.assert_not_called()
			self.assertEqual(self._note(note.name).custom_integration_status, "Not Created")
			self._fbpi_ready_account()
		noon.validate_ready(self._note(note.name))  # ready again

	# ---- create (the job) -----------------------------------------------------------------------

	def test_get_shipment_runs_before_create_and_a_typed_awb_goes_with_courier_noon(self):
		fx = self._order([("SKU-A", 2), ("SKU-B", 1)], ["SKU-A", "SKU-B", "SKU-A"])
		note = self._draft_note(fx, lr_no="STICKER-1")
		self._creating(note)
		client = self._run(note, _not_found(), _response(200))
		self.assertEqual(_paths(client), ["get", "create"])
		sent = client.request.call_args_list[1].kwargs["json"]
		self.assertEqual(sent["integration_shipment_nr"], note.name)
		self.assertEqual(sent["awbs"], [{"courier": "noon", "awb_nr": "STICKER-1"}])
		self.assertEqual(sorted(item["mp_item_nr"] for item in sent["items"]), ["1", "2", "3"])
		saved = self._note(note.name)
		self.assertEqual((saved.custom_integration_status, saved.lr_no, saved.docstatus), ("Created", "STICKER-1", 0))
		self.assertEqual(json.loads(saved.custom_carrier_request_snapshot), {"mp_item_nrs": ["1", "2", "3"]})
		self.assertEqual(saved.custom_shipping_status, "Acknowledged")
		self.assertTrue(saved.lr_date and saved.custom_last_sync_at)

	def test_a_shipment_found_at_noon_is_created_without_a_create_call(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="STICKER-1")
		self._creating(note)
		found = self._found(fx, [{"courier": "noon", "awb_nr": "STICKER-1"}])
		client = self._run(note, found)
		self.assertEqual(_paths(client), ["get"])
		self.assertEqual(self._note(note.name).custom_integration_status, "Created")

	def test_the_checkbox_asks_noon_for_one_awb_and_commits_it_before_create(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, generate_awb=1)
		self._creating(note)
		seen = {}

		def create_sees_stored_awb(*args, **kwargs):
			if args[1].endswith("/create"):
				seen["lr_no"] = frappe.db.get_value("Delivery Note", note.name, "lr_no")
				return _response(200)
			return {"get": _not_found(), "noon-logistics-awbs/get": _awb("NOON-AWB-1")}[
				args[1].split("/v1/shipment/")[-1]
			]

		with patch(_CLIENT) as client_class:
			client = client_class.return_value
			client.request.side_effect = create_sees_stored_awb
			carrier_shipments.run_create(note.name, log=None, lookup_first=True)
		self.assertEqual(_paths(client), ["get", "get", "create"])  # GetShipment, GetNoonLogisticsAWBs, CreateShipment
		self.assertEqual(client.request.call_args_list[1].kwargs["json"]["qty"], 1)
		self.assertEqual(seen["lr_no"], "NOON-AWB-1")
		self.assertEqual(self._note(note.name).lr_no, "NOON-AWB-1")
		self.assertEqual(
			frappe.db.count(
				"Integration Request",
				{"reference_docname": note.name, "custom_operation_type": "AWB", "custom_result": "Success"},
			),
			1,
		)

	def test_a_create_failure_after_the_awb_then_retry_requests_no_second_awb(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, generate_awb=1)
		self._creating(note)
		first = self._run(note, _not_found(), _awb("NOON-AWB-9"), _response(500, message="Noon down"))
		self.assertEqual(_paths(first), ["get", "get", "create"])
		saved = self._note(note.name)
		self.assertEqual((saved.custom_integration_status, saved.lr_no), ("Creation Unknown", "NOON-AWB-9"))

		self._creating(self._note(note.name))  # Submit again: the neutral request writes Creating
		second = self._run(note, _not_found(), _response(200))
		self.assertEqual(_paths(second), ["get", "create"])  # no AWB request
		self.assertEqual(second.request.call_args_list[1].kwargs["json"]["awbs"], [{"courier": "noon", "awb_nr": "NOON-AWB-9"}])
		self.assertEqual(self._note(note.name).custom_integration_status, "Created")

	def test_a_definite_refusal_fails_and_keeps_the_draft(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="STICKER-1")
		self._creating(note)
		self._run(note, _not_found(), _response(400, message="Unit not shippable"))
		saved = self._note(note.name)
		self.assertEqual((saved.custom_integration_status, saved.docstatus), ("Creation Failed", 0))
		self.assertIn("Unit not shippable", saved.custom_last_integration_error)

	def test_a_timeout_is_unknown_never_failed(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="STICKER-1")
		self._creating(note)
		self._run(note, _not_found(), TimeoutError("timed out"))
		self.assertEqual(self._note(note.name).custom_integration_status, "Creation Unknown")

	def test_an_unrecognised_get_shipment_answer_never_creates(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="STICKER-1")
		self._creating(note)
		client = self._run(note, _response(400, {"error": {"message": "Something else"}, }, message="Something else"))
		self.assertEqual(_paths(client), ["get"])
		self.assertEqual(self._note(note.name).custom_integration_status, "Creation Unknown")

	def test_a_missing_mapping_fails_without_an_awb_or_create_call(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, generate_awb=1)
		frappe.db.set_value("Marketplace Product Mapping", fx.mappings[0].name, "enabled", 0)
		frappe.db.commit()
		self._creating(note)
		client = self._run(note, _not_found())
		self.assertEqual(_paths(client), ["get"])
		saved = self._note(note.name)
		self.assertEqual(saved.custom_integration_status, "Creation Failed")
		self.assertIn("no enabled Noon product mapping", saved.custom_last_integration_error)

	def test_check_resolves_a_found_shipment_and_never_creates(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="STICKER-1")
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Creation Unknown", "custom_carrier_reference": note.name},
			update_modified=False,
		)
		frappe.db.commit()
		with patch(_CLIENT) as client_class:
			client = client_class.return_value
			client.request.side_effect = [_not_found()]
			result = carrier_shipments.check(note.name)
			self.assertFalse(result["found"])
			self.assertEqual(_paths(client), ["get"])
			client.request.side_effect = [self._found(fx)]
			self.assertTrue(carrier_shipments.check(note.name)["found"])
		self.assertEqual(self._note(note.name).custom_integration_status, "Created")

	# ---- units -----------------------------------------------------------------------------------

	def test_units_another_live_note_started_are_not_taken_again(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A", "SKU-A"])
		first = self._draft_note(fx, lr_no="AWB-ONE")
		frappe.db.set_value("Delivery Note Item", {"parent": first.name}, "qty", 1)
		frappe.db.set_value(
			"Delivery Note",
			first.name,
			{"custom_integration_status": "Created", "custom_carrier_request_snapshot": json.dumps({"mp_item_nrs": ["1"]})},
			update_modified=False,
		)
		second = self._draft_note(fx, lr_no="AWB-TWO")
		frappe.db.set_value("Delivery Note Item", {"parent": second.name}, "qty", 1)
		picked = noon._pick_units(self._note(second.name))
		self.assertEqual([unit["mp_item_nr"] for unit in picked], ["2"])
		# a cancelled shipment releases its unit
		frappe.db.set_value("Delivery Note", first.name, "custom_integration_status", "Cancelled", update_modified=False)
		picked = noon._pick_units(self._note(second.name))
		self.assertEqual([unit["mp_item_nr"] for unit in picked], ["1"])

	def test_too_few_shippable_units_is_refused(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="AWB-ONE")
		with self.assertRaises(frappe.ValidationError):
			noon.validate_ready(self._note(note.name))

	# ---- AWB rules ---------------------------------------------------------------------------------

	def test_an_awb_held_by_another_live_note_is_refused_ignoring_case(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		self._draft_note(fx, lr_no="Sticker-9")
		other = self._order([("SKU-B", 1)], ["SKU-B"])
		with self.assertRaises(frappe.ValidationError):
			self._draft_note(other, lr_no="STICKER-9")
		# a cancelled holder releases the AWB
		for name in fx.delivery_notes():
			frappe.db.set_value("Delivery Note", name, "docstatus", 2, update_modified=False)
		frappe.db.commit()
		self._draft_note(other, lr_no="STICKER-9")

	def test_ticked_checkbox_with_a_typed_awb_is_refused(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		with self.assertRaises(frappe.ValidationError):
			self._draft_note(fx, lr_no="TYPED-1", generate_awb=1)

	def test_a_generated_awb_and_the_started_shipment_are_locked(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, generate_awb=1)
		frappe.db.set_value("Delivery Note", note.name, "lr_no", "NOON-AWB-1", update_modified=False)
		frappe.db.commit()
		changed = self._note(note.name)
		changed.lr_no = "OTHER"
		with self.assertRaises(frappe.ValidationError):
			changed.save()
		unticked = self._note(note.name)
		unticked.custom_noon_generate_awb = 0
		with self.assertRaises(frappe.ValidationError):
			unticked.save()
		# once started, the carrier and the rows lock too
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Created", update_modified=False)
		frappe.db.commit()
		started = self._note(note.name)
		started.items[0].qty = 1  # same quantity: allowed
		started.save()
		started = self._note(note.name)
		started.items[0].qty = 2
		with self.assertRaises(frappe.ValidationError):
			started.save()

	# ---- cancel --------------------------------------------------------------------------------------

	def test_cancel_reads_the_order_first_and_a_cancelled_shipment_makes_no_cancel_call(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		result, client, get_order = self._cancel(note, order_items=[{"mp_item_nr": "1", "mp_status": _CANCELLED}])
		get_order.assert_called_once()
		client.request.assert_not_called()
		self.assertEqual((result["status"], result["outcome"]), ("Cancelled", "cancelled"))
		saved = self._note(note.name)
		self.assertEqual((saved.custom_integration_status, saved.custom_shipping_status), ("Cancelled", "Cancelled"))

	def test_cancel_sends_cancel_shipment_with_the_shipment_number(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		result, client, _get = self._cancel(note, _response(200))
		self.assertEqual(_paths(client), ["cancel"])
		self.assertEqual(client.request.call_args.kwargs["json"]["integration_shipment_nr"], note.name)
		self.assertEqual(result["outcome"], "cancelled")

	def test_a_noon_refusal_leaves_the_note_created_with_the_reason(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		result, _client, _get = self._cancel(note, _response(400, message="Already shipped"))
		self.assertEqual((result["status"], result["outcome"]), ("Created", "refused"))
		saved = self._note(note.name)
		self.assertEqual(saved.custom_integration_status, "Created")
		self.assertIn("Already shipped", saved.custom_last_integration_error)

	def test_an_unknown_cancel_result_is_cancellation_unknown_and_retry_asks_noon_first(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		result, _client, _get = self._cancel(note, _response(500, message="Noon down"))
		self.assertEqual((result["status"], result["outcome"]), ("Cancellation Unknown", "unknown"))
		retry, client, get_order = self._cancel(note, order_items=[{"mp_item_nr": "1", "mp_status": _CANCELLED}])
		get_order.assert_called_once()  # asked first again
		client.request.assert_not_called()
		self.assertEqual(retry["status"], "Cancelled")

	def test_cancelling_a_note_never_calls_an_order_level_path_including_the_auto_cancel(self):
		# Noon documents that CancelShipment also cancels the shipment's items; ERPNext only ever calls the
		# shipment cancel, never UpdateOrder or another order-level path (the Sales Order follows by sync).
		update_path = fulfillment.FBPI_ORDER_UPDATE_PATH.rsplit("/", 1)[-1]
		cancel_path = noon.SHIPMENT_CANCEL_PATH.rsplit("/", 1)[-1]
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		draft = self._created_note(fx)
		_result, client, _get = self._cancel(draft, _response(200))
		self.assertEqual(_paths(client), [cancel_path])
		self.assertNotIn(update_path, _paths(client))

		fx = self._order([("SKU-B", 1)], ["SKU-B"])
		submitted = self._created_note(fx)
		self._submit_flags(submitted)
		with (
			patch(_CLIENT) as client_class,
			patch(_GET_ORDER, return_value={"items": [{"mp_item_nr": "1", "mp_status": "MP_ITEM_STATUS_CONFIRMED"}]}),
			patch("frappe.model.document.Document.cancel"),
			patch.object(carrier_shipments, "_check_native_cancel_not_blocked"),
			patch.object(carrier_shipments, "_notify"),
		):
			client = client_class.return_value
			client.request.side_effect = [_response(200)]
			client.error_message.side_effect = lambda response: response.message
			carrier_shipments.cancel(submitted.name)
			carrier_shipments._auto_cancel(submitted.name)  # the job queued after a refresh found the cancel
		self.assertEqual(_paths(client), [cancel_path])
		frappe.db.set_value("Delivery Note", submitted.name, "docstatus", 0, update_modified=False)
		frappe.db.set_value("Delivery Note Item", {"parent": submitted.name}, "docstatus", 0, update_modified=False)
		frappe.db.commit()

	def test_after_a_cancel_shipment_200_the_same_units_are_shippable_again_by_a_new_note(self):
		# Assumes Noon keeps the units CONFIRMED + ACKNOWLEDGED after CancelShipment (UNVERIFIED: the docs say the
		# items are cancelled too). Documents what ERPNext does under that payload.
		fx = self._order([("SKU-A", 2)], ["SKU-A", "SKU-A"])
		first = self._draft_note(fx, lr_no="AWB-ONE")
		frappe.db.set_value("Delivery Note Item", {"parent": first.name}, "qty", 1)
		frappe.db.set_value(
			"Delivery Note",
			first.name,
			{
				"custom_integration_status": "Created",
				"custom_carrier_reference": first.name,
				"custom_carrier_request_snapshot": json.dumps({"mp_item_nrs": ["1"]}),
			},
			update_modified=False,
		)
		second = self._draft_note(fx, lr_no="AWB-TWO")
		frappe.db.set_value("Delivery Note Item", {"parent": second.name}, "qty", 1)
		frappe.db.commit()
		self.assertEqual([unit["mp_item_nr"] for unit in noon._pick_units(self._note(second.name))], ["2"])
		self.assertEqual(noon.started_units_of_order(fx.marketplace_order.name), {"1"})
		result, _client, _get = self._cancel(first, _response(200))
		self.assertEqual(result["status"], "Cancelled")
		self.assertEqual(noon.started_units_of_order(fx.marketplace_order.name), set())
		self.assertEqual([unit["mp_item_nr"] for unit in noon._pick_units(self._note(second.name))], ["1"])

	def test_a_failed_order_read_never_sends_a_blind_cancel(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		with patch(_CLIENT) as client_class, patch(_GET_ORDER, side_effect=frappe.ValidationError("down")):
			result = carrier_shipments.cancel(note.name)
		client_class.return_value.request.assert_not_called()
		self.assertEqual(result["outcome"], "unknown")

	def test_after_a_failed_local_cancel_the_next_cancel_makes_no_noon_call(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled", update_modified=False)
		frappe.db.set_value("Delivery Note", note.name, "docstatus", 1, update_modified=False)
		frappe.db.commit()
		with patch(_CLIENT) as client_class, patch(_GET_ORDER) as get_order:
			result = carrier_shipments.cancel(note.name)
		client_class.return_value.request.assert_not_called()
		get_order.assert_not_called()
		self.assertEqual(result["outcome"], "cancelled")
		frappe.db.set_value("Delivery Note", note.name, "docstatus", 0, update_modified=False)

	# ---- Sales Order cancel ---------------------------------------------------------------------------

	def test_a_sales_order_with_a_draft_note_holding_a_live_shipment_cannot_be_cancelled(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		order = frappe.get_doc("Sales Order", fx.name)
		with self.assertRaises(frappe.ValidationError):
			noon.refuse_sales_order_cancel(order)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled", update_modified=False)
		noon.refuse_sales_order_cancel(order)

	def test_a_draft_note_of_another_carrier_is_not_a_live_noon_shipment_to_bosta(self):
		from marketplace_integrations.integrations.bosta import delivery_note_shipment as bosta

		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		self._created_note(fx)
		bosta.refuse_sales_order_cancel(frappe.get_doc("Sales Order", fx.name))

	# ---- status: derivation, refresh, webhook, nightly -------------------------------------------------

	@staticmethod
	def _unit(nr, integration="SHIPPED", mp="MP_ITEM_STATUS_CONFIRMED", sku="SKU-A"):
		return {
			"mp_item_nr": nr,
			"partner_sku": sku,
			"mp_status": mp,
			"integration_status": f"INTEGRATION_ITEM_STATUS_{integration}",
		}

	def _derive(self, units, order_items, shipment_nrs=None):
		doc = frappe._dict(
			custom_carrier_request_snapshot=json.dumps({"mp_item_nrs": units}),
			items=[frappe._dict(name="row-1", item_code="ITEM-A")],
		)
		shipment = {"items": [{"mp_item_nr": nr} for nr in (shipment_nrs if shipment_nrs is not None else units)]}
		with patch.object(noon, "resolve_item", return_value="ITEM-A"):
			return noon.derive_status(doc, shipment, {"items": order_items}, "ACC")

	def test_the_derivation_table(self):
		unit = self._unit
		cancelled = {"integration": "ACKNOWLEDGED", "mp": "MP_ITEM_STATUS_CANCELLED"}
		cases = [
			([unit("1", "ACKNOWLEDGED")], "Acknowledged", None),
			([unit("1"), unit("2")], "Shipped", None),
			([unit("1", "OUT_OF_STOCK")], "Out of Stock", None),
			([unit("1", **cancelled), unit("2", **cancelled)], "Cancelled", "cancelled"),
			([unit("1"), unit("2", "ACKNOWLEDGED")], "Mixed", "different statuses"),
			([unit("1"), unit("2", **cancelled)], "Mixed", "cancelled 1 of 2"),
		]
		for order_items, expected, error_text in cases:
			derived = self._derive([i["mp_item_nr"] for i in order_items], order_items)
			self.assertEqual(derived.shipping_status, expected)
			# the one row of the Item shows the same word (all units belong to it)
			self.assertEqual(derived.rows, {"row-1": expected})
			if error_text:
				self.assertIn(error_text.lower(), (derived.error or "").lower())
			else:
				self.assertIsNone(derived.error)

	def test_an_unknown_unit_status_changes_nothing_and_items_that_differ_set_an_error(self):
		odd = self._derive(["1"], [{"mp_item_nr": "1", "partner_sku": "SKU-A", "integration_status": "INTEGRATION_ITEM_STATUS_UNSPECIFIED"}])
		self.assertIsNone(odd.shipping_status)
		self.assertEqual(odd.rows, {})
		self.assertTrue(odd.error)
		differ = self._derive(["1"], [self._unit("1")], shipment_nrs=["1", "9"])
		self.assertEqual(differ.shipping_status, "Shipped")
		self.assertIn("differ", differ.error)

	def _refresh(self, note, shipment, order_items):
		with patch(_CLIENT) as client_class, patch(_GET_ORDER, return_value={"items": order_items}) as get_order:
			client = client_class.return_value
			client.request.side_effect = [shipment]
			client.error_message.side_effect = lambda response: response.message
			result = noon.refresh(note.name)
		return result, client, get_order

	def test_refresh_writes_status_row_status_and_sync_time_and_never_edits_items(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		rows_before = [(r.item_code, r.qty) for r in self._note(note.name).items]
		shipment = self._found(fx)
		result, client, get_order = self._refresh(note, shipment, [self._unit("1")])
		self.assertEqual(result, "Shipped")
		self.assertEqual(_paths(client), ["get"])  # GetShipment, then the order (mocked) once
		get_order.assert_called_once()
		stored = self._note(note.name)
		self.assertEqual(stored.custom_shipping_status, "Shipped")
		self.assertEqual(stored.items[0].custom_noon_item_status, "Shipped")
		self.assertTrue(stored.custom_last_sync_at)
		self.assertFalse(stored.custom_last_integration_error)
		self.assertEqual([(r.item_code, r.qty) for r in stored.items], rows_before)

	def test_a_refresh_reading_shipped_leaves_a_submitted_note_ready_for_pickup_and_keeps_a_manual_stage(self):
		# Noon sets Shipped right after the shipment is created: it is not a pickup (#540)
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"docstatus": 1, lifecycle.FIELD: lifecycle.READY_FOR_PICKUP},
			update_modified=False,
		)
		self._refresh(note, self._found(fx), [self._unit("1")])
		self.assertEqual(self._note(note.name).get(lifecycle.FIELD), lifecycle.READY_FOR_PICKUP)

		# a person confirmed the pickup: later refreshes leave the stage alone
		for manual in (lifecycle.PICKED_UP, lifecycle.IN_TRANSIT):
			frappe.db.set_value("Delivery Note", note.name, lifecycle.FIELD, manual, update_modified=False)
			self._refresh(note, self._found(fx), [self._unit("1")])
			self.assertEqual(self._note(note.name).get(lifecycle.FIELD), manual)

	def _unflag(self, note):
		frappe.db.set_value("Delivery Note", note.name, "docstatus", 0, update_modified=False)
		frappe.db.set_value("Delivery Note Item", {"parent": note.name}, "docstatus", 0, update_modified=False)
		frappe.db.commit()

	def test_a_cancel_noon_did_holds_the_submitted_note_at_any_stage(self):
		# #527, #552: the stage no longer decides (Ready for Pickup, Picked Up, In Transit, Delivered). The note stays
		# submitted: Cancelled + Awaiting Return, no error text, no native cancel, one notification.
		for stage in (lifecycle.READY_FOR_PICKUP, *lifecycle.NOON_MANUAL_STATES):
			with self.subTest(stage=stage):
				fx = self._order([("SKU-A", 1)], ["SKU-A"])
				note = self._created_note(fx)
				self._submit_flags(note)
				frappe.db.set_value("Delivery Note", note.name, lifecycle.FIELD, stage, update_modified=False)
				frappe.db.commit()
				with (
					patch.object(noon, "_cancel_note_natively") as native,
					patch.object(carrier_shipments, "_notify") as notify,
				):
					result, _client, _get = self._refresh(
						note, self._found(fx), [self._unit("1", "ACKNOWLEDGED", mp=_CANCELLED)]
					)
				self.assertEqual(result, noon.SHIPPING_AWAITING_RETURN)
				stored = self._note(note.name)
				self.assertEqual(stored.docstatus, 1)
				self.assertEqual(stored.custom_integration_status, "Cancelled")
				self.assertEqual(stored.custom_shipping_status, "Awaiting Return")
				self.assertFalse(stored.custom_last_integration_error)
				self.assertEqual(stored.get(lifecycle.FIELD), lifecycle.RETURNED_FAILED)
				native.assert_not_called()
				notify.assert_called_once()
				self.assertIn(note.name, notify.call_args.args[1])
				self.assertTrue(noon.is_held(stored))
				self._unflag(note)
				# the next stage builds its own fixture: the mapping (account + SKU) is unique, so drop this one now
				for name in fx.delivery_notes():
					frappe.db.set_value("Delivery Note", name, {"custom_integration_status": "Cancelled", "custom_shipping_status": "Cancelled"}, update_modified=False)
				fx.cleanup()
				self.fixtures.remove(fx)
				frappe.db.commit()

	def test_a_cancel_noon_did_after_every_line_was_out_of_stock_holds_the_note_too(self):
		# #552: the stage Returned / Failed an Out of Stock read set no longer lets a later cancel skip the hold.
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		self._submit_flags(note)
		frappe.db.set_value("Delivery Note", note.name, lifecycle.FIELD, lifecycle.PICKED_UP, update_modified=False)
		frappe.db.commit()
		with patch.object(noon, "_cancel_note_natively") as native, patch.object(carrier_shipments, "_notify") as notify:
			self._refresh(note, self._found(fx), [self._unit("1", "OUT_OF_STOCK")])
			self.assertEqual(self._note(note.name).custom_shipping_status, "Out of Stock")
			self.assertFalse(noon.is_held(self._note(note.name)))
			result, _client, _get = self._refresh(note, self._found(fx), [self._unit("1", "OUT_OF_STOCK", mp=_CANCELLED)])
		self.assertEqual(result, noon.SHIPPING_AWAITING_RETURN)
		self.assertTrue(noon.is_held(self._note(note.name)))
		native.assert_not_called()
		notify.assert_called_once()
		self._unflag(note)

	def test_a_cancellation_unknown_resolved_by_a_read_holds_a_submitted_note(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		self._submit_flags(note)
		frappe.db.set_value(
			"Delivery Note", note.name, "custom_integration_status", "Cancellation Unknown", update_modified=False
		)
		frappe.db.commit()
		with patch.object(noon, "_cancel_note_natively") as native, patch.object(carrier_shipments, "_notify") as notify:
			result, _client, _get = self._refresh(note, None, [self._unit("1", "SHIPPED", mp=_CANCELLED)])
		self.assertEqual(result, noon.SHIPPING_AWAITING_RETURN)
		self.assertTrue(noon.is_held(self._note(note.name)))
		native.assert_not_called()
		notify.assert_called_once()
		self._unflag(note)

	def test_a_user_cancel_of_a_submitted_note_sends_the_cancel_to_noon_and_holds_the_note(self):
		# #552: Cancel from Ready for Pickup (cancel_at_noon, the cancel cascade): CancelShipment still goes out
		# automatically, then the note is held (no native cancel, no restock) until Confirm Returned.
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		self._submit_flags(note)
		frappe.db.set_value("Delivery Note", note.name, lifecycle.FIELD, lifecycle.READY_FOR_PICKUP, update_modified=False)
		frappe.db.commit()
		with (
			patch.object(carrier_shipments, "_check_native_cancel_not_blocked"),
			patch.object(carrier_shipments, "_notify") as notify,
		):
			result, client, _get = self._cancel(note, _response(200))
		self.assertEqual((result["status"], result["outcome"]), ("Cancelled", "cancelled"))
		self.assertEqual(_paths(client), [noon.SHIPMENT_CANCEL_PATH.rsplit("/", 1)[-1]])
		stored = self._note(note.name)
		self.assertEqual((stored.docstatus, stored.custom_integration_status), (1, "Cancelled"))
		self.assertEqual(stored.custom_shipping_status, noon.SHIPPING_AWAITING_RETURN)
		self.assertEqual(stored.get(lifecycle.FIELD), lifecycle.RETURNED_FAILED)
		self.assertTrue(noon.is_held(stored))
		notify.assert_called_once()
		with self.assertRaises(frappe.ValidationError):
			carrier_shipments.guard_cancel_or_delete(stored)  # the native cancel waits for Confirm Returned
		self._unflag(note)

	def test_cancel_remote_early_success_holds_a_submitted_note_and_marks_the_cancel_as_ours(self):
		# #552: Noon already shows every unit cancelled, so no CancelShipment is sent; the submitted note is still held.
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		self._submit_flags(note)
		with (
			patch.object(carrier_shipments, "_check_native_cancel_not_blocked"),
			patch.object(carrier_shipments, "_notify"),
		):
			result, client, get_order = self._cancel(note, order_items=[{"mp_item_nr": "1", "mp_status": _CANCELLED}])
		get_order.assert_called_once()
		client.request.assert_not_called()
		self.assertEqual((result["status"], result["outcome"]), ("Cancelled", "cancelled"))
		stored = self._note(note.name)
		self.assertTrue(noon.is_held(stored))
		self.assertEqual(stored.custom_shipping_status, noon.SHIPPING_AWAITING_RETURN)
		self.assertFalse(noon.cancelled_by_noon(stored))
		# the unit list the cancel read is kept next to the marker
		self.assertEqual(noon._snapshot_units(stored.custom_carrier_request_snapshot), ["1"])
		self._unflag(note)

	def test_cancelling_a_draft_note_ends_cancelled_not_awaiting_return(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		result, _client, _get = self._cancel(note, _response(200))
		self.assertEqual(result["outcome"], "cancelled")
		stored = self._note(note.name)
		self.assertEqual(stored.docstatus, 0)
		self.assertEqual(stored.custom_shipping_status, noon.SHIPPING_CANCELLED)
		self.assertFalse(noon.is_held(stored))

	def test_a_cancel_noon_did_is_not_marked_ours_but_a_resolved_cancellation_unknown_is(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		self._submit_flags(note)
		with patch.object(noon, "_cancel_note_natively"), patch.object(carrier_shipments, "_notify"):
			self._refresh(note, self._found(fx), [self._unit("1", "ACKNOWLEDGED", mp=_CANCELLED)])
		self.assertTrue(noon.cancelled_by_noon(self._note(note.name)))
		self._unflag(note)

		fx = self._order([("SKU-B", 1)], ["SKU-B"])
		note = self._created_note(fx)
		self._submit_flags(note)
		frappe.db.set_value(
			"Delivery Note", note.name, "custom_integration_status", "Cancellation Unknown", update_modified=False
		)
		frappe.db.commit()
		with patch.object(noon, "_cancel_note_natively"), patch.object(carrier_shipments, "_notify"):
			self._refresh(note, None, [self._unit("1", "SHIPPED", mp=_CANCELLED)])
		self.assertFalse(noon.cancelled_by_noon(self._note(note.name)))
		self._unflag(note)

	def test_a_held_note_is_not_cancelled_by_a_later_refresh_the_retry_or_the_nightly_run(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		self._submit_flags(note)
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{
				"custom_integration_status": "Cancelled",
				"custom_shipping_status": "Awaiting Return",
				lifecycle.FIELD: lifecycle.RETURNED_FAILED,
			},
			update_modified=False,
		)
		frappe.db.commit()
		with patch.object(noon, "_cancel_note_natively") as native, patch(_CLIENT) as client_class:
			noon.refresh(note.name)
			# even with an error text (a blocked earlier cancel) the retry branch skips a held note
			frappe.db.set_value("Delivery Note", note.name, "custom_last_integration_error", "x", update_modified=False)
			noon.refresh(note.name)
			noon.run_reconcile()
		native.assert_not_called()
		client_class.return_value.request.assert_not_called()  # a Cancelled note is never read at Noon
		self.assertTrue(noon.is_held(self._note(note.name)))
		self._unflag(note)

	def test_the_native_cancel_of_a_held_note_is_refused_unless_confirm_returned_passes_the_flag(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		self._submit_flags(note)
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Cancelled", "custom_shipping_status": "Awaiting Return"},
			update_modified=False,
		)
		frappe.db.commit()
		doc = self._note(note.name)
		with self.assertRaises(frappe.ValidationError) as caught:
			carrier_shipments.guard_cancel_or_delete(doc)
		self.assertIn("Use Confirm Returned", str(caught.exception))
		doc.flags[noon.CONFIRM_RETURNED_FLAG] = True
		carrier_shipments.guard_cancel_or_delete(doc)  # no refusal: only Confirm Returned sets the flag
		# a note Noon cancelled before pickup (Shipping Status Cancelled) is not held and passes
		frappe.db.set_value("Delivery Note", note.name, "custom_shipping_status", "Cancelled", update_modified=False)
		carrier_shipments.guard_cancel_or_delete(self._note(note.name))
		self._unflag(note)

	def test_an_amended_note_copies_no_shipping_status(self):
		meta = frappe.get_meta("Delivery Note")
		self.assertTrue(meta.get_field("custom_shipping_status").no_copy)
		self.assertIn("custom_shipping_status", noon.SYSTEM_FIELDS)
		self.assertIn("Awaiting Return", meta.get_field("custom_shipping_status").options.split("\n"))
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="TYPED-AM")
		note.custom_shipping_status = "Awaiting Return"  # what Amend copies from the cancelled source
		noon.clear_inherited(note)
		self.assertFalse(note.custom_shipping_status)

	def test_a_noon_note_is_counted_in_the_sales_order_summary_and_a_refresh_updates_it(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		summary = lambda: frappe.db.get_value("Sales Order", fx.name, "custom_shipping_status_summary")  # noqa: E731
		frappe.db.set_value("Delivery Note", note.name, "custom_shipping_status", "Acknowledged", update_modified=False)
		noon.after_created(note.name, {})
		self.assertEqual(json.loads(summary()), {"Acknowledged": 1})

		self._refresh(note, self._found(fx), [self._unit("1")])
		self.assertEqual(json.loads(summary()), {"Shipped": 1})

		# a Cancelled shipment is not live: it leaves the summary
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled", update_modified=False)
		noon.after_cancelled(note.name)
		self.assertIsNone(summary())

	def test_a_failed_summary_update_is_logged_and_never_raises_on_create_or_cancel(self):
		# The summary is display only: a lock wait on the Sales Order must not stop the auto-submit after
		# create or the native cancel after a carrier cancel, so the error is logged and swallowed.
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		with (
			patch(
				"marketplace_integrations.integrations.bosta.statuses.update_sales_order_summary_of",
				side_effect=frappe.QueryDeadlockError("Lock wait timeout exceeded"),
			) as update,
			patch.object(frappe, "log_error") as log_error,
		):
			noon.after_created(note.name, {})
			noon.after_cancelled(note.name)
		self.assertEqual(update.call_count, 2)
		self.assertEqual(log_error.call_count, 2)
		self.assertEqual(log_error.call_args.kwargs["title"], "Noon shipping summary")

	def test_refresh_shows_mixed_and_an_item_difference_sets_the_error(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A", "SKU-A"])
		note = self._created_note(fx, units=("1", "2"))
		shipment = self._found(fx)
		self._refresh(note, shipment, [self._unit("1"), self._unit("2", "ACKNOWLEDGED")])
		stored = self._note(note.name)
		self.assertEqual(stored.custom_shipping_status, "Mixed")
		self.assertEqual(stored.items[0].custom_noon_item_status, "Mixed")
		self.assertIn("differ", stored.custom_last_integration_error)

	def test_a_shipment_missing_at_noon_sets_the_error_and_keeps_the_status(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		self._refresh(note, _not_found(), [self._unit("1")])
		stored = self._note(note.name)
		self.assertTrue(stored.custom_last_integration_error)
		self.assertNotEqual(stored.custom_shipping_status, "Shipped")

	def test_refresh_resolves_a_cancellation_unknown_when_noon_shows_every_unit_cancelled(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancellation Unknown", update_modified=False)
		result, client, _get = self._refresh(
			note, None, [self._unit("1", "ACKNOWLEDGED", mp=_CANCELLED)]
		)
		self.assertEqual(result, "Cancelled")
		self.assertEqual(self._note(note.name).custom_integration_status, "Cancelled")
		client.request.assert_not_called()  # no GetShipment, never a cancel call

	def test_a_cancellation_unknown_note_whose_units_stay_confirmed_is_never_marked_cancelled(self):
		# The stuck state: if Noon cancelled the shipment but kept the units CONFIRMED (UNVERIFIED), a refresh
		# cannot prove the cancel, so the note stays Cancellation Unknown (the person retries or checks).
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancellation Unknown", update_modified=False)
		frappe.db.commit()
		with patch.object(noon, "_cancel_note_natively") as native:
			_result, client, _get = self._refresh(note, None, [self._unit("1", "ACKNOWLEDGED")])
		self.assertEqual(self._note(note.name).custom_integration_status, "Cancellation Unknown")
		native.assert_not_called()
		client.request.assert_not_called()

	def _submit_flags(self, note):
		frappe.db.set_value("Delivery Note", note.name, "docstatus", 1, update_modified=False)
		frappe.db.set_value("Delivery Note Item", {"parent": note.name}, "docstatus", 1, update_modified=False)
		frappe.db.commit()

	def test_the_webhook_queues_one_deduplicated_refresh_per_submitted_note_and_ignores_drafts(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		draft = self._created_note(fx)
		with patch("frappe.enqueue") as enqueue:
			self.assertEqual(noon.enqueue_note_refreshes(fx.marketplace_order.name), [])
		enqueue.assert_not_called()
		self._submit_flags(draft)
		with patch("frappe.enqueue") as enqueue:
			self.assertEqual(noon.enqueue_note_refreshes(fx.marketplace_order.name), [draft.name])
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["job_id"], f"noon-refresh|{draft.name}")
		self.assertTrue(kwargs["deduplicate"])
		self.assertTrue(kwargs["enqueue_after_commit"])

	def test_the_nightly_reconcile_is_one_deduplicated_long_job(self):
		with patch("frappe.enqueue") as enqueue:
			noon.reconcile()
		kwargs = enqueue.call_args.kwargs
		self.assertEqual((kwargs["job_id"], kwargs["queue"], kwargs["deduplicate"]), ("noon-reconcile", "long", True))

	def test_the_nightly_reconcile_survives_one_notes_failure(self):
		# One SKU per order: a mapping is unique per account and SKU.
		fx_one = self._order([("SKU-A", 1)], ["SKU-A"])
		fx_two = self._order([("SKU-B", 1)], ["SKU-B"])
		one, two = self._created_note(fx_one), self._created_note(fx_two)
		seen = []

		def reconcile_note(note, _service):
			seen.append(note.name)
			if note.name == one.name:
				raise RuntimeError("boom")

		with patch.object(noon, "_reconcile_note", side_effect=reconcile_note):
			noon.run_reconcile()
		self.assertIn(one.name, seen)
		self.assertIn(two.name, seen)

	def test_picked_up_is_the_workflow_stage_not_noons_shipped(self):
		for stage, expected in (
			(lifecycle.READY_FOR_PICKUP, False),
			(lifecycle.SHIPMENT_CREATED, False),
			("", False),
			(lifecycle.PICKED_UP, True),
			(lifecycle.IN_TRANSIT, True),
			(lifecycle.DELIVERED, True),
		):
			for shipping in ("Shipped", "Mixed", "Acknowledged"):
				doc = frappe._dict({lifecycle.FIELD: stage, "custom_shipping_status": shipping})
				self.assertEqual(noon.picked_up(doc), expected, (stage, shipping))

	def test_the_nightly_reconcile_reads_shipped_and_stops_at_delivered_cancelled_and_after_60_days(self):
		notes = {}
		for key in ("acknowledged", "shipped", "delivered", "cancelled", "old", "cancelled_submitted"):
			sku = f"SKU-{key}"  # one SKU per order: a mapping is unique per account and SKU
			notes[key] = self._created_note(self._order([(sku, 1)], [sku]))
		for key, status in (
			("acknowledged", "Acknowledged"),
			("shipped", "Shipped"),
			("delivered", "Shipped"),
			("cancelled", "Cancelled"),
			("old", "Acknowledged"),
			("cancelled_submitted", "Cancelled"),
		):
			frappe.db.set_value("Delivery Note", notes[key].name, "custom_shipping_status", status, update_modified=False)
		# the Shipped and Delivered notes are submitted, as real ones are (stage Ready for Pickup / Delivered)
		for key, stage in (("shipped", lifecycle.READY_FOR_PICKUP), ("delivered", lifecycle.DELIVERED)):
			self._submit_flags(notes[key])
			frappe.db.set_value("Delivery Note", notes[key].name, lifecycle.FIELD, stage, update_modified=False)
		# Noon shows it cancelled but the note is still Created and submitted: it must be refreshed so it
		# becomes Cancelled and is cancelled natively.
		self._submit_flags(notes["cancelled_submitted"])
		frappe.db.set_value(
			"Delivery Note", notes["old"].name, "creation", frappe.utils.add_days(frappe.utils.now_datetime(), -61), update_modified=False
		)
		frappe.db.commit()
		seen = []

		with patch.object(noon, "_reconcile_note", side_effect=lambda note, _service: seen.append(note.name)):
			noon.run_reconcile()

		self.assertIn(notes["acknowledged"].name, seen)
		self.assertIn(notes["cancelled_submitted"].name, seen)
		# Noon's Shipped is no longer a stop (#541): a Ready for Pickup note is still read
		self.assertIn(notes["shipped"].name, seen)
		for key in ("delivered", "cancelled", "old"):
			self.assertNotIn(notes[key].name, seen)
		for key in ("cancelled_submitted", "shipped", "delivered"):
			frappe.db.set_value("Delivery Note", notes[key].name, "docstatus", 0, update_modified=False)
			frappe.db.set_value("Delivery Note Item", {"parent": notes[key].name}, "docstatus", 0, update_modified=False)
		frappe.db.commit()

	def test_refresh_cancels_natively_only_when_it_moved_the_note_into_cancelled_or_retries_a_blocked_one(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		self._submit_flags(note)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled", update_modified=False)
		frappe.db.commit()
		# Cancelled by this app's own Cancel (no error): a refresh leaves it to the person cancelling it.
		with patch.object(noon, "_cancel_note_natively") as native:
			noon.refresh(note.name)
		native.assert_not_called()
		# Cancelled with the error a blocked automatic cancel left: the refresh retries.
		frappe.db.set_value("Delivery Note", note.name, "custom_last_integration_error", "blocked", update_modified=False)
		frappe.db.commit()
		with patch.object(noon, "_cancel_note_natively") as native:
			noon.refresh(note.name)
		native.assert_called_once_with(note.name)
		frappe.db.set_value("Delivery Note", note.name, "docstatus", 0, update_modified=False)
		frappe.db.set_value("Delivery Note Item", {"parent": note.name}, "docstatus", 0, update_modified=False)
		frappe.db.commit()

	def test_a_shipment_of_another_order_or_with_a_foreign_unit_is_never_adopted(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="STICKER-1")
		self._creating(note)
		doc = self._note(note.name)
		for shipment in (self._found(fx, order_nr="SOME-OTHER-ORDER"), self._found(fx, units=("1", "99"))):
			with patch(_CLIENT) as client_class:
				client_class.return_value.request.side_effect = [shipment]
				with self.assertRaises(noon.Unknown):
					noon.find_remote(doc)

	def test_needs_attention_lists_an_error_a_refresh_wrote_on_a_shipped_note(self):
		from marketplace_integrations.services import marketplace_attention as attention

		note = self._created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_shipping_status": "Shipped", "custom_last_integration_error": "The items of the Noon shipment differ"},
			update_modified=False,
		)
		frappe.db.commit()
		found = [i for i in attention._collect_carrier_items(attention.CARRIER_EXCEPTION) if i["reference_name"] == note.name]
		self.assertEqual(len(found), 1)

	def test_a_clean_refresh_clears_only_the_error_a_refresh_wrote(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A", "SKU-A"])
		note = self._created_note(fx)
		# a refusal written by the cancel stays through a clean refresh
		frappe.db.set_value("Delivery Note", note.name, "custom_last_integration_error", "Noon refused the cancel", update_modified=False)
		frappe.db.commit()
		self._refresh(note, self._found(fx), [self._unit("1")])
		self.assertEqual(self._note(note.name).custom_last_integration_error, "Noon refused the cancel")
		# the refresh's own error is written, then cleared by the next clean refresh
		# (unit 2 is on the Noon order, so the shipment is ours; it is not one of the units the note sent)
		self._refresh(note, self._found(fx, units=("1", "2")), [self._unit("1")])
		self.assertIn("differ",self._note(note.name).custom_last_integration_error)
		self._refresh(note, self._found(fx), [self._unit("1")])
		self.assertFalse(self._note(note.name).custom_last_integration_error)

	def test_a_webhook_during_a_running_refresh_makes_the_job_read_again(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		calls = []

		def refresh(name):
			calls.append(name)
			if len(calls) == 1:  # the webhook arrives while the first read runs (its job is dropped by the queue)
				frappe.cache().set_value(noon._REFRESH_AGAIN.format(name), 1)

		with patch.object(noon, "refresh", side_effect=refresh):
			noon.run_refresh(note.name)
		self.assertEqual(calls, [note.name, note.name])

	def test_mark_out_of_stock_and_reconciliation_treat_an_unresolved_create_as_started(self):
		from marketplace_integrations.integrations.noon import order_reconciliation

		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		order = frappe.get_doc("Marketplace Order", fx.marketplace_order.name)
		self.assertTrue(order_reconciliation.build_evidence(order)["lifecycle_permits_change"])

		note = self._draft_note(fx, lr_no="TYPED-1")
		for status in ("Creating", "Creation Unknown"):
			frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", status, update_modified=False)
			frappe.db.commit()
			self.assertTrue(noon.has_unresolved_create(order.name))
			evidence = order_reconciliation.build_evidence(order)
			self.assertEqual(evidence["fulfillment_started"], {"sku:SKU-A": True})
			self.assertFalse(evidence["lifecycle_permits_change"])
			row = frappe.db.get_value("Sales Order Item", {"parent": fx.name}, "name")
			with self.assertRaises(frappe.ValidationError):
				fulfillment.mark_row_out_of_stock(fx.name, row)

	def test_the_nightly_reconcile_resolves_an_unknown_create_by_lookup_only(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="AWB-N1")
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Creation Unknown", "custom_carrier_reference": note.name},
			update_modified=False,
		)
		frappe.db.commit()
		shipment = self._found(fx, [{"awb_nr": "AWB-N1"}])
		with patch(_CLIENT) as client_class:
			client = client_class.return_value
			client.request.side_effect = [shipment]
			client.error_message.side_effect = lambda response: response.message
			noon.run_reconcile()
		self.assertEqual(_paths(client), ["get"])  # GetShipment only, never CreateShipment
		self.assertEqual(self._note(note.name).custom_integration_status, "Created")

	def test_a_draft_that_holds_a_noon_shipment_gets_the_error_for_needs_attention(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="AWB-N2")
		frappe.db.set_value("Delivery Note", note.name, "custom_carrier_reference", note.name, update_modified=False)
		frappe.db.commit()
		shipment = self._found(fx)
		with patch(_CLIENT) as client_class:
			client = client_class.return_value
			client.request.side_effect = [shipment]
			noon.run_reconcile()
		self.assertIn("holds a Noon shipment", self._note(note.name).custom_last_integration_error)
		self.assertEqual(self._note(note.name).custom_integration_status, "Not Created")

	def test_needs_attention_lists_mixed_cancelled_and_draft_holds_rows_for_noon(self):
		from marketplace_integrations.services import marketplace_attention as attention

		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_shipping_status": "Mixed", "custom_last_integration_error": "units differ"},
			update_modified=False,
		)
		frappe.db.commit()
		found = [i for i in attention._collect_carrier_items(attention.CARRIER_EXCEPTION) if i["reference_name"] == note.name]
		self.assertEqual(len(found), 1)
		# Noon never has a carrier delivery id: a cancelled note still appears
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled", update_modified=False)
		frappe.db.commit()
		found = [i for i in attention._collect_carrier_items(attention.CARRIER_CANCELLED) if i["reference_name"] == note.name]
		self.assertEqual(len(found), 1)
		# a draft that holds a Noon shipment (Not Created with the error) is a creation problem
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Not Created", "custom_last_integration_error": "This draft holds a Noon shipment"},
			update_modified=False,
		)
		frappe.db.commit()
		found = [i for i in attention._collect_carrier_items(attention.CARRIER_CREATION_PROBLEM) if i["reference_name"] == note.name]
		self.assertEqual(len(found), 1)

	def test_needs_attention_keeps_a_held_note_with_its_own_text_until_it_is_confirmed(self):
		from marketplace_integrations.services import marketplace_attention as attention

		note = self._created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		self._submit_flags(note)
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Cancelled", "custom_shipping_status": "Awaiting Return"},
			update_modified=False,
		)
		frappe.db.commit()
		found = [i for i in attention._collect_carrier_items(attention.CARRIER_CANCELLED) if i["reference_name"] == note.name]
		self.assertEqual(len(found), 1)
		self.assertIn("press Confirm Returned", found[0]["explanation"])
		self.assertNotIn("Press Cancel", found[0]["explanation"])
		# a note Noon cancelled before pickup (the automatic cancel failed) keeps the old text
		frappe.db.set_value("Delivery Note", note.name, "custom_shipping_status", "Cancelled", update_modified=False)
		frappe.db.commit()
		found = [i for i in attention._collect_carrier_items(attention.CARRIER_CANCELLED) if i["reference_name"] == note.name]
		self.assertIn("Press Cancel", found[0]["explanation"])
		self._unflag(note)

	# ---- shipment invoice (#416) ------------------------------------------------------------------

	_PDF = (
		b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
		b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 3 3]>>endobj\nxref\n0 4\n0000000000 65535 f \n"
		b"0000000009 00000 n \n0000000052 00000 n \n0000000101 00000 n \ntrailer<</Size 4/Root 1 0 R>>\n"
		b"startxref\n249\n%%EOF"
	)

	def _submitted_created_note(self, fixture):
		"""A Created Noon note marked submitted (the database row only: nothing here posts stock)."""
		note = self._created_note(fixture)
		frappe.db.set_value("Delivery Note", note.name, "docstatus", 1, update_modified=False)
		frappe.db.commit()
		self.addCleanup(frappe.db.delete, "File", {"attached_to_doctype": "Delivery Note", "attached_to_name": note.name})
		self.addCleanup(frappe.db.delete, "Integration Request", {"reference_docname": note.name, "custom_operation_type": "Invoice"})
		return note

	def _loose_file(self, file_name, content):
		"""A File with no attachment. Earlier commits in these tests persist it, so it is deleted explicitly."""
		file_doc = frappe.get_doc(
			{"doctype": "File", "file_name": file_name, "content": content, "is_private": 1}
		).insert()
		self.addCleanup(self._delete_file, file_doc.name)
		return file_doc

	@staticmethod
	def _delete_file(name):
		if frappe.db.exists("File", name):
			frappe.delete_doc("File", name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def _loose_pdf(self):
		return self._loose_file("shipment-invoice.pdf", self._PDF)

	def _press(self, note, file_doc, key=None, invoice_nr="INV-1"):
		with patch("marketplace_integrations.integrations.noon.delivery_note_shipment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			result = noon.upload_invoice_for_note(note.name, file_doc.file_url, invoice_nr, "2026-11-01", key)
		return result, enqueue

	def _run_invoice(self, note, key, status=200, file_url=None):
		file_url = file_url or frappe.get_all(
			"File", filters={"attached_to_doctype": "Delivery Note", "attached_to_name": note.name}, pluck="file_url"
		)[0]
		with patch(_CLIENT) as client_class:
			client = client_class.return_value
			client.request.return_value = _response(status, message="refused <b>x</b>")
			client.error_message.side_effect = lambda response: response.message
			noon.run_upload_invoice(note.name, file_url, "INV-1", "2026-11-01", key)
		return client

	def test_the_invoice_is_a_private_file_on_the_note_and_the_job_uses_the_shipment_number(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		result, enqueue = self._press(note, self._loose_pdf())
		self.assertTrue(result["queued"])
		attached = frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Delivery Note", "attached_to_name": note.name},
			fields=["file_url", "is_private"],
		)
		self.assertEqual([(row.is_private, row.file_url) for row in attached], [(1, result["file_url"])])
		self.assertEqual(enqueue.call_args.kwargs["idempotency_key"], result["idempotency_key"])
		client = self._run_invoice(note, result["idempotency_key"])
		args, kwargs = client.request.call_args
		self.assertEqual(args, ("POST", "/fbpi/v1/shipment/invoice/upload"))
		self.assertEqual(kwargs["json"]["integration_shipment_nr"], note.name)
		self.assertEqual(kwargs["json"]["warehouse_code"], self.account.noon_warehouse_code)
		self.assertEqual(kwargs["json"]["idempotency_key"], result["idempotency_key"])
		self.assertEqual(kwargs["json"]["issued_at"], "2026-11-01")

	def test_success_records_the_upload_time_and_logs_without_secrets(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		result, _enqueue = self._press(note, self._loose_pdf())
		client = self._run_invoice(note, result["idempotency_key"])
		stored = self._note(note.name)
		self.assertTrue(stored.custom_noon_invoice_uploaded_on)
		self.assertEqual(frappe.cache().get_value(f"noon-invoice-result|{result['idempotency_key']}")["state"], "done")
		rows = frappe.get_all(
			"Integration Request",
			filters={"custom_operation_type": "Invoice", "reference_doctype": "Delivery Note", "reference_docname": note.name},
			fields=["*"],
		)
		self.assertEqual(len(rows), 1)
		dump = json.dumps(rows[0], default=str)
		self.assertEqual((rows[0].custom_result, rows[0].custom_external_reference), ("Success", note.name))
		self.assertNotIn(client.request.call_args.kwargs["json"]["invoice"], dump)
		self.assertNotIn("test-key-id", dump)

	def test_retry_reuses_the_key_and_a_new_press_makes_a_new_key(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		first, _enqueue = self._press(note, self._loose_pdf())
		self._run_invoice(note, first["idempotency_key"], status=500)
		failed = self._note(note.name)
		self.assertFalse(failed.custom_noon_invoice_uploaded_on, "a failure records no upload")
		retry, enqueue = self._press(note, frappe._dict(file_url=first["file_url"]), key=first["idempotency_key"])
		self.assertEqual(retry["idempotency_key"], first["idempotency_key"])
		self.assertEqual(enqueue.call_args.kwargs["idempotency_key"], first["idempotency_key"])
		self.assertEqual(
			frappe.cache().get_value(f"noon-invoice-result|{first['idempotency_key']}")["state"], "pending"
		)
		self.assertEqual(
			len(frappe.get_all("File", filters={"attached_to_doctype": "Delivery Note", "attached_to_name": note.name})), 1
		)
		self._run_invoice(note, first["idempotency_key"])
		second, _enqueue = self._press(note, self._loose_pdf(), invoice_nr="INV-2")
		self.assertNotEqual(second["idempotency_key"], first["idempotency_key"])

	def test_a_second_upload_replaces_the_time_of_the_first(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		first, _enqueue = self._press(note, self._loose_pdf())
		self._run_invoice(note, first["idempotency_key"])
		frappe.db.set_value("Delivery Note", note.name, "custom_noon_invoice_uploaded_on", "2026-01-01 10:00:00", update_modified=False)
		second, _enqueue = self._press(note, self._loose_pdf(), invoice_nr="INV-2")
		self._run_invoice(note, second["idempotency_key"])
		self.assertGreater(str(self._note(note.name).custom_noon_invoice_uploaded_on), "2026-01-02")

	def test_a_double_press_while_an_upload_runs_is_refused_and_the_job_clears_the_marker(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		first, _enqueue = self._press(note, self._loose_pdf())
		with self.assertRaises(frappe.ValidationError):
			self._press(note, self._loose_pdf(), invoice_nr="INV-2")
		# the same key (a Retry of the running press) is not a second upload
		retry, _enqueue = self._press(note, frappe._dict(file_url=first["file_url"]), key=first["idempotency_key"])
		self.assertEqual(retry["idempotency_key"], first["idempotency_key"])
		self._run_invoice(note, first["idempotency_key"])
		second, _enqueue = self._press(note, self._loose_pdf(), invoice_nr="INV-2")
		self.assertNotEqual(second["idempotency_key"], first["idempotency_key"])
		self._run_invoice(note, second["idempotency_key"])

	def test_a_key_is_bound_to_its_document(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._submitted_created_note(fx)
		other = self._submitted_created_note(self._order([("SKU-B", 1)], ["SKU-B"]))
		first, _enqueue = self._press(note, self._loose_pdf())
		fulfillment.set_invoice_result(first["idempotency_key"], "failed", "Noon said no")
		self.assertEqual(fulfillment.invoice_result(first["idempotency_key"], "Delivery Note", note.name)["state"], "failed")
		self.assertEqual(fulfillment.invoice_result(first["idempotency_key"], "Delivery Note", other.name)["state"], "pending")
		with self.assertRaises(frappe.ValidationError):
			self._press(other, frappe._dict(file_url=first["file_url"]), key=first["idempotency_key"])
		self._run_invoice(note, first["idempotency_key"])

	def test_a_loose_upload_that_shares_the_attached_copys_path_is_removed_but_the_file_stays(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		loose = self._loose_pdf()
		result, _enqueue = self._press(note, loose)
		self.assertFalse(frappe.db.exists("File", loose.name), "the orphan loose row is gone")
		self.assertTrue(frappe.get_all("File", filters={"file_url": result["file_url"]}), "the attached copy remains")
		self.assertTrue(frappe.get_doc("File", {"file_url": result["file_url"]}).get_content())
		self._run_invoice(note, result["idempotency_key"])

	def test_a_failure_shows_in_the_result_escaped_and_never_changes_the_note(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		result, _enqueue = self._press(note, self._loose_pdf())
		self._run_invoice(note, result["idempotency_key"], status=400)
		outcome = frappe.cache().get_value(f"noon-invoice-result|{result['idempotency_key']}")
		self.assertEqual(outcome["state"], "failed")
		self.assertNotIn("<b>", outcome["message"])
		stored = self._note(note.name)
		self.assertEqual((stored.docstatus, stored.custom_integration_status), (1, "Created"))
		# Submit and Cancel stay available: nothing in the invoice path touches the note's status
		self.assertFalse(stored.custom_noon_invoice_uploaded_on)

	def test_the_press_is_refused_for_what_is_not_a_created_submitted_noon_note(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		draft = self._created_note(fx)  # Created but still a draft
		with self.assertRaises(frappe.ValidationError):
			self._press(draft, self._loose_pdf())
		frappe.db.set_value("Delivery Note", draft.name, {"docstatus": 1, "custom_integration_status": "Cancellation Unknown"}, update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			self._press(draft, self._loose_pdf())
		self.assertFalse(frappe.get_all("File", filters={"attached_to_doctype": "Delivery Note", "attached_to_name": draft.name}))

	def test_the_press_is_refused_unless_the_warehouse_is_fbpi_ready(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		for overrides, reason in (
			({"noon_fulfillment_system_code": "OTHER"}, "OTHER, not FBPI"),
			({"noon_warehouse_active": 0}, "not active"),
		):
			self._fbpi_ready_account(**overrides)
			with self.assertRaisesRegex(frappe.ValidationError, reason):
				self._press(note, self._loose_pdf())
			self._fbpi_ready_account()
		self.assertFalse(frappe.get_all("File", filters={"attached_to_doctype": "Delivery Note", "attached_to_name": note.name}))

	def test_the_press_refuses_a_missing_number_date_a_bad_key_and_a_non_pdf(self):
		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		with patch("marketplace_integrations.integrations.noon.delivery_note_shipment.frappe.enqueue") as enqueue:
			with self.assertRaises(frappe.ValidationError):
				noon.upload_invoice_for_note(note.name, self._loose_pdf().file_url, " ", "2026-11-01")
			with self.assertRaises(frappe.ValidationError):
				noon.upload_invoice_for_note(note.name, self._loose_pdf().file_url, "INV-1", "")
			with self.assertRaises(frappe.ValidationError):
				noon.upload_invoice_for_note(note.name, self._loose_pdf().file_url, "INV-1", "2026-11-01", "bad key!")
			not_pdf = self._loose_file("x.bin", b"not a pdf")
			with self.assertRaises(frappe.ValidationError):
				noon.upload_invoice_for_note(note.name, not_pdf.file_url, "INV-1", "2026-11-01")
		enqueue.assert_not_called()

	def test_the_endpoint_needs_write_on_the_delivery_note(self):
		from marketplace_integrations.api import noon_fulfillment
		from marketplace_integrations.tests.utils import make_role_user

		note = self._submitted_created_note(self._order([("SKU-A", 1)], ["SKU-A"]))
		writer, _role = make_role_user(self, "Noon Invoice Note Writer", {"Delivery Note": ["read", "write"]})
		reader, _role = make_role_user(self, "Noon Invoice Note Reader", {"Delivery Note": ["read"]})
		with patch.object(noon, "upload_invoice_for_note", return_value={"queued": True}) as upload:
			with self.set_user(writer):
				noon_fulfillment.upload_delivery_note_invoice(note.name, "/private/files/i.pdf", "INV-1", "2026-11-01")
			upload.assert_called_once_with(note.name, "/private/files/i.pdf", "INV-1", "2026-11-01", None)
			with self.set_user(reader), self.assertRaises(frappe.PermissionError):
				noon_fulfillment.upload_delivery_note_invoice(note.name, "/private/files/i.pdf", "INV-1", "2026-11-01")
		self.assertEqual(upload.call_count, 1)

	def test_amending_a_note_does_not_carry_the_invoice_record(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._draft_note(fx, lr_no="TYPED-1")
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_noon_invoice_uploaded_on": "2026-11-01 10:00:00"},
			update_modified=False,
		)
		new = frappe.copy_doc(self._note(note.name), ignore_no_copy=False)  # Amend copies even no_copy fields
		new.lr_no = None  # the typed AWB belongs to the old note (the duplicate AWB rule is not under test)
		new.custom_noon_generate_awb = 1
		noon.guard_changes(new)
		self.assertFalse(new.custom_noon_invoice_uploaded_on)
