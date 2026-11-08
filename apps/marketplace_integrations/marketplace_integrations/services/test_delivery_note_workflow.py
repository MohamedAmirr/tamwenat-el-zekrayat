# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The Delivery Note Lifecycle Workflow (epic #502, issue #498): the status mapping, the shipped Workflow
definition, the migration backfill and the transitions on a real database."""

import json
from pathlib import Path

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0 import create_delivery_note_lifecycle_workflow as patch
from marketplace_integrations.services import carrier_shipments
from marketplace_integrations.services import delivery_note_workflow as lifecycle
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin

SPEC = Path(__file__).resolve().parent.parent / "workflow" / "delivery_note_lifecycle.json"


class TestStatusMapping(IntegrationTestCase):
	def test_draft_stages_follow_the_integration_status(self):
		for integration, stage in (
			("", lifecycle.ORDER_RECEIVED),
			("Not Created", lifecycle.ORDER_RECEIVED),
			("Creation Failed", lifecycle.ORDER_RECEIVED),
			("Creating", lifecycle.SHIPMENT_REQUESTED),
			("Creation Unknown", lifecycle.SHIPMENT_REQUESTED),
			("Created", lifecycle.SHIPMENT_CREATED),
			("Cancellation Pending", lifecycle.SHIPMENT_CREATED),
		):
			self.assertEqual(lifecycle.state_for(0, integration), stage, integration)

	def test_submitted_stages_follow_the_shipping_status(self):
		self.assertEqual(lifecycle.state_for(1), lifecycle.DELIVERED)
		self.assertEqual(lifecycle.state_for(1, "Created", "Delivered"), lifecycle.DELIVERED)
		self.assertEqual(lifecycle.state_for(1, "Created", "Returned to business"), lifecycle.RETURNED_FAILED)
		self.assertEqual(lifecycle.state_for(1, "Created", "Fulfilled"), lifecycle.IN_TRANSIT)
		self.assertEqual(lifecycle.state_for(1, "Created", ""), lifecycle.PICKED_UP)
		self.assertEqual(lifecycle.state_for(1, "", "", is_return=1), lifecycle.RETURNED_FAILED)
		self.assertEqual(lifecycle.state_for(2, "Created", "Delivered"), lifecycle.CANCELLED)

	def test_every_listed_shipping_status_is_a_real_select_option(self):
		options = (frappe.get_meta("Delivery Note").get_field("custom_shipping_status").options or "").split("\n")
		listed = (
			lifecycle.DELIVERED_STATUSES
			+ lifecycle.NOON_RETURNED_FAILED_STATUSES
			+ lifecycle.NOON_IN_TRANSIT_STATUSES
		)
		self.assertFalse([status for status in listed if status not in options])

	def test_a_submitted_note_never_gets_a_draft_only_stage(self):
		# Bosta statuses that map to Shipment Created (a draft stage) give a submitted note at least Picked Up
		for status in ("Pickup requested", "Waiting for route", "Route Assigned", "Picking up from consignee", "Picking up"):
			self.assertEqual(lifecycle.state_for(1, "Created", status), lifecycle.PICKED_UP, status)
			self.assertEqual(lifecycle.STATE_DOCSTATUS[lifecycle.state_for(1, "Created", status)], 1)
		# a draft keeps the draft stage
		self.assertEqual(lifecycle.state_for(0, "Created", "Route Assigned"), lifecycle.SHIPMENT_CREATED)

	def test_an_untouched_draft_keeps_waiting_for_goods(self):
		row = {"docstatus": 0, "custom_integration_status": "Not Created", lifecycle.FIELD: lifecycle.WAITING_FOR_GOODS}
		self.assertEqual(lifecycle.state_of_row(row), lifecycle.WAITING_FOR_GOODS)
		self.assertEqual(
			lifecycle.state_of_row(row, {"custom_integration_status": "Creating"}), lifecycle.SHIPMENT_REQUESTED
		)


class TestShippedWorkflow(IntegrationTestCase):
	def setUp(self):
		self.spec = json.loads(SPEC.read_text())

	def test_every_state_docstatus_matches_the_mapping(self):
		states = {row["state"]: int(row["doc_status"]) for row in self.spec["states"]}
		self.assertEqual(states, lifecycle.STATE_DOCSTATUS)

	def test_create_shipment_condition_names_every_carrier(self):
		condition = next(t["condition"] for t in self.spec["transitions"] if t["action"] == "Create Shipment")
		for adapter in carrier_shipments.CARRIERS.values():
			self.assertIn(adapter.SHIPPING_CARRIER, condition)

	def test_confirm_shipment_is_not_offered_for_a_carrier_that_submits_at_pickup(self):
		"""``require_shipment_created`` refuses a hand submit of such a note, so the action must not be offered."""
		condition = next(t["condition"] for t in self.spec["transitions"] if t["action"] == "Confirm Shipment")
		for adapter in carrier_shipments.CARRIERS.values():
			if getattr(adapter, "SUBMIT_AT_PICKUP", False):
				self.assertIn(f'doc.transporter_name != "{adapter.SHIPPING_CARRIER}"', condition)

	def test_only_stock_managers_cancel(self):
		cancel = [t for t in self.spec["transitions"] if t["action"] == "Cancel"]
		self.assertTrue(cancel)
		self.assertEqual({t["allowed"] for t in cancel}, {"Stock Manager"})


class TestPatch(BostaFixtureMixin, IntegrationTestCase):
	def test_backfill_sets_stages_without_touching_document_status_or_modified(self):
		note = self.make_note()
		before = frappe.db.get_value("Delivery Note", note.name, ["docstatus", "modified"], as_dict=True)
		frappe.db.set_value("Delivery Note", note.name, lifecycle.FIELD, None, update_modified=False)
		patch.backfill_states()
		after = frappe.db.get_value("Delivery Note", note.name, ["docstatus", "modified", lifecycle.FIELD], as_dict=True)
		self.assertEqual(after[lifecycle.FIELD], lifecycle.ORDER_RECEIVED)
		self.assertEqual((after.docstatus, after.modified), (before.docstatus, before.modified))
		patch.backfill_states()  # idempotent
		self.assertEqual(
			frappe.db.get_value("Delivery Note", note.name, lifecycle.FIELD), lifecycle.ORDER_RECEIVED
		)

	def test_install_is_idempotent(self):
		patch.execute()
		patch.execute()
		self.assertEqual(frappe.db.count("Workflow", {"document_type": "Delivery Note", "is_active": 1}), 1)


class TestTransitions(BostaFixtureMixin, IntegrationTestCase):
	def test_a_new_note_starts_at_order_received(self):
		note = self.make_note()
		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, lifecycle.FIELD), lifecycle.ORDER_RECEIVED)

	def test_create_shipment_moves_a_bosta_note_to_shipment_requested(self):
		note = self.make_note()
		frappe.set_user("Administrator")
		apply_workflow(frappe.get_doc("Delivery Note", note.name), "Create Shipment")
		self.assertEqual(
			frappe.db.get_value("Delivery Note", note.name, lifecycle.FIELD), lifecycle.SHIPMENT_REQUESTED
		)

	def test_confirm_delivery_is_not_offered_to_a_carrier_note(self):
		note = self.make_note()
		from frappe.model.workflow import get_transitions

		actions ={t.action for t in get_transitions(frappe.get_doc("Delivery Note", note.name))}
		self.assertIn("Create Shipment", actions)
		self.assertNotIn("Confirm Delivery", actions)

	def test_confirm_delivery_of_a_plain_note_ends_at_delivered(self):
		note = self.make_note(transporter=None)
		frappe.set_user("Administrator")
		from frappe.model.workflow import get_transitions

		actions = {t.action for t in get_transitions(frappe.get_doc("Delivery Note", note.name))}
		self.assertNotIn("Create Shipment", actions)
		apply_workflow(frappe.get_doc("Delivery Note", note.name), "Confirm Delivery")
		row = frappe.db.get_value("Delivery Note", note.name, ["docstatus", lifecycle.FIELD], as_dict=True)
		self.assertEqual((row.docstatus, row[lifecycle.FIELD]), (1, lifecycle.DELIVERED))

	def test_a_stage_cannot_be_changed_by_a_direct_save(self):
		note = self.make_note()
		doc = frappe.get_doc("Delivery Note", note.name)
		doc.set(lifecycle.FIELD, lifecycle.SHIPMENT_CREATED)
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_a_new_copy_resets_the_stage(self):
		note = self.make_note()
		copy = frappe.copy_doc(frappe.get_doc("Delivery Note", note.name))
		copy.set(lifecycle.FIELD, lifecycle.DELIVERED)
		lifecycle.keep_state_consistent(copy)
		self.assertFalse(copy.get(lifecycle.FIELD))

	def test_a_stock_user_cannot_cancel(self):
		note = self.make_note(transporter=None)
		frappe.set_user("Administrator")
		apply_workflow(frappe.get_doc("Delivery Note", note.name), "Confirm Delivery")
		email = self.make_user("Stock User")
		frappe.set_user(email)
		from frappe.model.workflow import get_transitions

		actions = {t.action for t in get_transitions(frappe.get_doc("Delivery Note", note.name))}
		self.assertNotIn("Cancel", actions)
		frappe.set_user("Administrator")
