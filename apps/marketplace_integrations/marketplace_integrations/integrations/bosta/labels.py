# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""On-demand, private Bosta A4 Arabic labels for individual shipments."""

import base64
import binascii
import json

import frappe
from frappe import _
from frappe.utils import escape_html
from frappe.utils.file_manager import save_file

from marketplace_integrations.integrations.bosta.client import BostaClient, is_success_response


def create_a4_arabic_label(shipment_name):
	"""Fetch one shipment's A4/Arabic AWB and attach it privately.

	Live ``POST /deliveries/mass-awb`` answers ``{"success": true, "data": "<base64 PDF>"}``
	(verified 2026-10-16); ``_decode_base64_pdf`` accepts that and a bare base64 body, and
	rejects any other shape. A non-success reply (including ``success: false`` on HTTP 200)
	is shown as Bosta's escaped error message."""
	shipment = frappe.get_doc("Bosta Shipment", shipment_name)
	shipment.check_permission("read")
	tracking_number = str(shipment.tracking_number or "").strip()
	if not tracking_number:
		frappe.throw(_("Bosta Shipment {0} has no tracking number yet.").format(shipment.name))

	client = BostaClient()
	response = client.get_a4_arabic_awb([tracking_number])
	if not is_success_response(response):
		frappe.throw(escape_html(client.error_message(response)))

	pdf_content = _decode_base64_pdf(response.content)
	file_doc = save_file(
		_label_filename(tracking_number),
		pdf_content,
		"Bosta Shipment",
		shipment.name,
		is_private=1,
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
	except ValueError, binascii.Error:
		frappe.throw(_("Bosta returned a label that is not a valid base64 PDF."))

	if not pdf_content.startswith(b"%PDF-"):
		frappe.throw(_("Bosta returned a label that is not a PDF."))
	return pdf_content


def _label_filename(tracking_number):
	"""Keep the remote opaque tracking value out of the filesystem filename."""
	return f"bosta-awb-{frappe.scrub(tracking_number)}.pdf"
