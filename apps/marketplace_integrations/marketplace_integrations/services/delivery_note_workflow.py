# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The "Delivery Note Lifecycle" Workflow of every Delivery Note (epic #502, issue #498).

Employees see a stage name (``workflow_state``), never Draft or Submitted. The Workflow itself ships from
``workflow/delivery_note_lifecycle.json`` (``patches/v0_0/create_delivery_note_lifecycle_workflow.py``). This
module owns what the Workflow cannot express:

- ``state_for``: the ONE mapping from ``docstatus`` + ``custom_integration_status`` + ``custom_shipping_status``
  (+ ``is_return``) to a stage. The migration patch and every runtime write use it, so they cannot drift.
- ``with_state`` / ``sync_state``: the carrier code writes statuses with ``db_set`` (no hooks run), so each such
  write adds the stage computed here to the same write.
- ``keep_state_consistent`` (called by ``carrier_shipments.validate_delivery_note``): the server-side lock.
  Workflow ``allow_edit`` is enforced only by the browser, so a direct API save must not be able to move a note
  to another stage.
- ``fix_state_after_submit``: ``doc.submit()`` puts a note in the FIRST submitted stage of the Workflow; the
  stage must come from the mapping.
- The two Workflow transition tasks of "Create Shipment" and "Retry Shipment" (``hooks.py`` ``workflow_methods``).
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr, strip_html_tags

from marketplace_integrations.services.carrier_log import (
	CANCELLATION_PENDING,
	CANCELLATION_UNKNOWN,
	CREATED,
	CREATING,
	FAILED,
	NOT_CREATED,
	UNKNOWN,
)

WORKFLOW = "Delivery Note Lifecycle"
FIELD = "workflow_state"

ORDER_RECEIVED = "Order Received"
WAITING_FOR_GOODS = "Waiting for Goods"
SHIPMENT_REQUESTED = "Shipment Requested"
SHIPMENT_CREATED = "Shipment Created"
READY_FOR_PICKUP = "Ready for Pickup"
PICKED_UP = "Picked Up"
IN_TRANSIT = "In Transit"
DELIVERED = "Delivered"
RETURNED_FAILED = "Returned / Failed"
CANCELLED = "Cancelled"

#: Stage -> document status. The Workflow JSON must agree (a test checks it): Frappe moves a note to the first
#: stage of the matching docstatus on submit/cancel, and refuses a transition that changes it any other way.
STATE_DOCSTATUS = {
	ORDER_RECEIVED: 0,
	WAITING_FOR_GOODS: 0,
	SHIPMENT_REQUESTED: 0,
	SHIPMENT_CREATED: 0,
	READY_FOR_PICKUP: 1,
	PICKED_UP: 1,
	IN_TRANSIT: 1,
	DELIVERED: 1,
	RETURNED_FAILED: 1,
	CANCELLED: 2,
}

# Shipping statuses (``custom_shipping_status`` Select options) of a SUBMITTED note. Bosta's names are mapped by
# the ONE table in ``integrations/bosta/statuses.py`` (``STAGE_BY_CODE``, #500). Noon's are listed here. Anything
# not listed is a carrier note whose carrier has the parcel and stays Picked Up (a submitted Bosta note is picked
# up by definition: only the pickup submits it).
#
# Noon (#540): Noon sets "Shipped" right after the shipment is created, so it does NOT mean the parcel left. A
# submitted Noon note is Ready for Pickup; Picked Up, In Transit (old notes) and Delivered are then set by a
# person (Workflow actions Confirm Pickup / Confirm Delivery), and ``state_of_row`` keeps them.
DELIVERED_STATUSES = ("Delivered",)
NOON_RETURNED_FAILED_STATUSES = ("Cancelled", "Out of Stock")
#: Stages a person sets on a submitted Noon note; a refresh of the Noon statuses never moves them.
NOON_MANUAL_STATES = (PICKED_UP, IN_TRANSIT, DELIVERED)

#: Set on the document by the Workflow task of a transition: a draft-to-draft stage change is only legitimate
#: through a Workflow action.
TRANSITION_FLAG = "delivery_note_workflow_transition"


def state_for(docstatus, integration_status="", shipping_status="", is_return=False, transporter_name=""):
	"""The stage of a note: the single mapping used by the migration patch and by runtime writes."""
	docstatus, integration, shipping = cint(docstatus), cstr(integration_status), cstr(shipping_status)
	if docstatus == 2:
		return CANCELLED
	if docstatus == 0:
		if integration in (CREATING, UNKNOWN):
			return SHIPMENT_REQUESTED
		if integration in (CREATED, CANCELLATION_PENDING, CANCELLATION_UNKNOWN):
			return SHIPMENT_CREATED
		return ORDER_RECEIVED
	# submitted
	if cint(is_return):
		return RETURNED_FAILED
	from marketplace_integrations.integrations.bosta import statuses
	from marketplace_integrations.integrations.noon import delivery_note_shipment as noon

	if shipping in DELIVERED_STATUSES:
		return DELIVERED
	if shipping in NOON_RETURNED_FAILED_STATUSES:
		return RETURNED_FAILED
	if cstr(transporter_name) == noon.SHIPPING_CARRIER:
		# the Noon statuses (Acknowledged, Shipped, Mixed) never say the parcel left: a person confirms pickup and delivery
		return READY_FOR_PICKUP
	bosta_stage = statuses.stage_for_name(shipping)
	if bosta_stage:
		# a submitted note never gets a draft-only stage (a Bosta status that maps to Shipment Created): the carrier
		# has the parcel, so at least Picked Up
		return bosta_stage if STATE_DOCSTATUS.get(bosta_stage) == 1 else PICKED_UP
	if shipping or integration not in ("", NOT_CREATED, FAILED):
		return PICKED_UP
	# no carrier involved: a person confirmed the delivery
	return DELIVERED


def state_of_row(row, overrides=None):
	"""``state_for`` of a row (dict with docstatus, is_return, the two statuses and optionally the stored
	stage), with ``overrides`` (the values about to be written) laid over it. A draft that waits for goods is
	not pulled back to Order Received."""
	data = {**row, **(overrides or {})}
	state = state_for(
		data.get("docstatus"),
		data.get("custom_integration_status"),
		data.get("custom_shipping_status"),
		data.get("is_return"),
		data.get("transporter_name"),
	)
	if state == READY_FOR_PICKUP and cstr(row.get(FIELD)) in NOON_MANUAL_STATES:
		# a person moved this submitted Noon note on (Confirm Pickup / Confirm Delivery); Returned / Failed and
		# Cancelled came out of ``state_for`` above and win
		return cstr(row.get(FIELD))
	if state == ORDER_RECEIVED and cstr(row.get(FIELD)) == WAITING_FOR_GOODS and cint(data.get("docstatus")) == 0:
		return WAITING_FOR_GOODS
	return state


def is_installed():
	"""The stage field exists (the Custom Field ships with the app and the migration patch)."""
	return frappe.get_meta("Delivery Note").has_field(FIELD)


_ROW_FIELDS = [
	"docstatus",
	"is_return",
	"transporter_name",
	"custom_integration_status",
	"custom_shipping_status",
	FIELD,
]


def with_state(delivery_note, values):
	"""``values`` (about to be written to the Delivery Note with ``db_set``/``set_value``) plus the stage they
	lead to, so the status and the stage change in the same write."""
	if not is_installed():
		return values
	row = frappe.db.get_value("Delivery Note", delivery_note, _ROW_FIELDS, as_dict=True)
	if not row:
		return values
	return {**values, FIELD: state_of_row(row, values)}


def sync_state(delivery_note):
	"""Write the stage the stored statuses lead to (when it differs). No hooks run, ``modified`` stays."""
	if not is_installed():
		return
	row = frappe.db.get_value("Delivery Note", delivery_note, _ROW_FIELDS, as_dict=True)
	if not row:
		return
	state = state_of_row(row)
	if cstr(row.get(FIELD)) != state:
		frappe.db.set_value("Delivery Note", delivery_note, FIELD, state, update_modified=False)


# ---------------------------------------------------------------------------
# Document rules
# ---------------------------------------------------------------------------


def keep_state_consistent(doc):
	"""``validate`` (called by ``carrier_shipments.validate_delivery_note``): the server-side lock on the stage.

	- A new note starts at the first stage, whatever an Amend or a copy carried over.
	- Once saved, the stage changes only through the Workflow: the new stage must belong to the document status
	  being saved, and a stage change of a draft needs the flag its Workflow task sets (so a staff API save
	  cannot claim "Shipment Requested" without a shipment, or "Delivered" on a draft). The carrier code and
	  the migration write the stage with ``db_set``, which never reaches this hook."""
	if not is_installed():
		return
	if doc.is_new():
		doc.set(FIELD, None)
		return
	before = doc.get_doc_before_save()
	stored, new = cstr(before.get(FIELD)) if before else "", cstr(doc.get(FIELD))
	if not before or not new or new == stored:
		return
	expected = STATE_DOCSTATUS.get(new)
	wrong_status = expected is not None and expected != cint(doc.docstatus)
	if wrong_status or (cint(doc.docstatus) == 0 and not doc.flags.get(TRANSITION_FLAG)):
		frappe.throw(_("The stage of a Delivery Note changes only with its workflow buttons."), title=_("Stage"))


def fix_state_after_submit(doc, method=None):
	"""``on_submit``: Frappe put the note in the first submitted stage; set the one the statuses lead to."""
	if not is_installed() or not doc.meta.get_workflow():
		return
	state = state_for(
		doc.docstatus,
		doc.get("custom_integration_status"),
		doc.get("custom_shipping_status"),
		doc.get("is_return"),
		doc.get("transporter_name"),
	)
	if cstr(doc.get(FIELD)) != state:
		doc.db_set(FIELD, state, update_modified=False)


# ---------------------------------------------------------------------------
# "Create Shipment" / "Retry Shipment" transition tasks (hooks.py workflow_methods)
# ---------------------------------------------------------------------------


def check_shipment_request(doc):
	"""Transition task (runs inside the transition; Create Shipment and Retry Shipment): refuse what the shipment
	request would refuse, so the note never reaches (or stays in) Shipment Requested without a request that can be sent. Also marks the save as a Workflow
	action (see ``keep_state_consistent``)."""
	from marketplace_integrations.services import carrier_shipments

	carrier_shipments.check_ready(doc)
	doc.flags[TRANSITION_FLAG] = True


def start_shipment(doc):
	"""Transition task (asynchronous, queued after the transition commits, runs as the user who pressed the
	button): start the carrier create through the same path as before (Create Shipment, and Retry Shipment of a Creation Unknown note). A Noon note is submitted by the job as that
	user once the carrier confirms Created; a Bosta note stays a draft until Bosta picks the parcel up (#500), then
	the pickup job submits it as the user recorded as requester. A refusal that only the locked check can see
	(the stock changed meanwhile) puts the note back to the stage its statuses lead to, with the reason.

	This never deletes the note, whoever made it (the Workflow's Create Shipment, the insert trigger of
	``carrier_shipments.run_request_on_insert``): a failure keeps the draft with the reason. Only the one-step flow
	(``order_shipment.ship_order``) deletes the notes it made."""
	from marketplace_integrations.services import carrier_shipments

	name = doc.name
	user = frappe.session.user

	try:
		answer = carrier_shipments.request_create(name, submit_as=user)
		if answer.get("needs_city"):
			# Only a note saved without a transition (the trigger on insert) can get here: the transition's own check
			# refuses an empty city first.
			record_request_failure(
				name, _("The city of the delivery address could not be found. Pick the city and create the shipment again.")
			)
	except Exception as exc:
		frappe.db.rollback()
		if not isinstance(exc, frappe.ValidationError):
			frappe.log_error(title="Starting the carrier shipment of a Delivery Note failed", message=f"{name}\n{frappe.get_traceback()}")
		frappe.clear_messages()
		record_request_failure(name, strip_html_tags(cstr(exc)).strip()[:300] or exc.__class__.__name__)


def record_request_failure(name, reason):
	"""Keep the note where its statuses lead (Order Received for a draft) with the reason on it, so the person fixes it
	and creates the shipment again."""
	frappe.db.set_value(
		"Delivery Note",
		name,
		"custom_last_integration_error",
		_("The shipment request was not sent: {0}").format(reason),
		update_modified=True,
	)
	sync_state(name)
	frappe.db.commit()
