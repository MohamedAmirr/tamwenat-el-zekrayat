# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import base64
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.bosta import labels


class UnitTestBostaLabels(UnitTestCase):
	def test_decode_base64_pdf_accepts_only_a_pdf(self):
		pdf_content = b"%PDF-1.7\nlabel"
		self.assertEqual(labels._decode_base64_pdf(base64.b64encode(pdf_content)), pdf_content)

		with self.assertRaises(frappe.ValidationError):
			labels._decode_base64_pdf(b"not base64")
		with self.assertRaises(frappe.ValidationError):
			labels._decode_base64_pdf(base64.b64encode(b"not a PDF"))

	def test_decode_accepts_the_json_wrapper_bosta_really_returns(self):
		pdf_content = b"%PDF-1.4\nlabel"
		wrapped = json.dumps({"success": True, "data": base64.b64encode(pdf_content).decode()}).encode()
		self.assertEqual(labels._decode_base64_pdf(wrapped), pdf_content)

		for bad in (b'{"success": true}', b'{"data": 5}', b"{not json", b'["x"]'):
			with self.assertRaises(frappe.ValidationError):
				labels._decode_base64_pdf(bad)

	def _note(self, label="", tracking="77873113"):
		return {"lr_no": tracking, "custom_shipping_label": label}

	def test_create_label_attaches_a_private_pdf_to_the_delivery_note(self):
		response = MagicMock(status_code=200, content=base64.b64encode(b"%PDF-1.7\nlabel"))
		client = MagicMock()
		client.get_a4_arabic_awb.return_value = response
		file_doc = MagicMock(file_url="/private/files/bosta-awb.pdf", file_name="bosta-awb.pdf")

		with (
			patch.object(labels.frappe.db, "get_value", return_value=frappe._dict(self._note())),
			patch.object(labels.frappe.db, "set_value") as set_value,
			patch.object(labels, "BostaClient", return_value=client),
			patch.object(labels, "save_file", return_value=file_doc) as save,
		):
			result = labels.create_a4_arabic_label("MAT-DN-1")

		client.get_a4_arabic_awb.assert_called_once_with(["77873113"])
		save.assert_called_once_with(
			"bosta-awb-77873113.pdf", b"%PDF-1.7\nlabel", "Delivery Note", "MAT-DN-1", is_private=1
		)
		set_value.assert_called_once_with(
			"Delivery Note",
			"MAT-DN-1",
			"custom_shipping_label",
			"/private/files/bosta-awb.pdf",
			update_modified=False,
		)
		self.assertEqual(result, {"file_url": "/private/files/bosta-awb.pdf", "file_name": "bosta-awb.pdf"})

	def test_an_existing_label_is_returned_without_calling_bosta(self):
		with (
			patch.object(
				labels.frappe.db, "get_value", return_value=frappe._dict(self._note("/private/files/x.pdf"))
			),
			patch.object(labels, "BostaClient") as client,
		):
			result = labels.create_a4_arabic_label("MAT-DN-1")

		client.assert_not_called()
		self.assertEqual(result["file_url"], "/private/files/x.pdf")

	def test_create_label_shows_the_escaped_error_when_bosta_says_success_false_on_http_200(self):
		response = MagicMock(status_code=200)
		response.json.return_value = {"success": False, "message": "<img src=x onerror=1> no such AWB"}
		client = MagicMock()
		client.get_a4_arabic_awb.return_value = response
		client.error_message.return_value = (
			"Bosta request failed (HTTP 200): <img src=x onerror=1> no such AWB"
		)

		with (
			patch.object(labels.frappe.db, "get_value", return_value=frappe._dict(self._note())),
			patch.object(labels, "BostaClient", return_value=client),
			patch.object(labels, "save_file") as save,
			self.assertRaises(frappe.ValidationError) as raised,
		):
			labels.create_a4_arabic_label("MAT-DN-1")

		self.assertNotIn("<img", str(raised.exception))
		self.assertIn("&lt;img", str(raised.exception))
		save.assert_not_called()

	def test_create_label_requires_a_tracking_number(self):
		with patch.object(labels.frappe.db, "get_value", return_value=frappe._dict(self._note(tracking=""))):
			with self.assertRaises(frappe.ValidationError):
				labels.create_a4_arabic_label("MAT-DN-1")
