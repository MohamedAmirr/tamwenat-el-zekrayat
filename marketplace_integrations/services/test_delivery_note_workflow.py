# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The Delivery Note Lifecycle Workflow (epic #502, issue #498): the status mapping, the shipped Workflow
definition, the migration backfill and the transitions on a real database."""

import json
from pathlib import Path
from unittest import mock

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
		listed = lifecycle.DELIVERED_STATUSES + lifecycle.NOON_RETURNED_FAILED_STATUSES
		self.assertFalse([status for status in listed if status not in options])

	def test_a_submitted_noon_note_is_ready_for_pickup_whatever_noon_reports(self):
		# Noon sets Shipped right after the shipment is created; it does not mean the parcel left (#540)
		for shipping in ("", "Acknowledged", "Shipped", "Mixed"):
			self.assertEqual(
				lifecycle.state_for(1, "Created", shipping, transporter_name="Noon"), lifecycle.READY_FOR_PICKUP, shipping
			)
		# Noon has no Delivered status (Delivered is manual only); Cancelled and Out of Stock lead to Returned / Failed
		for shipping in lifecycle.NOON_RETURNED_FAILED_STATUSES:
			self.assertEqual(
				lifecycle.state_for(1, "Created", shipping, transporter_name="Noon"), lifecycle.RETURNED_FAILED
			)
		self.assertEqual(lifecycle.state_for(1, "Created", "Shipped", True, "Noon"), lifecycle.RETURNED_FAILED)
		# a draft Noon note keeps its draft stage, a cancelled one is Cancelled
		self.assertEqual(lifecycle.state_for(0, "Created", "Shipped", transporter_name="Noon"), lifecycle.SHIPMENT_CREATED)
		self.assertEqual(lifecycle.state_for(2, "Created", "Shipped", transporter_name="Noon"), lifecycle.CANCELLED)

	def test_a_held_noon_note_is_returned_failed_but_stays_submitted(self):
		# #527: Noon cancelled after a person confirmed the pickup: Cancelled + Awaiting Return, docstatus 1
		self.assertIn("Awaiting Return", lifecycle.NOON_RETURNED_FAILED_STATUSES)
		self.assertEqual(
			lifecycle.state_for(1, "Cancelled", "Awaiting Return", transporter_name="Noon"), lifecycle.RETURNED_FAILED
		)
		for stored in lifecycle.NOON_MANUAL_STATES:
			row = {
				"docstatus": 1,
				"transporter_name": "Noon",
				"custom_integration_status": "Cancelled",
				"custom_shipping_status": "Awaiting Return",
				lifecycle.FIELD: stored,
			}
			self.assertEqual(lifecycle.state_of_row(row), lifecycle.RETURNED_FAILED, stored)

	def test_a_stored_manual_stage_of_a_submitted_noon_note_is_kept(self):
		row = {"docstatus": 1, "transporter_name": "Noon", "custom_integration_status": "Created"}
		for stored in (lifecycle.PICKED_UP, lifecycle.IN_TRANSIT, lifecycle.DELIVERED):
			held = {**row, "custom_shipping_status": "Shipped", lifecycle.FIELD: stored}
			self.assertEqual(lifecycle.state_of_row(held), stored)
		# Returned / Failed and Cancelled win over a manual stage
		picked = {**row, lifecycle.FIELD: lifecycle.PICKED_UP}
		self.assertEqual(
			lifecycle.state_of_row(picked, {"custom_shipping_status": "Out of Stock"}), lifecycle.RETURNED_FAILED
		)
		self.assertEqual(lifecycle.state_of_row(picked, {"custom_shipping_status": "Cancelled"}), lifecycle.RETURNED_FAILED)
		self.assertEqual(lifecycle.state_of_row(picked, {"docstatus": 2}), lifecycle.CANCELLED)
		# Ready for Pickup is recomputed, not kept as a manual stage
		ready = {**row, "custom_shipping_status": "Shipped", lifecycle.FIELD: lifecycle.READY_FOR_PICKUP}
		self.assertEqual(lifecycle.state_of_row(ready), lifecycle.READY_FOR_PICKUP)

	def test_a_bosta_note_does_not_keep_a_stored_stage_through_the_noon_rule(self):
		row = {
			"docstatus": 1,
			"transporter_name": "Bosta",
			"custom_integration_status": "Created",
			"custom_shipping_status": "Picked up",
			lifecycle.FIELD: lifecycle.DELIVERED,
		}
		self.assertEqual(lifecycle.state_of_row(row), lifecycle.state_for(1, "Created", "Picked up"))

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

	def test_retry_shipment_is_a_self_transition_with_the_create_shipment_task_and_role(self):
		retry = next(t for t in self.spec["transitions"] if t["action"] == "Retry Shipment")
		create = next(t for t in self.spec["transitions"] if t["action"] == "Create Shipment")
		self.assertEqual((retry["state"], retry["next_state"]), (lifecycle.SHIPMENT_REQUESTED, lifecycle.SHIPMENT_REQUESTED))
		self.assertEqual(retry["allowed"], create["allowed"])
		self.assertEqual(retry["transition_tasks"], create["transition_tasks"])
		for adapter in carrier_shipments.CARRIERS.values():
			self.assertIn(adapter.SHIPPING_CARRIER, retry["condition"])
		self.assertIn("not doc.is_return", retry["condition"])

	def test_the_noon_manual_transitions_are_for_noon_stock_users_and_not_returns(self):
		by_action = {}
		for t in self.spec["transitions"]:
			if t["action"] in ("Confirm Pickup", "Confirm Delivery") and t.get("condition", "").startswith('doc.transporter_name == "Noon"'):
				by_action.setdefault(t["action"], []).append(t)
		self.assertEqual(
			{(t["state"], t["next_state"]) for t in by_action["Confirm Pickup"]},
			{(lifecycle.READY_FOR_PICKUP, lifecycle.PICKED_UP)},
		)
		self.assertEqual(
			{(t["state"], t["next_state"]) for t in by_action["Confirm Delivery"]},
			{(lifecycle.PICKED_UP, lifecycle.DELIVERED), (lifecycle.IN_TRANSIT, lifecycle.DELIVERED)},
		)
		for t in by_action["Confirm Pickup"] + by_action["Confirm Delivery"]:
			self.assertEqual(t["allowed"], "Stock User")
			self.assertIn("not doc.is_return", t["condition"])
		cancel = [t for t in self.spec["transitions"] if t["state"] == lifecycle.READY_FOR_PICKUP and t["action"] == "Cancel"]
		self.assertEqual([(t["next_state"], t["allowed"]) for t in cancel], [(lifecycle.CANCELLED, "Stock Manager")])

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

	def _actions(self, name):
		from frappe.model.workflow import get_transitions

		return {t.action for t in get_transitions(frappe.get_doc("Delivery Note", name))}

	def _set_status(self, name, status):
		"""Write a status and the stage it leads to, as the carrier code does."""
		frappe.db.set_value("Delivery Note", name, lifecycle.with_state(name, {"custom_integration_status": status}))
		frappe.db.commit()

	def test_retry_shipment_is_offered_only_for_a_draft_whose_create_is_unknown(self):
		note = self.make_note()
		frappe.set_user("Administrator")
		self.assertNotIn("Retry Shipment", self._actions(note.name))  # Order Received
		for status, offered in (("Creation Unknown", True), ("Creating", False), ("Created", False)):
			self._set_status(note.name, status)
			self.assertEqual("Retry Shipment" in self._actions(note.name), offered, status)
		# Creation Failed goes back to Order Received, where Create Shipment already works, so Retry is not offered
		self._set_status(note.name, "Creation Failed")
		self.assertNotIn("Retry Shipment", self._actions(note.name))
		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, lifecycle.FIELD), lifecycle.ORDER_RECEIVED)
		self.assertIn("Create Shipment", self._actions(note.name))

	def test_retry_shipment_is_not_offered_for_a_return_or_a_plain_note(self):
		note = self.make_note()
		frappe.set_user("Administrator")
		self._set_status(note.name, "Creation Unknown")
		frappe.db.set_value("Delivery Note", note.name, "is_return", 1)
		self.assertNotIn("Retry Shipment", self._actions(note.name))
		frappe.db.set_value("Delivery Note", note.name, "is_return", 0)
		frappe.db.set_value("Delivery Note", note.name, "transporter_name", "Someone Else")
		self.assertNotIn("Retry Shipment", self._actions(note.name))

	def test_retry_shipment_needs_the_stock_user_role(self):
		note = self.make_note()
		frappe.set_user("Administrator")
		self._set_status(note.name, "Creation Unknown")
		stock_user = self.make_user("Stock User")
		sales_user = self.make_user("Sales User")
		frappe.set_user(stock_user)
		self.assertIn("Retry Shipment", self._actions(note.name))
		frappe.set_user(sales_user)
		self.assertNotIn("Retry Shipment", self._actions(note.name))
		frappe.set_user("Administrator")

	def test_retry_shipment_on_an_unknown_note_stays_in_shipment_requested_and_starts_the_create(self):
		note = self.make_note()
		frappe.set_user("Administrator")
		self._set_status(note.name, "Creation Unknown")
		apply_workflow(frappe.get_doc("Delivery Note", note.name), "Retry Shipment")
		self.assertEqual(
			frappe.db.get_value("Delivery Note", note.name, lifecycle.FIELD), lifecycle.SHIPMENT_REQUESTED
		)
		# the async task is queued with the note; running it calls request_create as the requesting user
		queued = [c for c in self.enqueue.call_args_list if c.args and c.args[0] is lifecycle.start_shipment]
		self.assertEqual(len(queued), 1)
		with mock.patch.object(carrier_shipments, "request_create", return_value={"status": "Creating"}) as request_create:
			lifecycle.start_shipment(queued[0].kwargs["doc"])
		request_create.assert_called_once_with(note.name, submit_as="Administrator")

	def test_the_retry_check_refuses_a_note_whose_create_is_running_or_done_or_submitted(self):
		note = self.make_note()
		for status in ("Creating", "Created"):
			self._set_status(note.name, status)
			with self.assertRaises(frappe.ValidationError):
				lifecycle.check_shipment_request(frappe.get_doc("Delivery Note", note.name))
		self._set_status(note.name, "Creation Unknown")
		lifecycle.check_shipment_request(frappe.get_doc("Delivery Note", note.name))  # does not raise

	def _make_submitted_noon_note(self, state):
		"""A Bosta-fixture note rewritten into a submitted Noon note at ``state`` (database writes, as the other
		tests do), so only the Workflow conditions and roles are exercised."""
		note = self.make_note()
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{
				"docstatus": 1,
				"transporter_name": "Noon",
				"custom_integration_status": "Created",
				"custom_shipping_status": "Shipped",
				lifecycle.FIELD: state,
			},
			update_modified=False,
		)
		frappe.db.set_value("Delivery Note Item", {"parent": note.name}, "docstatus", 1, update_modified=False)
		frappe.db.commit()
		self._faked_submitted.append(note.name)
		return note

	def tearDown(self):
		# The fixture purge CANCELS a submitted note (stock ledger, carrier hooks), but a faked-submitted note has no
		# ledger entries: put it back to a draft first so the purge just deletes it.
		frappe.set_user("Administrator")
		for name in self.__dict__.pop("_faked_submitted_list", []):
			if frappe.db.exists("Delivery Note", name):
				frappe.db.set_value("Delivery Note", name, "docstatus", 0, update_modified=False)
				frappe.db.set_value("Delivery Note Item", {"parent": name}, "docstatus", 0, update_modified=False)
		frappe.db.commit()
		super().tearDown()

	@property
	def _faked_submitted(self):
		return self.__dict__.setdefault("_faked_submitted_list", [])

	def test_the_noon_manual_actions_are_offered_by_stage_to_a_stock_user_only(self):
		stock_user = self.make_user("Stock User")
		sales_user = self.make_user("Sales User")
		for state, offered in (
			(lifecycle.READY_FOR_PICKUP, {"Confirm Pickup"}),
			(lifecycle.PICKED_UP, {"Confirm Delivery"}),
			(lifecycle.IN_TRANSIT, {"Confirm Delivery"}),
		):
			note = self._make_submitted_noon_note(state)
			frappe.set_user(stock_user)
			self.assertEqual(self._actions(note.name) - {"Cancel"}, offered, state)
			frappe.set_user(sales_user)
			self.assertFalse(self._actions(note.name), state)
			frappe.set_user("Administrator")
		# Cancel from Ready for Pickup is for a Stock Manager only
		note = self._make_submitted_noon_note(lifecycle.READY_FOR_PICKUP)
		frappe.set_user(stock_user)
		self.assertNotIn("Cancel", self._actions(note.name))
		frappe.set_user("Administrator")
		self.assertIn("Cancel", self._actions(note.name))

	def test_the_noon_manual_actions_are_not_offered_for_a_bosta_note_or_a_return(self):
		frappe.set_user("Administrator")
		note = self._make_submitted_noon_note(lifecycle.PICKED_UP)
		frappe.db.set_value("Delivery Note", note.name, "transporter_name", "Bosta")
		self.assertNotIn("Confirm Delivery", self._actions(note.name))
		frappe.db.set_value("Delivery Note", note.name, {"transporter_name": "Noon", "is_return": 1})
		self.assertNotIn("Confirm Delivery", self._actions(note.name))

	def test_confirm_pickup_then_confirm_delivery_move_a_noon_note_and_a_refresh_keeps_them(self):
		note = self._make_submitted_noon_note(lifecycle.READY_FOR_PICKUP)
		frappe.set_user("Administrator")
		for action, stage in (("Confirm Pickup", lifecycle.PICKED_UP), ("Confirm Delivery", lifecycle.DELIVERED)):
			apply_workflow(frappe.get_doc("Delivery Note", note.name), action)
			self.assertEqual(frappe.db.get_value("Delivery Note", note.name, lifecycle.FIELD), stage)
			# what a Noon refresh writes: the carrier status together with the stage it leads to
			written = lifecycle.with_state(note.name, {"custom_shipping_status": "Shipped"})
			self.assertEqual(written[lifecycle.FIELD], stage)

	def test_a_user_without_stock_user_is_refused_confirm_pickup_server_side(self):
		note = self._make_submitted_noon_note(lifecycle.READY_FOR_PICKUP)
		frappe.set_user(self.make_user("Sales User"))
		with self.assertRaises(frappe.ValidationError):
			apply_workflow(frappe.get_doc("Delivery Note", note.name), "Confirm Pickup")
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, lifecycle.FIELD), lifecycle.READY_FOR_PICKUP)

	def test_the_patch_that_adds_the_noon_stages_is_idempotent(self):
		from marketplace_integrations.patches.v0_0 import add_noon_manual_stage_transitions as noon_patch

		noon_patch.execute()
		noon_patch.execute()
		workflow = frappe.get_doc("Workflow", lifecycle.WORKFLOW)
		self.assertIn(lifecycle.READY_FOR_PICKUP, [row.state for row in workflow.states])
		full = [(t.state, t.action, t.next_state, t.condition) for t in workflow.transitions]
		self.assertEqual(len(full), len(set(full)))
		actions = [(t.state, t.action) for t in workflow.transitions]
		self.assertIn((lifecycle.READY_FOR_PICKUP, "Confirm Pickup"), actions)

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
