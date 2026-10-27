# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Named values of ``Marketplace Order.erpnext_sync_status`` and ``sync_action_state`` (#364).

A leaf module: it imports nothing from this app, so any service, integration or patch can
import it without a cycle.  App Python code uses these constants instead of typing the
strings.  The strings themselves also live in the doctype JSON Select options, in the
JS colour maps and in ``translations/ar.csv`` keys; ``services/test_sync_status.py`` keeps
the JSON and the JS in step with this module.
"""

SYNCING = "Syncing"
SYNCED = "Synced"
NEEDS_ACTION = "Needs Action"
SYNC_ERROR = "Sync Error"
CANCELLED = "Cancelled"

SYNC_STATUSES = (SYNCING, SYNCED, NEEDS_ACTION, SYNC_ERROR, CANCELLED)

WAITING_FOR_USER = "Waiting for User"
AWAITING_RECEIPT = "Awaiting Receipt"
DRAFT_PREPARED = "Draft Prepared"
RECHECK_QUEUED = "Recheck Queued"
RESOLVED = "Resolved"

SYNC_ACTION_STATES = (WAITING_FOR_USER, AWAITING_RECEIPT, DRAFT_PREPARED, RECHECK_QUEUED, RESOLVED)
