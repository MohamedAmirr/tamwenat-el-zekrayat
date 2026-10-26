# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Reason-code registry for ``Marketplace Order.erpnext_sync_status`` (#364).

Every reason a Marketplace Order ends in Needs Action or Sync Error (and the one
Syncing reason used while a transient failure is retried) has a stable machine code
here.  The code decides the visible status, the remediation mode, the action type and
the default instruction.  The planner (``sales_order_reconciliation.plan()``) and the
sync service pick a code; they never choose the status themselves, so the status
model stays defined in this one place.  The design is in
``docs/marketplace-integrations/sync-status.md``.

Remediation modes: ``auto_resolve`` (the app applies the fix itself), ``auto_prepare``
(the app prepares a draft for human review and never submits it) and ``manual`` (a person
decides and acts, no corrective transaction is created).
"""

from frappe import _
from frappe.utils import cstr

from marketplace_integrations.services.sync_status import (  # noqa: F401 (re-exported)
	AWAITING_RECEIPT,
	CANCELLED,
	DRAFT_PREPARED,
	NEEDS_ACTION,
	RECHECK_QUEUED,
	RESOLVED,
	SYNC_ERROR,
	SYNCED,
	SYNCING,
	WAITING_FOR_USER,
)

AUTO_RESOLVE = "auto_resolve"
AUTO_PREPARE = "auto_prepare"
MANUAL = "manual"

# Reason codes the planner/sync service use directly.
SYNC_RETRIES_EXHAUSTED = "SYNC_RETRIES_EXHAUSTED"
TRANSIENT_RETRY_SCHEDULED = "TRANSIENT_RETRY_SCHEDULED"
MAPPING_MISSING = "MAPPING_MISSING"
PRICE_MISSING = "PRICE_MISSING"
SYNC_BLOCKED = "SYNC_BLOCKED"
SNAPSHOT_LINE_MISMATCH = "SNAPSHOT_LINE_MISMATCH"
ERP_VALIDATION_FAILED = "ERP_VALIDATION_FAILED"
PERMISSION_DENIED = "PERMISSION_DENIED"
SYNC_FAILED = "SYNC_FAILED"
CANCELLED_PAID_REFUND_REQUIRED = "CANCELLED_PAID_REFUND_REQUIRED"
RETURN_REQUIRES_RECEIPT_REVIEW = "RETURN_REQUIRES_RECEIPT_REVIEW"
CANCELLED_FULLY_DELIVERED = "CANCELLED_FULLY_DELIVERED"
CANCELLED_AFTER_FULFILLMENT_DELIVERED = "CANCELLED_AFTER_FULFILLMENT_DELIVERED"
NO_BASELINE = "NO_BASELINE"
DUPLICATE_LINE_IDENTITY = "DUPLICATE_LINE_IDENTITY"
LINE_RESTORED_FROM_ZERO = "LINE_RESTORED_FROM_ZERO"
ORDER_INACTIVE = "ORDER_INACTIVE"
CLOSED_EXPECTS_FULFILLMENT = "CLOSED_EXPECTS_FULFILLMENT"
CANCELLED_AFTER_FULFILLMENT_BILLED = "CANCELLED_AFTER_FULFILLMENT_BILLED"
SHIPPING_CHANGED = "SHIPPING_CHANGED"
HEADER_CHANGED = "HEADER_CHANGED"
STRUCTURAL_LINE_CHANGE = "STRUCTURAL_LINE_CHANGE"
ROW_CHANGE_REFUSED = "ROW_CHANGE_REFUSED"
ERP_STATE_DIVERGED = "ERP_STATE_DIVERGED"
ALL_ZERO_DELIVERED = "ALL_ZERO_DELIVERED"
LIFECYCLE_BLOCKS_CHANGE = "LIFECYCLE_BLOCKS_CHANGE"
LINE_IDENTITY_CHANGED = "LINE_IDENTITY_CHANGED"
QTY_BELOW_DELIVERED = "QTY_BELOW_DELIVERED"
ALREADY_BILLED = "ALREADY_BILLED"
EXECUTION_BLOCKS_UPDATE = "EXECUTION_BLOCKS_UPDATE"
ADVANCE_BLOCKS_CANCEL = "ADVANCE_BLOCKS_CANCEL"
TOTAL_BELOW_ADVANCE = "TOTAL_BELOW_ADVANCE"
DRAFT_DOCUMENTS_BLOCK_CANCEL = "DRAFT_DOCUMENTS_BLOCK_CANCEL"
SO_LINE_MISSING = "SO_LINE_MISSING"
EDIT_OUTCOME_UNKNOWN = "EDIT_OUTCOME_UNKNOWN"
RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
VERIFY_ERP_STATE = "VERIFY_ERP_STATE"
OUTBOUND_EDIT_MISMATCH = "OUTBOUND_EDIT_MISMATCH"
SALES_ORDER_CHANGED_IN_ERP = "SALES_ORDER_CHANGED_IN_ERP"


def _entry(status, mode, action_type, instruction):
	return {"status": status, "mode": mode, "action_type": action_type, "instruction": instruction}


# The instruction is the default text (last_sync_error) when the caller has no more
# specific message; it is translated when used, see ``instruction()``.
REASONS = {
	TRANSIENT_RETRY_SCHEDULED: _entry(
		SYNCING,
		AUTO_RESOLVE,
		"WAIT_FOR_RETRY",
		"A temporary problem interrupted the sync. It will be retried automatically.",
	),
	SYNC_RETRIES_EXHAUSTED: _entry(
		SYNC_ERROR,
		AUTO_RESOLVE,
		"RETRY_SYNC",
		"The sync kept failing because of a temporary problem. Check the connection and use Retry Sync.",
	),
	MAPPING_MISSING: _entry(
		SYNC_ERROR,
		AUTO_RESOLVE,
		"FIX_MAPPING",
		"An item has no enabled Marketplace Product Mapping. Fix the mapping; the order syncs again automatically.",
	),
	PRICE_MISSING: _entry(
		SYNC_ERROR,
		AUTO_RESOLVE,
		"FIX_PRICE",
		"A line has no usable price. Refresh the order from the marketplace or correct its price.",
	),
	SYNC_BLOCKED: _entry(
		SYNC_ERROR, MANUAL, "FIX_SYNC_DATA", "The order cannot be synchronized until the problem is fixed."
	),
	SNAPSHOT_LINE_MISMATCH: _entry(
		SYNC_ERROR,
		MANUAL,
		"REVIEW_DATA_INTEGRITY",
		"The marketplace lines cannot be matched to the Sales Order rows. Review the order.",
	),
	ERP_VALIDATION_FAILED: _entry(
		SYNC_ERROR, MANUAL, "FIX_ERP_DATA", "ERPNext rejected the Sales Order change. Fix the cause and retry."
	),
	PERMISSION_DENIED: _entry(
		SYNC_ERROR,
		MANUAL,
		"GRANT_PERMISSION",
		"You do not have permission to change the linked Sales Order in ERPNext.",
	),
	SYNC_FAILED: _entry(
		SYNC_ERROR,
		AUTO_RESOLVE,
		"RETRY_SYNC",
		"ERPNext Sales Order synchronization failed. Please review the ERPNext configuration and try again.",
	),
	CANCELLED_PAID_REFUND_REQUIRED: _entry(
		NEEDS_ACTION,
		MANUAL,
		"REFUND_CUSTOMER",
		"The order was cancelled after payment. Refund the customer in the marketplace.",
	),
	RETURN_REQUIRES_RECEIPT_REVIEW: _entry(
		NEEDS_ACTION,
		AUTO_PREPARE,
		"REVIEW_RETURN_DRAFT",
		"The marketplace reported a return or refund after ERPNext delivered or billed this order. "
		"Confirm the goods were received to prepare a draft Sales Return.",
	),
	CANCELLED_FULLY_DELIVERED: _entry(
		NEEDS_ACTION,
		AUTO_PREPARE,
		"REVIEW_RETURN_DRAFT",
		"The marketplace cancelled an order ERPNext already delivered. "
		"Confirm the goods were received to prepare a draft Sales Return.",
	),
	CANCELLED_AFTER_FULFILLMENT_DELIVERED: _entry(
		NEEDS_ACTION,
		AUTO_PREPARE,
		"REVIEW_RETURN_DRAFT",
		"The marketplace cancelled the order after fulfillment and ERPNext has delivery. "
		"Confirm the goods were received to prepare a draft Sales Return.",
	),
	CANCELLED_AFTER_FULFILLMENT_BILLED: _entry(
		NEEDS_ACTION,
		MANUAL,
		"REVIEW_GOODS",
		"The order was cancelled after fulfillment and ERPNext has billing without delivery. "
		"Review what happened to the goods.",
	),
	NO_BASELINE: _entry(
		NEEDS_ACTION,
		MANUAL,
		"REVIEW_BASELINE",
		"No reconciled snapshot baseline is available for this Sales Order. Review the order against ERPNext.",
	),
	DUPLICATE_LINE_IDENTITY: _entry(
		NEEDS_ACTION,
		MANUAL,
		"REVIEW_DATA_INTEGRITY",
		"The marketplace snapshot has duplicate or missing line identities. Review the order data.",
	),
	LINE_RESTORED_FROM_ZERO: _entry(
		NEEDS_ACTION,
		MANUAL,
		"AMEND_SALES_ORDER",
		"A marketplace line returned to a positive quantity; this needs a structural change.",
	),
	ORDER_INACTIVE: _entry(
		NEEDS_ACTION,
		MANUAL,
		"REVIEW_SALES_ORDER",
		"The linked ERPNext Sales Order is no longer an active submitted document.",
	),
	CLOSED_EXPECTS_FULFILLMENT: _entry(
		NEEDS_ACTION,
		MANUAL,
		"REOPEN_OR_REVIEW_SALES_ORDER",
		"The Sales Order is Closed in ERPNext but the marketplace still expects fulfillment; "
		"reopen it or review the order.",
	),
	SHIPPING_CHANGED: _entry(
		NEEDS_ACTION,
		MANUAL,
		"AMEND_SALES_ORDER",
		"The shipping charge differs from the submitted Sales Order; cancel and amend it.",
	),
	HEADER_CHANGED: _entry(
		NEEDS_ACTION,
		MANUAL,
		"AMEND_SALES_ORDER",
		"A submitted Sales Order has structural commercial changes.",
	),
	STRUCTURAL_LINE_CHANGE: _entry(
		NEEDS_ACTION,
		MANUAL,
		"AMEND_SALES_ORDER",
		"A submitted Sales Order has structural line changes.",
	),
	ROW_CHANGE_REFUSED: _entry(
		NEEDS_ACTION,
		MANUAL,
		"AMEND_SALES_ORDER",
		"ERPNext refused to add or remove a Sales Order row (it is delivered, ordered, in a work order or "
		"billed). Amend the Sales Order manually.",
	),
	ERP_STATE_DIVERGED: _entry(
		NEEDS_ACTION,
		MANUAL,
		"INVESTIGATE_DIVERGENCE",
		"The ERPNext Sales Order no longer represents the reconciled marketplace state.",
	),
	ALL_ZERO_DELIVERED: _entry(
		NEEDS_ACTION,
		MANUAL,
		"CANCEL_EXPLICITLY",
		"All lines are zero but the order was already delivered; explicit cancellation is required.",
	),
	LIFECYCLE_BLOCKS_CHANGE: _entry(
		NEEDS_ACTION,
		MANUAL,
		"REVIEW_MARKETPLACE_STATE",
		"The marketplace order can no longer change, so the change was not applied.",
	),
	LINE_IDENTITY_CHANGED: _entry(
		NEEDS_ACTION,
		MANUAL,
		"REVIEW_MAPPING_CHANGE",
		"The item, unit of measure or warehouse of a line changed (mapping or account change); "
		"this cannot be applied automatically.",
	),
	QTY_BELOW_DELIVERED: _entry(
		NEEDS_ACTION,
		MANUAL,
		"REVIEW_DELIVERY",
		"Requested quantity is below the quantity already delivered in ERPNext.",
	),
	ALREADY_BILLED: _entry(
		NEEDS_ACTION,
		MANUAL,
		"CANCEL_OR_CREDIT_INVOICE",
		"A submitted Sales Order line is already billed in ERPNext. Cancel the invoice or issue a Credit Note.",
	),
	EXECUTION_BLOCKS_UPDATE: _entry(
		NEEDS_ACTION,
		MANUAL,
		"REVIEW_EXECUTION",
		"ERPNext delivery or billing execution blocks an automatic submitted-order update.",
	),
	ADVANCE_BLOCKS_CANCEL: _entry(
		NEEDS_ACTION,
		MANUAL,
		"REALLOCATE_ADVANCE",
		"An advance payment exists on the Sales Order; reallocate or cancel it before the order can be changed.",
	),
	TOTAL_BELOW_ADVANCE: _entry(
		NEEDS_ACTION,
		MANUAL,
		"REALLOCATE_ADVANCE",
		"This marketplace change would reduce the Sales Order total below its advance. "
		"Reallocate or cancel the advance before updating the order.",
	),
	DRAFT_DOCUMENTS_BLOCK_CANCEL: _entry(
		NEEDS_ACTION,
		MANUAL,
		"RESOLVE_DRAFT_DOCUMENTS",
		"ERPNext has draft documents for this order. Submit, cancel or delete them.",
	),
	SO_LINE_MISSING: _entry(
		NEEDS_ACTION,
		MANUAL,
		"INVESTIGATE_DIVERGENCE",
		"The reconciled Sales Order line is missing from ERPNext.",
	),
	EDIT_OUTCOME_UNKNOWN: _entry(
		NEEDS_ACTION,
		MANUAL,
		"CHECK_MARKETPLACE",
		"ERPNext's item edit may not have reached the marketplace (the outcome is unknown); "
		"check the order in the marketplace.",
	),
	RECONCILIATION_MISMATCH: _entry(
		NEEDS_ACTION,
		MANUAL,
		"INVESTIGATE_DIVERGENCE",
		"ERPNext cannot reproduce the marketplace amounts for the changed lines.",
	),
	VERIFY_ERP_STATE: _entry(
		NEEDS_ACTION,
		MANUAL,
		"VERIFY_ERP_STATE",
		"The database transaction was already rolled back; verify the Sales Order state in ERPNext.",
	),
	OUTBOUND_EDIT_MISMATCH: _entry(
		NEEDS_ACTION,
		MANUAL,
		"REVIEW_SALES_ORDER",
		"ERPNext's items do not match the order the marketplace now shows; review the order.",
	),
	SALES_ORDER_CHANGED_IN_ERP: _entry(
		NEEDS_ACTION,
		MANUAL,
		"REVIEW_SALES_ORDER",
		"The marketplace order changed after it was synced to ERPNext.",
	),
}

# Reasons that name a mapping or price problem; saving a mapping re-syncs these (#364).
MAPPING_OR_PRICE_REASONS = (MAPPING_MISSING, PRICE_MISSING)

# Group C reasons: the order waits for "Confirm Goods Received", then a draft is prepared.
RECEIPT_GATED_REASONS = (
	RETURN_REQUIRES_RECEIPT_REVIEW,
	CANCELLED_FULLY_DELIVERED,
	CANCELLED_AFTER_FULFILLMENT_DELIVERED,
)

# Needs Action reasons recorded after ERPNext refused an update the planner thought executable.
# A form load must not queue the same failing attempt again.
EXECUTION_FAILED_REASONS = (ROW_CHANGE_REFUSED, RECONCILIATION_MISMATCH, VERIFY_ERP_STATE)

_MESSAGE_LIMIT = 500


def status_for(reason_code):
	return REASONS[reason_code]["status"]


def instruction(reason_code):
	"""Translated default instruction for ``reason_code``."""
	text = REASONS[reason_code]["instruction"]
	return _(text)


def default_action_state(reason_code):
	if reason_code in RECEIPT_GATED_REASONS:
		return AWAITING_RECEIPT
	if REASONS[reason_code]["status"] == NEEDS_ACTION:
		return WAITING_FOR_USER
	return None


_STATE_FIELDS = (
	"sync_reason_code",
	"sync_action_type",
	"sync_remediation_mode",
	"sync_action_state",
	"sync_blocking_doctype",
	"sync_blocking_document",
	"sync_prepared_doctype",
	"sync_prepared_document",
)


def clear_sync_state(order, reset_retries=True):
	"""Clear the reason metadata in memory (for the save already in progress)."""
	for field in _STATE_FIELDS:
		order.set(field, None)
	if reset_retries:
		order.sync_retry_count = 0


def apply_sync_state(order, reason_code, message=None, blocking=None):
	"""Set status, ``last_sync_error`` and the reason metadata on ``order`` (in memory).

	Callers save the order.  ``message`` is the specific text (it may name documents);
	without it the registry's default instruction is used.  The prepared draft and the
	goods-receipt confirmation are kept only while the reason stays the same, so a
	re-check that lands on the same Group C reason does not forget a draft it already
	prepared.  ``blocking`` is an optional ``(doctype, name)`` of the document in the way.
	"""
	entry = REASONS[reason_code]
	same_reason = order.get("sync_reason_code") == reason_code
	prepared = (
		(order.get("sync_prepared_doctype"), order.get("sync_prepared_document")) if same_reason else (None, None)
	)
	order.erpnext_sync_status = entry["status"]
	order.last_sync_error = cstr(message).strip()[:_MESSAGE_LIMIT] or instruction(reason_code)
	order.sync_reason_code = reason_code
	order.sync_action_type = entry["action_type"]
	order.sync_remediation_mode = entry["mode"]
	order.sync_blocking_doctype, order.sync_blocking_document = blocking or (None, None)
	order.sync_prepared_doctype, order.sync_prepared_document = prepared
	order.sync_action_state = DRAFT_PREPARED if prepared[1] else default_action_state(reason_code)
	if not same_reason and reason_code not in RECEIPT_GATED_REASONS:
		order.receipt_confirmed_by = None
		order.receipt_confirmed_on = None
	if reason_code not in (TRANSIENT_RETRY_SCHEDULED, SYNC_RETRIES_EXHAUSTED):
		order.sync_retry_count = 0


def apply_synced(order):
	"""Synced: clear the error text and reason metadata; the receipt confirmation is kept as audit."""
	order.erpnext_sync_status = SYNCED
	order.last_sync_error = None
	prior_prepared = order.get("sync_prepared_document")
	clear_sync_state(order)
	if prior_prepared:
		order.sync_action_state = RESOLVED


def reason_row(code):
	"""Registry data for the client (status, mode, action type); ``None`` for an unknown code."""
	entry = REASONS.get(code)
	if not entry:
		return None
	return {
		"status": entry["status"],
		"remediation_mode": entry["mode"],
		"action_type": entry["action_type"],
		"instruction": instruction(code),
	}


def is_known_reason(code):
	return code in REASONS
