# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Always-on guard (#364): the sync status values must be the same everywhere.

``services/sync_status.py`` names every ``erpnext_sync_status`` and ``sync_action_state`` value.
The doctype JSON Select options and the JS colour maps keep literal copies; a value added or
removed on one side only would break a filter, a colour or a Select validation.
"""

import json
import re

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.services import sync_reason_codes, sync_status

REMOVED_STATUSES = ("Not Synced", "Modified After Sync")


def _doctype_field(fieldname):
	path = frappe.get_app_path(
		"marketplace_integrations",
		"marketplace_integrations",
		"doctype",
		"marketplace_order",
		"marketplace_order.json",
	)
	with open(path) as handle:
		fields = json.load(handle)["fields"]
	return next(field for field in fields if field["fieldname"] == fieldname)


def _js_map_keys(filename, const_name):
	path = frappe.get_app_path(
		"marketplace_integrations", "marketplace_integrations", "doctype", "marketplace_order", filename
	)
	with open(path) as handle:
		source = handle.read()
	body = re.search(rf"const {const_name} = \{{(?P<body>[^}}]*)\}}", source).group("body")
	return set(re.findall(r"""^\s*["']?([A-Za-z][A-Za-z ]*?)["']?\s*:""", body, flags=re.MULTILINE))


class UnitTestSyncStatusWiring(UnitTestCase):
	def test_doctype_options_match_the_module(self):
		status = _doctype_field("erpnext_sync_status")
		self.assertEqual(tuple(status["options"].split("\n")), sync_status.SYNC_STATUSES)
		self.assertEqual(status["default"], sync_status.SYNCING)
		state = _doctype_field("sync_action_state")
		self.assertEqual(tuple(state["options"].split("\n")[1:]), sync_status.SYNC_ACTION_STATES)

	def test_form_colour_map_covers_every_status_and_no_removed_one(self):
		keys = _js_map_keys("marketplace_order.js", "ERPNEXT_SYNC_STATUS_COLORS")
		self.assertEqual(keys, set(sync_status.SYNC_STATUSES))
		self.assertFalse(keys & set(REMOVED_STATUSES))
		states = _js_map_keys("marketplace_order.js", "SYNC_ACTION_STATE_COLORS")
		self.assertEqual(states, set(sync_status.SYNC_ACTION_STATES))

	def test_list_colour_map_covers_every_status_and_no_removed_one(self):
		keys = _js_map_keys("marketplace_order_list.js", "ORDER_SYNC_STATUS_COLORS")
		self.assertEqual(keys, set(sync_status.SYNC_STATUSES))
		self.assertFalse(keys & set(REMOVED_STATUSES))

	def test_every_reason_code_has_a_valid_status_and_mode(self):
		for code, entry in sync_reason_codes.REASONS.items():
			with self.subTest(code=code):
				self.assertIn(entry["status"], (sync_status.SYNCING, sync_status.NEEDS_ACTION, sync_status.SYNC_ERROR))
				self.assertIn(
					entry["mode"],
					(sync_reason_codes.AUTO_RESOLVE, sync_reason_codes.AUTO_PREPARE, sync_reason_codes.MANUAL),
				)
		for code in sync_reason_codes.RECEIPT_GATED_REASONS:
			self.assertEqual(sync_reason_codes.REASONS[code]["mode"], sync_reason_codes.AUTO_PREPARE)

	def test_planner_only_uses_registered_reason_codes(self):
		path = frappe.get_app_path("marketplace_integrations", "services", "sales_order_reconciliation.py")
		with open(path) as handle:
			used = set(re.findall(r"reasons\.([A-Z_]+)", handle.read()))
		self.assertFalse({code for code in used if code not in sync_reason_codes.REASONS})
