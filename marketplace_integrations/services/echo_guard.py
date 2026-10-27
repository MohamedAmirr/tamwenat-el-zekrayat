# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Expected-change marker for the Talabat and Noon outbound calls (#364).

An outbound call that changes the platform order stores a hash of the state we sent on the
Marketplace Order (``expected_change_hash``, plus ``expected_change_scope`` for what it
covers).  The next webhook, import or poll hashes the incoming platform state the same way:
equal means the change is our own echo, so the order is saved without the sync-status
re-plan and without a debounce bump, and the Sales Order sync is queued to run after the commit.  The
marker is cleared on every comparison, equal or not.  The rules and the fields hashed per
platform are in ``docs/marketplace-integrations/sync-status.md``.

Separate from the Shopify ``outbound_edit_*`` markers and from ``outbound_cancelled_on``.
"""

import hashlib
import json

import frappe
from frappe.utils import cstr, flt
from frappe.utils.background_jobs import is_job_enqueued

from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order

ORDER_DOCTYPE = "Marketplace Order"

TALABAT_CANCELLED = "CANCELLED"
TALABAT_UPDATE_CART = "UPDATE_CART"
# Talabat cancels an order itself when every item of an UPDATE_CART is NOT_FOUND (OpenAPI).
TALABAT_AUTO_CANCEL_REASON = "ITEM_UNAVAILABLE"
_PRESENT = "present"
_PRESENT_STATUSES = ("IN_CART", "ADDITION")
_NOT_FOUND = "NOT_FOUND"


def digest(state):
	return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


# -- Talabat ----------------------------------------------------------------


def _talabat_item_state(sku, pricing_type, status, quantity, weight):
	status = cstr(status).strip()
	status_class = _PRESENT if status in _PRESENT_STATUSES else status
	pricing_type = cstr(pricing_type).strip()
	if status_class != _PRESENT:
		return [cstr(sku).strip(), pricing_type, status_class]
	if pricing_type == "KG":
		# Weighted pieces are not hashed: the UI always sends them, Talabat may omit them.
		return [cstr(sku).strip(), pricing_type, status_class, flt(weight, 6)]
	return [cstr(sku).strip(), pricing_type, status_class, flt(quantity, 6)]


def _talabat_state(status, reason, items):
	status = cstr(status).strip()
	if status == TALABAT_CANCELLED:
		return {"status": status, "reason": cstr(reason).strip()}
	return {"status": status, "items": sorted(items, key=json.dumps)}


def talabat_sent_hash(payload):
	"""Hash of the order update we PUT, or ``None`` when it is not covered.

	Only ``CANCELLED`` and ``UPDATE_CART`` are covered.  An item without a SKU cannot be
	matched against the echo (items are keyed by SKU), so such an update is not covered.
	"""
	status = cstr(payload.get("status")).strip()
	if status not in (TALABAT_CANCELLED, TALABAT_UPDATE_CART):
		return None
	reason = cstr((payload.get("cancellation") or {}).get("reason")).strip()
	items = payload.get("items") or []
	if status == TALABAT_UPDATE_CART and items and all(
		cstr(item.get("status")).strip() == _NOT_FOUND for item in items
	):
		return digest(_talabat_state(TALABAT_CANCELLED, TALABAT_AUTO_CANCEL_REASON, []))
	if status == TALABAT_CANCELLED:
		return digest(_talabat_state(status, reason, []))
	states = []
	for item in items:
		pricing = item.get("pricing") or {}
		if not cstr(item.get("sku")).strip():
			return None
		states.append(
			_talabat_item_state(
				item.get("sku"),
				pricing.get("pricing_type"),
				item.get("status"),
				pricing.get("quantity"),
				pricing.get("weight"),
			)
		)
	return digest(_talabat_state(status, reason, states))


def talabat_incoming_hash(normalized):
	"""Hash of an incoming normalized Talabat order (``order_mapper.normalize_order``)."""
	states = [
		_talabat_item_state(
			item.get("external_sku"),
			item.get("pricing_type"),
			item.get("external_status"),
			item.get("fulfilled_quantity"),
			item.get("fulfilled_weight"),
		)
		for item in normalized.get("items") or []
	]
	return digest(_talabat_state(normalized.get("external_status"), normalized.get("cancellation_reason"), states))


def record_talabat_update(marketplace_order, payload):
	"""Store the marker for a Talabat PUT the platform accepted.  Does not commit: the caller
	commits right after, before anything that can fail (see ``send_order_update``).

	Call it after the HTTP response.  A payload that is not covered leaves any existing marker
	alone.
	"""
	expected = talabat_sent_hash(payload)
	if expected:
		record_expected_change(marketplace_order, expected, None)


def upsert_talabat_order(marketplace_account, normalized, trigger_source, fingerprint_before=None, bump=False):
	"""Upsert an incoming Talabat order through the echo guard.

	Returns ``(order, echo)``.  ``echo`` is True when the order is linked, carried our marker
	and the incoming state matched it: the Sales Order sync is then queued after commit and the
	caller must not bump the debounce.  ``fingerprint_before`` is ``existing_order_fingerprint()``
	when the caller already has it (it is read here otherwise).  With ``bump=True`` a new order,
	or one whose evidence changed, also gets the debounce bump (the webhook does its own).
	"""
	from marketplace_integrations.services.sales_order_sync_debounce import (
		bump_sync_debounce,
		existing_order_fingerprint,
		order_evidence_fingerprint,
	)

	external_order_id = normalized.get("external_order_id")
	if fingerprint_before is None:
		fingerprint_before = existing_order_fingerprint(marketplace_account, external_order_id)
	order, echo = upsert_checking_echo(
		marketplace_account,
		normalized,
		lambda scope: talabat_incoming_hash(normalized),
		trigger_source,
		fingerprint_before,
	)
	if bump and not echo and (fingerprint_before is None or order_evidence_fingerprint(order) != fingerprint_before):
		bump_sync_debounce(order.name, trigger_source)
	return order, echo


# -- Shared -------------------------------------------------------------------


def record_expected_change(marketplace_order, expected_hash, scope):
	frappe.db.set_value(
		ORDER_DOCTYPE,
		marketplace_order,
		{"expected_change_hash": expected_hash, "expected_change_scope": scope},
		update_modified=False,
	)


def clear_expected_change(marketplace_order):
	"""Clear the marker straight in the database (``before_save`` restores a stale copy's)."""
	frappe.db.set_value(
		ORDER_DOCTYPE,
		marketplace_order,
		{"expected_change_hash": None, "expected_change_scope": None},
		update_modified=False,
	)


def consume_expected_change(marketplace_account, external_order_id, incoming_hash_fn):
	"""Compare the incoming state with the stored marker, clear it, and return True on a match.

	``incoming_hash_fn(scope)`` hashes the incoming state over the marker's scope.  No order or
	no marker returns False and changes nothing.  Does not commit.
	"""
	name = frappe.db.get_value(
		ORDER_DOCTYPE,
		{"order_identity_key": compute_order_identity_key(marketplace_account, external_order_id)},
		"name",
	)
	if not name:
		return False
	marker = frappe.db.get_value(
		ORDER_DOCTYPE, name, ["expected_change_hash", "expected_change_scope"], as_dict=True
	)
	if not marker or not marker.expected_change_hash:
		return False
	echo = incoming_hash_fn(marker.expected_change_scope) == marker.expected_change_hash
	clear_expected_change(name)
	return echo


def linked_echo(fingerprint_before, echo):
	"""An echo only matters for a linked order (``fingerprint_before`` is None otherwise)."""
	return bool(echo and fingerprint_before is not None)


def upsert_checking_echo(marketplace_account, normalized, incoming_hash_fn, trigger_source, fingerprint_before):
	"""``fingerprint_before`` is ``existing_order_fingerprint()`` (None for a new order)."""
	external_order_id = normalized.get("external_order_id")
	matched = consume_expected_change(marketplace_account, external_order_id, incoming_hash_fn)
	echo = bool(matched and fingerprint_before is not None and _is_linked(marketplace_account, external_order_id))
	order = upsert_marketplace_order(marketplace_account, normalized, skip_status_replan=echo)
	if echo:
		finish_echo(order.name, fingerprint_before, trigger_source)
	return order, echo


def _is_linked(marketplace_account, external_order_id):
	return bool(
		frappe.db.get_value(
			ORDER_DOCTYPE,
			{"order_identity_key": compute_order_identity_key(marketplace_account, external_order_id)},
			"erpnext_sales_order",
		)
	)


ECHO_SYNC_JOB_PREFIX = "marketplace-echo-sync"


def finish_echo(order_name, fingerprint_before, trigger_source):
	"""Queue the Sales Order sync that follows an echo import; no status re-plan, no debounce.

	Nothing to do when the evidence did not change.  Otherwise ``run_echo_sync`` is queued with
	``enqueue_after_commit``, so it runs only once the import (with the cleared marker) is
	committed: a failure in the sync can never roll the import back, lose the webhook payload or
	bring the marker back, and no request or webhook worker runs a full sync itself.
	"""
	from marketplace_integrations.services.sales_order_sync_debounce import order_evidence_fingerprint

	if order_evidence_fingerprint(frappe.get_doc(ORDER_DOCTYPE, order_name)) == fingerprint_before:
		return
	job_id = f"{ECHO_SYNC_JOB_PREFIX}-{order_name}"
	if is_job_enqueued(job_id):
		# A job for this order is already queued or running, and it may have read the state
		# before this echo.  The queue would drop a second one (deduplicate), so fall back to the
		# normal debounce rather than lose the change.
		from marketplace_integrations.services.sales_order_sync_debounce import bump_sync_debounce

		bump_sync_debounce(order_name, trigger_source)
		return
	frappe.enqueue(
		"marketplace_integrations.services.echo_guard.run_echo_sync",
		queue="short",
		timeout=300,
		enqueue_after_commit=True,
		job_id=job_id,
		deduplicate=True,
		marketplace_order=order_name,
		trigger_source=trigger_source,
	)


def run_echo_sync(marketplace_order, trigger_source):
	"""Background job: bring the Sales Order up to date after an echo (applies the change and
	advances the baseline).  If the sync raises, the order falls back to the normal debounce."""
	from marketplace_integrations.services.sales_order_sync import sync_sales_order
	from marketplace_integrations.services.sales_order_sync_debounce import bump_sync_debounce

	if not frappe.db.exists(ORDER_DOCTYPE, marketplace_order):
		return  # deleted between the import and this job: nothing to sync, nothing to log
	frappe.set_user("Administrator")
	try:
		sync_sales_order(marketplace_order, trigger_source=trigger_source)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Echo guard: Sales Order sync failed", message=frappe.get_traceback())
		bump_sync_debounce(marketplace_order, trigger_source)
	frappe.db.commit()
