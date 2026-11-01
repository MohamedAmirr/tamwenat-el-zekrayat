# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Carrier shipments driven by a Delivery Note (#391, #393, epic #392).

``hooks.py`` registers these ``doc_events`` that land here: Delivery Note ``after_insert``
(``queue_shipment``), ``validate`` (``validate_delivery_note``), ``before_submit``
(``require_shipment_created``), ``before_cancel`` and ``on_trash`` (``guard_cancel_or_delete``), and
Sales Order ``before_cancel`` (``refuse_sales_order_cancel``). Each looks for the adapter whose carrier
owns the note and delegates; a note no adapter claims is left alone.

``CARRIERS`` is a plain ``{carrier: adapter}`` dict. It holds Noon and Bosta on purpose: no base class
and no registry until a second real carrier needs one. An adapter is a module exposing
``applies_to(doc)``, ``guard_changes(doc)``, ``require_created(doc)``,
``guard_cancel_or_delete(doc)``, ``refuse_sales_order_cancel(doc)``, ``queue(doc)`` and
``retry(delivery_note)``.
"""

import frappe

from marketplace_integrations.integrations.bosta import delivery_note_shipment as bosta_shipment
from marketplace_integrations.integrations.noon import delivery_note_shipment

#: Noon first, then Bosta. Bosta claims only a note whose transporter is the Bosta Supplier (and refuses
#: a Noon or Talabat order), so a Noon note is never taken by it. To add a carrier: one adapter module,
#: one row here, a Supplier, and its carrier-specific fields (docs/bosta-integration/README.md).
CARRIERS = {"Noon": delivery_note_shipment, "Bosta": bosta_shipment}


SKIP_FLAG = "skip_carrier_shipment"


def _adapter_for(doc):
	"""The adapter that owns ``doc``. A note the app drafts for a shipment that already exists (the
	old Noon handover, ``services/delivery_note_drafts.py``) sets ``flags.skip_carrier_shipment``
	and is left alone."""
	if doc.flags.get(SKIP_FLAG):
		return None
	return next((adapter for adapter in CARRIERS.values() if adapter.applies_to(doc)), None)


def saved_state(doc):
	"""The note as stored (ownership of an existing note is judged by it), else ``doc`` itself."""
	return doc if doc.is_new() else (doc.get_doc_before_save() or doc)


def queue_shipment(doc, method=None):
	"""``after_insert``: queue the carrier shipment of a new Delivery Note.

	Runs inside the saver's transaction, so it only queues (``enqueue_after_commit``) and never
	raises or calls a carrier: a failure here must not block saving a Delivery Note."""
	try:
		adapter = _adapter_for(doc)
		if adapter:
			adapter.queue(doc)
	except Exception:
		frappe.log_error(title="Carrier shipment: could not queue", message=frappe.get_traceback())


def validate_delivery_note(doc, method=None):
	"""``validate``: refuse changes the started shipment depends on (a deliberate refusal)."""
	# An existing note is judged by what is stored (rows just added in the form must not take it out of
	# the guard) and by the new copy (a person who just chose a carrier on an editable note).
	if doc.flags.get(SKIP_FLAG):
		return
	adapters = []
	for adapter in (_adapter_for(saved_state(doc)), _adapter_for(doc)):
		if adapter and adapter not in adapters:
			adapters.append(adapter)
	for adapter in adapters:
		adapter.guard_changes(doc)


def require_shipment_created(doc, method=None):
	"""``before_submit``: refuse to submit until the carrier shipment is Created."""
	adapter = _adapter_for(doc)
	if adapter:
		adapter.require_created(doc)


def guard_cancel_or_delete(doc, method=None):
	"""``before_cancel`` and ``on_trash``: refuse while a live carrier shipment is linked."""
	adapter = _adapter_for(doc)
	if adapter:
		adapter.guard_cancel_or_delete(doc)


def refuse_sales_order_cancel(doc, method=None):
	"""Sales Order ``before_cancel``: refuse while a draft Delivery Note has a live carrier shipment."""
	for adapter in CARRIERS.values():
		adapter.refuse_sales_order_cancel(doc)


def retry_shipment(delivery_note):
	"""Re-queue a failed carrier shipment of a draft Delivery Note (caller checked write access)."""
	doc = frappe.get_doc("Delivery Note", delivery_note)
	adapter = _adapter_for(doc)
	if not adapter:
		frappe.throw(frappe._("This Delivery Note has no carrier shipment to retry."))
	return adapter.retry(delivery_note)


def boot_session(bootinfo):
	"""Boot data: the carriers whose Delivery Note shows the generic Shipping section (an adapter with
	``SHIPPING_CARRIER``). The section's ``depends_on`` reads it, so no carrier name is hard-coded."""
	bootinfo.marketplace_shipping_carriers = [
		adapter.SHIPPING_CARRIER for adapter in CARRIERS.values() if getattr(adapter, "SHIPPING_CARRIER", None)
	]
