# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from io import BytesIO

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.xlsxutils import make_xlsx
from openpyxl import load_workbook

from marketplace_integrations.api import mapping_import
from marketplace_integrations.services import mapping_import as mapping_import_service
from marketplace_integrations.tests.utils import make_role_user

UOM = "Nos"
ITEM_GROUP = "Products"


class IntegrationTestMappingImport(IntegrationTestCase):
	def setUp(self):
		self._counter = 0
		self._tracked = []

	def tearDown(self):
		# make_role_user commits on cleanup, which would persist every fixture any test in
		# this class left in the open transaction, so each helper-made fixture is deleted here.
		accounts = [name for doctype, name in self._tracked if doctype == "Marketplace Account"]
		if accounts:
			# Rows the import/apply tests wrote themselves.
			frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": ["in", accounts]})
		for doctype, name in reversed(self._tracked):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)

	def track(self, doc):
		self._tracked.append((doc.doctype, doc.name))
		return doc

	def unique(self, prefix):
		self._counter += 1
		return f"{prefix}-{frappe.generate_hash(length=8)}"

	def make_item(self):
		item_code = self.unique("TEST-MI-ITEM")
		return self.track(
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_group": ITEM_GROUP,
					"stock_uom": UOM,
					"is_stock_item": 0,
				}
			).insert()
		)

	def make_account(self):
		return self.track(
			frappe.get_doc(
				{
					"doctype": "Marketplace Account",
					"account_name": self.unique("Mapping Import Account"),
					"platform": "Talabat",
					"external_account_id": self.unique("mapping-import-account"),
					"connection_doctype": "UOM",
					"connection": UOM,
				}
			).insert()
		)

	def make_mapping(self, account, item, reference=None, **kwargs):
		values = {
			"doctype": "Marketplace Product Mapping",
			"marketplace_account": account.name,
			"item": item.name,
			"external_reference": reference or self.unique("listing"),
		}
		values.update(kwargs)
		return self.track(frappe.get_doc(values).insert())

	def make_file(self, content, is_private=1):
		return self.track(
			frappe.get_doc(
				{
					"doctype": "File",
					"file_name": "mappings.xlsx",
					"content": content,
					"is_private": is_private,
				}
			).insert()
		).file_url

	def build_xlsx(self, rows):
		return make_xlsx(
			[mapping_import_service.HEADER_ROW, *rows], mapping_import_service.XLSX_SHEET_NAME
		).getvalue()

	def row(self, account, item, reference, **overrides):
		values = dict.fromkeys(mapping_import_service.HEADER_ROW, "")
		values.update(
			{"marketplace_account": account, "item": item, "external_reference": reference, "enabled": 1}
		)
		values.update(overrides)
		return [values[column] for column in mapping_import_service.HEADER_ROW]

	def export_bytes(self):
		mapping_import.export_mappings_xlsx()
		content = frappe.response["filecontent"]
		frappe.response.filecontent = None
		frappe.response.type = None
		frappe.response.filename = None
		return content

	def exported_row(self, content, reference):
		worksheet = load_workbook(filename=BytesIO(content), data_only=True).active
		header = [cell.value for cell in worksheet[1]]
		for row in worksheet.iter_rows(min_row=2, values_only=True):
			values = dict(zip(header, row, strict=True))
			if values["external_reference"] == reference:
				return values
		return None

	def test_export_and_unedited_reupload_are_unchanged(self):
		account = self.make_account()
		item = self.make_item()
		mapping = self.make_mapping(account, item, external_sku="sku-<opaque>")
		content = self.export_bytes()

		exported = self.exported_row(content, mapping.external_reference)
		self.assertEqual(list(exported), mapping_import_service.HEADER_ROW)
		self.assertEqual(exported["marketplace_account"], account.name)
		self.assertEqual(exported["external_sku"], "sku-<opaque>")
		self.assertEqual(exported["__modified"], str(mapping.modified))

		preview = mapping_import.preview_mappings_import(self.make_file(content))
		row = next(row for row in preview["rows"] if row["external_reference"] == mapping.external_reference)
		self.assertEqual(row["status"], "unchanged")

	def exported_rows(self, content):
		worksheet = load_workbook(filename=BytesIO(content), data_only=True).active
		header = [cell.value for cell in worksheet[1]]
		return [
			dict(zip(header, row, strict=True)) for row in worksheet.iter_rows(min_row=2, values_only=True)
		]

	def test_export_carries_every_column_of_a_shopify_style_mapping(self):
		account = self.make_account()
		item = self.make_item()
		variant = "gid://shopify/ProductVariant/123"
		mapping = self.make_mapping(
			account,
			item,
			variant,
			external_variant_id=variant,
			external_product_id="gid://shopify/Product/55",
			external_sku="SKU-<1>",
			sales_uom=UOM,
			enabled=0,
		)

		exported = self.exported_row(self.export_bytes(), variant)

		# A blank column is exported as an empty cell, not the string "None".
		self.assertIn(exported.pop("external_barcode"), ("", None))
		self.assertEqual(
			exported,
			{
				"marketplace_account": account.name,
				"item": item.name,
				"external_reference": variant,
				"external_product_id": "gid://shopify/Product/55",
				"external_sku": "SKU-<1>",
				"external_variant_id": variant,
				"sales_uom": UOM,
				"enabled": 0,
				"__modified": str(mapping.modified),
			},
		)

	def test_export_lists_all_accounts_ordered_by_account_then_reference(self):
		first, second = self.make_account(), self.make_account()
		item = self.make_item()
		prefix = self.unique("order")
		self.make_mapping(second, item, f"{prefix}-a")
		self.make_mapping(first, item, f"{prefix}-b")
		self.make_mapping(first, item, f"{prefix}-a")

		rows = [
			(row["marketplace_account"], row["external_reference"])
			for row in self.exported_rows(self.export_bytes())
			if row["external_reference"].startswith(prefix)
		]

		self.assertEqual(
			rows,
			sorted([(first.name, f"{prefix}-a"), (first.name, f"{prefix}-b"), (second.name, f"{prefix}-a")]),
		)

	def test_export_is_limited_to_accounts_the_user_may_read(self):
		# #322 (strict): a user sees marketplace rows only through a platform role, so the user
		# holds Talabat User (its shipped DocPerm rows grant mapping/account access). Export is
		# limited by platform (no Shopify rows) and by a User Permission on one Talabat account.
		allowed, other, foreign = self.make_account(), self.make_account(), self.make_account()
		frappe.db.set_value("Marketplace Account", foreign.name, "platform", "Shopify", update_modified=False)
		item = self.make_item()
		prefix = self.unique("scoped")
		self.make_mapping(allowed, item, f"{prefix}-allowed")
		self.make_mapping(other, item, f"{prefix}-other")
		self.make_mapping(foreign, item, f"{prefix}-foreign")
		email, _role = make_role_user(self, "Mapping Export Scoped", {})
		frappe.get_doc("User", email).add_roles("Talabat User")
		user_permission = frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": email,
				"allow": "Marketplace Account",
				"for_value": allowed.name,
			}
		).insert()
		self.track(user_permission)
		frappe.clear_cache(user=email)

		with self.set_user(email):
			content = self.export_bytes()

		references = {row["external_reference"] for row in self.exported_rows(content)}
		self.assertIn(f"{prefix}-allowed", references)
		self.assertNotIn(f"{prefix}-other", references)
		self.assertNotIn(f"{prefix}-foreign", references)

	def test_export_without_user_permission_is_limited_to_the_users_platform(self):
		talabat, shopify = self.make_account(), self.make_account()
		frappe.db.set_value("Marketplace Account", shopify.name, "platform", "Shopify", update_modified=False)
		item = self.make_item()
		prefix = self.unique("platform")
		self.make_mapping(talabat, item, f"{prefix}-talabat")
		self.make_mapping(shopify, item, f"{prefix}-shopify")
		email, _role = make_role_user(self, "Mapping Export Platform", {})
		frappe.get_doc("User", email).add_roles("Talabat User")
		frappe.clear_cache(user=email)

		with self.set_user(email):
			content = self.export_bytes()

		references = {row["external_reference"] for row in self.exported_rows(content)}
		self.assertIn(f"{prefix}-talabat", references)
		self.assertNotIn(f"{prefix}-shopify", references)

	def test_new_rows_create_and_existing_identity_updates_without_duplication(self):
		account = self.make_account()
		item_a = self.make_item()
		item_b = self.make_item()
		mapping = self.make_mapping(account, item_a, "listing-update")
		update = self.row(
			account.name,
			item_b.name,
			mapping.external_reference,
			external_sku="new-sku",
			__modified=str(mapping.modified),
		)
		new = self.row(account.name, item_a.name, "listing-new", external_product_id="product-new")

		result = mapping_import.apply_mappings_import(self.make_file(self.build_xlsx([update, new])))
		self.assertEqual(result["applied_count"], 2)
		mapping.reload()
		self.assertEqual(mapping.item, item_b.name)
		self.assertEqual(mapping.external_sku, "new-sku")
		self.assertEqual(
			frappe.db.count(
				"Marketplace Product Mapping",
				{"marketplace_account": account.name, "external_reference": mapping.external_reference},
			),
			1,
		)
		self.assertTrue(
			frappe.db.exists(
				"Marketplace Product Mapping",
				{"marketplace_account": account.name, "external_reference": "listing-new"},
			)
		)

	def test_two_different_references_for_one_item_create_two_mappings(self):
		account = self.make_account()
		item = self.make_item()
		rows = [
			self.row(account.name, item.name, "listing-a"),
			self.row(account.name, item.name, "listing-b"),
		]
		result = mapping_import.apply_mappings_import(self.make_file(self.build_xlsx(rows)))
		self.assertEqual(result["applied_count"], 2)
		self.assertEqual(
			frappe.db.count(
				"Marketplace Product Mapping", {"marketplace_account": account.name, "item": item.name}
			),
			2,
		)

	def test_enabled_zero_disables_an_existing_mapping(self):
		account = self.make_account()
		item = self.make_item()
		mapping = self.make_mapping(account, item, "listing-disable")
		row = self.row(
			account.name, item.name, mapping.external_reference, enabled=0, __modified=str(mapping.modified)
		)
		result = mapping_import.apply_mappings_import(self.make_file(self.build_xlsx([row])))
		self.assertEqual(result["applied_count"], 1)
		mapping.reload()
		self.assertEqual(mapping.enabled, 0)

	def test_blank_enabled_defaults_to_enabled(self):
		account = self.make_account()
		item = self.make_item()
		row = self.row(account.name, item.name, "listing-default-enabled", enabled="")
		mapping_import.apply_mappings_import(self.make_file(self.build_xlsx([row])))
		created = frappe.get_doc(
			"Marketplace Product Mapping",
			{"marketplace_account": account.name, "external_reference": "listing-default-enabled"},
		)
		self.assertEqual(created.enabled, 1)

	def test_unknown_account_item_and_uom_are_row_errors(self):
		account = self.make_account()
		item = self.make_item()
		rows = [
			self.row(self.unique("unknown-account"), item.name, "listing-bad-account"),
			self.row(account.name, self.unique("unknown-item"), "listing-bad-item"),
			self.row(account.name, item.name, "listing-bad-uom", sales_uom=self.unique("unknown-uom")),
		]
		preview = mapping_import.preview_mappings_import(self.make_file(self.build_xlsx(rows)))
		self.assertTrue(all(row["status"] == "error" for row in preview["rows"]))
		self.assertIn("Marketplace Account", preview["rows"][0]["errors"][0])
		self.assertIn("Item", preview["rows"][1]["errors"][0])
		self.assertIn("UOM", preview["rows"][2]["errors"][0])

	def test_preview_never_writes_a_valid_change(self):
		account = self.make_account()
		item = self.make_item()
		mapping = self.make_mapping(account, item, "listing-preview")
		row = self.row(
			account.name,
			item.name,
			mapping.external_reference,
			external_sku="must-not-persist",
			__modified=str(mapping.modified),
		)
		preview = mapping_import.preview_mappings_import(self.make_file(self.build_xlsx([row])))
		self.assertEqual(preview["rows"][0]["status"], "changed")
		mapping.reload()
		self.assertFalse(mapping.external_sku)

	def test_duplicate_identity_in_a_workbook_is_an_error(self):
		account = self.make_account()
		item = self.make_item()
		rows = [
			self.row(account.name, item.name, "listing-duplicate"),
			self.row(account.name, item.name, "listing-duplicate"),
		]
		preview = mapping_import.preview_mappings_import(self.make_file(self.build_xlsx(rows)))
		self.assertTrue(all(row["status"] == "error" for row in preview["rows"]))
		self.assertEqual(
			mapping_import.apply_mappings_import(self.make_file(self.build_xlsx(rows)))["applied_count"], 0
		)

	def test_stale_existing_row_is_rejected(self):
		account = self.make_account()
		item = self.make_item()
		mapping = self.make_mapping(account, item, "listing-stale")
		stale_modified = str(mapping.modified)
		mapping.external_sku = "changed-after-export"
		mapping.save()
		row = self.row(
			account.name, item.name, mapping.external_reference, enabled=0, __modified=stale_modified
		)
		result = mapping_import.apply_mappings_import(self.make_file(self.build_xlsx([row])))
		self.assertEqual(result["rows"][0]["status"], "error")
		mapping.reload()
		self.assertEqual(mapping.enabled, 1)

	def test_file_must_be_readable_and_preview_apply_are_post_only(self):
		account = self.make_account()
		item = self.make_item()
		file_url = self.make_file(self.build_xlsx([self.row(account.name, item.name, "listing-file")]))
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"mapping-import-{frappe.generate_hash(length=10)}@example.com",
				"first_name": "Mapping Import Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()
		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				mapping_import.preview_mappings_import(file_url)
			with self.assertRaises(frappe.PermissionError):
				mapping_import.apply_mappings_import(file_url)
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[mapping_import.preview_mappings_import], ["POST"]
		)
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[mapping_import.apply_mappings_import], ["POST"]
		)

	def test_select_only_user_cannot_export_or_preview_existing_mappings(self):
		account = self.make_account()
		item = self.make_item()
		mapping = self.make_mapping(account, item, "listing-select-only")
		file_url = self.make_file(
			self.build_xlsx(
				[
					self.row(
						account.name,
						item.name,
						mapping.external_reference,
						__modified=str(mapping.modified),
					)
				]
			),
			is_private=0,
		)
		role_name = self.unique("Mapping Import Select Only")
		frappe.get_doc({"doctype": "Role", "role_name": role_name}).insert()
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"mapping-import-{frappe.generate_hash(length=10)}@example.com",
				"first_name": "Mapping Import Select Only Test User",
				"send_welcome_email": 0,
				"roles": [{"role": role_name}],
			}
		).insert()
		had_custom_permissions = frappe.db.exists("Custom DocPerm", {"parent": "Marketplace Product Mapping"})
		permission_name = frappe.permissions.add_permission(
			"Marketplace Product Mapping", role_name, ptype="select"
		)
		# Frappe's permission editor automatically adds read when creating a
		# select rule; clear it again so this is the real select-only state.
		frappe.permissions.update_permission_property("Marketplace Product Mapping", role_name, 0, "read", 0)
		frappe.clear_cache(user=user.name, doctype="Marketplace Product Mapping")
		try:
			with self.set_user(user.name):
				self.assertFalse(frappe.has_permission("Marketplace Product Mapping", "read"))
				self.assertTrue(frappe.has_permission("Marketplace Product Mapping", "select"))
				# No get_list-leak assertion any more: with strict #322 filtering a user without a
				# platform role gets no marketplace rows even with select access, and a platform
				# role grants full rights, so select-only access to a row cannot be shown here.
				self.assertEqual(
					frappe.get_list("Marketplace Product Mapping", filters={"name": mapping.name}), []
				)
				with self.assertRaises(frappe.PermissionError):
					mapping_import.export_mappings_xlsx()
				with self.assertRaises(frappe.PermissionError):
					mapping_import.preview_mappings_import(file_url)
				with self.assertRaises(frappe.PermissionError):
					mapping_import.apply_mappings_import(file_url)
		finally:
			frappe.delete_doc("Custom DocPerm", permission_name, force=True)
			if not had_custom_permissions:
				frappe.db.delete("Custom DocPerm", {"parent": "Marketplace Product Mapping"})
			frappe.delete_doc("User", user.name, force=True)
			frappe.delete_doc("Role", role_name, force=True)
			frappe.clear_cache(user=user.name, doctype="Marketplace Product Mapping")
