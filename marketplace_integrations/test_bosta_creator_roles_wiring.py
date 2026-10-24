# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Always-on guard (#323): the Bosta creator roles must be the same everywhere.

``integrations/bosta/deliveries.py::CREATOR_ROLES`` is the server-side authority. The Sales Order
form and the Bosta Shipment form each keep a literal copy for the client, and the Sales Order list
view must read the Sales Order form's list instead of defining its own. A role added on one side
only would show a button the server refuses (or hide one it allows).
"""

import re

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.bosta.deliveries import CREATOR_ROLES

JS_LIST = re.compile(r"""(?:const|let|var)\s+(?P<name>\w+)\s*=\s*\[(?P<body>[^\]]*)\]""")


def read_js(*parts):
	path = frappe.get_app_path("marketplace_integrations", "public", "js", *parts)
	with open(path) as handle:
		return handle.read()


def roles_literal(source, name):
	for match in JS_LIST.finditer(source):
		if match.group("name") == name:
			return tuple(re.findall(r"""["']([^"']+)["']""", match.group("body")))
	raise AssertionError(f"{name} literal not found")


class UnitTestBostaCreatorRolesWiring(UnitTestCase):
	def test_sales_order_js_literal_matches_the_server_list(self):
		self.assertEqual(roles_literal(read_js("sales_order.js"), "BOSTA_CREATOR_ROLES"), CREATOR_ROLES)

	def test_bosta_shipment_js_literal_matches_the_server_list(self):
		self.assertEqual(roles_literal(read_js("bosta_shipment.js"), "CREATOR_ROLES"), CREATOR_ROLES)

	def test_sales_order_list_reads_the_shared_list_and_defines_none_of_its_own(self):
		source = read_js("sales_order_list.js")
		self.assertIn("frappe.marketplace_integrations.BOSTA_CREATOR_ROLES", source)
		self.assertNotIn("CREATOR_ROLES = [", source)
		self.assertNotRegex(source, r"""["']Stock User["']""")
