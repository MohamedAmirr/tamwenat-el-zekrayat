# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon AWB pool tests: bulk generation, quantity/platform validation,
composite uniqueness and atomic claiming. Mocks NoonClient - no real HTTP."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from marketplace_integrations.api.noon_awbs import generate_awbs, get_available_count
from marketplace_integrations.api.test_noon_returns import _make_non_noon_account, _make_noon_account
from marketplace_integrations.integrations.noon import awb_pool

IGNORE_TEST_RECORD_DEPENDENCIES = ["Marketplace Order"]

CLIENT = "marketplace_integrations.integrations.noon.awb_pool.NoonClient"


def _awb_response(awb_nrs):
	response = MagicMock(status_code=200)
	response.json.return_value = {"awb_nrs": awb_nrs}
	return response


class TestNoonAWB(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.account = _make_noon_account(f"AwbPool {frappe.generate_hash(length=8)}")

	def tearDown(self):
		frappe.db.delete("Noon AWB", {"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDown()

	def _add(self, awb_nr, status="Available"):
		return frappe.get_doc(
			{
				"doctype": "Noon AWB",
				"marketplace_account": self.account.name,
				"awb_nr": awb_nr,
				"country_code": self.account.noon_country_code,
				"status": status,
				"generated_on": now_datetime(),
			}
		).insert(ignore_permissions=True)

	def test_generate_stores_available_rows_from_one_noon_call(self):
		with patch(CLIENT) as client_class:
			client_class.return_value.request.return_value = _awb_response(["A1", "A2", "A3"])
			stored, skipped = awb_pool.generate(self.account.name, 3)

		self.assertEqual((stored, skipped), (3, 0))
		client_class.return_value.request.assert_called_once_with(
			"POST", awb_pool.NOON_LOGISTICS_AWBS_GET_PATH, json={"country_code": "ae", "qty": 3}
		)
		rows = frappe.get_all(
			"Noon AWB",
			filters={"marketplace_account": self.account.name},
			fields=["awb_nr", "status", "country_code"],
		)
		self.assertCountEqual([r.awb_nr for r in rows], ["A1", "A2", "A3"])
		self.assertTrue(all(r.status == "Available" and r.country_code == "ae" for r in rows))
		self.assertEqual(awb_pool.count_available(self.account.name), 3)

	def test_generate_skips_awbs_already_in_the_pool(self):
		self._add("A1")
		with patch(CLIENT) as client_class:
			client_class.return_value.request.return_value = _awb_response(["A1", "A2"])
			stored, skipped = awb_pool.generate(self.account.name, 2)
		self.assertEqual((stored, skipped), (1, 1))

	def test_generate_rejects_noon_error_and_empty_response(self):
		with patch(CLIENT) as client_class:
			client = client_class.return_value
			client.request.return_value = MagicMock(status_code=500)
			client.error_message.return_value = "boom"
			with self.assertRaises(frappe.ValidationError):
				awb_pool.generate(self.account.name, 2)
			client.request.return_value = _awb_response([])
			with self.assertRaises(frappe.ValidationError):
				awb_pool.generate(self.account.name, 2)
		self.assertEqual(awb_pool.count_available(self.account.name), 0)

	def test_quantity_outside_range_is_rejected_before_any_noon_call(self):
		for bad in (0, -1, 1001, "abc", "", None, 1.5):
			with (
				self.subTest(qty=bad),
				patch(CLIENT) as client_class,
				patch("marketplace_integrations.integrations.noon.awb_pool.frappe.enqueue") as enqueue,
			):
				# The ``qty: int`` hint rejects non-integers before validate_qty runs.
				with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
					generate_awbs(self.account.name, bad)
				with self.assertRaises(frappe.ValidationError):
					awb_pool.generate(self.account.name, bad)
				client_class.return_value.request.assert_not_called()
				enqueue.assert_not_called()

	def test_boundary_quantities_are_queued(self):
		for good in (1, 1000, "250"):
			with (
				self.subTest(qty=good),
				patch("marketplace_integrations.integrations.noon.awb_pool.frappe.enqueue") as enqueue,
			):
				enqueue.return_value = MagicMock()
				self.assertEqual(generate_awbs(self.account.name, good), {"queued": True})
				self.assertEqual(enqueue.call_args.kwargs["qty"], int(good))

	def test_non_noon_account_is_rejected(self):
		other = _make_non_noon_account(f"NonNoon {frappe.generate_hash(length=8)}")
		try:
			with patch("marketplace_integrations.integrations.noon.awb_pool.frappe.enqueue") as enqueue:
				with self.assertRaises(frappe.ValidationError):
					generate_awbs(other.name, 5)
				enqueue.assert_not_called()
		finally:
			frappe.delete_doc("Marketplace Account", other.name, ignore_permissions=True, force=True)
			frappe.delete_doc("Talabat Connection", other.connection, ignore_permissions=True, force=True)

	def test_awb_nr_is_unique_per_account(self):
		self._add("DUP-1")
		with self.assertRaises((frappe.UniqueValidationError, frappe.DuplicateEntryError)):
			self._add(" DUP-1 ")

	def test_available_count_counts_only_available(self):
		self._add("C1")
		self._add("C2", status="Used")
		self.assertEqual(get_available_count(self.account.name), 1)

	def test_claim_marks_used_and_two_claims_never_share_an_awb(self):
		self._add("B1")
		self._add("B2")
		first = awb_pool.claim_pool_awb(self.account.name, "ae", "ORD-1", "ORD-1-1")
		second = awb_pool.claim_pool_awb(self.account.name, "ae", "ORD-2", "ORD-2-1")
		self.assertCountEqual([first, second], ["B1", "B2"])
		self.assertIsNone(awb_pool.claim_pool_awb(self.account.name, "ae", "ORD-3", "ORD-3-1"))

		row = frappe.get_doc("Noon AWB", {"marketplace_account": self.account.name, "awb_nr": first})
		self.assertEqual(row.status, "Used")
		self.assertEqual(row.marketplace_order, "ORD-1")
		self.assertEqual(row.integration_shipment_nr, "ORD-1-1")
		self.assertIsNotNone(row.used_on)

	def test_repeat_claim_for_same_shipment_returns_same_awb(self):
		self._add("R1")
		self._add("R2")
		first = awb_pool.claim_pool_awb(self.account.name, "ae", "ORD-1", "ORD-1-1")
		again = awb_pool.claim_pool_awb(self.account.name, "ae", "ORD-1", "ORD-1-1")
		self.assertEqual(first, again)
		self.assertEqual(awb_pool.count_available(self.account.name), 1)

	def test_claim_ignores_other_accounts_and_countries(self):
		self._add("X1")
		self.assertIsNone(awb_pool.claim_pool_awb("some-other-account", "ae", "ORD-1", "ORD-1-1"))
		self.assertIsNone(awb_pool.claim_pool_awb(self.account.name, "sa", "ORD-1", "ORD-1-1"))
		self.assertEqual(awb_pool.count_available(self.account.name), 1)
