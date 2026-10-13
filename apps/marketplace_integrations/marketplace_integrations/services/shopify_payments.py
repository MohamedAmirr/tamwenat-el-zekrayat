# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Mirror successful Shopify gateway payments into ERPNext as Payment Entries (#230).

One submitted Receive Payment Entry per Shopify transaction (SALE or CAPTURE with status
SUCCESS), posted to the clearing account mapped to the transaction's gateway.  It is
allocated to the order's single submitted Sales Invoice when there is one, otherwise to the
Sales Order as an advance; whatever exceeds the outstanding stays on the entry as an
unallocated amount.  No payout, fee or refund handling.
"""

import json

import frappe
from frappe import _
from frappe.utils import cstr, flt, fmt_money, get_datetime, getdate, now_datetime, strip_html
from frappe.utils.data import convert_utc_to_system_timezone

from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.marketplace_account import (
	clearing_account_problem,
)
from marketplace_integrations.services.sales_order_sync import SyncBlocker, _shopify_transactions

ELIGIBLE_KINDS = ("SALE", "CAPTURE")
ELIGIBLE_STATUS = "SUCCESS"

_PAYMENT_SAVEPOINT = "marketplace_shopify_payment"


def mirror_shopify_payments(marketplace_order, trigger_source):
	"""Create the missing Payment Entries of one Shopify Marketplace Order.

	Returns one result dict per attempted transaction (``transaction_id``, ``status`` of
	Success / Invalid / Error, and ``payment_entry`` or ``message``); [] when nothing needed
	attempting.  Every attempt also writes one ``Marketplace Sync Log`` row (sync_type
	``Payment``).  A transaction that already has a Payment Entry in any docstatus is
	skipped, so a cancelled entry is never re-created.
	"""
	# Checked before anything else so a user who could not submit never reaches the savepoints.
	for ptype in ("create", "submit"):
		if not frappe.has_permission("Payment Entry", ptype=ptype):
			frappe.throw(_("Not permitted"), frappe.PermissionError)

	# Serializes concurrent runs for the same order (see docs/frappe-conventions.md, Concurrency).
	frappe.db.get_value("Marketplace Order", marketplace_order, "name", for_update=True)
	order = frappe.get_doc("Marketplace Order", marketplace_order)
	if order.platform != "Shopify":
		return []

	pending = [
		txn
		for txn in _shopify_transactions(order)
		if txn.get("kind") in ELIGIBLE_KINDS
		and txn.get("status") == ELIGIBLE_STATUS
		and not frappe.db.exists(
			"Payment Entry", {"custom_marketplace_transaction_id": cstr(txn.get("id")).strip()}
		)
	]
	if not pending or not order.erpnext_sales_order:
		return []

	account = frappe.get_doc("Marketplace Account", order.marketplace_account)
	results = []
	for txn in pending:
		transaction_id = cstr(txn.get("id")).strip()
		result = {"transaction_id": transaction_id}
		frappe.db.savepoint(_PAYMENT_SAVEPOINT)
		try:
			result["payment_entry"], result["message"] = _mirror_transaction(
				order, account, txn, transaction_id
			)
			# Released strictly: a missing savepoint means the transaction was already rolled back,
			# which must end as an Error, not a Success.
			frappe.db.release_savepoint(_PAYMENT_SAVEPOINT)
			result["status"] = "Success"
		except SyncBlocker as exc:
			_rollback_payment_savepoint()
			result.update(status="Invalid", message=str(exc))
		except frappe.PermissionError:
			_rollback_payment_savepoint()
			raise
		except frappe.ValidationError as exc:
			rolled_back = _rollback_payment_savepoint()
			result.pop("payment_entry", None)
			result.update(
				status="Invalid",
				message=strip_html(cstr(exc)) or _("ERPNext rejected the Payment Entry."),
			)
			if not rolled_back:
				result["message"] += " " + _rolled_back_note()
		except Exception:
			rolled_back = _rollback_payment_savepoint()
			frappe.log_error(title=_("Shopify payment mirroring"))
			result.pop("payment_entry", None)
			result.update(
				status="Error",
				message=_(
					"Creating the Payment Entry failed. Review the ERPNext configuration and try again."
				),
				traceback=frappe.get_traceback(),
			)
			if not rolled_back:
				result["message"] += " " + _rolled_back_note()
		frappe.clear_messages()
		_create_payment_log(order, trigger_source, result)
		results.append({key: value for key, value in result.items() if key != "traceback"})
	return results


def _rolled_back_note():
	return _(
		"The database transaction was already rolled back; verify in ERPNext whether a Payment Entry exists."
	)


def _rollback_payment_savepoint():
	"""Roll back to the savepoint; False when it was already gone (nothing was undone)."""
	try:
		frappe.db.rollback(save_point=_PAYMENT_SAVEPOINT)
	except Exception as exc:
		# The savepoint can already be gone (MariaDB rolls the whole transaction back on a
		# deadlock); anything else is a real error.
		if "SAVEPOINT" not in cstr(exc).upper():
			raise
		return False
	return True


def _mirror_transaction(order, account, txn, transaction_id):
	"""Insert and submit the Payment Entry of one transaction; (name, message)."""
	if not transaction_id:
		raise SyncBlocker(_("A Shopify transaction has no id, so it cannot be mirrored."))

	sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
	if sales_order.docstatus == 2 or sales_order.status == "Cancelled":
		raise SyncBlocker(
			_("Sales Order {0} is cancelled; the payment cannot be recorded.").format(sales_order.name)
		)
	if sales_order.docstatus != 1:
		raise SyncBlocker(
			_("Sales Order {0} is not submitted; the payment cannot be recorded.").format(sales_order.name)
		)
	if sales_order.status == "Closed":
		raise SyncBlocker(
			_("Sales Order {0} is closed; the payment cannot be recorded.").format(sales_order.name)
		)

	gateway = cstr(txn.get("gateway")).strip()
	mapping = next(
		(
			row
			for row in account.shopify_payment_gateways or []
			if cstr(row.shopify_gateway).strip() == gateway
		),
		None,
	)
	if not mapping:
		raise SyncBlocker(
			_(
				'Shopify payment gateway "{0}" is not mapped on Marketplace Account {1}. Add it to '
				"Shopify Payment Gateways with its Mode of Payment and Clearing Account."
			).format(gateway, account.name)
		)
	problem = clearing_account_problem(mapping.clearing_account, sales_order.company)
	if problem:
		raise SyncBlocker(problem)

	money = (txn.get("amountSet") or {}).get("shopMoney") or {}
	company_currency = frappe.get_cached_value("Company", sales_order.company, "default_currency")
	if cstr(money.get("currencyCode")) != company_currency:
		raise SyncBlocker(
			_("Transaction currency {0} differs from the Company currency {1}.").format(
				money.get("currencyCode") or _("(blank)"), company_currency
			)
		)

	payment_entry = frappe.new_doc("Payment Entry")
	precision = payment_entry.precision("paid_amount")
	amount = flt(money.get("amount"), precision)
	if amount <= 0:
		raise SyncBlocker(_("The transaction amount must be greater than zero."))

	posting_date = _transaction_date(txn)
	reference = _allocation_target(sales_order)
	receivable_currency = frappe.get_cached_value("Account", reference["party_account"], "account_currency")
	if receivable_currency != company_currency:
		raise SyncBlocker(
			_("The receivable account {0} is in {1}, not the Company currency {2}.").format(
				reference["party_account"], receivable_currency, company_currency
			)
		)

	outstanding = flt(reference["outstanding_amount"], precision)
	allocated = flt(max(min(amount, outstanding), 0), precision)
	# A Shopify total is never rounded, but a rounded Sales Order total is: the difference is
	# written off to the Company's round-off account, so it is not left as an unallocated amount.
	write_off = 0.0
	rounding = flt(reference["rounding"], precision)
	if outstanding > 0 and rounding > 0 and amount > outstanding:
		write_off = -min(flt(amount - outstanding, precision), rounding)
	elif outstanding > 0 and rounding < 0 and amount < outstanding and outstanding - amount <= -rounding:
		write_off = flt(outstanding - amount, precision)
	if write_off:
		allocated = outstanding
		round_off_account, round_off_cost_center = frappe.get_cached_value(
			"Company", sales_order.company, ["round_off_account", "round_off_cost_center"]
		)
		if not round_off_account:
			raise SyncBlocker(
				_(
					"Company {0} has no Round Off Account, which is needed to write off the "
					"{1} rounding difference of Sales Order {2}. Set it in the Company."
				).format(
					sales_order.company,
					fmt_money(abs(write_off), currency=company_currency),
					sales_order.name,
				)
			)

	payment_entry.payment_type = "Receive"
	payment_entry.company = sales_order.company
	payment_entry.posting_date = posting_date
	payment_entry.reference_date = posting_date
	payment_entry.reference_no = transaction_id
	payment_entry.party_type = "Customer"
	payment_entry.party = sales_order.customer
	payment_entry.mode_of_payment = mapping.mode_of_payment
	payment_entry.paid_from = reference["party_account"]
	payment_entry.paid_to = mapping.clearing_account
	payment_entry.paid_amount = amount
	payment_entry.received_amount = amount
	payment_entry.custom_marketplace_order = order.name
	payment_entry.custom_marketplace_transaction_id = transaction_id
	if allocated:
		payment_entry.append(
			"references",
			{
				"reference_doctype": reference["doctype"],
				"reference_name": reference["name"],
				"total_amount": reference["total_amount"],
				"outstanding_amount": reference["outstanding_amount"],
				"allocated_amount": allocated,
			},
		)
	if write_off:
		payment_entry.append(
			"deductions",
			{
				"account": round_off_account,
				"cost_center": round_off_cost_center
				or frappe.get_cached_value("Company", sales_order.company, "cost_center"),
				"amount": write_off,
				"description": _("Rounding difference of Sales Order {0}").format(sales_order.name),
			},
		)
	payment_entry.insert()
	payment_entry.submit()

	if allocated:
		message = _("Payment Entry {0} recorded {1} against {2} {3}.").format(
			payment_entry.name,
			fmt_money(amount, currency=company_currency),
			reference["doctype"],
			reference["name"],
		)
	else:
		message = _(
			"Payment Entry {0} recorded {1}; nothing was allocated because {2} {3} has nothing outstanding."
		).format(
			payment_entry.name,
			fmt_money(amount, currency=company_currency),
			reference["doctype"],
			reference["name"],
		)
	if write_off:
		message += " " + _("{0} rounding difference was written off to {1}.").format(
			fmt_money(abs(write_off), currency=company_currency), round_off_account
		)
	if flt(payment_entry.unallocated_amount):
		message += " " + _("{0} exceeds the outstanding amount and is left unallocated.").format(
			fmt_money(payment_entry.unallocated_amount, currency=company_currency)
		)
	return payment_entry.name, message


def _allocation_target(sales_order):
	"""The document the payment is allocated to: the order's one submitted Sales Invoice, else the Sales Order."""
	invoice_names = frappe.get_all(
		"Sales Invoice Item",
		filters={"sales_order": sales_order.name, "docstatus": 1},
		pluck="parent",
		distinct=True,
	)
	invoices = (
		frappe.get_all(
			"Sales Invoice",
			filters={"name": ["in", invoice_names], "docstatus": 1, "is_return": 0},
			fields=["name", "debit_to", "rounded_total", "grand_total", "outstanding_amount"],
		)
		if invoice_names
		else []
	)
	if len(invoices) > 1:
		raise SyncBlocker(
			_(
				"Sales Order {0} has more than one submitted Sales Invoice ({1}); allocate the payment manually."
			).format(sales_order.name, ", ".join(sorted(row.name for row in invoices)))
		)
	if invoices:
		invoice = invoices[0]
		return {
			"doctype": "Sales Invoice",
			"name": invoice.name,
			"party_account": invoice.debit_to,
			"total_amount": flt(invoice.rounded_total or invoice.grand_total),
			"outstanding_amount": flt(invoice.outstanding_amount),
			"rounding": 0,
		}

	from erpnext.accounts.party import get_party_account

	total = flt(sales_order.rounded_total or sales_order.grand_total)
	return {
		"doctype": "Sales Order",
		"name": sales_order.name,
		"party_account": get_party_account("Customer", sales_order.customer, sales_order.company),
		"total_amount": total,
		"outstanding_amount": total - flt(sales_order.advance_paid),
		"rounding": flt(sales_order.grand_total) - total if sales_order.rounded_total else 0,
	}


def _transaction_date(txn):
	"""The ``processedAt`` date in site time, falling back to ``createdAt``."""
	for key in ("processedAt", "createdAt"):
		value = txn.get(key)
		if value:
			return getdate(convert_utc_to_system_timezone(get_datetime(value)))
	raise SyncBlocker(_("The Shopify transaction has neither a processed nor a created date."))


def _create_payment_log(order, trigger_source, result):
	"""One ``Marketplace Sync Log`` row per attempted transaction (ignore_permissions, like the Sales Order sync log)."""
	now = now_datetime()
	frappe.get_doc(
		{
			"doctype": "Marketplace Sync Log",
			"sync_type": "Payment",
			"marketplace_order": order.name,
			"status": result["status"],
			"trigger_source": trigger_source,
			"attempted_at": now,
			"completed_at": now,
			"request_data": json.dumps({"transaction_id": result["transaction_id"]}),
			"response_data": json.dumps(
				{key: value for key, value in result.items() if key != "traceback"}, ensure_ascii=False
			),
			"traceback": result.get("traceback"),
		}
	).insert(ignore_permissions=True)
