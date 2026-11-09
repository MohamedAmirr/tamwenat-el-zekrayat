# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Ship the "Delivery Note Lifecycle" Workflow and give every Delivery Note its stage (epic #502, #498). Idempotent.

A Workflow fixture is unreliable (fixtures sync after patches, and import order and ``ignore_validate``
decide what is created), so the Workflow ships as ``workflow/delivery_note_lifecycle.json`` and this
``post_model_sync`` patch creates it. The order matters:

1. The ``workflow_state`` Custom Field (fixtures run AFTER patches, so it is created here from the fixture
   file). Its fixture import later changes nothing.
2. Every existing Delivery Note gets the stage its stored statuses lead to
   (``services.delivery_note_workflow.state_for``, the same mapping the runtime uses). Nothing is submitted
   and ``modified`` does not change. This must come BEFORE the Workflow is saved: saving a Workflow fills every
   empty ``workflow_state`` with the first stage of the note's document status, which would put each
   submitted note in "Picked Up".
3. The Workflow States, Workflow Action Masters and the Workflow Transition Tasks it uses, then the Workflow.

A second run recomputes the same stages and rewrites the Workflow rows with the same content. It stops
(changing nothing) if another active Workflow already manages Delivery Notes: saving ours would deactivate it.
"""

import json
import os

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from marketplace_integrations.services import delivery_note_workflow as lifecycle

_HERE = os.path.dirname(__file__)
WORKFLOW_FILE = os.path.join(_HERE, "..", "..", "workflow", "delivery_note_lifecycle.json")
CUSTOM_FIELD_FILE = os.path.join(_HERE, "..", "..", "fixtures", "custom_field.json")
CHUNK = 500


def execute():
	with open(WORKFLOW_FILE) as handle:
		spec = json.load(handle)
	_refuse_other_active_workflow(spec)
	_ensure_custom_field()
	frappe.clear_cache(doctype="Delivery Note")
	backfill_states()
	install_workflow(spec)


def _refuse_other_active_workflow(spec):
	other = frappe.db.get_value(
		"Workflow",
		{"document_type": spec["document_type"], "is_active": 1, "name": ["!=", spec["workflow_name"]]},
		"name",
	)
	if other:
		frappe.throw(
			f"The Workflow {other} is active on {spec['document_type']}. Saving {spec['workflow_name']} would "
			"deactivate it: resolve this by hand, then migrate again."
		)


def _ensure_custom_field():
	with open(CUSTOM_FIELD_FILE) as handle:
		entries = json.load(handle)
	fields = [
		{k: v for k, v in entry.items() if k not in ("doctype", "name", "modified", "module")}
		for entry in entries
		if entry["dt"] == "Delivery Note" and entry["fieldname"] == lifecycle.FIELD
	]
	create_custom_fields({"Delivery Note": fields}, ignore_validate=True)


def backfill_states():
	"""Write the stage of every Delivery Note whose stored stage differs from the mapping."""
	rows = frappe.get_all(
		"Delivery Note",
		fields=["name", "docstatus", "is_return", "custom_integration_status", "custom_shipping_status", lifecycle.FIELD],
		limit_page_length=0,
	)
	by_state = {}
	for row in rows:
		state = lifecycle.state_of_row(row)
		if (row.get(lifecycle.FIELD) or "") != state:
			by_state.setdefault(state, []).append(row.name)
	for state, names in by_state.items():
		for start in range(0, len(names), CHUNK):
			frappe.db.set_value(
				"Delivery Note", {"name": ["in", names[start : start + CHUNK]]}, lifecycle.FIELD, state, update_modified=False
			)


def install_workflow(spec):
	"""Create (or bring up to date) the Workflow and what it links to, from the parsed JSON."""
	for state in spec["states"]:
		if not frappe.db.exists("Workflow State", state["state"]):
			frappe.get_doc(
				{"doctype": "Workflow State", "workflow_state_name": state["state"], "style": state.get("style")}
			).insert(ignore_permissions=True)
	for action in {transition["action"] for transition in spec["transitions"]}:
		if not frappe.db.exists("Workflow Action Master", action):
			frappe.get_doc({"doctype": "Workflow Action Master", "workflow_action_name": action}).insert(
				ignore_permissions=True
			)
	tasks = _ensure_transition_tasks(spec.get("transition_tasks") or {})

	workflow = (
		frappe.get_doc("Workflow", spec["workflow_name"])
		if frappe.db.exists("Workflow", spec["workflow_name"])
		else frappe.new_doc("Workflow")
	)
	workflow.workflow_name = spec["workflow_name"]
	for key in ("document_type", "workflow_state_field", "is_active", "override_status", "send_email_alert", "enable_action_confirmation"):
		workflow.set(key, spec[key])
	workflow.set("states", [{k: v for k, v in state.items() if k != "style"} for state in spec["states"]])
	workflow.set(
		"transitions",
		[
			{
				"state": t["state"],
				"action": t["action"],
				"next_state": t["next_state"],
				"allowed": t["allowed"],
				"condition": t.get("condition"),
				"transition_tasks": tasks.get(t.get("transition_tasks")),
			}
			for t in spec["transitions"]
		],
	)
	workflow.save(ignore_permissions=True)


def _ensure_transition_tasks(spec_tasks):
	"""``{label: name of the Workflow Transition Tasks document}``. The doctype is named by prompt: the name is
	the label."""
	names = {}
	for label, rows in spec_tasks.items():
		doc = (
			frappe.get_doc("Workflow Transition Tasks", label)
			if frappe.db.exists("Workflow Transition Tasks", label)
			else frappe.new_doc("Workflow Transition Tasks")
		)
		doc.set("tasks", [{"task": row["task"], "enabled": 1, "asynchronous": row.get("asynchronous", 0)} for row in rows])
		if doc.is_new():
			doc.__newname = label
		doc.save(ignore_permissions=True)
		names[label] = doc.name
	return names
