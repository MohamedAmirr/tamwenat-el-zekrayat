# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""On-demand, private Bosta A4 Arabic labels for individual shipments."""

import base64
import binascii
import json

import frappe
from frappe import _
from frappe.utils import escape_html, flt
from frappe.utils.file_manager import save_file

from marketplace_integrations.integrations.bosta.client import BostaClient, is_success_response


def create_a4_arabic_label(delivery_note, replace=False, cod=None):
	"""Fetch the A4/Arabic AWB of a Bosta Delivery Note and attach it privately to the note. ``replace`` (#501,
	after a COD update) fetches a new label although one is stored; the old file stays attached to the note.
	``cod`` (with ``replace``): the COD this label was requested for. Two refresh jobs for two COD updates can finish
	out of order, so the COD in the note's request snapshot is checked again under the note's row lock right before
	the label is stored; when it moved on the label is NOT stored (the job of the newer COD stores its own) and
	``None`` is returned.

	Live ``POST /deliveries/mass-awb`` answers ``{"success": true, "data": "<base64 PDF>"}``
	(verified 2026-10-16); ``_decode_base64_pdf`` accepts that and a bare base64 body, and
	rejects any other shape. A non-success reply (including ``success: false`` on HTTP 200)
	is shown as Bosta's escaped error message. The caller checked access to the note."""
	values = frappe.db.get_value(
		"Delivery Note", delivery_note, ["lr_no", "custom_shipping_label"], as_dict=True
	)
	if values and values.custom_shipping_label and not replace:
		return {"file_url": values.custom_shipping_label, "file_name": ""}
	tracking_number = str((values and values.lr_no) or "").strip()
	if not tracking_number:
		frappe.throw(_("Delivery Note {0} has no Bosta tracking number yet.").format(delivery_note))

	client = BostaClient()
	response = client.get_a4_arabic_awb([tracking_number])
	if not is_success_response(response):
		frappe.throw(escape_html(client.error_message(response)))

	pdf_content = _decode_base64_pdf(response.content)
	if replace and cod is not None:
		from marketplace_integrations.integrations.bosta import delivery_note_shipment as shipment

		# Commit, then lock as the next statement (MariaDB snapshot isolation); the caller commits after the write.
		frappe.db.commit()
		frappe.db.get_value("Delivery Note", delivery_note, "name", for_update=True)
		if shipment._snapshot_cod(delivery_note) != flt(cod, 2):
			frappe.db.commit()
			return None
	file_doc = save_file(
		_label_filename(tracking_number),
		pdf_content,
		"Delivery Note",
		delivery_note,
		is_private=1,
	)
	# Not a change the person made: bumping ``modified`` here would make the native submit that follows
	# a Created shipment fail with "modified after you opened it" (the label job runs right after Created).
	frappe.db.set_value(
		"Delivery Note", delivery_note, "custom_shipping_label", file_doc.file_url, update_modified=False
	)
	return {"file_url": file_doc.file_url, "file_name": file_doc.file_name}


def _decode_base64_pdf(encoded_content):
	"""Decode Bosta's base64 PDF label.

	The label endpoint documentation only says the response is a base64-encoded PDF for no
	more than 50 AWBs. A live call (checked 2026-10-16) answers a JSON object
	``{"success": true, "data": "<base64 PDF>"}``; a bare base64 body is accepted too. Any
	other shape (data URI, JSON without a string ``data``) is rejected, not guessed.
	"""
	if isinstance(encoded_content, str):
		encoded_content = encoded_content.encode()
	if not isinstance(encoded_content, bytes):
		frappe.throw(_("Bosta returned an invalid label response."))

	if encoded_content.lstrip().startswith(b"{"):
		try:
			body = json.loads(encoded_content)
		except ValueError:
			frappe.throw(_("Bosta returned a label that is not a valid base64 PDF."))
		data = body.get("data") if isinstance(body, dict) else None
		if not isinstance(data, str):
			frappe.throw(_("Bosta returned an invalid label response."))
		encoded_content = data.encode()

	try:
		pdf_content = base64.b64decode(b"".join(encoded_content.split()), validate=True)
	except (ValueError, binascii.Error):
		frappe.throw(_("Bosta returned a label that is not a valid base64 PDF."))

	if not pdf_content.startswith(b"%PDF-"):
		frappe.throw(_("Bosta returned a label that is not a PDF."))
	return pdf_content


def _label_filename(tracking_number):
	"""Keep the remote opaque tracking value out of the filesystem filename."""
	return f"bosta-awb-{frappe.scrub(tracking_number)}.pdf"
