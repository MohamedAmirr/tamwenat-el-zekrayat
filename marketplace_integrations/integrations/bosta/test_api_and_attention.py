# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The Bosta Desk API guards that the adapter tests do not cover, and the Bosta Needs Attention items
(epic #413). Real database; only the HTTP layer is faked (``tests/bosta_fixtures.py``)."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_to_date, now_datetime

from marketplace_integrations.api import bosta_shipments
from marketplace_integrations.api import carrier_shipments as carrier_api
from marketplace_integrations.integrations.bosta.statuses import BOSTA_STATE_NAMES
from marketplace_integrations.services import marketplace_attention as attention
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin
from marketplace_integrations.tests.utils import make_role_user


class IntegrationTestBostaApi(BostaFixtureMixin, IntegrationTestCase):
	def test_cities_need_write_permission_on_the_note(self):
		note = self.make_note()
		email, _role = make_role_user(self, "Bosta City Reader", {"Delivery Note": ["read"]})
		frappe.set_user(email)
		with self.assertRaises(frappe.PermissionError):
			bosta_shipments.get_cities(note.name)
		frappe.set_user("Administrator")

	def test_cities_list_the_bosta_cities(self):
		note = self.make_note()
		result = bosta_shipments.get_cities(note.name)
		self.assertEqual({c["id"] for c in result["cities"]}, {"city-cairo", "city-giza", "city-alex"})

	def test_resolve_city_returns_the_code_and_name(self):
		self.assertEqual(bosta_shipments.resolve_city("city-cairo"), {"code": "EG-01", "name": "Cairo"})

	def test_resolve_city_refuses_an_unknown_city(self):
		with self.assertRaises(frappe.ValidationError):
			bosta_shipments.resolve_city("city-nowhere")

	def test_an_api_call_on_a_note_that_is_not_a_bosta_note_is_refused(self):
		note = self.make_note()
		frappe.db.set_value("Delivery Note", note.name, "transporter_name", "Someone Else")
		with self.assertRaises(frappe.ValidationError):
			carrier_api.check_shipment(note.name)

	def test_refresh_and_label_need_a_created_shipment(self):
		note = self.make_note()
		with self.assertRaises(frappe.ValidationError):
			carrier_api.refresh_shipment(note.name)
		with self.assertRaises(frappe.ValidationError):
			bosta_shipments.fetch_label(note.name)


class IntegrationTestBostaAttention(BostaFixtureMixin, IntegrationTestCase):
	def _items(self, note, problem_type):
		return [i for i in attention._collect_carrier_items(problem_type) if i["reference_name"] == note.name]

	def _set(self, note, **values):
		frappe.db.set_value("Delivery Note", note.name, values, update_modified=False)

	def test_a_draft_whose_creation_failed_or_is_unknown_is_listed(self):
		note = self.make_note()
		self._set(note, custom_integration_status="Creation Failed", custom_last_integration_error="bad phone")
		items = self._items(note, attention.CARRIER_CREATION_PROBLEM)
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0]["explanation"], "Bosta: bad phone")
		self._set(note, custom_integration_status="Creation Unknown", custom_last_integration_error="")
		self.assertEqual(len(self._items(note, attention.CARRIER_CREATION_PROBLEM)), 1)
		self._set(note, custom_integration_status="Created")
		self.assertEqual(self._items(note, attention.CARRIER_CREATION_PROBLEM), [])

	def test_a_delivery_bosta_cancelled_is_listed(self):
		note = self.make_note()
		self._set(note, custom_integration_status="Cancelled", custom_carrier_delivery_id="D-1")
		self.assertEqual(len(self._items(note, attention.CARRIER_CANCELLED)), 1)

	def test_a_bosta_cancelled_item_clears_after_14_days_and_when_the_note_is_cancelled_in_erpnext(self):
		note = self.make_note()
		old = add_days(now_datetime(), -15)
		self._set(note, custom_integration_status="Cancelled", custom_carrier_delivery_id="D-1", custom_last_sync_at=old)
		self.assertEqual(self._items(note, attention.CARRIER_CANCELLED), [])

		fresh = add_days(now_datetime(), -13)
		self._set(note, custom_last_sync_at=fresh)
		self.assertEqual(len(self._items(note, attention.CARRIER_CANCELLED)), 1)

		# A Delivery Note cancelled in ERPNext (docstatus 2) is no longer listed.
		self._set(note, docstatus=2)
		self.assertEqual(self._items(note, attention.CARRIER_CANCELLED), [])
		self._set(note, docstatus=0)

	def test_the_stored_error_is_explained_raw_so_the_report_escapes_it_once(self):
		note = self.make_note()
		self._set(note, custom_integration_status="Creation Failed", custom_last_integration_error="bad <b>phone</b> & more")
		items = self._items(note, attention.CARRIER_CREATION_PROBLEM)
		self.assertEqual(items[0]["explanation"], "Bosta: bad <b>phone</b> & more")

	def test_an_exception_state_is_listed_but_a_cancelled_note_is_not(self):
		note = self.make_note()
		self._set(
			note,
			custom_integration_status="Created",
			custom_carrier_delivery_id="D-1",
			custom_shipping_status=BOSTA_STATE_NAMES[47],
		)
		# #500: a picked-up DRAFT is listed once, as "Picked Up Not Submitted" (Exception is a pickup status); the
		# Carrier Exception row is for a submitted note (only Lost/Damaged keep both rows on a draft).
		self.assertEqual(self._items(note, attention.CARRIER_EXCEPTION), [])
		self.assertEqual(len(self._items(note, attention.PICKED_UP_NOT_SUBMITTED)), 1)
		self._set(note, docstatus=1)
		self.assertEqual(len(self._items(note, attention.CARRIER_EXCEPTION)), 1)
		self._set(note, custom_integration_status="Cancelled")
		self.assertEqual(self._items(note, attention.CARRIER_EXCEPTION), [])
		self._set(note, docstatus=0)

	def test_a_created_draft_with_a_submit_error_says_the_note_was_not_submitted(self):
		note = self.make_note()
		self._set(
			note,
			custom_integration_status="Created",
			custom_last_integration_error="Shipment created at Bosta; the Delivery Note could not be confirmed: negative stock",
		)
		items = self._items(note, attention.CARRIER_EXCEPTION)
		self.assertEqual(len(items), 1)
		self.assertIn("shipment created, Delivery Note not confirmed.", items[0]["explanation"])
		self.assertIn("negative stock", items[0]["explanation"])

	def test_a_created_bosta_draft_is_never_flagged_by_age_only_once_picked_up(self):
		# #500: Bosta keeps the Created draft by design until its pickup event, so an old draft is not a problem
		note = self.make_note()
		self._set(note, custom_integration_status="Created", custom_carrier_delivery_id="D-1")
		self.assertEqual(self._items(note, attention.CARRIER_EXCEPTION), [])
		self._set(note, modified=add_to_date(now_datetime(), minutes=-16))
		self.assertEqual(self._items(note, attention.CARRIER_EXCEPTION), [])
		self.assertEqual(self._items(note, attention.PICKED_UP_NOT_SUBMITTED), [])
		# picked up but still a draft: the job did not submit it
		self._set(note, custom_shipping_status=BOSTA_STATE_NAMES[21])
		items = self._items(note, attention.PICKED_UP_NOT_SUBMITTED)
		self.assertEqual(len(items), 1)
		self.assertIn("picked up", items[0]["explanation"])
		self._set(note, docstatus=1)
		self.assertEqual(self._items(note, attention.PICKED_UP_NOT_SUBMITTED), [])
		self._set(note, docstatus=0)

	def test_a_return_state_without_a_return_note_is_listed(self):
		note = self.make_note()
		self._set(
			note,
			custom_integration_status="Created",
			custom_carrier_delivery_id="D-1",
			custom_shipping_status=BOSTA_STATE_NAMES[46],
		)
		self.assertEqual(len(self._items(note, attention.CARRIER_RETURN)), 1)

	def test_a_created_note_with_no_recent_update_is_stale(self):
		note = self.make_note()
		self._set(
			note,
			custom_integration_status="Created",
			custom_carrier_delivery_id="D-1",
			custom_last_sync_at=add_days(now_datetime(), -6),
		)
		self.assertEqual(len(self._items(note, attention.CARRIER_STATUS_STALE)), 1)
		self._set(note, custom_last_sync_at=now_datetime())
		self.assertEqual(self._items(note, attention.CARRIER_STATUS_STALE), [])
