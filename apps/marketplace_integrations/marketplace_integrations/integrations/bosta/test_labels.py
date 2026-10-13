# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import base64
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

	def test_create_label_checks_shipment_read_permission_and_saves_private_file(self):
		shipment = MagicMock()
		shipment.name = "BOSTA-SHP-00001"
		shipment.tracking_number = "77873113"
		response = MagicMock(status_code=200, content=base64.b64encode(b"%PDF-1.7\nlabel"))
		client = MagicMock()
		client.get_a4_arabic_awb.return_value = response
		file_doc = MagicMock(file_url="/private/files/bosta-awb.pdf", file_name="bosta-awb.pdf")

		with (
			patch.object(labels.frappe, "get_doc", return_value=shipment),
			patch.object(labels, "BostaClient", return_value=client),
			patch.object(labels, "save_file", return_value=file_doc) as save,
		):
			result = labels.create_a4_arabic_label("BOSTA-SHP-00001")

		shipment.check_permission.assert_called_once_with("read")
		client.get_a4_arabic_awb.assert_called_once_with(["77873113"])
		save.assert_called_once_with(
			"bosta-awb-77873113.pdf",
			b"%PDF-1.7\nlabel",
			"Bosta Shipment",
			"BOSTA-SHP-00001",
			is_private=1,
		)
		self.assertEqual(result, {"file_url": "/private/files/bosta-awb.pdf", "file_name": "bosta-awb.pdf"})

	def test_create_label_requires_a_tracking_number(self):
		shipment = MagicMock()
		shipment.name = "BOSTA-SHP-00001"
		shipment.tracking_number = ""
		with patch.object(labels.frappe, "get_doc", return_value=shipment):
			with self.assertRaises(frappe.ValidationError):
				labels.create_a4_arabic_label("BOSTA-SHP-00001")

		shipment.check_permission.assert_called_once_with("read")
