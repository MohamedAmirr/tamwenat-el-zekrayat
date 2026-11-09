# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bring an already installed "Delivery Note Lifecycle" Workflow up to date with ``workflow/delivery_note_lifecycle.json``
(epic #502). Idempotent.

``create_delivery_note_lifecycle_workflow`` ran once per site, so a later change of the JSON never reaches a site that
ran it. This patch re-applies the JSON through the same ``install_workflow``. The change it ships: "Confirm Shipment"
(Shipment Created to Picked Up) is no longer offered for a Bosta note (the pickup job submits it;
``carrier_shipments.require_shipment_created`` refuses every hand submit). It does nothing on a site where the Workflow
does not exist yet (the first patch creates it from the same JSON) and stops, changing nothing, when another active
Workflow manages Delivery Notes.
"""

import json

import frappe

from marketplace_integrations.patches.v0_0.create_delivery_note_lifecycle_workflow import (
	WORKFLOW_FILE,
	_refuse_other_active_workflow,
	install_workflow,
)


def execute():
	with open(WORKFLOW_FILE) as handle:
		spec = json.load(handle)
	if not frappe.db.exists("Workflow", spec["workflow_name"]):
		return
	_refuse_other_active_workflow(spec)
	install_workflow(spec)
