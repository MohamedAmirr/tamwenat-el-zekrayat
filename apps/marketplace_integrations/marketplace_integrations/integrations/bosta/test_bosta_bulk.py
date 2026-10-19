# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bulk Bosta shipment creation (#305): the local pre-check plan, the isolated job, retry.

Real database and cache; only the HTTP layer and the district list are faked.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api import bosta_shipments
from marketplace_integrations.integrations.bosta import bulk, deliveries
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin, FakeResponse, created

DIRECTORY = [
	{
		"id": "city-cairo",
		"name": "Cairo",
		"name_ar": "القاهرة",
		"code": "EG-01",
		"districts": [
			{"id": "d-maadi", "name": "Maadi", "name_ar": "المعادي"},
			{"id": "d-nasr", "name": "Nasr City", "name_ar": "مدينة نصر"},
		],
	}
]


class IntegrationTestBostaBulk(BostaFixtureMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		patcher = patch(
			"marketplace_integrations.integrations.bosta.district_match.locations.get_directory",
			return_value=DIRECTORY,
		)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _addressed_order(self, address_line1, phone="01000000000", **order_kwargs):
		order = self.make_order(**order_kwargs)
		address = frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": f"Bosta Bulk {self.suffix} {order.name}",
				"address_type": "Shipping",
				"address_line1": address_line1,
				"city": "Cairo",
				"country": frappe.db.get_value("Country", {}, "name"),
				"links": [{"link_doctype": "Customer", "link_name": self.customer.name}],
			}
		).insert()
		self._track(address)
		frappe.db.set_value("Sales Order", order.name, "shipping_address_name", address.name)
		frappe.db.set_value("Sales Order", order.name, "contact_mobile", phone)
		frappe.db.commit()
		return order

	def _plan(self, *orders):
		return bulk.get_bulk_create_plan(frappe.as_json([order.name for order in orders]))

	def _row(self, order, **overrides):
		return {
			"sales_order": order.name,
			"receiver_name": self._receiver(),
			"receiver_phone": "01000000000",
			"city": "Cairo",
			"district_id": "d-maadi",
			"address_first_line": "12 Example Street",
			"expected_cod": 100,
			**overrides,
		}

	def _run(self, orders, skipped=None):
		job_id = frappe.generate_hash(length=16)
		bulk.run_bulk_create(job_id, frappe.session.user, orders, skipped or [])
		return frappe.cache().get_value(bulk._cache_key(job_id))

	# Plan

	def test_plan_sorts_orders_into_ready_review_and_skipped_without_calling_bosta(self):
		ready = self._addressed_order("12 Example Street, Maadi")
		no_match = self._addressed_order("12 Example Street, Somewhere Else")
		ambiguous = self._addressed_order("12 Example Street, Maadi near Nasr City")
		no_phone = self._addressed_order("12 Example Street, Maadi", phone="")
		short_line = self._addressed_order("Maadi")
		draft = self.make_order(submit=False)
		too_much_cod = self.make_order(rate=bulk.deliveries.BOSTA_COD_LIMIT + 1)
		closed = self.make_order()
		frappe.db.set_value("Sales Order", closed.name, "status", "Closed")
		has_shipment = self._addressed_order("Maadi, 12 Example Street")
		self.http.queue["create"].append(created())
		self.create(has_shipment)
		self.http.calls.clear()

		plan = self._plan(
			ready, no_match, ambiguous, no_phone, short_line, draft, too_much_cod, closed, has_shipment
		)

		self.assertEqual([row["sales_order"] for row in plan["ready"]], [ready.name])
		self.assertEqual(plan["ready"][0]["defaults"]["district_id"], "d-maadi")
		self.assertEqual(
			[row["sales_order"] for row in plan["review"]],
			[no_match.name, ambiguous.name, no_phone.name, short_line.name],
		)
		self.assertNotIn("district_id", plan["review"][0]["defaults"])
		self.assertNotIn("district_id", plan["review"][1]["defaults"])
		reasons = {row["sales_order"]: row["reason"] for row in plan["skipped"]}
		self.assertEqual(set(reasons), {draft.name, too_much_cod.name, closed.name, has_shipment.name})
		self.assertIn("submitted", reasons[draft.name])
		self.assertIn(str(bulk.deliveries.BOSTA_COD_LIMIT), reasons[too_much_cod.name])
		self.assertIn("Closed", reasons[closed.name])
		self.assertIn("already exists", reasons[has_shipment.name])
		self.assertEqual(self.http.calls, [])

	def test_plan_skips_an_order_with_nothing_left_to_deliver(self):
		order = self.make_order()
		frappe.db.set_value("Sales Order Item", {"parent": order.name}, "delivered_qty", 1)
		self.assertIn("remaining quantity", self._plan(order)["skipped"][0]["reason"])

	def test_more_than_fifty_orders_are_refused_naming_the_limit(self):
		names = frappe.as_json([f"SO-{index}" for index in range(51)])
		with self.assertRaisesRegex(frappe.ValidationError, "at most 50"):
			bulk.get_bulk_create_plan(names)
		with self.assertRaisesRegex(frappe.ValidationError, "at most 50"):
			bulk.queue_bulk_create(frappe.as_json([{"sales_order": f"SO-{index}"} for index in range(51)]))

	def test_users_without_a_creator_role_are_rejected(self):
		viewer = self.make_user("Sales User")
		order = self.make_order()
		frappe.set_user(viewer)
		try:
			with self.assertRaises(frappe.PermissionError):
				bosta_shipments.get_bulk_create_plan(frappe.as_json([order.name]))
			with self.assertRaises(frappe.PermissionError):
				bosta_shipments.queue_bulk_create(frappe.as_json([self._row(order)]))
		finally:
			frappe.set_user("Administrator")

	# Queue and status

	def test_queue_enqueues_one_long_job_and_only_the_owner_reads_its_status(self):
		order = self.make_order()
		with patch.object(bulk.frappe, "enqueue") as enqueue:
			job_id = bulk.queue_bulk_create(
				frappe.as_json([self._row(order)]),
				frappe.as_json([{"sales_order": "SO-X", "reason": "Closed"}]),
			)["job_id"]

		kwargs = enqueue.call_args.kwargs
		self.assertEqual(enqueue.call_args.args, (bulk.run_bulk_create,))
		self.assertEqual(kwargs["queue"], "long")
		self.assertEqual(kwargs["orders"][0]["sales_order"], order.name)
		self.assertEqual(kwargs["skipped"], [{"sales_order": "SO-X", "reason": "Closed"}])
		self.assertEqual(bulk.get_bulk_create_status(job_id)["status"], "queued")

		other = self.make_user("Sales Manager")
		frappe.set_user(other)
		try:
			with self.assertRaises(frappe.PermissionError):
				bulk.get_bulk_create_status(job_id)
		finally:
			frappe.set_user("Administrator")

	# Job

	def test_one_failing_order_does_not_stop_the_others(self):
		first, failing, last = self.make_order(), self.make_order(), self.make_order()
		self.http.queue["create"] += [
			created("TRK-1", "D-1"),
			FakeResponse(
				400,
				{
					"success": False,
					"errorCode": 777,
					"message": "dropOffAddress must contain at least one of [districtId, districtName]",
				},
			),
			created("TRK-3", "D-3"),
		]

		state = self._run(
			[self._row(first), self._row(failing), self._row(last)],
			[{"sales_order": "SO-PRECHECK", "reason": "Sales Order SO-PRECHECK is Closed."}],
		)

		self.assertEqual(state["status"], "completed")
		rows = {row["mapping_name"]: row for row in state["result"]["results"]}
		self.assertTrue(rows[first.name]["ok"])
		self.assertIn("TRK-1", rows[first.name]["detail"])
		self.assertFalse(rows[failing.name]["ok"])
		self.assertFalse(rows[failing.name]["skipped"])
		self.assertEqual(rows[failing.name]["detail"], "Bosta needs a district for this address.")
		self.assertTrue(rows[last.name]["ok"])
		self.assertTrue(rows["SO-PRECHECK"]["skipped"])
		result = state["result"]
		self.assertEqual((result["succeeded"], result["failed"], result["skipped_mappings"]), (2, 1, 1))
		self.assertEqual(self.shipment_for(failing).status, "Bosta Failed")
		self.assertEqual(self.shipment_for(last).status, "Completed")

	def test_other_bosta_errors_show_bostas_message(self):
		order = self.make_order()
		self.http.queue["create"].append(
			FakeResponse(400, {"success": False, "message": "Receiver phone is invalid"})
		)

		state = self._run([self._row(order)])

		detail = state["result"]["results"][0]["detail"]
		self.assertEqual(detail, "Receiver phone is invalid")

	def test_bosta_accepted_but_delivery_note_failed_names_the_tracking_number_and_retry_makes_only_the_note(
		self,
	):
		order = self.make_order()
		self.http.queue["create"].append(created("TRK-9", "D-9"))
		with patch.object(
			deliveries, "make_delivery_note", side_effect=frappe.ValidationError("stock problem")
		):
			state = self._run([self._row(order)])

		row = state["result"]["results"][0]
		self.assertFalse(row["ok"])
		self.assertIn("TRK-9", row["detail"])
		self.assertEqual(self.shipment_for(order).status, "Delivery Note Failed")

		# Retry sends the same values again; no second Bosta delivery is created.
		state = self._run([self._row(order)])

		self.assertTrue(state["result"]["results"][0]["ok"])
		self.assertEqual(len(self.http.calls_of("create")), 1)
		self.assertEqual(frappe.db.count("Bosta Shipment", {"sales_order": order.name}), 1)
		self.assertEqual(self.shipment_for(order).status, "Completed")

	def test_retry_after_a_failed_bosta_call_searches_first_and_reuses_the_shipment(self):
		order = self.make_order()
		self.http.queue["create"].append(FakeResponse(500, {"success": False, "message": "down"}))
		self._run([self._row(order)])
		self.http.queue["search"].append(FakeResponse(200, {"success": True, "data": []}))
		self.http.queue["create"].append(created("TRK-2", "D-2"))

		state = self._run([self._row(order, district_id="d-nasr")])

		self.assertTrue(state["result"]["results"][0]["ok"])
		self.assertEqual(frappe.db.count("Bosta Shipment", {"sales_order": order.name}), 1)
		self.assertEqual(self.shipment_for(order).district_id, "d-nasr")
