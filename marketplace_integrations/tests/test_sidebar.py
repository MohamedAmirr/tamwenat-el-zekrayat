# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Always-on guard for the app's Sidebar fixture (Frappe 16.51 format).

The file lives at <module>/sidebar/<name>/<name>.json, which is where Frappe's
sync imports a module-rooted Sidebar from. The old workspace_sidebar/ folder is
not imported by Frappe 16.51.

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
SIDEBAR_NAME = "Tamwenat Marketplaces"
SIDEBAR_MODULE = "Marketplace Integrations"
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


class UnitTestSidebar(UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.path = frappe.get_app_path(
			APP, MODULE_FOLDER, "sidebar", "tamwenat_marketplaces", "tamwenat_marketplaces.json"
		)
		sidebar = _load(cls.path)
		cls.sidebar = sidebar
		cls.items = sidebar["items"]
		cls.links = [i for i in cls.items if i["type"] == "Link"]

	def test_file_sits_where_frappe_imports_it(self):
		# sync imports <module folder>/sidebar/<scrubbed name>/<scrubbed name>.json
		module_path = frappe.get_module_path(SIDEBAR_MODULE)
		self.assertEqual(
			os.path.realpath(self.path),
			os.path.realpath(
				os.path.join(module_path, "sidebar", "tamwenat_marketplaces", "tamwenat_marketplaces.json")
			),
		)
		self.assertEqual(frappe.scrub(SIDEBAR_NAME), "tamwenat_marketplaces")
		self.assertFalse(os.path.exists(frappe.get_app_path(APP, "workspace_sidebar")))

	def test_sidebar_identity(self):
		self.assertEqual(self.sidebar["doctype"], "Sidebar")
		self.assertEqual(self.sidebar["name"], SIDEBAR_NAME)
		self.assertEqual(self.sidebar["title"], SIDEBAR_NAME)
		self.assertEqual(self.sidebar["module"], SIDEBAR_MODULE)
		self.assertEqual(self.sidebar["app"], APP)
		self.assertEqual(self.sidebar["standard"], 1)

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

	def test_filters_are_valid_filter_lists_on_the_linked_doctype(self):
		for item in self.items:
			if item.get("filters"):
				with self.subTest(label=item["label"]):
					filters = json.loads(item["filters"])
					self.assertIsInstance(filters, list)
					for doctype, fieldname, operator, value in filters:
						self.assertEqual(doctype, item["link_to"])
						self.assertTrue(fieldname and operator and value)

	def test_filtered_links_use_filters_not_route_options(self):
		# Frappe 16.51 reads a DocType link's filter from `filters`; `filters` is also part of
		# the item's identity, so rows differing only by route_options are collapsed to one.
		for item in self.items:
			with self.subTest(label=item["label"]):
				if item["link_type"] != "Page":
					self.assertFalse(item.get("route_options"))

	def test_link_identities_are_unique(self):
		# Frappe keeps the first link per (type, link_type, link_to, url, filters) and drops
		# the rest when it resolves the sidebar.
		identities = [
			(i["type"], i["link_type"], i.get("link_to"), i.get("url"), i.get("filters")) for i in self.links
		]
		self.assertEqual(len(identities), len(set(identities)))

	def test_labels_are_unique(self):
		labels = [i["label"] for i in self.items]
		self.assertEqual(len(labels), len(set(labels)))
