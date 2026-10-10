# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Always-on guard for workspace_sidebar/tamwenat_marketplaces.json.

A wrong link target or icon id does not fail migrate; the sidebar row just renders
nothing or opens a broken route. This reads the JSON fixtures in the app folder
(not the DB), so it does not depend on a migrate having run, and checks the file's
structure, link targets, icon ids and route_options.
"""

import json
import os

import frappe
from frappe.tests import UnitTestCase

APP = "marketplace_integrations"
MODULE_FOLDER = "marketplace_integrations"
ICON_SPRITES = ("lucide", "timeless", "espresso")
# link_type -> (folder holding <name>/<name>.json fixtures, key holding the record name)
TARGET_FOLDERS = {
	"DocType": ("doctype", "name"),
	"Report": ("report", "report_name"),
	"Page": ("page", "name"),
	"Workspace": ("workspace", "name"),
}


def _load(path):
	with open(path) as f:
		return json.load(f)


def _app_record_names(folder, name_key):
	root = frappe.get_app_path(APP, MODULE_FOLDER, folder)
	names = set()
	for entry in os.listdir(root):
		path = os.path.join(root, entry, f"{entry}.json")
		if os.path.isfile(path):
			names.add(_load(path)[name_key])
	return names


def _sprite_icon_ids():
	ids = set()
	for sprite in ICON_SPRITES:
		path = frappe.get_app_path("frappe", "public", "icons", sprite, "icons.svg")
		with open(path) as f:
			source = f.read()
		ids.update(part.split('"', 1)[0] for part in source.split('id="icon-')[1:])
	return ids


class UnitTestWorkspaceSidebar(UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		sidebar = _load(frappe.get_app_path(APP, "workspace_sidebar", "tamwenat_marketplaces.json"))
		cls.sidebar = sidebar
		cls.items = sidebar["items"]
		cls.links = [i for i in cls.items if i["type"] == "Link"]

	def test_children_follow_a_section_break(self):
		in_section = False
		for item in self.items:
			if item["type"] == "Section Break":
				in_section = True
			elif item["child"]:
				self.assertTrue(in_section, f"{item['label']} is a child before any Section Break")
			else:
				# a top-level Link ends the previous section
				in_section = False

	def test_every_link_target_exists(self):
		existing = {
			link_type: _app_record_names(folder, key) for link_type, (folder, key) in TARGET_FOLDERS.items()
		}
		for item in self.links:
			with self.subTest(label=item["label"]):
				self.assertIn(item["link_type"], existing)
				self.assertIn(item["link_to"], existing[item["link_type"]])

	def test_every_icon_exists_in_the_frappe_sprites(self):
		icon_ids = _sprite_icon_ids()
		icons = {self.sidebar["header_icon"]} | {i["icon"] for i in self.items if i.get("icon")}
		for icon in sorted(icons):
			with self.subTest(icon=icon):
				self.assertIn(icon, icon_ids)

	def test_route_options_are_valid_json_objects(self):
		for item in self.items:
			if item.get("route_options"):
				with self.subTest(label=item["label"]):
					self.assertIsInstance(json.loads(item["route_options"]), dict)

	def test_labels_are_unique(self):
		labels = [i["label"] for i in self.items]
		self.assertEqual(len(labels), len(set(labels)))
