# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Add the "Retry Shipment" transition (Shipment Requested to Shipment Requested) to an installed "Delivery Note
Lifecycle" Workflow. Idempotent.

A carrier note whose create ended Creation Unknown stays in Shipment Requested, which had no way out. The new
transition ships in ``workflow/delivery_note_lifecycle.json``; this patch re-applies that JSON through the same
code as ``update_delivery_note_lifecycle_workflow`` (a patch runs once per site, so the earlier one cannot carry a
later change of the JSON).
"""

from marketplace_integrations.patches.v0_0 import update_delivery_note_lifecycle_workflow


def execute():
	update_delivery_note_lifecycle_workflow.execute()
