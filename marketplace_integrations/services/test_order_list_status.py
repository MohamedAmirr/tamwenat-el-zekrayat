# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Stage and payment of the Sales Orders on a list page (services/order_list_status.py, api/order_list_status.py).

The pure helpers and the mocked calls are unit tests; the queries run against the real database in
``IntegrationTestGetStatesQueries`` (written without a run: run it on test.localhost first)."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from marketplace_integrations.api import order_list_status as api
from marketplace_integrations.services import delivery_note_workflow as lifecycle
from marketplace_integrations.services import order_list_status as service
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin
from marketplace_integrations.tests.utils import make_role_user


def note(docstatus=1, integration="", shipping="", state=None, is_return=0):
	return frappe._dict(
		docstatus=docstatus,
		is_return=is_return,
		custom_integration_status=integration,
		custom_shipping_status=shipping,
		workflow_state=state,
	)


class UnitTestStageOfNote(UnitTestCase):
	def test_a_draft_note_is_ready_and_a_draft_waiting_for_goods_has_its_own_stage(self):
		self.assertEqual(service.stage_of_note(note(0)), service.READY)
		self.assertEqual(service.stage_of_note(note(0, "Created")), service.READY)
		self.assertEqual(
			service.stage_of_note(note(0, state=lifecycle.WAITING_FOR_GOODS)), service.WAITING_FOR_GOODS
		)

	def test_a_submitted_carrier_note_follows_the_carrier_status(self):
		self.assertEqual(service.stage_of_note(note(1, "Created", "Picked up")), service.ON_THE_WAY)
		self.assertEqual(
			service.stage_of_note(note(1, "Created", "In transit between Hubs")), service.ON_THE_WAY
		)
		self.assertEqual(service.stage_of_note(note(1, "Created", "Shipped")), service.ON_THE_WAY)
		self.assertEqual(service.stage_of_note(note(1, "Created", "Delivered")), service.DELIVERED)
		self.assertEqual(service.stage_of_note(note(1, "Created", "Returned to business")), service.PROBLEM)
		self.assertEqual(service.stage_of_note(note(1, "Created", "Lost")), service.PROBLEM)

	def test_a_submitted_noon_note_is_ready_until_a_person_confirms_pickup_and_delivery(self):
		def noon_note(state, shipping="Shipped"):
			row = note(1, "Created", shipping, state)
			row.transporter_name = "Noon"
			return row

		self.assertEqual(service.stage_of_note(noon_note(lifecycle.READY_FOR_PICKUP)), service.READY)
		self.assertEqual(service.stage_of_note(noon_note(None)), service.READY)
		self.assertEqual(service.stage_of_note(noon_note(lifecycle.PICKED_UP)), service.ON_THE_WAY)
		self.assertEqual(service.stage_of_note(noon_note(lifecycle.IN_TRANSIT)), service.ON_THE_WAY)
		self.assertEqual(service.stage_of_note(noon_note(lifecycle.DELIVERED)), service.DELIVERED)
		self.assertEqual(service.stage_of_note(noon_note(lifecycle.PICKED_UP, "Cancelled")), service.PROBLEM)

	def test_a_bosta_exception_code_is_a_failed_attempt_not_on_the_way(self):
		# EXCEPTION_CODES 47, 102, 103, 105
		for shipping in ("Exception", "Investigation", "Awaiting your action", "On hold"):
			self.assertEqual(
				service.stage_of_note(note(1, "Created", shipping)), service.ATTEMPT_FAILED, shipping
			)
		self.assertEqual(service.stage_of_note(note(1, "Created", "Picked up")), service.ON_THE_WAY)

	def test_a_submitted_note_without_a_carrier_is_delivered(self):
		# the pickup-from-store one-click button and a manual delivery
		self.assertEqual(service.stage_of_note(note(1)), service.DELIVERED)


class UnitTestCombineStages(UnitTestCase):
	def test_no_note_is_not_prepared_unless_erpnext_shows_it_delivered(self):
		self.assertEqual(service.combine_stages([], 0), service.NOT_PREPARED)
		# an update-stock invoice or a drop ship delivers with no Delivery Note
		self.assertEqual(service.combine_stages([], 100), service.DELIVERED)

	def test_the_least_advanced_note_wins(self):
		self.assertEqual(service.combine_stages([service.DELIVERED, service.READY], 100, 2, 2), service.READY)
		self.assertEqual(
			service.combine_stages([service.DELIVERED, service.ON_THE_WAY], 100, 2, 2), service.ON_THE_WAY
		)
		self.assertEqual(
			service.combine_stages([service.READY, service.WAITING_FOR_GOODS], 0, 2, 2),
			service.WAITING_FOR_GOODS,
		)

	def test_quantity_the_notes_do_not_cover_is_not_prepared(self):
		self.assertEqual(service.combine_stages([service.DELIVERED], 40, 1, 3), service.NOT_PREPARED)
		self.assertEqual(service.combine_stages([service.READY], 0, 1, 3), service.NOT_PREPARED)
		self.assertEqual(service.combine_stages([service.DELIVERED], 100, 3, 3), service.DELIVERED)
		# ERPNext says everything is delivered (another document delivered the rest)
		self.assertEqual(service.combine_stages([service.ON_THE_WAY], 100, 1, 3), service.ON_THE_WAY)

	def test_a_problem_shows_only_when_the_newest_note_is_the_problem(self):
		# stages are oldest first
		self.assertEqual(
			service.combine_stages([service.DELIVERED, service.PROBLEM], 100, 2, 2), service.PROBLEM
		)
		self.assertEqual(
			service.combine_stages([service.READY, service.ATTEMPT_FAILED], 100, 2, 2), service.ATTEMPT_FAILED
		)

	def test_an_older_problem_note_replaced_by_a_newer_note_is_ignored(self):
		self.assertEqual(
			service.combine_stages([service.PROBLEM, service.DELIVERED], 100, 2, 1), service.DELIVERED
		)
		self.assertEqual(
			service.combine_stages([service.ATTEMPT_FAILED, service.ON_THE_WAY], 100, 2, 1),
			service.ON_THE_WAY,
		)
		self.assertEqual(
			service.combine_stages([service.PROBLEM, service.READY, service.DELIVERED], 100, 3, 1),
			service.READY,
		)


class UnitTestPaymentKind(UnitTestCase):
	def test_the_advance_covers_the_total(self):
		order = {"grand_total": 100, "advance_paid": 100, "per_billed": 0}
		self.assertEqual(service.payment_kind(order, []), service.PAID_IN_ADVANCE)
		self.assertEqual(service.payment_kind({**order, "advance_paid": 60}, []), service.PARTIALLY_PAID)
		self.assertEqual(service.amount_owed({**order, "advance_paid": 60}), 40)
		self.assertEqual(service.amount_owed({**order, "advance_paid": 60}, [30, 5]), 35)
		self.assertEqual(
			service.payment_kind({**order, "advance_paid": 0}, []), service.PAY_ON_DELIVERY
		)
		self.assertEqual(
			service.payment_kind({"grand_total": 0, "advance_paid": 0, "per_billed": 0}, []),
			service.PAY_ON_DELIVERY,
		)

	def test_an_invoiced_order_with_part_of_the_advance_left_to_pay(self):
		order = {"grand_total": 100, "advance_paid": 60, "per_billed": 100}
		# INTERIM (2026-11-10, until the Bosta COD receivable "Option A" exists): a full invoice counts as paid even
		# with 35 outstanding, because the carrier collects the rest. When Option A is built this goes back to
		# PARTIALLY_PAID.
		self.assertEqual(service.payment_kind(order, [35]), service.PAID_BY_INVOICE)
		# the owed amount of an invoiced order is the invoice's, not total minus advance
		self.assertEqual(service.amount_owed(order, [35]), 35)
		self.assertEqual(service.payment_kind(order, [0, 0]), service.PAID_BY_INVOICE)
		# partly billed with part of the advance in: partially paid
		self.assertEqual(
			service.payment_kind({**order, "per_billed": 50}, [35]), service.PARTIALLY_PAID
		)

	def test_no_advance_is_pay_on_delivery_and_an_advance_over_the_total_is_paid_in_advance(self):
		order = {"grand_total": 100, "advance_paid": 0, "per_billed": 0}
		self.assertEqual(service.payment_kind(order, []), service.PAY_ON_DELIVERY)
		self.assertEqual(service.payment_kind({**order, "advance_paid": 130}, []), service.PAID_IN_ADVANCE)

	def test_the_rounded_total_is_the_amount_to_cover(self):
		order = {"grand_total": 99.6, "rounded_total": 100, "advance_paid": 99.6, "per_billed": 0}
		self.assertEqual(service.payment_kind(order, []), service.PARTIALLY_PAID)
		self.assertEqual(service.payment_kind({**order, "advance_paid": 100}, []), service.PAID_IN_ADVANCE)

	def test_talabat_and_noon_orders_are_paid_by_the_marketplace(self):
		unpaid = {"grand_total": 100, "advance_paid": 0, "per_billed": 0}
		for platform in ("Talabat", "Noon"):
			self.assertEqual(
				service.payment_kind({**unpaid, "custom_marketplace_platform": platform}, []),
				service.PAID_BY_MARKETPLACE,
				platform,
			)
		# a marketplace paying wins even when an invoice or an advance would also say paid
		self.assertEqual(
			service.payment_kind({**unpaid, "advance_paid": 100, "custom_marketplace_platform": "Noon"}, []),
			service.PAID_BY_MARKETPLACE,
		)
		# Shopify and orders without a platform keep the normal rules
		self.assertEqual(
			service.payment_kind({**unpaid, "custom_marketplace_platform": "Shopify"}, []),
			service.PAY_ON_DELIVERY,
		)
		self.assertEqual(service.payment_kind(unpaid, []), service.PAY_ON_DELIVERY)

	def test_a_full_invoice_is_paid_even_with_an_outstanding_amount(self):
		# INTERIM (2026-11-10, until the Bosta COD receivable "Option A" exists): the carrier still holds the cash
		# of an invoiced Bosta order, so the outstanding amount must not turn it back into "pay on delivery".
		# When Option A is built, this test goes back to expecting PAY_ON_DELIVERY for an unsettled invoice.
		order = {"grand_total": 100, "advance_paid": 0, "per_billed": 100}
		self.assertEqual(service.payment_kind(order, [0]), service.PAID_BY_INVOICE)
		self.assertEqual(service.payment_kind(order, [0, 0.0]), service.PAID_BY_INVOICE)
		self.assertEqual(service.payment_kind(order, [100]), service.PAID_BY_INVOICE)
		self.assertEqual(service.payment_kind(order, [0, 40]), service.PAID_BY_INVOICE)

	def test_a_partly_billed_or_not_invoiced_order_is_not_paid(self):
		order = {"grand_total": 100, "advance_paid": 0, "per_billed": 100}
		self.assertEqual(service.payment_kind(order, []), service.PAY_ON_DELIVERY)
		self.assertEqual(service.payment_kind({**order, "per_billed": 50}, [0]), service.PAY_ON_DELIVERY)
		self.assertEqual(service.payment_kind({**order, "per_billed": 50}, [100]), service.PAY_ON_DELIVERY)


class UnitTestGetStatesMocked(UnitTestCase):
	def test_an_empty_request_makes_no_query(self):
		with patch.object(service.frappe, "get_list") as get_list:
			self.assertEqual(service.get_states([]), {})
		get_list.assert_not_called()

	def test_too_many_orders_are_refused(self):
		with self.assertRaises(frappe.ValidationError):
			service.get_states([f"SO-{i}" for i in range(service.MAX_ORDERS + 1)])

	def test_orders_the_user_cannot_read_are_left_out_and_the_rest_is_asked_for_once(self):
		readable = [
			frappe._dict(
				name="SO-1", grand_total=100, rounded_total=0, advance_paid=100, per_delivered=0, per_billed=0
			)
		]
		with (
			patch.object(service.frappe, "get_list", return_value=readable),
			patch.object(service, "_order_quantities", return_value={"SO-1": 1}) as quantities,
			patch.object(service, "_notes_by_order", return_value={"SO-1": ([service.READY], 1)}) as notes,
			patch.object(service, "_invoice_outstanding_by_order", return_value={}) as invoices,
		):
			result = service.get_states(["SO-1", "SO-2", "SO-1"])
		self.assertEqual(result, {"SO-1": {"stage": service.READY, "payment": service.PAID_IN_ADVANCE}})
		for mocked in (quantities, notes, invoices):
			mocked.assert_called_once_with(["SO-1"])

	def test_the_endpoint_accepts_a_json_list_and_refuses_anything_else(self):
		with (
			patch.object(api.frappe, "has_permission"),
			patch.object(api.order_list_status, "get_states", return_value={}) as get_states,
		):
			api.get_order_states('["SO-1"]')
			get_states.assert_called_once_with(["SO-1"])
			with self.assertRaises(frappe.ValidationError):
				api.get_order_states('{"a": 1}')


class IntegrationTestGetStatesQueries(BostaFixtureMixin, IntegrationTestCase):
	"""``get_states`` against real rows. Documents are built with the Bosta fixtures (a draft Delivery Note from a
	submitted Sales Order); a note is made "submitted", "returned" or "cancelled" with a plain write of the
	column, which is all the queries read."""

	def _stage(self, order):
		return service.get_states([order.name])[order.name]["stage"]

	def _payment(self, order):
		return service.get_states([order.name])[order.name]["payment"]

	def test_no_note_and_a_draft_note_and_one_call_for_many_orders(self):
		bare = self.make_order(qty=2)
		with_note = self.make_order(qty=1)
		self.make_note(order=with_note)
		states = service.get_states([bare.name, with_note.name])
		self.assertEqual(states[bare.name]["stage"], service.NOT_PREPARED)
		self.assertEqual(states[with_note.name]["stage"], service.READY)

	def test_an_order_erpnext_shows_delivered_with_no_note_is_delivered(self):
		order = self.make_order()
		frappe.db.set_value("Sales Order", order.name, "per_delivered", 100)
		self.assertEqual(self._stage(order), service.DELIVERED)

	def test_a_submitted_note_without_a_carrier_is_delivered(self):
		order = self.make_order()
		made = self.make_note(order=order)
		frappe.db.set_value(
			"Delivery Note", made.name, {"docstatus": 1, "transporter": None, "custom_integration_status": ""}
		)
		self.assertEqual(self._stage(order), service.DELIVERED)

	def test_cancelled_and_return_notes_are_not_live_notes(self):
		order = self.make_order()
		made = self.make_note(order=order)
		frappe.db.set_value("Delivery Note", made.name, "docstatus", 2)
		self.assertEqual(self._stage(order), service.NOT_PREPARED)
		frappe.db.set_value("Delivery Note", made.name, {"docstatus": 0, "is_return": 1})
		self.assertEqual(self._stage(order), service.NOT_PREPARED)

	def test_only_the_newest_live_note_can_make_the_order_a_problem(self):
		order = self.make_order(qty=1)
		older = self.make_note(order=order)
		newer = self.make_note(order=order)
		frappe.db.set_value("Delivery Note", older.name, "creation", "2026-01-01 10:00:00")
		frappe.db.set_value("Delivery Note", newer.name, "creation", "2026-01-02 10:00:00")
		returned = {
			"docstatus": 1,
			"custom_integration_status": "Created",
			"custom_shipping_status": "Returned to business",
		}
		delivered = {"docstatus": 1, "transporter": None, "custom_integration_status": ""}
		frappe.db.set_value("Delivery Note", older.name, returned)
		frappe.db.set_value("Delivery Note", newer.name, delivered)
		self.assertEqual(self._stage(order), service.DELIVERED)
		# the other way round: the newest note is the returned one
		frappe.db.set_value("Delivery Note", older.name, delivered)
		frappe.db.set_value("Delivery Note", newer.name, returned)
		self.assertEqual(self._stage(order), service.PROBLEM)

	def test_a_marketplace_platform_order_is_paid_by_the_marketplace(self):
		order = self.make_order()
		self.assertEqual(self._payment(order), service.PAY_ON_DELIVERY)
		frappe.db.set_value("Sales Order", order.name, "custom_marketplace_platform", "Talabat")
		self.assertEqual(self._payment(order), service.PAID_BY_MARKETPLACE)
		frappe.db.set_value("Sales Order", order.name, "custom_marketplace_platform", "Shopify")
		self.assertEqual(self._payment(order), service.PAY_ON_DELIVERY)

	def test_quantity_the_notes_do_not_cover_is_not_prepared(self):
		order = self.make_order(qty=3)
		made = self.make_note(order=order)
		frappe.db.set_value("Delivery Note Item", {"parent": made.name}, "qty", 1)
		self.assertEqual(self._stage(order), service.NOT_PREPARED)
		frappe.db.set_value("Delivery Note Item", {"parent": made.name}, "qty", 3)
		self.assertEqual(self._stage(order), service.READY)

	def test_a_submitted_invoice_counts_as_paid_whatever_is_outstanding_and_a_return_invoice_does_not(self):
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		order = self.make_order()
		invoice = make_sales_invoice(order.name)
		invoice.insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Sales Invoice", invoice.name, force=True, ignore_permissions=True)
		frappe.db.set_value("Sales Order", order.name, "per_billed", 100)
		# a draft invoice is not an invoice
		self.assertEqual(self._payment(order), service.PAY_ON_DELIVERY)
		# INTERIM (until Bosta COD "Option A"): still outstanding, still paid
		frappe.db.set_value("Sales Invoice", invoice.name, {"docstatus": 1, "outstanding_amount": 50})
		self.assertEqual(self._payment(order), service.PAID_BY_INVOICE)
		frappe.db.set_value("Sales Invoice", invoice.name, "outstanding_amount", 0)
		self.assertEqual(self._payment(order), service.PAID_BY_INVOICE)
		frappe.db.set_value("Sales Invoice", invoice.name, "is_return", 1)
		self.assertEqual(self._payment(order), service.PAY_ON_DELIVERY)

	def test_the_advance_makes_it_paid_in_advance(self):
		order = self.make_order(rate=100, advance_paid=100)
		self.assertEqual(self._payment(order), service.PAID_IN_ADVANCE)

	def test_a_user_who_cannot_read_sales_orders_is_refused(self):
		order = self.make_order()
		email, _role = make_role_user(self, "Dn Only", {"Delivery Note": ["read"]})
		previous = frappe.session.user
		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				service.get_states([order.name])
		finally:
			frappe.set_user(previous)

	def test_orders_outside_the_users_permissions_are_left_out(self):
		order = self.make_order()
		other = frappe.get_doc(
			{"doctype": "Customer", "customer_name": f"List Status Other {frappe.generate_hash(length=6)}"}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Customer", other.name, force=True, ignore_permissions=True)
		email, _role = make_role_user(self, "So Reader", {"Sales Order": ["read"]})
		permission = frappe.get_doc(
			{"doctype": "User Permission", "user": email, "allow": "Customer", "for_value": other.name}
		).insert(ignore_permissions=True)
		self.addCleanup(
			frappe.delete_doc, "User Permission", permission.name, force=True, ignore_permissions=True
		)
		previous = frappe.session.user
		frappe.set_user(email)
		try:
			self.assertEqual(service.get_states([order.name]), {})
		finally:
			frappe.set_user(previous)
