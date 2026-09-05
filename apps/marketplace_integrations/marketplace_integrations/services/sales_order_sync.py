# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Explicit Marketplace Order to ERPNext Sales Order synchronization.

Marketplace Order remains Talabat's operational record.  This module creates
or updates only a Draft Sales Order after an authorized user explicitly asks
for it.

Commercial values are the historical, finalized marketplace transaction, not
whatever ERPNext's current Item Price/Pricing Rules would compute today:

    Marketplace Account.erpnext_selling_price_list = catalog offer/reference
    Marketplace Order fulfilled pricing            = historical accepted transaction
    ERPNext Sales Order                            = explicit copy of the final transaction

ERPNext's standard Sales Order controller (`set_missing_values()`) is used
only once, to resolve structural defaults (Item/UOM/conversion, accounts,
currency, company/customer party details) - never to price a line.  Every
line's `rate`/`price_list_rate` is then explicitly overwritten with the
Talabat fulfilled effective rate, `ignore_pricing_rule` is set, and totals
are recalculated from that overridden rate.  The current Selling Price List
remains only a pricing *context* on the Sales Order header (matching
Marketplace Account's configured reference), never the historical-order rate
source.
"""

import hashlib

import frappe
from frappe import _
from frappe.utils import cstr, flt, getdate, now_datetime

from marketplace_integrations.marketplace_integrations.doctype.marketplace_product_mapping.marketplace_product_mapping import (
	compute_mapping_identity_key,
)
from marketplace_integrations.services.account_warehouse import validate_account_warehouse

FINAL_ITEM_STATUSES = {"IN_CART", "ADDITION"}

# `effective_rate = fulfilled_total_price / fulfilled_quantity` is an
# intermediate value that ERPNext later multiplies back by `qty` and rounds
# to the Sales Order Item `amount` field's own currency precision
# (`_amount_precision()`, 2dp on this site). Rounding the division itself
# down to that SAME 2dp precision before the multiply is a double-rounding
# bug: e.g. 9.99 / 2 = 4.995, which rounds to 5.00 at 2dp, and 5.00 * 2 =
# 10.00 != 9.99 - `_verify_line_amounts()` then blocks a routine,
# non-evenly-divisible discounted line as though it were a real data
# problem. Rounding the division to 6dp instead (comfortably higher than any
# realistic currency precision) keeps the rounding remainder from the
# division below the final amount precision's rounding threshold for any
# realistic single order-line quantity, so `rate * qty`, rounded once more to
# `amount`'s precision, reliably reconstructs `fulfilled_total_price` exactly.
#
# This alone is not sufficient, though: ERPNext's own
# `calculate_taxes_and_totals()` unconditionally re-rounds `Sales Order
# Item.rate`/`price_list_rate` down to their OWN field precision
# (`round_floats_in()`, called before `calculate_item_rate()`, confirmed
# directly against the installed `erpnext/controllers/taxes_and_totals.py`)
# on every `calculate_taxes_and_totals()` call - including the one inside
# `AccountsController.validate()` that reruns on every `.insert()`/`.save()`.
# Since neither field has an explicit `precision` in ERPNext's own doctype
# JSON, both default to the site's `currency_precision` (2dp here) - the
# SAME precision as `amount` - so a 6dp Python value assigned to
# `so_item.rate` before calling `calculate_taxes_and_totals()` would
# otherwise be silently re-truncated back to 2dp regardless of how precisely
# it was computed, reproducing the exact same bug this rounds to fix.
# `fixtures/property_setter.json` therefore raises `Sales Order Item.rate`
# and `.price_list_rate`'s own field precision to `EFFECTIVE_RATE_PRECISION`
# via two Property Setters (never edit `erpnext`/`frappe` doctype JSON
# directly), so ERPNext's internal rounding no longer clips them below what
# this module already computed - verified end-to-end against the real
# `calculate_taxes_and_totals()` call, not assumed (see
# `docs/frappe-conventions.md`). `amount` itself is deliberately left at the
# site's normal `currency_precision` - only the two rate fields need the
# escape hatch.
EFFECTIVE_RATE_PRECISION = 6


class SyncBlocker(frappe.ValidationError):
	"""A user-actionable prerequisite that prevents Sales Order synchronization."""


def sync_sales_order(marketplace_order):
	"""Create or update one Marketplace Order's linked Draft Sales Order.

	The Marketplace Order row is locked before it is reloaded, so concurrent
	button clicks observe a successful link rather than creating a second Sales
	Order.  Expected blockers are persisted as sanitized sync state and returned
	to the API without raising, so that status write is not rolled back.
	"""
	frappe.db.get_value("Marketplace Order", marketplace_order, "name", for_update=True)
	order = frappe.get_doc("Marketplace Order", marketplace_order)

	try:
		account = _get_account(order)
		_validate_sync_permissions(order, account)
		_validate_account_configuration(account)
		lines = _build_sales_order_lines(order, account)
		customer = _get_or_create_customer(account)
		snapshot = _make_snapshot(account, customer, lines, _transaction_date(order), _delivery_date(order))
		current_hash = _snapshot_hash(snapshot)

		if order.erpnext_sales_order:
			sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
			if order.last_synced_hash == current_hash:
				return _save_success(order, current_hash, sales_order.name, updated=False)
			if sales_order.docstatus != 0:
				return _save_submitted_blocker(order)
			_apply_snapshot(sales_order, snapshot)
			_verify_line_amounts(sales_order, snapshot, account)
			sales_order.save()
			return _save_success(order, current_hash, sales_order.name, updated=True)

		sales_order = frappe.new_doc("Sales Order")
		_apply_snapshot(sales_order, snapshot)
		_verify_line_amounts(sales_order, snapshot, account)
		sales_order.insert()
		return _save_success(order, current_hash, sales_order.name, updated=False)
	except SyncBlocker as exc:
		return _save_failure(order, str(exc))
	except frappe.PermissionError:
		raise
	except Exception:
		# Deliberately no `message=` kwarg: frappe.log_error() only calls
		# frappe.get_traceback() itself when `message` is falsy - passing a
		# static string there (as this used to) replaces the real traceback
		# in the Error Log with that string, discarding the actual diagnostic
		# content for every plausible ERPNext-side throw this can catch
		# (warehouse/company mismatch, negative rate, below-cost validation,
		# delivery_date < transaction_date, etc.). The user-facing message
		# below is unaffected either way - it never came from this call.
		frappe.log_error(title=_("Marketplace Sales Order synchronization"))
		return _save_failure(
			order,
			_(
				"ERPNext Sales Order synchronization failed. Please review the ERPNext configuration and try again."
			),
		)


def build_sales_order_snapshot(order, account=None, customer=None):
	"""Return the deterministic ERPNext-commercial input for an order.

	No Talabat customer PII, status, address, raw payload, or current Item
	Price belongs in the snapshot.  The historical Talabat fulfilled amount
	for each final line is the only commercial rate input.
	"""
	account = account or _get_account(order)
	_validate_account_configuration(account)
	lines = _build_sales_order_lines(order, account)
	customer = customer or _require_customer(account)
	return _make_snapshot(account, customer, lines, _transaction_date(order), _delivery_date(order))


def _build_sales_order_lines(order, account):
	"""Validate and return the final UNIT lines that affect a Sales Order.

	The authoritative Talabat line amount is `fulfilled_total_price`: zero is
	a valid, final amount, a negative amount is never valid, and a genuinely
	missing (null) amount blocks the line. `effective_rate =
	fulfilled_total_price / fulfilled_quantity`, rounded to
	`EFFECTIVE_RATE_PRECISION` (see the module-level comment above that
	constant for why this is a fixed 6dp value rather than the resulting
	Sales Order Item's own currency precision).

	The null-vs-zero distinction cannot be read off `fulfilled_total_price`
	itself: it is a Frappe Float column (NOT NULL DEFAULT 0), so a genuinely
	absent Talabat `pricing.total_price` is already coerced to `0.0` by the
	time it reaches this row - indistinguishable here from a real zero
	fulfilled amount. `fulfilled_total_price_missing` is computed once, at
	parse time against the raw payload, before that coercion happens (see
	`integrations/talabat/order_mapper.py`), and is the only reliable signal
	for "missing" (#85 review finding #1).

	A row with a nonzero `fulfilled_unit_price` but a zero
	`fulfilled_total_price` is treated as suspicious/likely-missing data, not
	an honest free item: `fulfilled_total_price` is itself a NOT NULL Float
	column (see above), so a genuinely-missing Talabat total is
	indistinguishable from a real zero once persisted - but a nonzero unit
	price on the same row is strong evidence the total was supposed to be
	nonzero too. A row where both are genuinely `0` still passes through as a
	legitimate free item.
	"""

	lines = []
	for row in order.items or []:
		if row.external_status not in FINAL_ITEM_STATUSES:
			continue
		if row.pricing_type == "KG":
			raise SyncBlocker(
				_(
					"Talabat KG line {0} ({1}) cannot be synchronized until its ERPNext UOM setup is configured."
				).format(row.external_item_id, _line_identifier(row))
			)
		if row.pricing_type != "UNIT":
			raise SyncBlocker(
				_("Talabat line {0} ({1}) has an unsupported pricing type.").format(
					row.external_item_id, _line_identifier(row)
				)
			)
		if flt(row.fulfilled_quantity) <= 0:
			raise SyncBlocker(
				_(
					"Talabat line {0} ({1}) needs a positive fulfilled quantity before it can be synchronized."
				).format(row.external_item_id, _line_identifier(row))
			)
		# Both conditions matter: fulfilled_total_price_missing is the reliable
		# signal once a row has round-tripped through the DB's NOT NULL Float
		# coercion (see the module docstring above), but `is None` is kept
		# too as defence-in-depth for an in-memory caller that hasn't gone
		# through that save/reload yet - dropping it would leave exactly that
		# caller's `fulfilled_total_price=None`/flag-not-yet-set row treated
		# as a valid free line via `flt(None) == 0` (#85 review finding #8).
		if row.fulfilled_total_price_missing or row.fulfilled_total_price is None:
			raise SyncBlocker(
				_("Talabat line {0} ({1}) has no fulfilled total price and cannot be synchronized.").format(
					row.external_item_id, _line_identifier(row)
				)
			)
		if flt(row.fulfilled_total_price) < 0:
			raise SyncBlocker(
				_(
					"Talabat line {0} ({1}) has a negative fulfilled total price and cannot be synchronized."
				).format(row.external_item_id, _line_identifier(row))
			)
		if flt(row.fulfilled_total_price) == 0 and flt(row.fulfilled_unit_price) > 0:
			raise SyncBlocker(
				_(
					"Talabat line {0} ({1}) has a nonzero unit price but a zero fulfilled total - this looks "
					"like missing pricing data, not a free item. Verify the Talabat order data before "
					"synchronizing."
				).format(row.external_item_id, _line_identifier(row))
			)

		mapping = _get_enabled_mapping(account.name, row.external_sku)
		if not mapping:
			raise SyncBlocker(
				_("No enabled Marketplace Product Mapping exists for Talabat SKU {0} on line {1}.").format(
					_line_identifier(row), row.external_item_id
				)
			)
		item_uom = mapping.sales_uom or frappe.get_cached_value("Item", mapping.item, "stock_uom")
		fulfilled_total_price = flt(row.fulfilled_total_price)
		effective_rate = flt(fulfilled_total_price / flt(row.fulfilled_quantity), EFFECTIVE_RATE_PRECISION)
		lines.append(
			{
				"external_item_id": cstr(row.external_item_id).strip(),
				"item_code": mapping.item,
				"qty": flt(row.fulfilled_quantity),
				"uom": item_uom,
				"warehouse": account.warehouse,
				"fulfilled_total_price": fulfilled_total_price,
				"effective_rate": effective_rate,
			}
		)

	if not lines:
		raise SyncBlocker(_("No final Talabat order lines are available to synchronize."))
	return sorted(lines, key=lambda line: line["external_item_id"])


def _resolved_currency(account):
	"""Return the currency the resulting Sales Order will actually be in.

	ERPNext's real resolution order (confirmed directly against the
	installed `erpnext/accounts/party.py::get_party_details()`, called via
	`selling_controller.set_missing_values()`) is Customer.default_currency
	FIRST, falling back to the Sales Order's already-set `currency` (which
	itself is only ever backfilled from the Price List when still blank -
	never true here, since party details are resolved before that), and
	only finally to the Company's default currency. This function does not
	need to read `Customer.default_currency` itself to mirror that, though:
	`_validate_account_configuration()` requires both the Price List's
	currency AND the account's Customer's `default_currency` (when either is
	set) to already equal the Company's default currency (#85 review finding
	#1/#9, V1 single-currency assumption) - so by the time this runs, every
	link in that chain that could matter is guaranteed to already agree, and
	reading the Price List/Company currency here is equivalent to reading
	whichever of the three ERPNext would actually pick.

	Resolved from account/Price List configuration rather than a live Sales
	Order instance, so it is available before any Sales Order document
	exists (e.g. while only computing a snapshot for hash/status
	comparison).

	This is the source `_amount_precision()` (post-calculation amount
	verification) resolves its precision from (#85 review finding #8). The
	line rate itself is deliberately NOT resolved from this currency: see
	`EFFECTIVE_RATE_PRECISION`'s module-level comment for why a fixed 6dp
	value is used there instead of a currency-derived one.
	"""
	price_list_currency = frappe.get_cached_value(
		"Price List", account.erpnext_selling_price_list, "currency"
	)
	return price_list_currency or frappe.get_cached_value(
		"Company", account.erpnext_company, "default_currency"
	)


def _amount_precision(account):
	"""Return the ERPNext amount precision the resulting Sales Order Item will use."""
	return frappe.get_precision("Sales Order Item", "amount", currency=_resolved_currency(account))


def _make_snapshot(account, customer, lines, transaction_date, delivery_date):
	return {
		"company": account.erpnext_company,
		"customer": customer,
		"selling_price_list": account.erpnext_selling_price_list,
		"transaction_date": transaction_date,
		"delivery_date": delivery_date,
		"items": lines,
	}


def set_sync_status_from_current_snapshot(order, preserve_sync_error=False):
	"""Set in-memory status while Marketplace Order is being saved.

	A source-order save must never be blocked merely because a mapping or later
	ERPNext configuration has changed.  A linked order with no longer-valid
	commercial input is therefore visibly modified, not silently considered in
	sync.
	"""
	if preserve_sync_error and order.erpnext_sync_status == "Sync Error":
		return
	if not order.erpnext_sales_order or not order.last_synced_hash:
		return
	try:
		current_hash = _snapshot_hash(build_sales_order_snapshot(order))
	except SyncBlocker:
		order.erpnext_sync_status = "Modified After Sync"
		return
	order.erpnext_sync_status = "Synced" if current_hash == order.last_synced_hash else "Modified After Sync"


def get_current_sync_status(marketplace_order):
	"""Calculate the persisted order's current status for the form load."""
	order = frappe.get_doc("Marketplace Order", marketplace_order)
	if not order.erpnext_sales_order or not order.last_synced_hash:
		return order.erpnext_sync_status

	previous_status = order.erpnext_sync_status
	set_sync_status_from_current_snapshot(order, preserve_sync_error=True)
	if order.erpnext_sync_status != previous_status:
		order.flags.skip_sync_status_update = True
		order.save()
	return order.erpnext_sync_status


def _get_account(order):
	account = frappe.get_doc("Marketplace Account", order.marketplace_account)
	if not account.enabled:
		raise SyncBlocker(_("Marketplace Account {0} is disabled.").format(account.name))
	return account


def _validate_sync_permissions(order, account):
	order.check_permission("write")
	account.check_permission("write")
	sales_order_permission = "write" if order.erpnext_sales_order else "create"
	if not frappe.has_permission("Sales Order", ptype=sales_order_permission):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if not account.erpnext_customer and not frappe.has_permission("Customer", ptype="create"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def _validate_account_configuration(account):
	if not account.erpnext_company:
		raise SyncBlocker(
			_("Marketplace Account {0} needs an ERPNext Company before synchronizing orders.").format(
				account.name
			)
		)
	if not account.erpnext_selling_price_list:
		raise SyncBlocker(
			_(
				"Marketplace Account {0} needs an ERPNext Selling Price List before synchronizing orders."
			).format(account.name)
		)
	price_list = frappe.get_doc("Price List", account.erpnext_selling_price_list)
	if not price_list.enabled or not price_list.selling:
		raise SyncBlocker(
			_("ERPNext Selling Price List {0} must be enabled and marked for selling.").format(
				price_list.name
			)
		)
	# V1 assumes single-currency operation: Talabat fulfilled amounts are bare
	# numbers in the outlet's local currency, and Marketplace Order itself
	# stores no currency of its own. If the Selling Price List's currency, or
	# the account's generic Customer's own default_currency, ever diverged
	# from the Company's default currency, those bare Talabat numbers would
	# get copied as a raw rate into a different currency without any
	# conversion - so both are enforced rather than silently trusted (#85
	# review finding #9, corrected by finding #1 to also cover the Customer:
	# ERPNext actually resolves a Sales Order's own `currency` from
	# Customer.default_currency FIRST - see `_resolved_currency()` - so a
	# Customer-level override slips past a Price-List-only check entirely
	# while `rate * qty == fulfilled_total_price` still holds numerically).
	company_currency = frappe.get_cached_value("Company", account.erpnext_company, "default_currency")
	if price_list.currency and price_list.currency != company_currency:
		raise SyncBlocker(
			_(
				"ERPNext Selling Price List {0}'s currency ({1}) must match Company {2}'s default "
				"currency ({3}) before synchronizing orders. Multi-currency Sales Order synchronization "
				"is not supported."
			).format(price_list.name, price_list.currency, account.erpnext_company, company_currency)
		)
	if account.erpnext_customer:
		customer_currency = frappe.get_cached_value("Customer", account.erpnext_customer, "default_currency")
		if customer_currency and customer_currency != company_currency:
			raise SyncBlocker(
				_(
					"ERPNext Customer {0}'s default currency ({1}) must match Company {2}'s default "
					"currency ({3}) before synchronizing orders. Multi-currency Sales Order synchronization "
					"is not supported."
				).format(
					account.erpnext_customer, customer_currency, account.erpnext_company, company_currency
				)
			)
	try:
		validate_account_warehouse(account, context=_("Sales Order synchronization"))
	except frappe.ValidationError as exc:
		# validate_account_warehouse() raises via frappe.throw(), which already
		# appended its (non-escape_html'd) message to frappe.local.message_log
		# before raising - catching the exception here does not undo that.
		# Left uncleared, that raw message could still reach the client
		# alongside the properly re-raised SyncBlocker, bypassing the
		# escaping discipline #85 review finding #7 established for exactly
		# this class of message. Clear it so only the SyncBlocker's own
		# str(exc) (escaped client-side by marketplace_order.js) is shown.
		frappe.clear_messages()
		raise SyncBlocker(str(exc)) from exc


def _get_or_create_customer(account):
	if account.erpnext_customer:
		return _require_customer(account)

	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": _generic_customer_name(account),
			"customer_type": "Company",
		}
	).insert()
	account.erpnext_customer = customer.name
	account.save()
	return customer.name


def _require_customer(account):
	if not account.erpnext_customer:
		raise SyncBlocker(_("Marketplace Account {0} has no ERPNext Customer.").format(account.name))
	if not frappe.db.exists("Customer", account.erpnext_customer):
		raise SyncBlocker(
			_("ERPNext Customer {0} configured on Marketplace Account {1} does not exist.").format(
				account.erpnext_customer, account.name
			)
		)
	return account.erpnext_customer


def _generic_customer_name(account):
	return _("Marketplace Account {0}").format(account.name)


def _get_enabled_mapping(marketplace_account, external_sku):
	external_sku = cstr(external_sku).strip()
	if not external_sku:
		return None
	key = compute_mapping_identity_key(marketplace_account, external_sku)
	values = frappe.db.get_value(
		"Marketplace Product Mapping",
		{"mapping_identity_key": key, "enabled": 1},
		["item", "sales_uom"],
		as_dict=True,
	)
	return frappe._dict(values) if values else None


def _apply_snapshot(sales_order, snapshot):
	"""Populate a Sales Order, then overwrite every line with the historical
	Talabat transaction amount so it survives ERPNext's own recalculation.

	`set_missing_values()` is used exactly once, purely for structural
	defaults (Item/UOM/conversion, accounts, party/company details,
	currency) - not to price a line: `ignore_pricing_rule = 1` is set before
	it runs so no Pricing Rule is applied even for that first pass, and
	whatever Item Price it fetches for `rate`/`price_list_rate` is discarded
	immediately below.  `rate`/`price_list_rate`/`discount_*`/`is_free_item`
	are then explicitly overwritten from the snapshot's `effective_rate`,
	and totals are recalculated from that overridden rate.

	Because `rate`/`price_list_rate` are non-None the moment this function
	returns, ERPNext's own `AccountsController.validate()` - which reruns
	`set_missing_values(for_validate=True)` on every subsequent `.insert()`/
	`.save()` of this same document - cannot silently refill them from a
	current Item Price. This is specific to those two fields, not a general
	property of `set_missing_item_details()`: that function unconditionally
	overwrites several OTHER child row fields on every call regardless of
	their current value, via its own hard-coded `force_item_fields` tuple
	(`item_group`, `brand`, `stock_uom`, `is_fixed_asset`, `pricing_rules`,
	`weight_per_unit`, `weight_uom`, `total_weight`, `valuation_rate` -
	confirmed directly against the installed
	`erpnext/controllers/accounts_controller.py`). `rate` and
	`price_list_rate` are simply absent from that tuple, so they fall back to
	its plain `item.get(fieldname) is None` gate instead, which the explicit
	overwrite below already defeats. A separate `pricing_rule_removed` branch
	elsewhere in ERPNext resets `rate`/`discount_percentage`/`discount_amount`
	when a line's stale `pricing_rules` no longer resolve - genuinely
	unreachable here: `ignore_pricing_rule = 1` (set below) means no line
	from this function ever carries `pricing_rules` in the first place, and
	`sales_order.items` is rebuilt from scratch on every call, so no stale
	`pricing_rules` value can survive from a prior snapshot either.

	See also `docs/frappe-conventions.md`'s note on this same
	`force_item_fields`/None-check-gate/`ignore_pricing_rule` behavior.
	"""
	sales_order.company = snapshot["company"]
	sales_order.customer = snapshot["customer"]
	sales_order.selling_price_list = snapshot["selling_price_list"]
	sales_order.transaction_date = snapshot["transaction_date"]
	sales_order.delivery_date = snapshot["delivery_date"]
	sales_order.ignore_pricing_rule = 1
	# A pre-existing Draft being re-synced may carry header discount/tax/
	# shipping configuration from a prior manual Desk edit made between
	# syncs. apply_discount_amount() (erpnext/controllers/taxes_and_totals.py)
	# redistributes `discount_amount` across every item's net_amount/net_rate
	# when it is set, and a Sales Taxes and Charges Template/Shipping Rule
	# can add further header-level amounts - #85 forbids any of that
	# silently cutting the merchandise value below the Talabat transaction
	# total, so every re-sync resets all of it explicitly rather than
	# trusting whatever the existing Draft happened to have (#85 review
	# finding #3).
	sales_order.apply_discount_on = "Grand Total"
	sales_order.additional_discount_percentage = 0
	sales_order.discount_amount = 0
	sales_order.taxes_and_charges = None
	sales_order.taxes = []
	sales_order.shipping_rule = None
	sales_order.items = []
	for line in snapshot["items"]:
		sales_order.append(
			"items",
			{
				"item_code": line["item_code"],
				"qty": line["qty"],
				"uom": line["uom"],
				"warehouse": line["warehouse"],
			},
		)
	sales_order.set_missing_values()

	for line, so_item in zip(snapshot["items"], sales_order.items, strict=True):
		so_item.price_list_rate = line["effective_rate"]
		so_item.rate = line["effective_rate"]
		so_item.discount_percentage = 0
		so_item.discount_amount = 0
		so_item.is_free_item = 1 if line["effective_rate"] == 0 else 0

	# Recalculates net/grand totals from the rates just overridden above and
	# the header fields just reset - no Sales Taxes and Charges Template,
	# Shipping Rule, or additional discount survives from before this call.
	sales_order.calculate_taxes_and_totals()


def _verify_line_amounts(sales_order, snapshot, account):
	"""Block instead of silently drifting if a line's calculated amount does
	not reproduce the Talabat fulfilled total within ERPNext's own currency
	precision for that field (e.g. an effective-rate rounding remainder).

	Precision is resolved from `_amount_precision(account)` rather than
	`so_item.precision("amount")` independently, so this check and the header
	`_resolved_currency()` source can never resolve to different currencies
	(#85 review finding #8).
	"""
	precision = _amount_precision(account)
	for line, so_item in zip(snapshot["items"], sales_order.items, strict=True):
		expected = flt(line["fulfilled_total_price"], precision)
		actual = flt(so_item.amount, precision)
		if expected != actual:
			raise SyncBlocker(
				_(
					"Talabat line {0} ({1}) would post to ERPNext as {2} but the Talabat fulfilled total is {3}. "
					"This can happen when the fulfilled amount does not divide evenly by the fulfilled quantity "
					"at ERPNext's currency precision. Please review before retrying."
				).format(line["external_item_id"], so_item.item_code, actual, expected)
			)


def _snapshot_hash(snapshot):
	return hashlib.sha256(frappe.as_json(snapshot).encode()).hexdigest()


def _save_success(order, snapshot_hash, sales_order, updated):
	order.erpnext_sales_order = sales_order
	order.last_synced_hash = snapshot_hash
	order.last_erpnext_sync_on = now_datetime()
	order.erpnext_sync_status = "Synced"
	order.last_sync_error = None
	order.flags.skip_sync_status_update = True
	order.save()
	return {"ok": True, "sales_order": sales_order, "updated": updated}


def _save_submitted_blocker(order):
	message = _("ERPNext Sales Order {0} is submitted and cannot be updated from Marketplace Order.").format(
		order.erpnext_sales_order
	)
	order.erpnext_sync_status = "Modified After Sync"
	order.last_sync_error = message
	order.flags.skip_sync_status_update = True
	order.save()
	return {"ok": False, "status": order.erpnext_sync_status, "error": message}


def _save_failure(order, message):
	order.erpnext_sync_status = "Sync Error"
	order.last_sync_error = cstr(message).strip()[:500] or _("ERPNext Sales Order synchronization failed.")
	order.flags.skip_sync_status_update = True
	order.save()
	return {"ok": False, "status": order.erpnext_sync_status, "error": order.last_sync_error}


def _line_identifier(row):
	return cstr(row.external_sku).strip() or _("no SKU")


def _delivery_date(order):
	if not order.promised_for:
		raise SyncBlocker(_("Talabat Promised For is required to set the ERPNext Sales Order Delivery Date."))
	return getdate(order.promised_for)


def _transaction_date(order):
	# received_on (Talabat order.sys.created_at) is not in openapi.json's
	# required list for order.OrderSys, and the Marketplace Order field
	# itself is read-only/non-mandatory - so this is not formally guaranteed
	# to be set. In practice Talabat's own system stamps every order with a
	# creation timestamp; this guard exists as the fail-closed, actionable
	# behavior for the (currently unobserved) case where it is ever blank,
	# rather than silently defaulting the Sales Order's Transaction Date to
	# something else (#85 review finding #11).
	if not order.received_on:
		raise SyncBlocker(
			_("Talabat Received On is required to set the ERPNext Sales Order Transaction Date.")
		)
	return getdate(order.received_on)
