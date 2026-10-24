# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#326 navigation: the Dock mounted on ERPNext's rail and the two Sidebar shells.

Navigation is presentation. Reach is decided by the read permissions of #322/#323; a
shell shows for a user only when that user can read at least one item in it. These tests
resolve the shells through the framework's own function (`resolve_sidebar`) for throwaway
users, read the shipped Dock file, and pin the framework internals the mount relies on
(verified on Frappe 16.52.0 / ERPNext 16.51.1; recheck on any Frappe upgrade).
"""

import json
import os
import re

import frappe
from frappe.desk.doctype.sidebar.sidebar import resolve_sidebar
from frappe.tests import UnitTestCase

from marketplace_integrations.tests.test_sidebar import (
	SHELL_MODULES,
	SHELLS,
	_load,
	_shell_path,
	_sprite_icon_ids,
)
from marketplace_integrations.tests.utils import make_role_user

APP = "marketplace_integrations"
MODULE_FOLDER = "marketplace_integrations"
HOME_WORKSPACE = "Tamwenat Marketplaces"


def _read(app, *parts):
	with open(frappe.get_app_path(app, *parts)) as f:
		return f.read()


class UnitTestNavigationFiles(UnitTestCase):
	"""Shipped files only; no site data needed."""

	def setUp(self):
		self.dock = _load(frappe.get_app_path(APP, "dock", APP, f"{APP}.json"))

	def test_dock_is_a_standard_mount_on_erpnext(self):
		self.assertEqual(self.dock["doctype"], "Dock")
		self.assertEqual(self.dock["name"], APP)
		self.assertEqual(self.dock["app"], APP)
		self.assertEqual(self.dock["standard"], 1)
		self.assertEqual(self.dock["mount_on"], "erpnext")
		self.assertFalse(self.dock.get("user"))

	def test_dock_rows_have_icon_title_and_an_existing_shell(self):
		icon_ids = _sprite_icon_ids()
		titles = []
		for row in self.dock["items"]:
			with self.subTest(row=row.get("title")):
				self.assertTrue(row.get("icon"), "a standard Dock row needs an icon")
				self.assertTrue(row.get("title"), "a standard Dock row needs a title")
				self.assertEqual(row["link_type"], "Sidebar")
				self.assertIn(row["icon"], icon_ids)
				self.assertTrue(os.path.isfile(_shell_path(frappe.scrub(row["link_to"]))))
				self.assertEqual(_load(_shell_path(frappe.scrub(row["link_to"])))["name"], row["link_to"])
				titles.append(row["title"])
		self.assertEqual(sorted(titles), sorted(SHELLS))

	def test_each_dock_row_opens_a_sidebar_with_its_own_module(self):
		# Frappe resolves a Workspace route to the first shell whose module lists it, so two
		# shells under one module open the first of them for the other's Home (#326).
		modules = []
		for row in self.dock["items"]:
			with self.subTest(row=row.get("title")):
				self.assertEqual(row["link_type"], "Sidebar")
				sidebar = _load(_shell_path(frappe.scrub(row["link_to"])))
				self.assertEqual(sidebar["module"], SHELL_MODULES[row["link_to"]])
				modules.append(sidebar["module"])
		self.assertEqual(len(modules), len(set(modules)))
		self.assertTrue(set(modules) <= set(_read(APP, "modules.txt").splitlines()))

	def test_old_navigation_is_retired(self):
		root = frappe.get_app_path(APP, MODULE_FOLDER)
		self.assertFalse(os.path.exists(os.path.join(root, "sidebar", "tamwenat_marketplaces")))
		self.assertFalse(os.path.exists(os.path.join(root, "sidebar", "bosta")))
		self.assertFalse(os.path.exists(frappe.get_app_path(APP, "desktop_icon")))
		self.assertFalse(os.path.exists(frappe.get_app_path(APP, "api", "permission.py")))
		self.assertEqual(frappe.get_hooks("add_to_apps_screen", app_name=APP), [])

	def test_home_workspace_is_open_to_every_platform_role(self):
		# The first sidebar item is Home; a platform user must be allowed to see it or the
		# landing item becomes a report.
		path = frappe.get_app_path(
			APP, MODULE_FOLDER, "workspace", "tamwenat_marketplaces", "tamwenat_marketplaces.json"
		)
		roles = {r["role"] for r in _load(path)["roles"]}
		self.assertTrue({"System Manager", "Talabat User", "Noon User", "Shopify User"} <= roles)
		path = frappe.get_app_path(
			APP, MODULE_FOLDER, "workspace", "shopify_reconciliation", "shopify_reconciliation.json"
		)
		self.assertEqual({r["role"] for r in _load(path)["roles"]}, {"System Manager", "Shopify User"})

	def test_marketplaces_workspace_has_no_static_platform_headers(self):
		# Workspace header blocks are not permission-filtered (shortcuts are), so a Talabat
		# user would see empty "Shopify"/"Noon"/"Bosta" headings. Only neutral headers allowed.
		path = frappe.get_app_path(
			APP, MODULE_FOLDER, "workspace", "tamwenat_marketplaces", "tamwenat_marketplaces.json"
		)
		blocks = json.loads(_load(path)["content"])
		headers = [re.sub(r"<[^>]+>", "", b["data"]["text"]) for b in blocks if b["type"] == "header"]
		self.assertEqual(sorted(headers), ["Operations", "Overview", "Tamwenat Marketplaces"])


class UnitTestNavigationPerRole(UnitTestCase):
	"""Resolve both shells through the framework for throwaway users."""

	def tearDown(self):
		frappe.set_user("Administrator")

	def _user(self, label, perms, roles=()):
		email, _role = make_role_user(self, label, perms)
		if roles:
			frappe.get_doc("User", email).add_roles(*roles)
			frappe.clear_cache(user=email)
		return email

	def _shells(self, email):
		"""{shell name: ResolvedSidebar} for the shells that resolve for the user."""
		frappe.set_user(email)
		result = {}
		for name in SHELLS:
			resolved = resolve_sidebar(name, email)
			if resolved is not None:
				self._assert_no_empty_section(name, resolved.items)
				result[name] = resolved
		frappe.set_user("Administrator")
		return result

	def _assert_no_empty_section(self, shell, items):
		# A section's items are the rows right after it flagged `child`; a following
		# section, spacer or top-level (non-child) row means the section is empty.
		for index, item in enumerate(items):
			if item.get("type") == "Section Break":
				following = items[index + 1] if index + 1 < len(items) else None
				self.assertTrue(
					following
					and following.get("type") not in ("Section Break", "Spacer")
					and following.get("child"),
					f"{shell}: empty section '{item.get('label')}' survived permission filtering",
				)

	@staticmethod
	def _targets(resolved):
		return {i.get("link_to") for i in resolved.items if i.get("link_to")}

	def _platform_user(self, role):
		return self._user(f"Nav {role}", {"User": ["read"]}, roles=(role,))

	def test_talabat_user_sees_marketplaces_not_bosta(self):
		shells = self._shells(self._platform_user("Talabat User"))
		self.assertEqual(set(shells), {"Marketplaces"})
		targets = self._targets(shells["Marketplaces"])
		self.assertIn("Marketplace Order", targets)
		self.assertIn("marketplace-talabat-catalog-mapping", targets)
		self.assertFalse(
			{"Noon Return", "Shopify Reconciliation Summary", "marketplace-shopify-catalog-mapping"} & targets
		)
		# Landing: the first item is Home, not a report.
		self.assertEqual(shells["Marketplaces"].items[0].get("link_to"), HOME_WORKSPACE)

	def test_noon_user_sees_marketplaces_with_returns(self):
		shells = self._shells(self._platform_user("Noon User"))
		self.assertEqual(set(shells), {"Marketplaces"})
		targets = self._targets(shells["Marketplaces"])
		self.assertIn("Noon Return", targets)
		self.assertNotIn("Shopify Reconciliation Summary", targets)
		self.assertEqual(shells["Marketplaces"].items[0].get("link_to"), HOME_WORKSPACE)

	def test_shopify_user_sees_shopify_items(self):
		shells = self._shells(self._platform_user("Shopify User"))
		self.assertEqual(set(shells), {"Marketplaces"})
		targets = self._targets(shells["Marketplaces"])
		self.assertTrue(
			{
				"Shopify Reconciliation Summary",
				"Shopify Order Import Failures",
				"Shopify Reconciliation",
				"marketplace-shopify-catalog-mapping",
				"Shopify Webhook Delivery",
			}
			<= targets
		)
		self.assertNotIn("Noon Return", targets)
		self.assertEqual(shells["Marketplaces"].items[0].get("link_to"), HOME_WORKSPACE)

	def test_bosta_only_user_sees_bosta_without_settings(self):
		shells = self._shells(self._user("Nav Bosta Reader", {"Bosta Shipment": ["read"]}))
		self.assertEqual(set(shells), {"Bosta"})
		self.assertEqual(self._targets(shells["Bosta"]), {"Bosta Shipment"})

	def test_real_bosta_user_role_sees_bosta_only_without_settings(self):
		shells = self._shells(self._user("Nav Real Bosta", {"User": ["read"]}, roles=("Bosta User",)))
		self.assertEqual(set(shells), {"Bosta"})
		self.assertEqual(self._targets(shells["Bosta"]), {"Bosta Shipment"})

	def test_sales_user_sees_bosta_only(self):
		# Not a navigation setting: Bosta Shipment already grants read to Sales User.
		shells = self._shells(self._user("Nav Sales", {"User": ["read"]}, roles=("Sales User",)))
		self.assertEqual(set(shells), {"Bosta"})

	def test_accounts_user_sees_neither(self):
		self.assertEqual(
			self._shells(self._user("Nav Accounts", {"User": ["read"]}, roles=("Accounts User",))), {}
		)

	def test_system_manager_sees_both_with_settings(self):
		shells = self._shells(self._user("Nav Manager", {"User": ["read"]}, roles=("System Manager",)))
		self.assertEqual(set(shells), {"Marketplaces", "Bosta"})
		self.assertTrue({"Bosta Shipment", "Bosta Settings"} <= self._targets(shells["Bosta"]))
		self.assertIn("Marketplace Order", self._targets(shells["Marketplaces"]))


class UnitTestNavigationRouting(UnitTestCase):
	"""Site data: shells and module rows exist after migrate; routes resolve to one shell."""

	def test_module_defs_exist(self):
		for module in SHELL_MODULES.values():
			self.assertTrue(frappe.db.exists("Module Def", module), module)

	def test_sidebar_row_sits_on_its_own_module(self):
		for shell, module in SHELL_MODULES.items():
			self.assertEqual(frappe.db.get_value("Sidebar", shell, "module"), module, shell)
		self.assertEqual(frappe.db.get_value("Sidebar", "Bosta", "module"), "Bosta")

	def _index(self):
		from frappe.desk.doctype.sidebar.sidebar import ShellIndex

		email = "Administrator"
		resolved = {s: resolve_sidebar(s, email) for s in SHELLS}
		return ShellIndex({s: r.as_boot_entry() for s, r in resolved.items() if r})

	def test_every_shell_workspace_belongs_to_exactly_one_shell(self):
		index = self._index()
		self.assertEqual(dict(index.workspace_owners()).get(HOME_WORKSPACE), "Marketplaces")
		owners = {}
		for shell, entry in index.all.items():
			for workspace in entry.get("workspaces") or []:
				owners.setdefault(workspace, []).append(shell)
		for name, shells in owners.items():
			self.assertEqual(len(shells), 1, f"workspace {name} is owned by {shells}")

	def test_bosta_doctypes_resolve_to_the_bosta_shell(self):
		index = self._index()
		for doctype in ("Bosta Shipment", "Bosta Settings"):
			module = frappe.db.get_value("DocType", doctype, "module")
			self.assertEqual(index.resolve("DocType", doctype, module), "Bosta")
		self.assertEqual(
			index.resolve("DocType", "Marketplace Order", "Marketplace Integrations"), "Marketplaces"
		)


class UnitTestDockFrameworkWiring(UnitTestCase):
	"""The Dock mount is framework behavior no hook protects. If an upgrade changes it, the
	entries silently vanish from the rail. Verified: Frappe 16.52.0 / ERPNext 16.51.1."""

	def test_dock_still_supports_mounting_and_requires_icon_and_title(self):
		source = _read("frappe", "desk", "doctype", "dock", "dock.py")
		checks = {
			"a `mount_on` field on Dock": r"mount_on",
			"`mounted_apps()` resolving companion -> host": r"def mounted_apps\(",
			"`validate_added_rows` requiring an icon and title on standard rows": r"def validate_added_rows\(self\)[\s\S]*?\(self\.standard or row\.added\) and not \(row\.icon and row\.title\)",
			"`resolve_app_dock(app)` building the rail": r"def resolve_app_dock\(",
		}
		for what, pattern in checks.items():
			self.assertTrue(
				re.search(pattern, source),
				f"dock.py no longer provides: {what}. Recheck dock/{APP}/{APP}.json "
				"(mount_on erpnext, icon and title on every row) and docs/frappe-best-practices.md "
				"'Version compatibility' (#326).",
			)

	def test_sync_still_imports_app_rooted_dock_and_sidebar(self):
		source = _read("frappe", "model", "sync.py")
		match = re.search(r"APP_ROOTED_DOCTYPES\s*=\s*\[(.*?)\]", source, re.S)
		self.assertTrue(
			match, "sync.py no longer defines APP_ROOTED_DOCTYPES. Recheck how a Dock is synced (#326)."
		)
		for pair in ('("desk", "dock")', '("desk", "sidebar")'):
			self.assertIn(
				pair,
				match.group(1),
				f"sync.py APP_ROOTED_DOCTYPES no longer lists {pair}: <app>/dock/<app>/<app>.json "
				"or <module>/sidebar/<name>/<name>.json may not be imported. Recheck (#326).",
			)

	def test_sidebar_resolution_still_drops_empty_shells(self):
		source = _read("frappe", "desk", "doctype", "sidebar", "sidebar.py")
		self.assertTrue(
			re.search(r"def resolve_sidebar\(shell", source)
			and re.search(r'row\.get\("type"\) not in \("Section Break", "Spacer"\)', source),
			"sidebar.py no longer drops a shell with no permitted item through resolve_sidebar. "
			"Bosta-only / Marketplaces-only visibility in test_navigation depends on it; recheck (#326).",
		)
