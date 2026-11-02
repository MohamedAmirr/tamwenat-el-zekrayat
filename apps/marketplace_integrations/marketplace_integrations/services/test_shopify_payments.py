# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import PaymentEntry
from erpnext.accounts.party import get_party_account
from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, flt, now_datetime

from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.marketplace_account import (
	clearing_account_problem,
)
from marketplace_integrations.services import test_sales_order_sync
from marketplace_integrations.services.marketplace_attention import (
	_collect_payment_errors,
	_shopify_order_accounts,
)
from marketplace_integrations.services.sales_order_sync import sync_sales_order
from marketplace_integrations.services.shopify_payments import manual_payment_totals, mirror_shopify_payments
from marketplace_integrations.tests.utils import make_role_user

KASHIER = "Pay with Card, Wallet and Installment via Kashier"


def transaction(
	transaction_id, *, amount, kind="SALE", status="SUCCESS", gateway=KASHIER, currency="EGP", **extra
):
	return {
		"id": transaction_id,
		"kind": kind,
		"status": status,
		"gateway": gateway,
		"manualPaymentGateway": False,
		"amountSet": {"shopMoney": {"amount": str(amount), "currencyCode": currency}},
		"createdAt": "2026-10-01T09:00:00Z",
		"processedAt": "2026-10-01T10:00:00Z",
		**extra,
	}


@contextmanager
def as_user(email):
	previous = frappe.session.user
	frappe.set_user(email)
	try:
		yield
	finally:
		frappe.set_user(previous)


class IntegrationTestShopifyPayments(IntegrationTestCase):
	def setUp(self):
		self.fixtures = test_sales_order_sync.IntegrationTestSalesOrderSync()
		self.fixtures.setUp()
		self.company = self.fixtures.company
		self.cash = frappe.get_all(
			"Account",
			filters={
				"company": self.company,
				"is_group": 0,
				"disabled": 0,
				"account_type": ["in", ["Bank", "Cash"]],
			},
			pluck="name",
			limit=1,
		)[0]
		self.mode_of_payment = frappe.get_all("Mode of Payment", pluck="name", limit=1)[0]
		self.suffix = frappe.generate_hash(length=6)

	# --- fixtures ---------------------------------------------------------------------

	def tid(self, number):
		"""A transaction id unique to this test: the class shares one transaction and the id is unique-indexed."""
		return f"gid://shopify/OrderTransaction/{self.suffix}-{number}"

	def _make_synced_order(self, transactions, *, transaction_amount=75, shipping_total=420, quantity=1):
		self.fixtures.suffix = frappe.generate_hash(length=8)  # a fresh Shopify Connection domain per order
		income = self.fixtures._income_account()
		account = self.fixtures._make_shopify_account(income)
		account.append(
			"shopify_payment_gateways",
			{
				"shopify_gateway": KASHIER,
				"mode_of_payment": self.mode_of_payment,
				"clearing_account": self.cash,
			},
		)
		account.save()
		order, _mapping = self.fixtures._make_shopify_order(
			account,
			transaction_amount=transaction_amount,
			current_quantity=quantity,
			shipping_total=shipping_total,
			payment_transactions=transactions,
		)
		# These tests exercise the payment service explicitly below. Keep fixture
		# creation focused on the Sales Order sync; production sync now also kicks
		# off independent automatic payment mirroring after a successful submit.
		with patch(
			"marketplace_integrations.services.sales_order_sync._mirror_shopify_payments_independently"
		):
			result = sync_sales_order(order.name)
		self.assertTrue(result["ok"], result.get("error"))
		order.reload()
		return account, order

	def test_missing_payment_entry_permissions_are_logged_as_payment_errors(self):
		_account, order = self._make_synced_order([])
		frappe.db.set_value(
			"Marketplace Order",
			order.name,
			"payment_transactions",
			frappe.as_json([transaction(self.tid(1), amount=100)]),
		)
		email, _role = make_role_user(self, "Payment Read Only", {"Payment Entry": ["read"]})
		previous_user = frappe.session.user
		try:
			frappe.set_user(email)
			results = mirror_shopify_payments(order.name, "Shopify Webhook")
		finally:
			frappe.set_user(previous_user)
		self.assertEqual(frappe.session.user, previous_user)

		self.assertEqual([row["status"] for row in results], ["Invalid"])
		self.assertIn("Create and Submit permission", results[0]["message"])
		logs = self._logs(order)
		self.assertEqual([row.status for row in logs], ["Invalid"])
		self.assertIn("Create and Submit permission", logs[0].response_data)
		accounts = {_account.name: _account}
		attention = _collect_payment_errors(accounts, _shopify_order_accounts(accounts))
		self.assertEqual([row["problem_type"] for row in attention], ["Payment Error"])

		# A second sync by the same user repeats the problem without writing a second row.
		try:
			frappe.set_user(email)
			mirror_shopify_payments(order.name, "Shopify Webhook")
		finally:
			frappe.set_user(previous_user)
		self.assertEqual([row.status for row in self._logs(order)], ["Invalid"])

	def _payment_entries(self, order):
		return frappe.get_all(
			"Payment Entry",
			filters={"custom_marketplace_order": order.name},
			fields=["name", "docstatus", "paid_amount", "unallocated_amount", "paid_to", "reference_no"],
			order_by="creation asc",
		)

	def _logs(self, order):
		return frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name, "sync_type": "Payment"},
			fields=["status", "marketplace_account", "request_data", "response_data"],
			order_by="creation asc",
		)

	# --- acceptance: happy path ---------------------------------------------------------

	def test_kashier_order_with_shipping_gets_one_fully_allocated_payment_entry(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=495)])
		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
		self.assertEqual(sales_order.grand_total, flt(order.order_total))

		results = mirror_shopify_payments(order.name, "Manual")

		self.assertEqual([r["status"] for r in results], ["Success"])
		entries = self._payment_entries(order)
		self.assertEqual(len(entries), 1)
		entry = frappe.get_doc("Payment Entry", entries[0].name)
		self.assertEqual(entry.docstatus, 1)
		self.assertEqual(entry.payment_type, "Receive")
		self.assertEqual(entry.party, sales_order.customer)
		self.assertEqual(entry.paid_to, self.cash)
		self.assertEqual(entry.mode_of_payment, self.mode_of_payment)
		self.assertEqual(entry.paid_amount, 495)
		self.assertEqual(entry.received_amount, 495)
		self.assertEqual(entry.reference_no, self.tid(1))
		self.assertEqual(str(entry.posting_date), "2026-10-01")
		self.assertEqual(str(entry.reference_date), "2026-10-01")
		self.assertEqual(flt(entry.unallocated_amount), 0)
		self.assertEqual(
			[(r.reference_doctype, r.reference_name, r.allocated_amount) for r in entry.references],
			[("Sales Order", sales_order.name, 495)],
		)
		self.assertEqual(flt(frappe.db.get_value("Sales Order", sales_order.name, "advance_paid")), 495)
		logs = self._logs(order)
		self.assertEqual([log.status for log in logs], ["Success"])
		self.assertEqual(logs[0].marketplace_account, order.marketplace_account)
		self.assertEqual(json.loads(logs[0].request_data), {"transaction_id": self.tid(1)})

	def test_running_again_creates_nothing_and_the_database_enforces_the_unique_id(self):
		_account, order = self._make_synced_order(
			[
				transaction(self.tid(1), amount=100),
				transaction(self.tid(2), amount=50),
			]
		)
		mirror_shopify_payments(order.name, "Manual")
		self.assertEqual(len(self._payment_entries(order)), 2)
		logs_before = len(self._logs(order))

		self.assertEqual(mirror_shopify_payments(order.name, "Manual"), [])

		self.assertEqual(len(self._payment_entries(order)), 2)
		self.assertEqual(len(self._logs(order)), logs_before)

		second = frappe.get_doc("Payment Entry", self._payment_entries(order)[1].name)
		second.custom_marketplace_transaction_id = self.tid(1)
		second.flags.ignore_validate = True
		second.flags.ignore_validate_update_after_submit = True
		with self.assertRaises(frappe.UniqueValidationError):
			second.save()

	def test_a_cancelled_entry_is_never_recreated_and_amending_it_copies_the_transaction_id(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=100)])
		mirror_shopify_payments(order.name, "Manual")
		entry = frappe.get_doc("Payment Entry", self._payment_entries(order)[0].name)
		entry.cancel()

		# Frappe v16's Desk Amend (copy_doc with from_amend) and frappe.copy_doc() default both IGNORE
		# no_copy, so the id is carried into the amended draft; the unique index then refuses to save it
		# while the cancelled entry exists.  Only frappe.copy_doc(ignore_no_copy=False) honours no_copy.
		self.assertEqual(
			frappe.get_meta("Payment Entry").get_field("custom_marketplace_transaction_id").no_copy, 1
		)
		self.assertFalse(frappe.copy_doc(entry, ignore_no_copy=False).custom_marketplace_transaction_id)
		amended = frappe.copy_doc(entry)
		amended.docstatus = 0
		amended.amended_from = entry.name
		self.assertEqual(amended.custom_marketplace_transaction_id, self.tid(1))
		with self.assertRaises(frappe.UniqueValidationError):
			amended.insert()

		self.assertEqual(mirror_shopify_payments(order.name, "Manual"), [])
		self.assertEqual([e.docstatus for e in self._payment_entries(order)], [2])

	def test_payment_entries_without_a_transaction_id_do_not_collide_on_the_unique_index(self):
		_account, order = self._make_synced_order([])
		customer = frappe.db.get_value("Sales Order", order.erpnext_sales_order, "customer")
		for _i in range(2):
			frappe.get_doc(
				{
					"doctype": "Payment Entry",
					"payment_type": "Receive",
					"company": self.company,
					"party_type": "Customer",
					"party": customer,
					"paid_to": self.cash,
					"paid_amount": 10,
					"received_amount": 10,
				}
			).insert()

	def test_only_successful_sale_and_capture_transactions_are_mirrored(self):
		transactions = [
			transaction(self.tid(1), amount=10),
			transaction(self.tid(2), amount=20),
			transaction(self.tid(3), amount=30, kind="AUTHORIZATION"),
			transaction(self.tid(4), amount=30, status="PENDING"),
			transaction(self.tid(5), amount=30, status="FAILURE"),
			transaction(self.tid(6), amount=30, kind="VOID"),
			transaction(self.tid(7), amount=30, kind="REFUND"),
		]
		_account, order = self._make_synced_order(transactions)

		mirror_shopify_payments(order.name, "Manual")

		self.assertEqual(
			sorted(e.reference_no for e in self._payment_entries(order)),
			[self.tid(1), self.tid(2)],
		)
		self.assertEqual(len(self._logs(order)), 2)

	def test_a_successful_capture_gives_one_payment_entry(self):
		_account, order = self._make_synced_order(
			[
				transaction(self.tid(1), amount=100, kind="AUTHORIZATION"),
				transaction(self.tid(2), amount=100, kind="CAPTURE"),
			]
		)

		mirror_shopify_payments(order.name, "Manual")

		self.assertEqual([e.reference_no for e in self._payment_entries(order)], [self.tid(2)])

	def test_an_order_without_a_sales_order_returns_without_a_log_row(self):
		account = self.fixtures._make_shopify_account(self.fixtures._income_account())
		order, _mapping = self.fixtures._make_shopify_order(
			account, payment_transactions=[transaction(self.tid(1), amount=10)]
		)

		self.assertEqual(mirror_shopify_payments(order.name, "Manual"), [])
		self.assertEqual(self._logs(order), [])
		self.assertEqual(self._payment_entries(order), [])

	# --- excess -------------------------------------------------------------------------

	def test_an_amount_above_the_outstanding_leaves_the_rest_unallocated(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=500)])

		results = mirror_shopify_payments(order.name, "Manual")

		self.assertEqual(results[0]["status"], "Success")
		entry = frappe.get_doc("Payment Entry", self._payment_entries(order)[0].name)
		self.assertEqual(entry.docstatus, 1)
		self.assertEqual(entry.paid_amount, 500)
		self.assertEqual(entry.references[0].allocated_amount, 495)
		self.assertEqual(flt(entry.unallocated_amount), 5)
		self.assertIn("5.00 exceeds", json.loads(self._logs(order)[0].response_data)["message"])

	def _round_off_account(self):
		return frappe.db.get_value("Company", self.company, "round_off_account")

	def test_a_rounding_difference_is_written_off_so_nothing_is_left_unallocated(self):
		"""ERPNext's Sales Order outstanding is rounded_total - advance_paid, and it rejects an
		allocation above it; the grand_total - rounded_total difference goes to the round-off account."""
		_account, order = self._make_synced_order(
			[transaction(self.tid(1), amount=494.21)], transaction_amount=74.99, shipping_total=419.22
		)
		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
		self.assertEqual((sales_order.grand_total, sales_order.rounded_total), (494.21, 494))

		results = mirror_shopify_payments(order.name, "Manual")

		self.assertEqual(results[0]["status"], "Success", results)
		entry = frappe.get_doc("Payment Entry", self._payment_entries(order)[0].name)
		self.assertEqual(entry.docstatus, 1)
		self.assertEqual(entry.paid_amount, 494.21)
		self.assertEqual(entry.references[0].allocated_amount, 494)
		self.assertEqual(flt(entry.unallocated_amount), 0)
		self.assertEqual(flt(entry.difference_amount), 0)
		self.assertEqual(
			[(row.account, row.amount) for row in entry.deductions], [(self._round_off_account(), -0.21)]
		)
		self.assertEqual(flt(frappe.db.get_value("Sales Order", sales_order.name, "advance_paid")), 494)
		self.assertIn("0.21", json.loads(self._logs(order)[0].response_data)["message"])
		# the entry's GL balances and the clearing account got the full amount
		gl = frappe.get_all(
			"GL Entry",
			filters={"voucher_no": entry.name, "is_cancelled": 0},
			fields=["account", "debit", "credit"],
		)
		self.assertEqual(flt(sum(row.debit for row in gl)), flt(sum(row.credit for row in gl)))
		self.assertEqual(sum(row.debit for row in gl if row.account == self.cash), 494.21)

	def test_a_rounded_up_order_total_is_written_off_the_other_way(self):
		_account, order = self._make_synced_order(
			[transaction(self.tid(1), amount=494.6)], transaction_amount=74.6, shipping_total=420
		)
		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
		self.assertEqual((sales_order.grand_total, sales_order.rounded_total), (494.6, 495))

		results = mirror_shopify_payments(order.name, "Manual")

		self.assertEqual(results[0]["status"], "Success", results)
		entry = frappe.get_doc("Payment Entry", self._payment_entries(order)[0].name)
		self.assertEqual(entry.references[0].allocated_amount, 495)
		self.assertEqual(flt(entry.unallocated_amount), 0)
		self.assertEqual([row.amount for row in entry.deductions], [0.4])
		self.assertEqual(flt(frappe.db.get_value("Sales Order", sales_order.name, "advance_paid")), 495)

	def test_an_excess_beyond_the_rounding_difference_stays_unallocated(self):
		_account, order = self._make_synced_order(
			[transaction(self.tid(1), amount=500)], transaction_amount=74.99, shipping_total=419.22
		)

		mirror_shopify_payments(order.name, "Manual")

		entry = frappe.get_doc("Payment Entry", self._payment_entries(order)[0].name)
		self.assertEqual(entry.references[0].allocated_amount, 494)
		self.assertEqual([row.amount for row in entry.deductions], [-0.21])
		self.assertEqual(flt(entry.unallocated_amount), 5.79)

	def test_a_missing_round_off_account_is_a_blocker_naming_the_setting(self):
		_account, order = self._make_synced_order(
			[transaction(self.tid(1), amount=494.21)], transaction_amount=74.99, shipping_total=419.22
		)
		original = self._round_off_account()
		frappe.db.set_value("Company", self.company, "round_off_account", None)
		try:
			mirror_shopify_payments(order.name, "Manual")
		finally:
			frappe.db.set_value("Company", self.company, "round_off_account", original)

		self._assert_one_invalid_log(order, "Round Off Account")

	def test_a_missing_round_off_account_does_not_matter_without_a_rounding_difference(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=495)])
		original = self._round_off_account()
		frappe.db.set_value("Company", self.company, "round_off_account", None)
		try:
			results = mirror_shopify_payments(order.name, "Manual")
		finally:
			frappe.db.set_value("Company", self.company, "round_off_account", original)

		self.assertEqual(results[0]["status"], "Success", results)

	def test_two_payments_together_above_the_outstanding_allocate_cumulatively(self):
		_account, order = self._make_synced_order(
			[transaction(self.tid(1), amount=300), transaction(self.tid(2), amount=300)]
		)

		mirror_shopify_payments(order.name, "Manual")

		entries = {
			e.reference_no: frappe.get_doc("Payment Entry", e.name) for e in self._payment_entries(order)
		}
		self.assertEqual(entries[self.tid(1)].references[0].allocated_amount, 300)
		self.assertEqual(flt(entries[self.tid(1)].unallocated_amount), 0)
		self.assertEqual(entries[self.tid(2)].references[0].allocated_amount, 195)
		self.assertEqual(flt(entries[self.tid(2)].unallocated_amount), 105)

	def test_a_payment_with_nothing_outstanding_is_all_unallocated_and_says_so(self):
		_account, order = self._make_synced_order(
			[transaction(self.tid(1), amount=495), transaction(self.tid(2), amount=20)]
		)

		mirror_shopify_payments(order.name, "Manual")

		second = frappe.get_doc("Payment Entry", self._payment_entries(order)[1].name)
		self.assertEqual(len(second.references), 0)
		self.assertEqual(flt(second.unallocated_amount), 20)
		message = json.loads(self._logs(order)[1].response_data)["message"]
		self.assertIn("nothing was allocated", message)
		self.assertNotIn("against", message)

	# --- dates ----------------------------------------------------------------------------

	def test_the_created_date_is_used_when_there_is_no_processed_date(self):
		_account, order = self._make_synced_order(
			[transaction(self.tid(1), amount=10, processedAt=None, createdAt="2026-10-02T09:00:00Z")]
		)

		mirror_shopify_payments(order.name, "Manual")

		self.assertEqual(
			str(frappe.get_doc("Payment Entry", self._payment_entries(order)[0].name).posting_date),
			"2026-10-02",
		)

	def test_the_posting_date_is_the_processed_date_in_site_time(self):
		self.assertEqual(
			frappe.utils.get_system_timezone(), "Asia/Kolkata"
		)  # UTC+5:30: 23:30Z is already the next day
		_account, order = self._make_synced_order(
			[transaction(self.tid(1), amount=10, processedAt="2026-10-01T23:30:00Z")]
		)

		mirror_shopify_payments(order.name, "Manual")

		entry = frappe.get_doc("Payment Entry", self._payment_entries(order)[0].name)
		self.assertEqual((str(entry.posting_date), str(entry.reference_date)), ("2026-10-02", "2026-10-02"))

	# --- isolation and errors -------------------------------------------------------------

	def test_an_unexpected_failure_on_one_transaction_leaves_no_orphan_and_the_next_still_runs(self):
		_account, order = self._make_synced_order(
			[transaction(self.tid(1), amount=100), transaction(self.tid(2), amount=50)]
		)
		real_submit = PaymentEntry.submit
		calls = []

		def submit_failing_once(entry):
			calls.append(entry.name)
			if len(calls) == 1:
				raise RuntimeError("submit exploded")
			return real_submit(entry)

		with patch.object(PaymentEntry, "submit", submit_failing_once):
			results = mirror_shopify_payments(order.name, "Manual")

		self.assertEqual([r["status"] for r in results], ["Error", "Success"])
		self.assertEqual([e.reference_no for e in self._payment_entries(order)], [self.tid(2)])
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name, "sync_type": "Payment"},
			fields=["status", "traceback"],
			order_by="creation asc",
		)
		self.assertEqual([log.status for log in logs], ["Error", "Success"])
		self.assertIn("submit exploded", logs[0].traceback)
		self.assertFalse(logs[1].traceback)

	def test_an_erpnext_validation_error_is_an_invalid_row_with_the_plain_message(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=100)])

		def reject(entry):
			frappe.throw("<b>ERPNext</b> says no")

		with patch.object(PaymentEntry, "submit", reject):
			results = mirror_shopify_payments(order.name, "Manual")

		self.assertEqual(results[0]["status"], "Invalid")
		self.assertEqual(results[0]["message"], "ERPNext says no")
		self.assertEqual(self._payment_entries(order), [])
		logs = self._logs(order)
		self.assertEqual([log.status for log in logs], ["Invalid"])
		self.assertFalse(frappe.local.message_log)

	def test_a_lost_savepoint_still_writes_the_log_row_and_continues(self):
		_account, order = self._make_synced_order(
			[transaction(self.tid(1), amount=100), transaction(self.tid(2), amount=50)]
		)
		real_rollback = frappe.db.rollback
		failed = []

		def rollback_without_savepoint(*args, **kwargs):
			if kwargs.get("save_point") and not failed:
				failed.append(1)
				raise Exception("SAVEPOINT marketplace_shopify_payment does not exist")
			return real_rollback(*args, **kwargs)

		real_submit = PaymentEntry.submit
		calls = []

		def submit_failing_once(entry):
			calls.append(1)
			if len(calls) == 1:
				raise RuntimeError("boom")
			return real_submit(entry)

		with (
			patch.object(PaymentEntry, "submit", submit_failing_once),
			patch.object(frappe.db, "rollback", rollback_without_savepoint),
		):
			results = mirror_shopify_payments(order.name, "Manual")

		self.assertEqual([r["status"] for r in results], ["Error", "Success"])
		self.assertIn("already rolled back", results[0]["message"])
		self.assertEqual(len(self._logs(order)), 2)

	# --- duplicate guard, PermissionError row and retry (#425) ----------------------------------

	def _manual_payment_entry(self, order, amount):
		"""A submitted Receive Payment Entry allocated to the order's Sales Order, with no transaction id."""
		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
		total = flt(sales_order.rounded_total or sales_order.grand_total)
		entry = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Receive",
				"company": sales_order.company,
				"party_type": "Customer",
				"party": sales_order.customer,
				"mode_of_payment": self.mode_of_payment,
				"paid_from": get_party_account("Customer", sales_order.customer, sales_order.company),
				"paid_to": self.cash,
				"paid_amount": amount,
				"received_amount": amount,
				"references": [
					{
						"reference_doctype": "Sales Order",
						"reference_name": sales_order.name,
						"total_amount": total,
						"outstanding_amount": total,
						"allocated_amount": amount,
					}
				],
			}
		)
		entry.insert()
		entry.submit()
		return entry

	def test_a_manual_payment_entry_covering_the_amount_blocks_a_second_entry(self):
		account, order = self._make_synced_order([transaction(self.tid(1), amount=495)])
		manual = self._manual_payment_entry(order, 495)

		results = mirror_shopify_payments(order.name, "Manual")

		self.assertEqual([r["status"] for r in results], ["Invalid"])
		self.assertIn("No second Payment Entry was created", results[0]["message"])
		self.assertEqual(self._payment_entries(order), [])
		self.assertEqual([log.status for log in self._logs(order)], ["Invalid"])
		accounts = {account.name: account}
		attention = _collect_payment_errors(accounts, _shopify_order_accounts(accounts))
		self.assertEqual([row["problem_type"] for row in attention], ["Payment Error"])

		# A later sync repeats the same problem without a second log row.
		mirror_shopify_payments(order.name, "Manual")
		self.assertEqual(len(self._logs(order)), 1)
		self.assertEqual(frappe.db.get_value("Payment Entry", manual.name, "docstatus"), 1)

	def test_a_manual_entry_covering_the_rounded_total_blocks_the_unrounded_shopify_amount(self):
		"""Shopify 494.21 against a Sales Order rounded to 494: a manual entry can never allocate more than 494."""
		_account, order = self._make_synced_order(
			[transaction(self.tid(1), amount=494.21)], transaction_amount=74.99, shipping_total=419.22
		)
		self._manual_payment_entry(order, 494)

		results = mirror_shopify_payments(order.name, "Manual")

		self.assertEqual([r["status"] for r in results], ["Invalid"], results)
		self.assertIn("No second Payment Entry was created", results[0]["message"])
		self.assertEqual(self._payment_entries(order), [])
		self.assertEqual(results[0]["guard_required"], 494)

	def test_manual_payment_totals_counts_an_entry_allocated_to_a_sales_invoice(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=495)])
		invoice = make_sales_invoice(order.erpnext_sales_order)
		invoice.insert()
		invoice.submit()
		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
		entry = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Receive",
				"company": sales_order.company,
				"party_type": "Customer",
				"party": sales_order.customer,
				"mode_of_payment": self.mode_of_payment,
				"paid_from": invoice.debit_to,
				"paid_to": self.cash,
				"paid_amount": 300,
				"received_amount": 300,
				"references": [
					{
						"reference_doctype": "Sales Invoice",
						"reference_name": invoice.name,
						"total_amount": invoice.grand_total,
						"outstanding_amount": invoice.outstanding_amount,
						"allocated_amount": 300,
					}
				],
			}
		)
		entry.insert()
		entry.submit()

		self.assertEqual(manual_payment_totals([sales_order.name]), {sales_order.name: 300})

	def test_a_smaller_manual_payment_entry_does_not_block_the_mirrored_entry(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=495)])
		self._manual_payment_entry(order, 100)

		results = mirror_shopify_payments(order.name, "Manual")

		self.assertEqual([r["status"] for r in results], ["Success"])
		self.assertEqual(len(self._payment_entries(order)), 1)

	def test_mirrored_entries_of_other_transactions_never_block_each_other(self):
		_account, order = self._make_synced_order(
			[transaction(self.tid(1), amount=100), transaction(self.tid(2), amount=100)]
		)

		results = mirror_shopify_payments(order.name, "Manual")

		self.assertEqual([r["status"] for r in results], ["Success", "Success"])

	def test_a_permission_error_is_logged_before_it_is_raised(self):
		account, order = self._make_synced_order([transaction(self.tid(1), amount=100)])

		def deny(entry):
			raise frappe.PermissionError("denied")

		with patch.object(PaymentEntry, "submit", deny), self.assertRaises(frappe.PermissionError):
			mirror_shopify_payments(order.name, "Manual")

		logs = self._logs(order)
		self.assertEqual([log.status for log in logs], ["Invalid"])
		self.assertIn("Permission was denied", logs[0].response_data)
		self.assertEqual(self._payment_entries(order), [])
		accounts = {account.name: account}
		attention = _collect_payment_errors(accounts, _shopify_order_accounts(accounts))
		self.assertEqual([row["problem_type"] for row in attention], ["Payment Error"])

		# The same failure again does not add a second row.
		with patch.object(PaymentEntry, "submit", deny), self.assertRaises(frappe.PermissionError):
			mirror_shopify_payments(order.name, "Manual")
		self.assertEqual(len(self._logs(order)), 1)

	def test_the_automatic_wrapper_swallows_a_permission_error_but_the_row_is_kept(self):
		from marketplace_integrations.services.sales_order_sync import _mirror_shopify_payments_independently

		_account, order = self._make_synced_order([transaction(self.tid(1), amount=100)])

		def deny(entry):
			raise frappe.PermissionError("denied")

		with patch.object(PaymentEntry, "submit", deny):
			_mirror_shopify_payments_independently(order.name, "Manual")

		self.assertEqual([log.status for log in self._logs(order)], ["Invalid"])

	def _failing_submit(self, error=frappe.QueryDeadlockError):
		def explode(entry):
			raise error("deadlock")

		return patch.object(PaymentEntry, "submit", explode)

	def test_a_failed_payment_is_retried_by_the_sweep_without_touching_the_sales_order_sync(self):
		from marketplace_integrations.services import sales_order_sync_debounce as debounce

		_account, order = self._make_synced_order([transaction(self.tid(1), amount=100)])
		before = frappe.db.get_value(
			"Marketplace Order",
			order.name,
			["erpnext_sync_status", "sync_debounce_until", "sync_retry_count", "last_sync_error"],
			as_dict=True,
		)
		with self._failing_submit():
			self.assertEqual(mirror_shopify_payments(order.name, "Manual")[0]["status"], "Error")
		self.assertIn(order.name, debounce._payment_retry_candidates())

		with patch.object(debounce.frappe, "enqueue") as enqueue:
			debounce._enqueue_payment_retries()
		jobs = [call.kwargs["marketplace_order"] for call in enqueue.call_args_list]
		self.assertIn(order.name, jobs)

		debounce._retry_payment_mirroring(order.name)

		self.assertEqual([e.reference_no for e in self._payment_entries(order)], [self.tid(1)])
		self.assertEqual([log.status for log in self._logs(order)], ["Error", "Success"])
		self.assertNotIn(order.name, debounce._payment_retry_candidates())
		after = frappe.db.get_value(
			"Marketplace Order",
			order.name,
			["erpnext_sync_status", "sync_debounce_until", "sync_retry_count", "last_sync_error"],
			as_dict=True,
		)
		self.assertEqual(after, before)

	def test_payment_retries_stop_after_three_attempts_and_the_error_row_stays(self):
		from marketplace_integrations.services import sales_order_sync_debounce as debounce

		account, order = self._make_synced_order([transaction(self.tid(1), amount=100)])
		with self._failing_submit():
			mirror_shopify_payments(order.name, "Manual")
			for _attempt in range(debounce.PAYMENT_RETRY_ATTEMPTS):
				self.assertIn(order.name, debounce._payment_retry_candidates())
				debounce._retry_payment_mirroring(order.name)

		self.assertEqual([log.status for log in self._logs(order)], ["Error"] * 4)
		self.assertNotIn(order.name, debounce._payment_retry_candidates())
		accounts = {account.name: account}
		attention = _collect_payment_errors(accounts, _shopify_order_accounts(accounts))
		self.assertEqual([row["problem_type"] for row in attention], ["Payment Error"])

	def test_payment_retries_do_not_restart_when_old_error_rows_leave_the_window(self):
		from marketplace_integrations.services import sales_order_sync_debounce as debounce

		_account, order = self._make_synced_order([transaction(self.tid(1), amount=100)])
		with self._failing_submit():
			mirror_shopify_payments(order.name, "Manual")
			for _attempt in range(debounce.PAYMENT_RETRY_ATTEMPTS):
				debounce._retry_payment_mirroring(order.name)
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name, "sync_type": "Payment"},
			pluck="name",
			order_by="creation asc",
		)
		self.assertEqual(len(logs), 4)
		# The 60-minute window slides past the first failures; the run is still 4 consecutive Errors.
		old = add_to_date(now_datetime(), minutes=-(debounce.PAYMENT_RETRY_WINDOW_MINUTES + 30))
		frappe.db.set_value("Marketplace Sync Log", logs[0], "attempted_at", old)
		frappe.db.set_value("Marketplace Sync Log", logs[1], "attempted_at", old)

		self.assertNotIn(order.name, debounce._payment_retry_candidates())

	def test_a_run_of_errors_that_started_over_an_hour_ago_is_not_retried(self):
		from marketplace_integrations.services import sales_order_sync_debounce as debounce

		_account, order = self._make_synced_order([transaction(self.tid(1), amount=100)])
		with self._failing_submit():
			mirror_shopify_payments(order.name, "Manual")
		logs = frappe.get_all("Marketplace Sync Log", filters={"marketplace_order": order.name}, pluck="name")
		log = logs[0]
		old = add_to_date(now_datetime(), minutes=-(debounce.PAYMENT_RETRY_WINDOW_MINUTES + 30))
		frappe.db.set_value("Marketplace Sync Log", log, "attempted_at", old)

		self.assertNotIn(order.name, debounce._payment_retry_candidates())

	def test_a_run_whose_first_error_is_old_is_not_retried_although_the_last_is_recent(self):
		from marketplace_integrations.services import sales_order_sync_debounce as debounce

		_account, order = self._make_synced_order([transaction(self.tid(1), amount=100)])
		with self._failing_submit():
			mirror_shopify_payments(order.name, "Manual")
			mirror_shopify_payments(order.name, "Manual")
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name, "sync_type": "Payment"},
			pluck="name",
			order_by="creation asc",
		)
		self.assertEqual(len(logs), 2)
		frappe.db.set_value(
			"Marketplace Sync Log", logs[0], "attempted_at", add_to_date(now_datetime(), minutes=-90)
		)
		frappe.db.set_value(
			"Marketplace Sync Log", logs[1], "attempted_at", add_to_date(now_datetime(), minutes=-5)
		)

		self.assertNotIn(order.name, debounce._payment_retry_candidates())

	def test_an_order_without_a_submitted_sales_order_is_not_a_retry_candidate(self):
		from marketplace_integrations.services import sales_order_sync_debounce as debounce

		_account, order = self._make_synced_order([transaction(self.tid(1), amount=100)])
		with self._failing_submit():
			mirror_shopify_payments(order.name, "Manual")
		self.assertIn(order.name, debounce._payment_retry_candidates())

		frappe.db.set_value("Sales Order", order.erpnext_sales_order, "docstatus", 2)
		self.assertNotIn(order.name, debounce._payment_retry_candidates())

		frappe.db.set_value("Marketplace Order", order.name, "erpnext_sales_order", None)
		self.assertNotIn(order.name, debounce._payment_retry_candidates())

	def test_a_duplicate_guard_row_is_retried_once_the_covering_entry_is_cancelled(self):
		from marketplace_integrations.services import sales_order_sync_debounce as debounce

		_account, order = self._make_synced_order([transaction(self.tid(1), amount=495)])
		manual = self._manual_payment_entry(order, 495)
		self.assertEqual(mirror_shopify_payments(order.name, "Manual")[0]["status"], "Invalid")
		# The guard still blocks: not a candidate.
		self.assertNotIn(order.name, debounce._payment_retry_candidates())

		manual.cancel()
		self.assertEqual(debounce._payment_retry_candidates(), {order.name: [self.tid(1)]})
		with patch.object(debounce.frappe, "enqueue") as enqueue:
			debounce._enqueue_payment_retries()
		self.assertEqual(enqueue.call_args.kwargs["marketplace_order"], order.name)
		self.assertEqual(enqueue.call_args.kwargs["transaction_ids"], [self.tid(1)])

		debounce._retry_payment_mirroring(order.name, [self.tid(1)])

		self.assertEqual([e.reference_no for e in self._payment_entries(order)], [self.tid(1)])
		self.assertEqual([log.status for log in self._logs(order)], ["Invalid", "Success"])
		# The run has ended: a second sweep finds nothing.
		self.assertEqual(debounce._payment_retry_candidates(), {})

	def test_a_guard_row_that_is_blocked_again_is_not_a_candidate_and_adds_no_row(self):
		from marketplace_integrations.services import sales_order_sync_debounce as debounce

		_account, order = self._make_synced_order([transaction(self.tid(1), amount=495)])
		first = self._manual_payment_entry(order, 495)
		mirror_shopify_payments(order.name, "Manual")
		first.cancel()
		self.assertIn(order.name, debounce._payment_retry_candidates())

		# Covered again before the retry runs: the re-run is blocked with the same message.
		self._manual_payment_entry(order, 495)
		debounce._retry_payment_mirroring(order.name, [self.tid(1)])

		self.assertEqual([log.status for log in self._logs(order)], ["Invalid"])
		self.assertEqual(self._payment_entries(order), [])
		self.assertNotIn(order.name, debounce._payment_retry_candidates())

	def test_a_guard_row_of_a_transaction_that_is_no_longer_pending_is_not_a_candidate(self):
		from marketplace_integrations.services import sales_order_sync_debounce as debounce

		_account, order = self._make_synced_order([transaction(self.tid(1), amount=495)])
		manual = self._manual_payment_entry(order, 495)
		mirror_shopify_payments(order.name, "Manual")
		manual.cancel()
		self.assertIn(order.name, debounce._payment_retry_candidates())

		# A re-run could neither record it nor write a row, so it must not be queued every minute.
		frappe.db.set_value("Marketplace Order", order.name, "payment_transactions", frappe.as_json([]))
		self.assertEqual(debounce._payment_retry_candidates(), {})

	def test_only_a_transient_failure_is_retried(self):
		from marketplace_integrations.services import sales_order_sync_debounce as debounce

		_account, order = self._make_synced_order([transaction(self.tid(1), amount=100)])
		with self._failing_submit(RuntimeError):
			result = mirror_shopify_payments(order.name, "Manual")[0]

		self.assertEqual(result["status"], "Error")
		self.assertFalse(result["transient"])
		self.assertNotIn(order.name, debounce._payment_retry_candidates())

	def test_a_retry_attempts_only_the_failing_transactions(self):
		from marketplace_integrations.services import sales_order_sync_debounce as debounce

		_account, order = self._make_synced_order(
			[transaction(self.tid(1), amount=100), transaction(self.tid(2), amount=50)]
		)
		real_submit = PaymentEntry.submit
		calls = []

		def submit_failing_once(entry):
			calls.append(1)
			if len(calls) == 1:
				raise frappe.QueryDeadlockError("deadlock")
			return real_submit(entry)

		with patch.object(PaymentEntry, "submit", submit_failing_once):
			results = mirror_shopify_payments(order.name, "Manual")
		self.assertEqual([r["status"] for r in results], ["Error", "Success"])
		self.assertEqual(debounce._payment_retry_candidates(), {order.name: [self.tid(1)]})

		with (
			patch.object(debounce, "_payment_retry_candidates", return_value={order.name: [self.tid(1)]}),
			patch.object(debounce.frappe, "enqueue") as enqueue,
		):
			debounce._enqueue_payment_retries()
		self.assertEqual(enqueue.call_args.kwargs["transaction_ids"], [self.tid(1)])

		results = mirror_shopify_payments(order.name, "Manual", [self.tid(1)])
		self.assertEqual([(r["transaction_id"], r["status"]) for r in results], [(self.tid(1), "Success")])

	def test_a_repeated_invalid_result_adds_only_one_log_row(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=100, gateway="Unmapped")])

		for _attempt in range(3):
			self.assertEqual(mirror_shopify_payments(order.name, "Manual")[0]["status"], "Invalid")

		self.assertEqual([log.status for log in self._logs(order)], ["Invalid"])

	def test_an_invalid_payment_row_is_not_retried(self):
		from marketplace_integrations.services import sales_order_sync_debounce as debounce

		_account, order = self._make_synced_order([transaction(self.tid(1), amount=100, gateway="Unmapped")])
		self.assertEqual(mirror_shopify_payments(order.name, "Manual")[0]["status"], "Invalid")

		self.assertNotIn(order.name, debounce._payment_retry_candidates())

	# --- other guards -----------------------------------------------------------------------

	def test_a_draft_sales_order_is_a_blocker(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=10)])
		frappe.db.set_value("Sales Order", order.erpnext_sales_order, "docstatus", 0)

		mirror_shopify_payments(order.name, "Manual")

		self._assert_one_invalid_log(order, "not submitted")

	def test_an_order_of_another_platform_returns_nothing(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=10)])
		frappe.db.set_value("Marketplace Order", order.name, "platform", "Talabat")

		self.assertEqual(mirror_shopify_payments(order.name, "Manual"), [])
		self.assertEqual(self._payment_entries(order), [])
		self.assertEqual(self._logs(order), [])

	def test_zero_negative_and_missing_amounts_and_a_blank_id_are_blockers(self):
		cases = {
			"zero": transaction(self.tid(1), amount=0),
			"negative": transaction(self.tid(2), amount=-5),
			"blank id": transaction("", amount=10),
		}
		for label, txn in cases.items():
			with self.subTest(case=label):
				_account, order = self._make_synced_order([txn])
				results = mirror_shopify_payments(order.name, "Manual")
				self.assertEqual([r["status"] for r in results], ["Invalid"])
				self.assertEqual(self._payment_entries(order), [])

	def test_a_clearing_account_in_another_currency_is_a_blocker_at_payment_time(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=10)])
		original = frappe.db.get_value("Account", self.cash, "account_currency")
		frappe.db.set_value("Account", self.cash, "account_currency", "USD")
		try:
			mirror_shopify_payments(order.name, "Manual")
		finally:
			frappe.db.set_value("Account", self.cash, "account_currency", original)

		self._assert_one_invalid_log(order, "Company currency")

	def test_a_receivable_account_in_another_currency_is_a_blocker(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=10)])
		customer = frappe.db.get_value("Sales Order", order.erpnext_sales_order, "customer")
		receivable = get_party_account("Customer", customer, self.company)
		original = frappe.db.get_value("Account", receivable, "account_currency")
		frappe.db.set_value("Account", receivable, "account_currency", "USD")
		try:
			mirror_shopify_payments(order.name, "Manual")
		finally:
			frappe.db.set_value("Account", receivable, "account_currency", original)

		self._assert_one_invalid_log(order, "receivable account")

	# --- blockers -----------------------------------------------------------------------

	def _assert_one_invalid_log(self, order, text):
		self.assertEqual(self._payment_entries(order), [])
		logs = self._logs(order)
		self.assertEqual([log.status for log in logs], ["Invalid"])
		self.assertIn(text, json.loads(logs[0].response_data)["message"])

	def test_an_unmapped_gateway_is_a_blocker_naming_the_gateway(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=10, gateway="paymob")])

		mirror_shopify_payments(order.name, "Manual")

		self._assert_one_invalid_log(order, '"paymob"')

	def test_a_renamed_gateway_is_not_matched_by_similarity(self):
		_account, order = self._make_synced_order(
			[transaction(self.tid(1), amount=10, gateway=KASHIER.lower())]
		)

		mirror_shopify_payments(order.name, "Manual")

		self._assert_one_invalid_log(order, KASHIER.lower())

	def test_a_transaction_currency_other_than_the_company_currency_is_a_blocker(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=10, currency="USD")])

		mirror_shopify_payments(order.name, "Manual")

		self._assert_one_invalid_log(order, "USD")

	def test_a_clearing_account_that_became_unusable_is_a_blocker(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=10)])
		frappe.db.set_value("Account", self.cash, "disabled", 1)
		try:
			mirror_shopify_payments(order.name, "Manual")
		finally:
			frappe.db.set_value("Account", self.cash, "disabled", 0)

		self._assert_one_invalid_log(order, "disabled")

	def test_a_closed_sales_order_is_a_blocker(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=10)])
		frappe.db.set_value("Sales Order", order.erpnext_sales_order, "status", "Closed")

		mirror_shopify_payments(order.name, "Manual")

		self._assert_one_invalid_log(order, "closed")

	def test_a_cancelled_sales_order_is_a_blocker(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=10)])
		frappe.get_doc("Sales Order", order.erpnext_sales_order).cancel()

		mirror_shopify_payments(order.name, "Manual")

		self._assert_one_invalid_log(order, "cancelled")

	def test_more_than_one_submitted_sales_invoice_is_a_blocker(self):
		_account, order = self._make_synced_order(
			[transaction(self.tid(1), amount=10)], transaction_amount=36, shipping_total=0, quantity=2
		)
		for _i in range(2):
			invoice = make_sales_invoice(order.erpnext_sales_order)
			invoice.items[0].qty = 1
			invoice.insert()
			invoice.submit()

		mirror_shopify_payments(order.name, "Manual")

		self._assert_one_invalid_log(order, "more than one submitted Sales Invoice")

	# --- Sales Invoice allocation ---------------------------------------------------------

	def test_a_billed_sales_order_allocates_to_its_single_invoice(self):
		_account, order = self._make_synced_order([transaction(self.tid(1), amount=200)])
		invoice = make_sales_invoice(order.erpnext_sales_order)
		invoice.insert()
		invoice.submit()
		self.assertEqual(invoice.outstanding_amount, 495)

		mirror_shopify_payments(order.name, "Manual")

		entry = frappe.get_doc("Payment Entry", self._payment_entries(order)[0].name)
		self.assertEqual(
			[(r.reference_doctype, r.reference_name) for r in entry.references],
			[("Sales Invoice", invoice.name)],
		)
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", invoice.name, "outstanding_amount")), 295)
		self.assertEqual(flt(entry.unallocated_amount), 0)

	# --- Marketplace Account form -----------------------------------------------------------

	def _account_with_rows(self, *rows):
		self.fixtures.suffix = frappe.generate_hash(length=8)  # a fresh Shopify Connection domain per account
		account = self.fixtures._make_shopify_account(self.fixtures._income_account())
		for gateway, clearing_account in rows:
			account.append(
				"shopify_payment_gateways",
				{
					"shopify_gateway": gateway,
					"mode_of_payment": self.mode_of_payment,
					"clearing_account": clearing_account,
				},
			)
		return account

	def test_account_form_rejects_a_duplicate_trimmed_gateway(self):
		account = self._account_with_rows(("Kashier", self.cash), ("  Kashier ", self.cash))

		with self.assertRaises(frappe.ValidationError) as caught:
			account.save()
		self.assertIn("more than once", str(caught.exception))

	def test_account_form_trims_the_gateway(self):
		account = self._account_with_rows((" Kashier ", self.cash))
		account.save()
		self.assertEqual(account.shopify_payment_gateways[0].shopify_gateway, "Kashier")

	def test_account_form_rejects_unusable_clearing_accounts(self):
		group = frappe.get_all(
			"Account",
			filters={"company": self.company, "is_group": 1, "account_type": ["in", ["Bank", "Cash"]]},
			pluck="name",
			limit=1,
		)
		income = self.fixtures._income_account()
		cases = {"Bank or Cash": income}
		if group:
			cases["group account"] = group[0]
		for label, clearing_account in cases.items():
			with self.subTest(case=label):
				account = self._account_with_rows(("Kashier", clearing_account))
				with self.assertRaises(frappe.ValidationError):
					account.save()

		account = self._account_with_rows(("Kashier", self.cash))
		frappe.db.set_value("Account", self.cash, "disabled", 1)
		try:
			with self.assertRaises(frappe.ValidationError) as caught:
				account.save()
		finally:
			frappe.db.set_value("Account", self.cash, "disabled", 0)
		self.assertIn("disabled", str(caught.exception))

	def test_account_form_rejects_a_clearing_account_of_another_company(self):
		account = self._account_with_rows(("Kashier", self.cash))
		frappe.db.set_value("Account", self.cash, "company", "Another Company")
		try:
			with self.assertRaises(frappe.ValidationError) as caught:
				account.save()
		finally:
			frappe.db.set_value("Account", self.cash, "company", self.company)
		self.assertIn("does not belong to Company", str(caught.exception))

	def test_account_form_rejects_a_clearing_account_in_another_currency(self):
		account = self._account_with_rows(("Kashier", self.cash))
		original = frappe.db.get_value("Account", self.cash, "account_currency")
		frappe.db.set_value("Account", self.cash, "account_currency", "USD")
		try:
			with self.assertRaises(frappe.ValidationError) as caught:
				account.save()
		finally:
			frappe.db.set_value("Account", self.cash, "account_currency", original)
		self.assertIn("Company currency", str(caught.exception))

	def test_clearing_account_problem_rejects_another_company_and_a_missing_account(self):
		self.assertIsNone(clearing_account_problem(self.cash, self.company))
		self.assertIn("does not belong to Company", clearing_account_problem(self.cash, "Another Company"))
		self.assertIn("Set the ERPNext Company", clearing_account_problem(self.cash, None))
		self.assertIn("does not exist", clearing_account_problem("No Such Account", self.company))


class IntegrationTestShopifyPaymentsPermissions(IntegrationTestCase):
	"""The order is loaded before work/permissions are known; role users are cleaned up."""

	def test_create_without_submit_requires_an_existing_order_before_permission_check(self):
		email, _role = make_role_user(self, "Payment Creator", {"Payment Entry": ["create"]})
		with as_user(email), self.assertRaises(frappe.DoesNotExistError):
			mirror_shopify_payments("MKTORD-DOES-NOT-EXIST", "Manual")

	def test_create_and_submit_pass_the_permission_check(self):
		email, _role = make_role_user(self, "Payment Submitter", {"Payment Entry": ["create", "submit"]})
		with as_user(email), self.assertRaises(frappe.DoesNotExistError):
			mirror_shopify_payments("MKTORD-DOES-NOT-EXIST", "Manual")

	def test_submit_without_create_requires_an_existing_order_before_permission_check(self):
		email, _role = make_role_user(self, "Payment Submit Only", {"Payment Entry": ["submit"]})
		with as_user(email), self.assertRaises(frappe.DoesNotExistError):
			mirror_shopify_payments("MKTORD-DOES-NOT-EXIST", "Manual")
