# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Add the Noon manual stages (#540) to an installed "Delivery Note Lifecycle" Workflow. Idempotent.

The new state "Ready for Pickup" and the actions "Confirm Pickup" (Ready for Pickup to Picked Up) and "Confirm
Delivery" (Picked Up / In Transit to Delivered, Noon notes) ship in ``workflow/delivery_note_lifecycle.json``. This
patch re-applies that JSON through the same code as ``update_delivery_note_lifecycle_workflow`` (a patch runs once
per site, so the earlier one cannot carry a later change of the JSON). Existing notes keep their stage.
"""

from marketplace_integrations.patches.v0_0 import update_delivery_note_lifecycle_workflow


def execute():
	update_delivery_note_lifecycle_workflow.execute()
