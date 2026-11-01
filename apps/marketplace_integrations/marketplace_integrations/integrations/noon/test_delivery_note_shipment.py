# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Delivery Note-first Noon shipment tests (#391): saving a draft Delivery Note of a Noon Sales
Order queues the shipment, the worker picks units by SKU and quantity, reuses
``run_create_shipment`` and writes the result back, errors show on the note and in Needs
Attention, Retry reuses the same shipment, and submit/edits are guarded. Noon HTTP is mocked."""

import html
from unittest.mock import MagicMock, patch

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

from marketplace_integrations.api import noon_fulfillment as noon_api
from marketplace_integrations.integrations.noon import delivery_note_shipment as dn_shipment
from marketplace_integrations.integrations.noon import fulfillment
from marketplace_integrations.integrations.noon.test_fulfillment import (
	IntegrationTestNoonFulfillmentBase,
	_add_noon_awb,
	_confirmed_acknowledged,
)
from marketplace_integrations.patches.v0_0 import create_noon_transporter_supplier
from marketplace_integrations.services import carrier_shipments, marketplace_attention
from marketplace_integrations.tests.handover_fixture import SubmittedOrderFixture
from marketplace_integrations.tests.utils import make_role_user

_CLIENT = "marketplace_integrations.integrations.noon.fulfillment.NoonClient"
_CLAIM = "marketplace_integrations.integrations.noon.fulfillment.claim_pool_awb"
_IMPORT = "marketplace_integrations.integrations.noon.fulfillment.import_order"
_GET_ORDER = "marketplace_integrations.integrations.noon.fulfillment.get_fbpi_order"


class IntegrationTestNoonDeliveryNoteShipment(IntegrationTestNoonFulfillmentBase):
	def setUp(self):
		super().setUp()
		self.fixtures = []
		self.created_supplier = False
		if not dn_shipment.transporter_name():
			create_noon_transporter_supplier.execute()
			self.created_supplier = True
			self.created_supplier_name = dn_shipment.transporter_name()

	def tearDown(self):
		frappe.set_user("Administrator")
		for fixture in self.fixtures:
			# the cancel/delete guard (#393) refuses notes with a live shipment: close them first
			frappe.db.set_value(
				"Marketplace Order Shipment",
				{"parent": fixture.marketplace_order.name},
				"status",
				"Cancelled",
				update_modified=False,
			)
			fixture.cleanup()
		frappe.db.commit()
		super().tearDown()
		if self.created_supplier:
			frappe.delete_doc("Supplier", self.created_supplier_name, force=True, ignore_permissions=True)
			frappe.db.commit()

	# ---- helpers -------------------------------------------------------------------------

	def _order(self, rows, payload_skus, **kwargs):
		"""A submitted Noon Sales Order of ``rows`` ([(sku, qty)]) whose Noon payload holds one
		CONFIRMED + ACKNOWLEDGED unit per entry of ``payload_skus`` (mp_item_nr 1, 2, ...)."""
		fixture = SubmittedOrderFixture(self.account, rows, **kwargs)
		self.fixtures.append(fixture)
		self._set_payload(fixture, payload_skus)
		return fixture

	def _set_payload(self, fixture, payload_skus):
		items = [_confirmed_acknowledged(index, sku) for index, sku in enumerate(payload_skus, start=1)]
		self.payload = {"fbpi_order_nr": fixture.marketplace_order.external_order_id, "items": items}
		frappe.db.set_value(
			"Marketplace Order", fixture.marketplace_order.name, "latest_payload", frappe.as_json(self.payload)
		)
		frappe.db.commit()

	def _draft_note(self, fixture, lr_no=None, user=None):
		"""Save a draft Delivery Note from the Sales Order the way Create > Delivery Note does."""
		if user:
			frappe.set_user(user)
		try:
			note = make_delivery_note(fixture.name)
			if lr_no:
				note.lr_no = lr_no
			with patch("frappe.enqueue") as enqueue:
				note.insert()
		finally:
			frappe.set_user("Administrator")
		frappe.db.commit()
		return note, enqueue

	def _noon(self, *responses, units=None):
		"""Patches for the Noon HTTP of one worker run; ``responses`` are the NoonClient.request
		results in order (GetShipment, [GetNoonLogisticsAWBs,] CreateShipment)."""
		payload = {**self.payload, "items": units or self.payload["items"]}
		patches = (
			patch(_CLAIM, return_value=None),
			patch(_GET_ORDER, return_value=payload),
			patch(_IMPORT, return_value=(None, [])),
			patch(_CLIENT),
		)
		return patches, responses

	def _run_worker(self, note_name, *responses):
		patches, responses = self._noon(*responses)
		with patches[0], patches[1], patches[2], patches[3] as client_class:
			client = client_class.return_value
			client.request.side_effect = list(responses)
			client.error_message.side_effect = lambda response: response.message
			dn_shipment.run(note_name)
		return client

	@staticmethod
	def _response(status=200, awb=None, message=None):
		response = MagicMock(status_code=status)
		response.json.return_value = {"awb_nrs": [awb]} if awb else {}
		response.message = message
		return response

	def _happy_responses(self, awb="NOON-AWB-1"):
		return [self._response(404), self._response(200, awb=awb), self._response(200)]

	def _order_doc(self, fixture):
		return frappe.get_doc("Marketplace Order", fixture.marketplace_order.name)

	def _note(self, name):
		return frappe.get_doc("Delivery Note", name)

	# ---- creation ------------------------------------------------------------------------

	def test_saving_the_draft_queues_one_deduplicated_job_after_commit(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A", "SKU-A"])
		note, enqueue = self._draft_note(fx)
		enqueue.assert_called_once()
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["queue"], "long")
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertTrue(kwargs["deduplicate"])
		self.assertIn(note.name, kwargs["job_id"])
		self.assertEqual(kwargs["delivery_note"], note.name)
		self.assertEqual(self._note(note.name).custom_carrier_status, "Pending")

		# a second save of the same draft queues nothing
		reloaded = self._note(note.name)
		with patch("frappe.enqueue") as again:
			reloaded.save()
		again.assert_not_called()

	def test_the_worker_creates_one_created_shipment_and_writes_back(self):
		fx = self._order([("SKU-A", 2), ("SKU-B", 1)], ["SKU-A", "SKU-B", "SKU-A", "SKU-A"])
		note, _enqueue = self._draft_note(fx)
		client = self._run_worker(note.name, *self._happy_responses())

		order = self._order_doc(fx)
		self.assertEqual(len(order.noon_shipments), 1)
		shipment = order.noon_shipments[0]
		self.assertEqual((shipment.status, shipment.delivery_note), ("Created", note.name))
		# exactly the first two shippable units of SKU-A plus the one SKU-B unit
		self.assertEqual(
			sorted((row.mp_item_nr, row.partner_sku) for row in order.noon_shipment_units),
			[("1", "SKU-A"), ("2", "SKU-B"), ("3", "SKU-A")],
		)
		sent = client.request.call_args_list[-1].kwargs["json"]
		self.assertEqual(sorted(item["mp_item_nr"] for item in sent["items"]), ["1", "2", "3"])
		self.assertEqual(sent["awbs"], [{"courier": "noon", "awb_nr": "NOON-AWB-1"}])

		saved = self._note(note.name)
		self.assertEqual(saved.docstatus, 0)  # the app never submits
		self.assertEqual(
			(saved.transporter, saved.lr_no, saved.custom_carrier_status, saved.custom_carrier_error),
			("Noon", "NOON-AWB-1", "Created", None),
		)
		self.assertTrue(saved.lr_date)

	def test_a_duplicate_job_creates_nothing_more(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _enqueue = self._draft_note(fx)
		self._run_worker(note.name, *self._happy_responses())
		client = self._run_worker(note.name)  # no Noon response needed: nothing is called
		client.request.assert_not_called()
		self.assertEqual(len(self._order_doc(fx).noon_shipments), 1)

	# ---- refusals before any Noon call --------------------------------------------------------

	def _assert_refused(self, fx, note, text, existing_shipments=0):
		client = self._run_worker(note.name)
		client.request.assert_not_called()
		order = self._order_doc(fx)
		self.assertEqual(len(order.noon_shipments), existing_shipments)
		saved = self._note(note.name)
		self.assertEqual(saved.custom_carrier_status, "Error")
		self.assertIn(text, html.unescape(saved.custom_carrier_error))
		self.assertEqual(saved.docstatus, 0)  # the draft stays
		return saved

	def test_too_few_shippable_units_ends_in_error_without_a_noon_call(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		self._assert_refused(fx, note, "only 1 are available")

	def test_units_in_an_active_shipment_are_not_taken_again(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A", "SKU-A"])
		order = self._order_doc(fx)
		order.append("noon_shipments", {"integration_shipment_nr": "X-1", "status": "Created", "awb_source": "Noon"})
		order.append("noon_shipment_units", {"integration_shipment_nr": "X-1", "mp_item_nr": "1", "partner_sku": "SKU-A"})
		order.save()
		note, _ = self._draft_note(fx)
		self._assert_refused(fx, note, "only 1 are available", existing_shipments=1)

	def test_an_unmapped_item_ends_in_error(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		frappe.db.set_value("Marketplace Product Mapping", fx.mappings[0].name, "enabled", 0)
		frappe.db.commit()
		self._assert_refused(fx, note, "no enabled Noon product mapping")

	def test_an_item_mapped_to_two_noon_skus_on_the_order_is_refused(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A", "SKU-A2"])
		second = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"external_reference": "SKU-A2",
				"external_sku": "SKU-A2",
				"item": fx.items["SKU-A"].name,
				"enabled": 1,
			}
		).insert()
		fx.mappings.append(second)  # cleaned up with the fixture
		frappe.db.commit()
		note, _ = self._draft_note(fx)
		self._assert_refused(fx, note, "more than one Noon SKU")

	def test_a_typed_awb_is_sent_and_one_in_use_on_the_account_is_refused(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx, lr_no="STICKER-1")
		client = self._run_worker(note.name, self._response(404), self._response(200))
		self.assertEqual(
			client.request.call_args_list[-1].kwargs["json"]["awbs"],
			[{"courier": "noon", "awb_nr": "STICKER-1"}],
		)
		self.assertEqual(self._note(note.name).lr_no, "STICKER-1")

		other = self._order([("SKU-B", 1)], ["SKU-B"])
		_add_noon_awb(self.account, "STICKER-2", "Used", fx.marketplace_order.name)
		frappe.db.commit()
		used, _ = self._draft_note(other, lr_no="sticker-2")
		saved = self._assert_refused(other, used, "is already in use")
		self.assertEqual(saved.lr_no, "sticker-2")  # what the user typed stays editable

	# ---- Error, Needs Attention, Retry -------------------------------------------------------------

	def test_a_noon_failure_shows_on_the_note_and_retry_reuses_the_shipment(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		self._run_worker(
			note.name, self._response(404), self._response(200, awb="NOON-AWB-9"), self._response(500, message="Noon <b>down")
		)
		order = self._order_doc(fx)
		self.assertEqual(order.noon_shipments[0].status, "Error")
		saved = self._note(note.name)
		self.assertEqual(saved.custom_carrier_status, "Error")
		self.assertNotIn("<b>", saved.custom_carrier_error)  # escaped
		self.assertIn("Noon", saved.custom_carrier_error)

		# Needs Attention names the Delivery Note on the Noon Fulfillment row
		items = marketplace_attention.get_attention_items({"platform": "Noon", "account": self.account.name})
		self.assertTrue(
			any(note.name in item["explanation"] for item in items if item["problem_type"] == "Noon Fulfillment")
		)

		with patch("frappe.enqueue") as enqueue:
			result = noon_api.retry_delivery_note_shipment(note.name)
		self.assertTrue(result["queued"])
		enqueue.assert_called_once()
		self.assertEqual(self._note(note.name).custom_carrier_status, "Pending")

		# GetShipment finds nothing, the AWB row already exists, CreateShipment now works
		self._run_worker(note.name, self._response(404), self._response(200))
		order = self._order_doc(fx)
		self.assertEqual([row.integration_shipment_nr for row in order.noon_shipments], [f"{fx.marketplace_order.external_order_id}-1"])
		self.assertEqual(order.noon_shipments[0].status, "Created")
		saved = self._note(note.name)
		self.assertEqual((saved.custom_carrier_status, saved.lr_no, saved.transporter), ("Created", "NOON-AWB-9", "Noon"))

	def test_a_refusal_appears_in_needs_attention_with_the_note_until_fixed(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		self._run_worker(note.name)
		filters = {"platform": "Noon", "account": self.account.name}
		rows = [i for i in marketplace_attention.get_attention_items(filters) if i["problem_type"] == "Fulfillment Error"]
		self.assertEqual(len(rows), 1)
		self.assertIn(note.name, rows[0]["explanation"])
		# once the note is deleted the row is gone
		frappe.delete_doc("Delivery Note", note.name, force=True)
		frappe.db.commit()
		self.assertEqual(
			[i for i in marketplace_attention.get_attention_items(filters) if i["problem_type"] == "Fulfillment Error"],
			[],
		)

	def test_retry_needs_write_permission_and_an_errored_draft(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		self._run_worker(note.name)  # Error: too few units
		email, _role = make_role_user(self, "DN Reader", {"Delivery Note": ["read"]})
		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				noon_api.retry_delivery_note_shipment(note.name)
		finally:
			frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			# not in Error: Created notes cannot be retried
			frappe.db.set_value("Delivery Note", note.name, "custom_carrier_status", "Created")
			noon_api.retry_delivery_note_shipment(note.name)

	# ---- guards -------------------------------------------------------------------------------------

	def test_items_and_transporter_are_locked_once_the_shipment_exists(self):
		fx = self._order([("SKU-A", 2), ("SKU-B", 1)], ["SKU-A", "SKU-A", "SKU-B"])
		note, _ = self._draft_note(fx)
		# before a shipment exists the note is freely editable
		editable = self._note(note.name)
		editable.items[0].qty = 1
		editable.save()
		editable.items[0].qty = 2
		editable.save()

		self._run_worker(note.name, *self._happy_responses())
		other = frappe.get_doc(
			{"doctype": "Supplier", "supplier_name": f"Other Transporter {frappe.generate_hash(length=6)}",
			 "supplier_group": "All Supplier Groups", "is_transporter": 1}
		).insert()
		self.addCleanup(lambda: (frappe.delete_doc("Supplier", other.name, force=True), frappe.db.commit()))

		def lower_qty(doc):
			doc.items[0].qty = 1

		def drop_row(doc):
			doc.remove(doc.items[1])

		def other_transporter(doc):
			doc.transporter = other.name

		def add_row(doc):
			row = doc.append("items", {})
			row.update({"item_code": doc.items[0].item_code, "qty": 1, "uom": "Nos", "warehouse": doc.items[0].warehouse})

		for change in (lower_qty, drop_row, other_transporter, add_row):
			doc = self._note(note.name)
			change(doc)
			with self.assertRaises(frappe.ValidationError, msg=change.__name__) as caught:
				doc.save()
			self.assertIn("Noon shipment", str(caught.exception) + frappe.as_json(frappe.local.message_log))
		# header edits still work
		doc = self._note(note.name)
		doc.po_no = "PO-1"
		doc.save()

	def test_submit_is_refused_until_the_shipment_is_created(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		with self.assertRaises(frappe.ValidationError):
			self._note(note.name).submit()
		self._run_worker(note.name, *self._happy_responses())
		submitted = self._note(note.name)
		submitted.submit()
		self.assertEqual(submitted.docstatus, 1)

	def test_a_delivery_note_of_another_platform_is_unaffected(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		# a Sales Order that is not linked to a Noon order
		frappe.db.set_value("Sales Order", fx.name, "custom_marketplace_order", None)
		note, enqueue = self._draft_note(fx)
		enqueue.assert_not_called()
		saved = self._note(note.name)
		self.assertFalse(saved.custom_carrier_status)
		saved.items[0].qty = 1
		saved.save()
		saved.submit()
		self.assertEqual(saved.docstatus, 1)

	def test_a_stock_user_creates_retries_and_submits(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A", "SKU-A"])
		# Stock User is the native right the flow relies on. On this site ERPNext also checks read on
		# the party's default Account when a Delivery Note is saved, which a Stock User lacks, so the
		# test user gets that single extra right (an environment finding, see the report).
		email, _role = make_role_user(self, "Stock Test", {"Account": ["read"]})
		frappe.get_doc("User", email).add_roles("Stock User")
		frappe.db.commit()
		note, enqueue = self._draft_note(fx, user=email)
		enqueue.assert_called_once()
		self.assertEqual(self._note(note.name).owner, email)

		# the worker writes the Noon records as the system user: no Marketplace Order rights needed
		self._run_worker(note.name, self._response(404), self._response(200, awb="A-1"), self._response(500, message="x"))
		self.assertEqual(self._note(note.name).custom_carrier_status, "Error")
		frappe.set_user(email)
		try:
			with patch("frappe.enqueue"):
				noon_api.retry_delivery_note_shipment(note.name)
		finally:
			frappe.set_user("Administrator")
		self._run_worker(note.name, self._response(404), self._response(200))
		frappe.set_user(email)
		try:
			doc = self._note(note.name)
			doc.submit()
			self.assertEqual(doc.docstatus, 1)
		finally:
			frappe.set_user("Administrator")

	# ---- old paths, seam, patch ------------------------------------------------------------

	def test_the_old_endpoints_refuse_and_change_nothing(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		with self.assertRaises(frappe.ValidationError):
			noon_api.create_shipment(fx.marketplace_order.name, ["1"], "Noon")
		with self.assertRaises(frappe.ValidationError):
			noon_api.mark_handed_over(fx.marketplace_order.name, "X-1")
		order = self._order_doc(fx)
		self.assertEqual(len(order.noon_shipments), 0)
		self.assertEqual(fx.delivery_notes(), [])

	def test_the_carrier_seam_is_a_plain_dict_holding_noon(self):
		# Bosta joined the seam in epic #413; Noon's own adapter is unchanged.
		self.assertIs(carrier_shipments.CARRIERS["Noon"], dn_shipment)
		self.assertEqual(list(carrier_shipments.CARRIERS), ["Noon", "Bosta"])
		self.assertIs(type(carrier_shipments.CARRIERS), dict)

	def test_the_queue_handler_really_reaches_queue_and_never_raises(self):
		doc = frappe._dict(name="DN-X", items=[], flags=frappe._dict())
		adapter = MagicMock()
		adapter.applies_to.return_value = True
		adapter.queue.side_effect = RuntimeError("boom")
		with patch.dict(carrier_shipments.CARRIERS, {"Noon": adapter}), patch("frappe.log_error") as log_error:
			carrier_shipments.queue_shipment(doc)  # does not raise
		adapter.queue.assert_called_once_with(doc)
		log_error.assert_called_once()

	def test_the_transporter_patch_is_idempotent_and_never_flips_an_existing_supplier(self):
		name = dn_shipment.transporter_name() or (create_noon_transporter_supplier.execute() or dn_shipment.transporter_name())
		original = frappe.db.get_value("Supplier", name, "is_transporter")
		# later commits in this class would make the change below permanent: always restore it
		self.addCleanup(lambda: (frappe.db.set_value("Supplier", name, "is_transporter", original), frappe.db.commit()))
		frappe.db.set_value("Supplier", name, "is_transporter", 1)
		create_noon_transporter_supplier.execute()
		create_noon_transporter_supplier.execute()
		self.assertEqual(frappe.db.count("Supplier", {"supplier_name": "Noon"}), 1)
		self.assertEqual(frappe.db.get_value("Supplier", name, "is_transporter"), 1)
		# an existing supplier that is not a transporter is reported, not changed
		frappe.db.set_value("Supplier", name, "is_transporter", 0)
		with patch("frappe.log_error") as log_error:
			create_noon_transporter_supplier.execute()
		log_error.assert_called_once()
		self.assertEqual(frappe.db.get_value("Supplier", name, "is_transporter"), 0)
		self.assertEqual(frappe.db.count("Supplier", {"supplier_name": "Noon"}), 1)

	def test_the_transporter_is_the_resolved_supplier_name_not_a_literal(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		other = frappe.get_doc(
			{"doctype": "Supplier", "supplier_name": f"Resolved {frappe.generate_hash(length=6)}",
			 "supplier_group": "All Supplier Groups", "is_transporter": 1}
		).insert()
		self.addCleanup(lambda: (frappe.delete_doc("Supplier", other.name, force=True), frappe.db.commit()))
		with patch.object(dn_shipment, "transporter_name", return_value=other.name):
			self._run_worker(note.name, *self._happy_responses())
		self.assertEqual(self._note(note.name).transporter, other.name)

	# ---- review fixes: old-flow notes, stale Pending, retry routing, locks, lr_no ------------

	def _old_flow_note(self, fx, status="Created", extra_note=None):
		"""A Noon Delivery Note the old flow made (skip flag: no carrier status) tracked by a shipment row."""
		note = make_delivery_note(fx.name)
		note.flags.skip_carrier_shipment = True
		note.insert()
		order = self._order_doc(fx)
		nr = f"{order.external_order_id}-1"
		row = order.append(
			"noon_shipments",
			{"integration_shipment_nr": nr, "status": status, "awb_source": "Noon", "handed_over_on": frappe.utils.now_datetime()},
		)
		row.delivery_note = extra_note or note.name
		if extra_note:
			row.delivery_notes = frappe.as_json([extra_note, note.name])
		order.save()
		frappe.db.commit()
		return note

	def test_a_second_note_of_an_old_flow_shipment_can_be_submitted(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A", "SKU-A"])
		first = self._old_flow_note(fx)
		second = make_delivery_note(fx.name)
		second.flags.skip_carrier_shipment = True
		second.insert()
		order = self._order_doc(fx)
		order.noon_shipments[0].delivery_notes = frappe.as_json([first.name, second.name])
		order.save()
		frappe.db.commit()
		self.assertEqual(self._note(second.name).custom_carrier_status or "", "")
		self._note(second.name).submit()  # matched through delivery_notes; Created shipment
		self.assertEqual(self._note(second.name).docstatus, 1)

	def test_an_old_flow_note_of_a_shipment_that_is_not_created_is_still_refused(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._old_flow_note(fx, status="Error")
		with self.assertRaises(frappe.ValidationError):
			self._note(note.name).submit()

	def test_a_noon_note_from_before_this_flow_stays_submittable(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = make_delivery_note(fx.name)
		note.flags.skip_carrier_shipment = True
		note.insert()  # no carrier status, no shipment row: as notes made before the flow existed
		doc = self._note(note.name)
		self.assertFalse(doc.custom_carrier_status)
		doc.submit()
		self.assertEqual(doc.docstatus, 1)

	def test_a_stuck_pending_note_can_be_retried_after_the_threshold_only(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		with patch("frappe.enqueue") as enqueue, self.assertRaises(frappe.ValidationError):
			dn_shipment.retry(note.name)  # just created: still Pending and fresh
		enqueue.assert_not_called()

		stale = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-(dn_shipment.STALE_PENDING_MINUTES + 1))
		frappe.db.set_value("Delivery Note", note.name, "modified", stale, update_modified=False)
		frappe.db.commit()
		with patch("frappe.enqueue") as enqueue:
			result = noon_api.retry_delivery_note_shipment(note.name)
		self.assertTrue(result["queued"])
		kwargs = enqueue.call_args.kwargs
		self.assertTrue(kwargs["deduplicate"])
		self.assertIn(note.name, kwargs["job_id"])  # same per-note id: a running job is not doubled
		# the clock restarted, so an immediate second retry is refused
		with patch("frappe.enqueue"), self.assertRaises(frappe.ValidationError):
			dn_shipment.retry(note.name)

	def test_a_double_run_never_creates_a_second_shipment(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		self._run_worker(note.name, self._response(404), self._response(200, awb="A-1"), self._response(500, message="boom"))
		# two retries run one after the other (as two jobs would): GetShipment finds nothing, then
		# CreateShipment works; the second run finds the note's Created shipment and calls nothing
		self._run_worker(note.name, self._response(404), self._response(200))
		client = self._run_worker(note.name)
		client.request.assert_not_called()
		self.assertEqual(len(self._order_doc(fx).noon_shipments), 1)

	def test_a_failure_inside_the_error_handler_still_ends_the_note_in_error(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		with patch.object(dn_shipment, "_fail", side_effect=RuntimeError("cannot log")):
			self._run_worker(note.name)
		saved = self._note(note.name)
		self.assertEqual(saved.custom_carrier_status, "Error")
		self.assertIn("only 1 are available", html.unescape(saved.custom_carrier_error))

	def test_sales_order_retry_of_a_note_shipment_goes_through_the_note(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		self._run_worker(note.name, self._response(404), self._response(200, awb="A-1"), self._response(500, message="boom"))
		order = self._order_doc(fx)
		nr = order.noon_shipments[0].integration_shipment_nr
		with patch("frappe.enqueue") as enqueue:
			result = fulfillment.retry(order.name, nr)
		self.assertTrue(result["queued"])
		enqueue.assert_called_once()
		self.assertIs(enqueue.call_args.args[0], dn_shipment.run)
		self.assertIn(note.name, enqueue.call_args.kwargs["job_id"])
		self.assertEqual(self._note(note.name).custom_carrier_status, "Pending")
		# the shared job then writes the result back to the note
		self._run_worker(note.name, self._response(404), self._response(200))
		self.assertEqual(self._note(note.name).custom_carrier_status, "Created")

	def _lock_order(self, action):
		original = frappe.db.get_value
		locked = []

		def spy(doctype, *args, **kwargs):
			if kwargs.get("for_update") and doctype in ("Delivery Note", "Marketplace Order"):
				locked.append(doctype)
			return original(doctype, *args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=spy):
			action()
		return locked

	def test_locks_are_taken_delivery_note_first_in_the_worker_and_on_delete(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		locked = self._lock_order(lambda: self._run_worker(note.name, *self._happy_responses()))
		self.assertEqual(locked[:2], ["Delivery Note", "Marketplace Order"])

		frappe.db.set_value("Marketplace Order Shipment", {"parent": fx.marketplace_order.name}, "status", "Cancelled")
		locked = self._lock_order(lambda: frappe.delete_doc("Delivery Note", note.name, force=True))
		self.assertIn("Marketplace Order", locked)
		self.assertEqual(locked[0], "Delivery Note")
		self.assertLess(locked.index("Delivery Note"), locked.index("Marketplace Order"))
		frappe.db.commit()

	def test_the_awb_is_locked_once_the_shipment_exists_but_not_before(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		self._run_worker(note.name)  # refused (too few units): no shipment row yet
		doc = self._note(note.name)
		doc.lr_no = "STICKER-EDIT"
		doc.save()  # still editable

		fx2 = self._order([("SKU-B", 1)], ["SKU-B"])
		created, _ = self._draft_note(fx2)
		self._run_worker(created.name, *self._happy_responses())
		doc = self._note(created.name)
		doc.lr_no = "SOMETHING-ELSE"
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	# ---- #393: the note carries cancel, delete, Add AWB, invoice, Retry ------------------------

	def _created_note(self, fx, submit=False):
		note, _ = self._draft_note(fx)
		self._run_worker(note.name, *self._happy_responses())
		if submit:
			self._note(note.name).submit()
		return note

	def _shipment_row(self, fx):
		return self._order_doc(fx).noon_shipments[0]

	def test_cancel_and_delete_are_refused_while_the_noon_shipment_is_live(self):
		fx = self._order([("SKU-A", 1), ("SKU-B", 1)], ["SKU-A", "SKU-B"])
		draft = self._created_note(fx)
		with self.assertRaises(frappe.ValidationError) as caught:
			frappe.delete_doc("Delivery Note", draft.name, force=True)
		self.assertIn("must be cancelled first", str(caught.exception))
		self.assertIn("Cancel Noon Shipment", str(caught.exception))
		self.assertTrue(frappe.db.exists("Delivery Note", draft.name))

		fx2 = self._order([("SKU-C", 1)], ["SKU-C"])
		submitted = self._created_note(fx2, submit=True)
		with self.assertRaises(frappe.ValidationError):
			self._note(submitted.name).cancel()
		self.assertEqual(self._note(submitted.name).docstatus, 1)
		self.assertEqual(self._shipment_row(fx2).status, "Created")

	def test_a_shipment_that_never_reached_noon_is_discarded_and_the_note_goes_on(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		order = self._order_doc(fx)
		nr = f"{order.external_order_id}-1"
		order.append("noon_shipments", {"integration_shipment_nr": nr, "status": "Pending", "awb_source": "Noon", "delivery_note": note.name})
		order.save()
		frappe.db.commit()
		frappe.delete_doc("Delivery Note", note.name, force=True)
		self.assertFalse(frappe.db.exists("Delivery Note", note.name))
		self.assertEqual(self._shipment_row(fx).status, "Discarded")
		frappe.db.commit()

	def test_an_error_shipment_with_an_awb_may_exist_on_noon_and_blocks_the_delete(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		self._run_worker(note.name, self._response(404), self._response(200, awb="A-1"), self._response(500, message="x"))
		self.assertEqual(self._shipment_row(fx).status, "Error")
		with self.assertRaises(frappe.ValidationError) as caught:
			frappe.delete_doc("Delivery Note", note.name, force=True)
		self.assertIn("may already exist on Noon", str(caught.exception))
		self.assertIn("Discard Noon Shipment", str(caught.exception))

	def _run_cancel(self, fx, note, status_code=200, message=None):
		order = self._order_doc(fx)
		nr = order.noon_shipments[0].integration_shipment_nr
		with patch(_CLIENT) as client_class:
			client = client_class.return_value
			client.request.return_value = self._response(status_code, message=message)
			client.error_message.side_effect = lambda response: response.message
			dn_shipment.run_cancel(order.name, nr, note.name)
		return client

	def test_cancelling_the_note_cancels_the_noon_shipment_first_then_the_note_goes_on(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx, submit=True)
		with patch("frappe.enqueue") as enqueue:
			state = noon_api.cancel_delivery_note_shipment(note.name)
		self.assertEqual(state["state"], "cancelling")
		self.assertEqual(self._shipment_row(fx).status, "Cancel Pending")
		kwargs = enqueue.call_args.kwargs
		self.assertIs(enqueue.call_args.args[0], dn_shipment.run_cancel)
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(noon_api.get_delivery_note_cancel_state(note.name)["state"], "cancelling")

		client = self._run_cancel(fx, note)
		self.assertEqual(client.request.call_args.args[1], fulfillment.SHIPMENT_CANCEL_PATH)
		self.assertEqual(self._shipment_row(fx).status, "Cancelled")
		self.assertEqual(noon_api.get_delivery_note_cancel_state(note.name)["state"], "done")
		doc = self._note(note.name)
		doc.cancel()  # the note's own cancel now goes through
		self.assertEqual(doc.docstatus, 2)

	def test_a_noon_refusal_stops_the_flow_and_leaves_the_note_unchanged(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx, submit=True)
		with patch("frappe.enqueue"):
			noon_api.cancel_delivery_note_shipment(note.name)
		self._run_cancel(fx, note, status_code=400, message="Shipment already picked up")
		state = noon_api.get_delivery_note_cancel_state(note.name)
		self.assertEqual(state, {"state": "error", "message": "Shipment already picked up"})
		self.assertEqual(self._note(note.name).docstatus, 1)
		with self.assertRaises(frappe.ValidationError):
			self._note(note.name).cancel()
		# a new attempt clears the old error and queues again
		with patch("frappe.enqueue") as enqueue:
			state = noon_api.cancel_delivery_note_shipment(note.name)
		self.assertEqual(state["state"], "cancelling")
		enqueue.assert_called_once()

	def test_a_cancelled_draft_note_can_get_a_new_shipment_with_retry(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		with patch("frappe.enqueue"):
			noon_api.cancel_delivery_note_shipment(note.name)
		self._run_cancel(fx, note)
		saved = self._note(note.name)
		self.assertEqual(saved.custom_carrier_status, "Cancelled")
		self.assertFalse(saved.custom_carrier_error)
		self.assertFalse(saved.lr_no)
		with patch("frappe.enqueue"):
			noon_api.retry_delivery_note_shipment(note.name)
		self._run_worker(note.name, *self._happy_responses("NOON-AWB-2"))
		rows = self._order_doc(fx).noon_shipments
		self.assertEqual([r.status for r in rows], ["Cancelled", "Created"])
		self.assertEqual(self._note(note.name).lr_no, "NOON-AWB-2")

	def test_the_cancel_endpoint_needs_the_note_permission(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		email, _role = make_role_user(self, "DN Reader Cancel", {"Delivery Note": ["read"]})
		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				noon_api.cancel_delivery_note_shipment(note.name)
			with self.assertRaises(frappe.PermissionError):
				noon_api.add_delivery_note_awb(note.name, "X")
			with self.assertRaises(frappe.PermissionError):
				noon_api.upload_delivery_note_invoice(note.name, "/private/files/x.pdf", "I-1", "2026-10-29")
			with self.assertRaises(frappe.PermissionError):
				noon_api.retry_delivery_note_awb_or_invoice(note.name)
		finally:
			frappe.set_user("Administrator")

	def test_a_sales_order_cannot_be_cancelled_while_a_draft_note_has_a_live_shipment(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		so = frappe.get_doc("Sales Order", fx.name)
		with self.assertRaises(frappe.ValidationError) as caught:
			so.cancel()
		self.assertIn(note.name, str(caught.exception))
		self.assertEqual(frappe.db.get_value("Sales Order", fx.name, "docstatus"), 1)

	def test_an_amended_note_gets_a_new_shipment_only_after_the_previous_one_is_cancelled(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A", "SKU-A"])
		first = self._created_note(fx, submit=True)
		frappe.db.set_value("Marketplace Order Shipment", {"parent": fx.marketplace_order.name}, "status", "Cancelled")
		self._note(first.name).cancel()
		# a legacy cancel that left the shipment live
		frappe.db.set_value("Marketplace Order Shipment", {"parent": fx.marketplace_order.name}, "status", "Created")
		amended = frappe.copy_doc(self._note(first.name))
		amended.amended_from = first.name
		amended.docstatus = 0
		with patch("frappe.enqueue"):
			amended.insert()
		frappe.db.commit()
		self._run_worker(amended.name)
		saved = self._note(amended.name)
		self.assertEqual(saved.custom_carrier_status, "Error")
		self.assertIn("not Cancelled yet", html.unescape(saved.custom_carrier_error))
		self.assertEqual(len(self._order_doc(fx).noon_shipments), 1)

		frappe.db.set_value("Marketplace Order Shipment", {"parent": fx.marketplace_order.name}, "status", "Cancelled")
		self._run_worker(amended.name, *self._happy_responses("NOON-AWB-3"))
		self.assertEqual(self._note(amended.name).custom_carrier_status, "Created")
		self.assertEqual(len(self._order_doc(fx).noon_shipments), 2)

	def _stock_user(self, label):
		"""A real Stock User; ERPNext also wants read on the party's Account when a note is saved (see
		test_a_stock_user_creates_retries_and_submits). No Marketplace Order rights at all."""
		email, _role = make_role_user(self, label, {"Account": ["read"]})
		frappe.get_doc("User", email).add_roles("Stock User")
		frappe.db.commit()
		return email

	def test_a_stock_user_runs_cancel_add_awb_invoice_and_retry_from_the_note(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx, submit=True)
		email = self._stock_user("Stock Noon Actions")
		self.assertFalse(frappe.has_permission("Marketplace Order", "write", user=email))
		frappe.set_user(email)
		try:
			with patch("frappe.enqueue") as enqueue:
				self.assertTrue(noon_api.add_delivery_note_awb(note.name, "REPL-AWB-1")["queued"])
			enqueue.assert_called_once()
			summary = noon_api.get_delivery_note_shipment(note.name)
			# the real invoice path; only the PDF content check is replaced
			with patch.object(fulfillment, "_validate_invoice_file"), patch("frappe.enqueue") as enqueue:
				self.assertTrue(
					noon_api.upload_delivery_note_invoice(note.name, "/private/files/i.pdf", "INV-1", "2026-10-29")["queued"]
				)
			enqueue.assert_called_once()
			with patch("frappe.enqueue") as enqueue:
				noon_api.retry_delivery_note_awb_or_invoice(note.name)
			enqueue.assert_called_once()
			# cancelling the submitted note's shipment (permission "cancel" on the note)
			with patch("frappe.enqueue") as enqueue:
				state = noon_api.cancel_delivery_note_shipment(note.name)
			self.assertEqual(state["state"], "cancelling")
		finally:
			frappe.set_user("Administrator")
		self.assertFalse(frappe.flags.ignore_permissions)  # nothing global was left switched on
		self.assertIn("REPL-AWB-1", [a["awb_nr"] for a in summary["awbs"]])
		row = self._shipment_row(fx)
		self.assertEqual((row.invoice_nr, row.invoice_status), ("INV-1", "Pending"))
		self.assertEqual(self._note(note.name).lr_no, "NOON-AWB-1")  # the lr_no lock keeps the real AWB

	def test_the_sales_order_no_longer_offers_add_awb_or_invoice_for_a_shipment_with_a_note(self):
		fx = self._order([("SKU-A", 1), ("SKU-B", 1)], ["SKU-A", "SKU-B"])
		self._created_note(fx)
		order = self._order_doc(fx)
		actions = fulfillment.get_available_actions(order)["actions"]
		for gone in ("add_awb", "upload_invoice"):
			self.assertNotIn(gone, actions)
		self.assertIn("upload_order_invoice", actions)
		# an old-flow shipment without a note keeps them
		order.append("noon_shipments", {"integration_shipment_nr": "X-2", "status": "Created", "awb_source": "Noon"})
		order.save()
		actions = fulfillment.get_available_actions(order)["actions"]
		self.assertIn("add_awb", actions)
		self.assertIn("upload_invoice", actions)
		# the endpoints refuse the noted shipment
		with self.assertRaises(frappe.ValidationError):
			noon_api.add_awb(order.name, order.noon_shipments[0].integration_shipment_nr, "noon", "Z-1")

	def test_a_stuck_error_shipment_can_be_discarded_from_the_note_and_the_note_then_deletes(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		self._run_worker(note.name, self._response(404), self._response(200, awb="A-1"), self._response(400, message="rejected"))
		self.assertEqual(self._shipment_row(fx).status, "Error")
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Delivery Note", note.name, force=True)

		with patch(_CLIENT) as client_class:
			result = noon_api.discard_delivery_note_shipment(note.name)
		client_class.return_value.request.assert_not_called()  # no Noon call
		self.assertTrue(result["discarded"])
		self.assertEqual(self._shipment_row(fx).status, "Discarded")
		saved = self._note(note.name)
		self.assertEqual(saved.custom_carrier_status, "Cancelled")
		self.assertFalse(saved.lr_no)
		# the summary no longer offers a shipment to discard
		self.assertIsNone(noon_api.get_delivery_note_shipment(note.name))
		# units are released: Retry creates a new shipment
		with patch("frappe.enqueue"):
			noon_api.retry_delivery_note_shipment(note.name)
		self._run_worker(note.name, *self._happy_responses("NOON-AWB-7"))
		self.assertEqual([r.status for r in self._order_doc(fx).noon_shipments], ["Discarded", "Created"])
		# and a discarded one lets the note be deleted
		frappe.db.set_value("Marketplace Order Shipment", {"parent": fx.marketplace_order.name}, "status", "Discarded")
		frappe.delete_doc("Delivery Note", note.name, force=True)
		self.assertFalse(frappe.db.exists("Delivery Note", note.name))
		frappe.db.commit()

	def test_a_created_shipment_cannot_be_discarded_from_the_note(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		with self.assertRaises(frappe.ValidationError) as caught:
			noon_api.discard_delivery_note_shipment(note.name)
		self.assertIn("Created shipment must be cancelled", str(caught.exception))
		self.assertEqual(self._shipment_row(fx).status, "Created")

	def test_discard_needs_write_permission_on_the_note(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		self._run_worker(note.name, self._response(404), self._response(200, awb="A-1"), self._response(400, message="x"))
		email, _role = make_role_user(self, "DN Reader Discard", {"Delivery Note": ["read"]})
		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				noon_api.discard_delivery_note_shipment(note.name)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self._shipment_row(fx).status, "Error")

	def test_the_sales_order_still_refuses_to_cancel_or_discard_a_noted_shipment(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		self._run_worker(note.name, self._response(404), self._response(200, awb="A-1"), self._response(400, message="x"))
		order = self._order_doc(fx)
		nr = order.noon_shipments[0].integration_shipment_nr
		with self.assertRaises(frappe.ValidationError):
			fulfillment.discard_shipment(order.name, nr)
		self.assertEqual(self._shipment_row(fx).status, "Error")
		frappe.db.set_value("Marketplace Order Shipment", {"parent": order.name}, "status", "Created")
		with self.assertRaises(frappe.ValidationError):
			fulfillment.cancel_shipment(order.name, nr)

	# ---- review fixes (#391/#393) -----------------------------------------------------------

	def test_clearing_the_carrier_status_does_not_open_the_guard(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		with self.assertRaises(frappe.ValidationError):  # the status is locked once a shipment exists
			frappe.client.set_value("Delivery Note", note.name, "custom_carrier_status", "")
		# even if it were cleared underneath, the guard decides from the shipment rows
		frappe.db.set_value("Delivery Note", note.name, "custom_carrier_status", "")
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Delivery Note", note.name, force=True)
		with patch("frappe.enqueue"):
			self.assertEqual(noon_api.cancel_delivery_note_shipment(note.name)["state"], "cancelling")
		frappe.db.set_value("Delivery Note", note.name, "custom_carrier_status", "Created")

	def test_an_old_flow_handed_over_note_keeps_its_sales_order_actions_and_is_not_guarded(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A", "SKU-A"])
		note = self._old_flow_note(fx)
		order = self._order_doc(fx)
		actions = fulfillment.get_available_actions(order)["actions"]
		self.assertIn("add_awb", actions)
		self.assertIn("upload_invoice", actions)
		with patch("frappe.enqueue"):
			noon_api.add_awb(order.name, order.noon_shipments[0].integration_shipment_nr, "noon", "OLD-AWB-1")
		doc = self._note(note.name)
		doc.items[0].qty = 1
		doc.save()  # no items lock for an old-flow note
		frappe.delete_doc("Delivery Note", note.name, force=True)  # no guard for an old-flow note
		self.assertFalse(frappe.db.exists("Delivery Note", note.name))
		frappe.db.commit()

	def test_discard_is_refused_while_a_retry_is_in_flight(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		self._run_worker(note.name, self._response(404), self._response(200, awb="A-1"), self._response(400, message="x"))
		frappe.db.set_value("Delivery Note", note.name, "custom_carrier_status", "Pending")
		with self.assertRaises(frappe.ValidationError) as caught:
			noon_api.discard_delivery_note_shipment(note.name)
		self.assertIn("retry is in progress", str(caught.exception))
		frappe.db.set_value("Delivery Note", note.name, "custom_carrier_status", "Error")
		with patch.object(dn_shipment, "is_job_enqueued", return_value=True), self.assertRaises(frappe.ValidationError):
			noon_api.discard_delivery_note_shipment(note.name)
		self.assertEqual(self._shipment_row(fx).status, "Error")

	def test_a_discard_by_the_guard_logs_discard_shipment_success(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		order = self._order_doc(fx)
		nr = f"{order.external_order_id}-1"
		order.append("noon_shipments", {"integration_shipment_nr": nr, "status": "Pending", "awb_source": "Noon", "delivery_note": note.name})
		order.save()
		frappe.db.commit()
		frappe.delete_doc("Delivery Note", note.name, force=True)
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name, "status": "Success"},
			fields=["request_data"],
		)
		self.assertTrue(any("DiscardShipment" in frappe.utils.cstr(log.request_data) for log in logs))
		frappe.db.commit()

	def test_a_sales_order_retry_of_a_cancel_pending_note_shipment_uses_the_note_cancel_job(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		with patch("frappe.enqueue"):
			noon_api.cancel_delivery_note_shipment(note.name)
		order = self._order_doc(fx)
		nr = order.noon_shipments[0].integration_shipment_nr
		self.assertEqual(order.noon_shipments[0].status, "Cancel Pending")
		with patch("frappe.enqueue") as enqueue:
			fulfillment.retry(order.name, nr)
		self.assertIs(enqueue.call_args.args[0], dn_shipment.run_cancel)
		# a Sales Order retry of a Pending/Error note shipment also needs write on the note
		frappe.db.set_value("Marketplace Order Shipment", {"parent": order.name}, "status", "Error")
		email, _role = make_role_user(self, "MO Writer No DN", {"Marketplace Order": ["write"]})
		frappe.get_doc("User", email).add_roles("Noon User")
		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				noon_api.retry(order.name, nr)
		finally:
			frappe.set_user("Administrator")

	def test_cancel_on_a_fresh_pending_note_is_refused_but_a_stale_one_may_go_on(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note, _ = self._draft_note(fx)  # Pending, no shipment row yet
		with self.assertRaises(frappe.ValidationError) as caught:
			noon_api.cancel_delivery_note_shipment(note.name)
		self.assertIn("still being created", str(caught.exception))
		stale = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-(dn_shipment.STALE_PENDING_MINUTES + 1))
		frappe.db.set_value("Delivery Note", note.name, "modified", stale, update_modified=False)
		self.assertEqual(noon_api.cancel_delivery_note_shipment(note.name)["state"], "done")

	# ---- held findings: 4xx refusal, Cancel Pending lock, Cancelled status ---------------------

	def test_a_definite_noon_refusal_puts_the_shipment_back_to_created_and_the_note_keeps_working(self):
		fx = self._order([("SKU-A", 1)], ["SKU-A"])
		note = self._created_note(fx)
		with patch("frappe.enqueue"):
			noon_api.cancel_delivery_note_shipment(note.name)
		self._run_cancel(fx, note, status_code=400, message="Already picked up")
		row = self._shipment_row(fx)
		self.assertEqual((row.status, row.last_error), ("Created", "Already picked up"))
		self.assertEqual(
			noon_api.get_delivery_note_cancel_state(note.name), {"state": "error", "message": "Already picked up"}
		)
		self._note(note.name).submit()  # still submittable
		self.assertEqual(noon_api.get_delivery_note_shipment(note.name)["status"], "Created")
		# a new attempt clears the error
		with patch("frappe.enqueue"):
			self.assertEqual(noon_api.cancel_delivery_note_shipment(note.name)["state"], "cancelling")

	def test_a_server_error_or_rate_limit_leaves_cancel_pending(self):
		for status_code in (500, 429):
			with self.subTest(status_code=status_code):
				sku = f"SKU-{status_code}"
				fx = self._order([(sku, 1)], [sku])
				note = self._created_note(fx)
				with patch("frappe.enqueue"):
					noon_api.cancel_delivery_note_shipment(note.name)
				self._run_cancel(fx, note, status_code=status_code, message="try later")
				self.assertEqual(self._shipment_row(fx).status, "Cancel Pending")
				self.assertEqual(noon_api.get_delivery_note_cancel_state(note.name)["state"], "error")

	def test_everything_is_locked_and_submit_refused_while_cancel_pending(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A", "SKU-A"])
		note = self._created_note(fx)
		with patch("frappe.enqueue"):
			noon_api.cancel_delivery_note_shipment(note.name)
		self.assertEqual(self._shipment_row(fx).status, "Cancel Pending")
		for field, value in (("lr_no", "OTHER"), ("custom_carrier_status", "Error")):
			doc = self._note(note.name)
			doc.set(field, value)
			with self.assertRaises(frappe.ValidationError, msg=field):
				doc.save()
		doc = self._note(note.name)
		doc.items[0].qty = 1
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		with self.assertRaises(frappe.ValidationError):
			self._note(note.name).submit()
		# a stale create job must not make a second shipment while it is being cancelled
		with self.assertRaises(frappe.ValidationError):  # retry needs Error, Cancelled or stale Pending
			dn_shipment.retry(note.name)
		client = self._run_worker(note.name)
		client.request.assert_not_called()
		self.assertEqual(len(self._order_doc(fx).noon_shipments), 1)

	def test_a_cancelled_note_shows_cancelled_not_error_and_is_not_in_needs_attention(self):
		fx = self._order([("SKU-A", 2)], ["SKU-A"])
		note, _ = self._draft_note(fx)
		self._run_worker(note.name)  # refused: too few units -> Error + a logged refusal
		filters = {"platform": "Noon", "account": self.account.name}
		self.assertEqual(
			len([i for i in marketplace_attention.get_attention_items(filters) if i["problem_type"] == "Fulfillment Error"]), 1
		)
		frappe.db.set_value("Delivery Note", note.name, "custom_carrier_status", "Cancelled")
		self.assertEqual(
			[i for i in marketplace_attention.get_attention_items(filters) if i["problem_type"] == "Fulfillment Error"], []
		)
