# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon FBPI return reference lookup and evidence (#153).

Evidence capture only: this module never creates a Credit Note, Sales
Invoice, Payment Entry, GL entry, stock ledger entry, or Sales Return, and
never touches a Sales Order (the draft Sales Return of #261 is created by a
user action on the ``Noon Return`` doctype, not here). It only calls Noon's ``ListReturnReferences`` and records what Noon
returns, linking it to an existing ``Marketplace Order`` only on an exact
``order_nr`` match. (#164) A newly linked return on an order that already
has a Sales Order also re-queues that order's sync via the debounce, so the
planner sees the return evidence.

**Documented provisional assumptions, not confirmed against a real Noon
account (carry into the release-doc verification checklist, not blocking
questions - see #153):**
- ``docs/noon-integration/noon_api_full.md``'s "Returns" section documents
  this endpoint's path/request/response shape, but its own paired ``.yml``
  entry is empty/unconfirmed - unlike every other Noon endpoint this app
  calls, there is no independently-sourced confirmation of this shape.
- What ``order_nr`` in a return item actually is (which Marketplace Order
  field it corresponds to) is assumed to be either ``fbpi_order_nr``
  (``external_order_id``) or ``mp_order_nr`` (``platform_order_reference``),
  per #153's locked scope - never confirmed by Noon's own docs.
- The daily AWB scan's entire premise - that an ``awb_nr``-typed reference
  in a return item means the outbound shipment AWB this app itself
  generated/recorded - is explicitly unverified (#153 scope: "experimental").
- Whether ``merchant_codes`` actually behaves as documented (omitting it
  falls back to the connection's ``default_project_code`` scope) is also
  unconfirmed.

See ``docs/noon-integration/fulfillment-release.md``'s Returns section for
the full verification checklist.
"""

import hashlib
import json
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from marketplace_integrations.integrations.noon.catalog import get_account, validate_connection_enabled
from marketplace_integrations.integrations.noon.client import NoonClient
from marketplace_integrations.integrations.noon.constants import (
	AWB_SCAN_LOOKBACK_DAYS,
	MERCHANT_CODES_MAX,
	MERCHANT_CODES_SCAN_ORDERS,
	RETURN_REFERENCES_LIST_PATH,
)
from marketplace_integrations.marketplace_integrations.doctype.noon_return.noon_return import (
	compute_return_identity_key,
)
from marketplace_integrations.services.sales_order_sync_debounce import bump_sync_debounce

RQ_JOB_PREFIX = "marketplace-noon-return"


# ---------------------------------------------------------------------------
# merchant_codes derivation
# ---------------------------------------------------------------------------


def _account_merchant_codes(account):
	"""Derive ``merchant_codes`` from every distinct ``merchant_code`` value
	seen in this account's ``MERCHANT_CODES_SCAN_ORDERS`` most recently modified
	imported Noon orders' ``latest_payload``
	(order-level field - ``order_mapper.py``'s ``REQUIRED_ORDER_FIELDS``
	requires every imported order to carry one).

	Returns ``None`` (omit the field entirely) when the account has no
	imported orders yet, relying on Noon's documented
	``default_project_code`` scoping per #153's locked scope - this
	omit-when-empty behavior is itself one of the unconfirmed assumptions
	listed in this module's docstring.

	Noon's own contract caps ``merchant_codes`` at 200 items. Orders are
	read most-recently-modified first, and a given code's FIRST occurrence
	in that order wins its position - so the list is implicitly ordered by
	recency, and truncating to ``MERCHANT_CODES_MAX`` keeps the 200 most
	recently seen codes rather than an arbitrary/unstable subset. Exceeding
	the cap is itself worth flagging (it means this account's derived scope
	may be stale/incomplete), so it is also logged to the Error Log.
	"""
	rows = frappe.get_all(
		"Marketplace Order",
		filters={"marketplace_account": account.name, "platform": "Noon"},
		fields=["latest_payload"],
		order_by="modified desc",
		limit=MERCHANT_CODES_SCAN_ORDERS,
	)
	if not rows:
		return None
	codes = []
	seen = set()
	for row in rows:
		raw = row.latest_payload
		if not raw:
			continue
		try:
			payload = json.loads(raw) if isinstance(raw, str) else raw
		except (TypeError, ValueError):
			continue
		if not isinstance(payload, dict):
			continue
		code = cstr(payload.get("merchant_code")).strip()
		if code and code not in seen:
			seen.add(code)
			codes.append(code)
	if not codes:
		return None
	if len(codes) > MERCHANT_CODES_MAX:
		frappe.log_error(
			title="Noon returns: merchant_codes exceeds Noon's 200-item cap",
			message=(
				f"Marketplace Account {account.name} has {len(codes)} distinct merchant_codes across "
				f"its imported Noon orders - truncated to the {MERCHANT_CODES_MAX} most recently seen. "
				"The derived scope may be stale/incomplete for older merchant_codes."
			),
		)
		codes = codes[:MERCHANT_CODES_MAX]
	return codes


# ---------------------------------------------------------------------------
# ListReturnReferences call + evidence log
# ---------------------------------------------------------------------------


def _log_return_lookup(
	account, barcode, request_body, status, response_data=None, traceback_text=None, trigger_source="Manual"
):
	"""One committed ``Marketplace Sync Log`` row per ``ListReturnReferences``
	call (#153 scope) - mirrors ``integrations/noon/fulfillment.py``'s
	``_log_fulfillment()`` shape: never raises outward past logging itself,
	bounded request/response, no invoice-style sensitive payload here to
	exclude.

	``trigger_source`` defaults to ``"Manual"`` (the manual barcode lookup
	path) but is threaded through from the caller (same pattern
	``integrations/noon/orders.py`` already uses) so the daily AWB scan logs
	with ``trigger_source="Scheduled Reconciliation"`` instead."""
	try:
		now = now_datetime()
		frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Return Lookup",
				"marketplace_account": account.name,
				"status": status,
				"trigger_source": trigger_source,
				"attempted_at": now,
				"completed_at": now,
				"request_data": frappe.as_json({"barcode": barcode, **request_body}),
				"response_data": frappe.as_json(response_data) if response_data is not None else None,
				"traceback": traceback_text,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="Noon return lookup: failed to write sync log", message=frappe.get_traceback())
	else:
		frappe.db.commit()


def list_return_references(account, barcode, merchant_codes=None, trigger_source="Manual"):
	"""Call Noon's ``ListReturnReferences`` for one barcode (a scanned
	barcode for a manual lookup, or an outbound shipment AWB for the daily
	scan) and write one committed ``Marketplace Sync Log`` row for the call
	(#153 scope - "each ListReturnReferences call writes one committed
	Marketplace Sync Log row").

	``merchant_codes`` defaults to ``None``, which means "derive it myself"
	via ``_account_merchant_codes(account)`` - correct for a one-off manual
	lookup. ``run_awb_scan()`` instead computes ``merchant_codes`` ONCE per
	scan run and passes it in explicitly, so this function is never called
	N times (once per AWB) with its own M-order-scanning derivation - that
	N x M recomputation was itself the bug being fixed here.

	Returns the response's ``items[]`` list (possibly empty - an empty list
	is a Success, not an error). Raises on a transport error, non-2xx
	response, or malformed body - the Error log row is already written
	before raising in every one of those cases, so the caller never needs
	to log the failure itself.
	"""
	barcode = cstr(barcode).strip()
	if not barcode:
		frappe.throw(_("A barcode is required."))

	if merchant_codes is None:
		merchant_codes = _account_merchant_codes(account)
	body = {"barcode": barcode}
	if merchant_codes:
		body["merchant_codes"] = merchant_codes

	client = NoonClient(account.connection)
	try:
		response = client.request("POST", RETURN_REFERENCES_LIST_PATH, json=body)
	except Exception:
		_log_return_lookup(
			account,
			barcode,
			body,
			"Error",
			traceback_text=frappe.get_traceback(),
			trigger_source=trigger_source,
		)
		raise

	if response.status_code != 200:
		message = client.error_message(response)
		_log_return_lookup(
			account, barcode, body, "Error", traceback_text=message, trigger_source=trigger_source
		)
		frappe.throw(message)

	try:
		data = response.json()
	except ValueError:
		message = _("Noon returned an invalid return reference response.")
		_log_return_lookup(
			account, barcode, body, "Error", traceback_text=message, trigger_source=trigger_source
		)
		frappe.throw(message)

	if not isinstance(data, dict) or not isinstance(data.get("items"), list):
		message = _("Noon returned an invalid return reference response.")
		_log_return_lookup(
			account, barcode, body, "Error", traceback_text=message, trigger_source=trigger_source
		)
		frappe.throw(message)

	items = data["items"]
	_log_return_lookup(
		account,
		barcode,
		body,
		"Success",
		response_data={"item_count": len(items)},
		trigger_source=trigger_source,
	)
	return items


# ---------------------------------------------------------------------------
# Order linking + evidence upsert
# ---------------------------------------------------------------------------


def _resolve_order_link(account, order_nr):
	"""Exact match of ``order_nr`` against either ``external_order_id``
	(``fbpi_order_nr``) or ``platform_order_reference`` (``mp_order_nr``) on
	a Marketplace Order scoped to this account and ``platform == "Noon"``
	(#153 locked scope) - no fuzzy fallback.

	Requires EXACTLY one match across both fields combined. ``order_nr`` is
	not guaranteed unique against ``external_order_id``/``platform_order_reference``
	considered together (e.g. one order's ``external_order_id`` collides with
	a different order's ``platform_order_reference``) - a naive
	``frappe.db.get_value()`` lookup would silently return an arbitrary first
	match in that case. Zero matches or more than one match both leave the
	return unlinked rather than force-attaching to a guess.

	This is a plain read, never a write to Marketplace Order - so it does
	not need ``fulfillment.py``'s ``_lock_and_load_order()`` row-locking
	pattern, which exists to protect WRITES that could race with
	``Document.save()``'s child-table re-sync (docs/frappe-conventions.md).
	Nothing here ever saves a Marketplace Order document.
	"""
	order_nr = cstr(order_nr).strip()
	if not order_nr:
		return None
	names = frappe.get_all(
		"Marketplace Order",
		filters={"marketplace_account": account.name, "platform": "Noon"},
		or_filters=[{"external_order_id": order_nr}, {"platform_order_reference": order_nr}],
		pluck="name",
	)
	if len(names) == 1:
		return names[0]
	return None


def _as_json(value):
	"""Same JSON-field-write helper as ``services/marketplace_orders.py``'s
	own ``_as_json()`` - a JSON field only auto-serializes a ``dict`` on
	save, never a ``list`` (docs/frappe-conventions.md), so a references
	array must be explicitly ``frappe.as_json()``'d before assignment."""
	if value is None or isinstance(value, str):
		return value
	return frappe.as_json(value)


def upsert_return_item(account, item, found_via):
	"""Create or update one ``Noon Return`` row from one
	``ListReturnReferences`` response item, keyed by
	``(marketplace_account, purchase_item_nr)`` (#153 dedupe key).

	A re-lookup of the same ``purchase_item_nr`` updates the existing row's
	``last_seen_on``/``references``/etc. rather than creating a duplicate.
	This is a check-then-act, retry-based safety, NOT inherently race-safe
	just because ``return_identity_key`` carries a hidden unique constraint
	(docs/frappe-conventions.md's composite-uniqueness pattern): two
	concurrent callers (manual lookup + AWB scan, or two manual lookups) can
	both read no existing row for the same ``purchase_item_nr`` and both
	attempt an insert. In practice that collision surfaces as
	``NoonReturn.validate()``'s own duplicate precheck raising a plain
	``frappe.ValidationError`` (it runs its own ``frappe.db.exists()`` check
	before the raw INSERT ever reaches the DB's unique constraint) - but the
	raw DB constraint itself (``frappe.UniqueValidationError``, a
	``ValidationError`` subclass) and ``frappe.DuplicateEntryError`` are
	caught too, for whichever one actually fires. One caller's update can
	also race the other's insert under MariaDB's
	``innodb_snapshot_isolation=ON`` and raise error 1020
	(``frappe.QueryDeadlockError``) - caught alongside the others. Any of
	these roll back the transaction and retry the save exactly once as an
	update against whatever now exists post-rollback - the loser of the race
	becomes a normal update instead of crashing. A second failure on the
	retry is not caught again and propagates to the caller (``run_awb_scan``'s
	own per-AWB try/except is the backstop for that case).

	A successful save commits immediately so that this rollback-and-retry,
	if it has to run for a LATER item in the same batch, can never also
	discard an EARLIER item in that same batch that already saved
	successfully.

	A return with no exact ``order_nr`` match is stored unlinked
	(``marketplace_order`` blank) - never force-attached to a best-guess
	order.

	Returns the saved ``Noon Return`` document, or ``None`` if ``item`` has
	no ``purchase_item_nr`` to key on (a malformed entry is skipped, not
	fatal to the rest of the batch).
	"""
	purchase_item_nr = cstr(item.get("purchase_item_nr")).strip()
	if not purchase_item_nr:
		return None

	partner_sku = cstr(item.get("partner_sku")).strip()
	order_nr = cstr(item.get("order_nr")).strip()
	mp_code = cstr(item.get("mp_code")).strip()
	merchant_code = cstr(item.get("merchant_code")).strip()
	references = _as_json(item.get("references"))
	marketplace_order = _resolve_order_link(account, order_nr)
	identity_key = compute_return_identity_key(account.name, purchase_item_nr)

	def _save(existing_name):
		now = now_datetime()
		if existing_name:
			doc = frappe.get_doc("Noon Return", existing_name)
		else:
			doc = frappe.new_doc("Noon Return")
			doc.marketplace_account = account.name
			doc.purchase_item_nr = purchase_item_nr
			doc.first_seen_on = now

		doc.partner_sku = partner_sku
		doc.order_nr = order_nr
		doc.mp_code = mp_code
		doc.merchant_code = merchant_code
		doc.references = references
		doc.found_via = found_via
		previous_order = doc.marketplace_order
		# Never unlink: a later sighting that resolves no (or an ambiguous) order must not
		# drop the link of a return that may already be inspected or have documents drafted.
		linked_order = marketplace_order or previous_order
		doc.marketplace_order = linked_order
		doc.last_seen_on = now
		doc.save(ignore_permissions=True)
		# #164: a return newly linked to an order that already has a Sales Order is
		# planner evidence, so re-plan that order. A repeat sighting of an unchanged
		# link (the daily AWB scan) does not re-bump. Same transaction as the save.
		if (
			linked_order
			and linked_order != previous_order
			and frappe.db.get_value("Marketplace Order", linked_order, "erpnext_sales_order")
		):
			bump_sync_debounce(
				linked_order,
				{"Manual Lookup": "Manual", "Webhook": "Noon Webhook"}.get(
					found_via, "Scheduled Reconciliation"
				),
			)
		frappe.db.commit()
		return doc

	existing_name = frappe.db.get_value("Noon Return", {"return_identity_key": identity_key}, "name")
	try:
		return _save(existing_name)
	except (frappe.ValidationError, frappe.DuplicateEntryError, frappe.QueryDeadlockError):
		frappe.db.rollback()
		existing_name = frappe.db.get_value("Noon Return", {"return_identity_key": identity_key}, "name")
		return _save(existing_name)


# ---------------------------------------------------------------------------
# Manual barcode lookup
# ---------------------------------------------------------------------------


def _upsert_items(account, items, found_via):
	"""Upsert each returned item on its own, so one bad item (a duplicate-key race,
	an over-long Noon field, a future validation error) is logged and skipped
	instead of dropping the rest of the batch, like the AWB scan does per AWB."""
	for item in items:
		if not isinstance(item, dict):
			continue
		try:
			upsert_return_item(account, item, found_via)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="Noon return lookup: item failed", message=frappe.get_traceback())


def run_webhook_lookup(account_name, barcode):
	"""The enqueued worker target for a ``RETURNS::REFERENCE_UPDATE`` event (#263).

	Same read-only lookup as the manual one, recorded with ``found_via="Webhook"``
	and ``trigger_source="Noon Webhook"``. The barcode comes from an unverified,
	AI-described payload field; the receiver only passes a non-empty string."""
	frappe.set_user("Administrator")
	account = get_account(account_name)
	validate_connection_enabled(account)
	items = list_return_references(account, barcode, trigger_source="Noon Webhook")
	_upsert_items(account, items, "Webhook")


def queue_webhook_lookup(account_name, barcode):
	"""Queue a deduplicated lookup for one webhook barcode on the ``long`` queue.

	No permission check: the caller is the webhook receiver, which has already
	authenticated the request with the connection's key. Never makes a Noon HTTP
	call itself. Returns whether a new job was queued."""
	job_id_hash = hashlib.sha256(f"{account_name}:{barcode}".encode()).hexdigest()[:16]
	job = frappe.enqueue(
		run_webhook_lookup,
		queue="long",
		timeout=300,
		job_id=f"{RQ_JOB_PREFIX}-webhook-{job_id_hash}",
		deduplicate=True,
		account_name=account_name,
		barcode=barcode,
	)
	return job is not None


def run_manual_lookup(account_name, barcode):
	"""The enqueued worker target for a manual barcode lookup - switches to
	Administrator (same rationale as every other Noon background worker in
	this app) since this runs detached from whatever Desk session originally
	queued it."""
	frappe.set_user("Administrator")
	account = get_account(account_name)
	validate_connection_enabled(account)
	items = list_return_references(account, barcode)
	_upsert_items(account, items, "Manual Lookup")


def queue_manual_lookup(account_name, barcode):
	"""Queue a manual ``ListReturnReferences`` lookup for one barcode on the
	``long`` queue, deduplicated by a job_id keyed on account + barcode
	(#153 scope) - never makes a Noon HTTP call in the calling request
	itself."""
	account = get_account(account_name)
	account.check_permission("write")
	validate_connection_enabled(account)
	barcode = cstr(barcode).strip()
	if not barcode:
		frappe.throw(_("A barcode is required."))

	# A deterministic hash (not frappe.generate_hash(), which is a random
	# string generator, not a function of its input) - two concurrent lookup
	# requests for the same account + barcode must collide on the same
	# job_id for RQ's own deduplicate=True to actually catch them.
	job_id_hash = hashlib.sha256(f"{account.name}:{barcode}".encode()).hexdigest()[:16]
	job = frappe.enqueue(
		run_manual_lookup,
		queue="long",
		timeout=300,
		job_id=f"{RQ_JOB_PREFIX}-lookup-{job_id_hash}",
		deduplicate=True,
		account_name=account.name,
		barcode=barcode,
	)
	return {"queued": job is not None}


# ---------------------------------------------------------------------------
# Daily AWB scan (experimental, #153 scope)
# ---------------------------------------------------------------------------


def _account_awb_numbers(account):
	"""Distinct AWB numbers (``lr_no``) of this account's Noon Delivery Notes (#415; the old shipment child
	rows are gone) - scoped to this account's Noon orders only, never another account's or another
	platform's AWBs.

	#153 review finding #3: further scoped to notes whose shipment is live (Created, Cancellation Pending or
	Unknown - a cancelled note's outbound AWB is no longer an active one a return could plausibly reference
	going forward) and modified within the last ``AWB_SCAN_LOOKBACK_DAYS`` days, so the daily scan stays
	bounded instead of re-scanning every AWB this account has ever had.
	"""
	from marketplace_integrations.integrations.noon.delivery_note_shipment import recent_awbs_of_orders

	order_names = frappe.get_all(
		"Marketplace Order",
		filters={"marketplace_account": account.name, "platform": "Noon"},
		pluck="name",
	)
	if not order_names:
		return []

	cutoff = now_datetime() - timedelta(days=AWB_SCAN_LOOKBACK_DAYS)
	return recent_awbs_of_orders(order_names, cutoff)


def _item_has_matching_awb_reference(item, awb_nr):
	"""Whether ``item``'s ``references[]`` contains an ``awb_nr``-typed entry
	whose ``barcode`` equals the AWB this scan searched for (#153 scope -
	"AWB-scan result without a matching awb_nr reference is ignored"). This
	is the experimental, unverified assumption flagged in this module's
	docstring: that an ``awb_nr``-typed reference means the outbound
	shipment AWB this app itself recorded."""
	references = item.get("references")
	if not isinstance(references, list):
		return False
	return any(
		isinstance(ref, dict)
		and ref.get("barcode_type") == "awb_nr"
		and cstr(ref.get("barcode")).strip() == awb_nr
		for ref in references
	)


def run_awb_scan(account_name):
	"""The enqueued worker target for one account's daily AWB scan.
	Isolates one AWB's lookup-AND-upsert failure from the rest of the scan
	(same per-unit isolation rationale as every other bulk Noon run in this
	app) - a transport/HTTP failure, or an ``upsert_return_item()`` failure
	(duplicate-key race, a Noon field exceeding a Data field's length, any
	future validation error) for one AWB is logged and skipped, not fatal to
	the rest of the AWBs on this account.

	#153 review finding #3: ``merchant_codes`` is derived ONCE per scan run
	(not once per AWB - that was an N x M recomputation, N AWBs times M
	imported orders scanned each time) and passed into every
	``list_return_references()`` call below."""
	frappe.set_user("Administrator")
	account = get_account(account_name)
	validate_connection_enabled(account)
	merchant_codes = _account_merchant_codes(account)
	for awb_nr in _account_awb_numbers(account):
		try:
			items = list_return_references(
				account, awb_nr, merchant_codes=merchant_codes, trigger_source="Scheduled Reconciliation"
			)
			for item in items:
				if not isinstance(item, dict):
					continue
				if not _item_has_matching_awb_reference(item, awb_nr):
					continue
				upsert_return_item(account, item, "AWB Scan")
		except Exception:
			frappe.log_error(title="Noon return scan by AWB: lookup failed", message=frappe.get_traceback())
			continue


def scan_awbs_all_accounts():
	"""Daily ``hooks.py`` scheduler entry (#153 scope) - deliberately
	lightweight, same shape as ``integrations/noon/orders.py``'s
	``poll_all_accounts()``: lists enabled Noon accounts and enqueues one
	deduplicated background job per account on the ``long`` queue, then
	returns immediately. It never lists AWBs or calls Noon itself."""
	accounts = frappe.get_all(
		"Marketplace Account",
		filters={"platform": "Noon", "connection_doctype": "Noon Connection", "enabled": 1},
		pluck="name",
		order_by="name asc",
	)
	queued = 0
	for account_name in accounts:
		try:
			job = frappe.enqueue(
				run_awb_scan,
				queue="long",
				timeout=3600,
				job_id=f"{RQ_JOB_PREFIX}-awbscan-{account_name}",
				deduplicate=True,
				account_name=account_name,
			)
		except Exception:
			frappe.log_error(
				title="Noon return AWB scan: failed to enqueue account", message=frappe.get_traceback()
			)
			continue
		if job is not None:
			queued += 1
	return {"queued": queued}
