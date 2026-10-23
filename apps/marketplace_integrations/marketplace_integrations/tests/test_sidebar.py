# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Always-on guard for the app's two Sidebar shells, Marketplaces and Bosta (Frappe 16.51+ format).

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
# Sprites a Dock/shell icon id may come from: Lucide, plus the `*-soft` module icons that
# frappe and erpnext ship for their own rows (our `store`/`truck` are Lucide stroke icons).
ICON_SPRITES = (
	("frappe", "public", "icons", "lucide", "icons.svg"),
	("frappe", "public", "icons", "module-icons.svg"),
	("erpnext", "public", "icons", "module-icons.svg"),
)
SHELLS = {"Marketplaces": "marketplaces", "Bosta": "bosta"}
SIDEBAR_MODULE = "Marketplace Integrations"
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
	for app, *parts in ICON_SPRITES:
		path = frappe.get_app_path(app, *parts)
		with open(path) as f:
			source = f.read()
		ids.update(part.split('"', 1)[0] for part in source.split('id="icon-')[1:])
	return ids


def _shell_path(folder):
	return frappe.get_app_path(APP, MODULE_FOLDER, "sidebar", folder, f"{folder}.json")


class UnitTestSidebar(UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.shells = {name: _load(_shell_path(folder)) for name, folder in SHELLS.items()}

	def test_files_sit_where_frappe_imports_them(self):
		# sync imports <module folder>/sidebar/<scrubbed name>/<scrubbed name>.json
		module_path = frappe.get_module_path(SIDEBAR_MODULE)
		for name, folder in SHELLS.items():
			with self.subTest(shell=name):
				self.assertEqual(frappe.scrub(name), folder)
				self.assertEqual(
					os.path.realpath(_shell_path(folder)),
					os.path.realpath(os.path.join(module_path, "sidebar", folder, f"{folder}.json")),
				)
		self.assertFalse(os.path.exists(frappe.get_app_path(APP, "workspace_sidebar")))

	def test_sidebar_identity(self):
		for name, sidebar in self.shells.items():
			with self.subTest(shell=name):
				self.assertEqual(sidebar["doctype"], "Sidebar")
				self.assertEqual(sidebar["name"], name)
				self.assertEqual(sidebar["title"], name)
				self.assertEqual(sidebar["module"], SIDEBAR_MODULE)
				self.assertEqual(sidebar["app"], APP)
				self.assertEqual(sidebar["standard"], 1)

	def test_children_follow_a_section_break(self):
		for name, sidebar in self.shells.items():
			in_section = False
			for item in sidebar["items"]:
				with self.subTest(shell=name, label=item["label"]):
					if item["type"] == "Section Break":
						in_section = True
					elif item["child"]:
						self.assertTrue(in_section, f"{item['label']} is a child before any Section Break")
					else:
						# a top-level Link ends the previous section
						in_section = False

	def test_every_section_has_children(self):
		# An empty section only matters here for the shipped file; the per-role result is
		# asserted in test_navigation.
		for name, sidebar in self.shells.items():
			items = sidebar["items"]
			for index, item in enumerate(items):
				if item["type"] == "Section Break":
					with self.subTest(shell=name, section=item["label"]):
						following = items[index + 1] if index + 1 < len(items) else None
						self.assertTrue(following and following["child"])

	def test_every_link_target_exists(self):
		existing = {
			link_type: _app_record_names(folder, key) for link_type, (folder, key) in TARGET_FOLDERS.items()
		}
		for name, sidebar in self.shells.items():
			for item in (i for i in sidebar["items"] if i["type"] == "Link"):
				with self.subTest(shell=name, label=item["label"]):
					self.assertIn(item["link_type"], existing)
					self.assertIn(item["link_to"], existing[item["link_type"]])

	def test_every_icon_exists_in_the_frappe_sprites(self):
		icon_ids = _sprite_icon_ids()
		for name, sidebar in self.shells.items():
			icons = {sidebar["header_icon"]} | {i["icon"] for i in sidebar["items"] if i.get("icon")}
			for icon in sorted(icons):
				with self.subTest(shell=name, icon=icon):
					self.assertIn(icon, icon_ids)

	def test_filters_are_valid_filter_lists_on_the_linked_doctype(self):
		for name, sidebar in self.shells.items():
			for item in sidebar["items"]:
				if item.get("filters"):
					with self.subTest(shell=name, label=item["label"]):
						filters = json.loads(item["filters"])
						self.assertIsInstance(filters, list)
						for doctype, fieldname, operator, value in filters:
							self.assertEqual(doctype, item["link_to"])
							self.assertTrue(fieldname and operator and value)

	def test_filtered_links_use_filters_not_route_options(self):
		# Frappe 16.51 reads a DocType link's filter from `filters`; `filters` is also part of
		# the item's identity, so rows differing only by route_options are collapsed to one.
		for name, sidebar in self.shells.items():
			for item in sidebar["items"]:
				with self.subTest(shell=name, label=item["label"]):
					if item["link_type"] != "Page":
						self.assertFalse(item.get("route_options"))

	def test_link_identities_are_unique(self):
		# Frappe keeps the first link per (type, link_type, link_to, url, filters) and drops
		# the rest when it resolves the sidebar.
		for name, sidebar in self.shells.items():
			links = [i for i in sidebar["items"] if i["type"] == "Link"]
			identities = [
				(i["type"], i["link_type"], i.get("link_to"), i.get("url"), i.get("filters")) for i in links
			]
			with self.subTest(shell=name):
				self.assertEqual(len(identities), len(set(identities)))

	def test_labels_are_unique(self):
		for name, sidebar in self.shells.items():
			labels = [i["label"] for i in sidebar["items"]]
			with self.subTest(shell=name):
				self.assertEqual(len(labels), len(set(labels)))

	def test_settings_are_only_in_the_bosta_shell(self):
		links = {n: {i.get("link_to") for i in s["items"]} for n, s in self.shells.items()}
		self.assertIn("Bosta Settings", links["Bosta"])
		self.assertIn("Bosta Shipment", links["Bosta"])
		self.assertFalse({"Bosta Settings", "Bosta Shipment"} & links["Marketplaces"])
