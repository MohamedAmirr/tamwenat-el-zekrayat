# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

from frappe.tests import UnitTestCase

from marketplace_integrations.api import bosta_shipments


class UnitTestBostaShipments(UnitTestCase):
	def test_print_a4_arabic_label_delegates_to_the_label_service(self):
		expected = {"file_url": "/private/files/bosta-awb.pdf"}
		with patch.object(bosta_shipments, "create_a4_arabic_label", return_value=expected) as create:
			self.assertEqual(bosta_shipments.print_a4_arabic_label("BOSTA-SHP-00001"), expected)

		create.assert_called_once_with("BOSTA-SHP-00001")
