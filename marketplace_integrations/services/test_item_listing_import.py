# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Marketplace listings on the native Item Data Import and Export (#373).

Data Import commits once per record, so the tests that run a real import delete everything
they create in tearDown. Run them on test.localhost, never on the dev site.
"""

import csv
import io
from unittest.mock import patch

import frappe
from frappe.core.doctype.data_import.exporter import Exporter
from frappe.core.doctype.data_import.importer import INSERT, UPDATE, UPSERT
from frappe.tests import IntegrationTestCase

from marketplace_integrations.services import item_listing_import as listing_import
from marketplace_integrations.services.item_listing_import import (
	LISTING_FIELDS,
	LISTINGS_TABLE,
	MAPPING,
	ItemImportProvider,
)
from marketplace_integrations.tests.utils import make_role_user

PROVIDER_PATH = "marketplace_integrations.services.item_listing_import.ItemImportProvider"
UOM = "Nos"
ITEM_GROUP = "Products"
INSERT_TYPE = INSERT
UPDATE_TYPE = UPDATE
UPSERT_TYPE = UPSERT

ACCOUNT_COLUMN = f"{LISTINGS_TABLE}.marketplace_account"
REFERENCE_COLUMN = f"{LISTINGS_TABLE}.external_reference"
SKU_COLUMN = f"{LISTINGS_TABLE}.external_sku"
ENABLED_COLUMN = f"{LISTINGS_TABLE}.enabled"
PRODUCT_ID_COLUMN = f"{LISTINGS_TABLE}.external_product_id"
VARIANT_ID_COLUMN = f"{LISTINGS_TABLE}.external_variant_id"


class TestItemListingImportRegistration(IntegrationTestCase):
	"""Always-on guards: no fixtures."""

	def test_item_provider_is_the_last_registered(self):
		# Frappe uses only the last provider registered for a doctype.
		registered = frappe.get_hooks("data_import_providers")["Item"]
		self.assertEqual(registered[-1], PROVIDER_PATH)

	def test_erpnext_registers_no_item_provider(self):
		# If ERPNext ever registers one, ours would silently replace or be replaced by it.
		erpnext_hooks = frappe.get_hooks("data_import_providers", app_name="erpnext")
		self.assertNotIn("Item", erpnext_hooks)
		self.assertEqual(list(frappe.get_hooks("data_import_providers")["Item"]), [PROVIDER_PATH])

	def test_item_doctype_stores_no_marketplace_identifier(self):
		meta = frappe.get_meta("Item")
		self.assertIsNone(meta.get_field(LISTINGS_TABLE))
		for fieldname in LISTING_FIELDS:
			if fieldname in ("sales_uom", "enabled"):
				continue
			self.assertIsNone(meta.get_field(fieldname), fieldname)

	def test_platform_roles_have_no_bulk_mapping_import(self):
		meta = frappe.get_meta(MAPPING)
		self.assertFalse(meta.allow_import)
		self.assertFalse(any(perm.get("import") for perm in meta.permissions))


class IntegrationTestItemListingImport(IntegrationTestCase):
	def setUp(self):
		self._tracked = []
		self._items = []
		self._accounts = []
		self._data_imports = []
		self._files = []

	def tearDown(self):
		# Data Import and make_role_user commit, so every fixture is deleted here.
		frappe.set_user("Administrator")
		for name in self._data_imports:
			if frappe.db.exists("Data Import", name):
				frappe.db.delete("Data Import Log", {"data_import": name})
				frappe.delete_doc("Data Import", name, force=True, ignore_permissions=True)
		if self._items:
			frappe.db.delete("Marketplace Product Mapping", {"item": ["in", self._items]})
		if self._accounts:
			frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": ["in", self._accounts]})
		for doctype, name in reversed(self._tracked):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		for name in self._files:
			if frappe.db.exists("File", name):
				frappe.delete_doc("File", name, force=True, ignore_permissions=True)
		frappe.db.commit()  # nosemgrep

	# --- fixtures -------------------------------------------------------------------------

	def unique(self, prefix):
		return f"{prefix}-{frappe.generate_hash(length=8)}"

	def make_item(self, item_code=None):
		item_code = item_code or self.unique("TEST-ILI-ITEM")
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": item_code,
				"item_group": ITEM_GROUP,
				"stock_uom": UOM,
				"is_stock_item": 0,
			}
		).insert()
		self._items.append(item.name)
		self._tracked.append(("Item", item.name))
		return item

	def make_account(self, platform="Talabat"):
		account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": self.unique("Listing Import Account"),
				"platform": "Talabat",
				"external_account_id": self.unique("listing-import-account"),
				"connection_doctype": "UOM",
				"connection": UOM,
			}
		).insert()
		if platform != "Talabat":
			frappe.db.set_value("Marketplace Account", account.name, "platform", platform, update_modified=False)
		self._accounts.append(account.name)
		self._tracked.append(("Marketplace Account", account.name))
		return account

	def make_mapping(self, account, item, reference, **values):
		mapping = frappe.get_doc(
			{
				"doctype": MAPPING,
				"marketplace_account": account.name,
				"item": item.name,
				"external_reference": reference,
				**values,
			}
		).insert()
		self._tracked.append((MAPPING, mapping.name))
		return mapping

	def mapping_of(self, account, reference):
		return frappe.db.get_value(
			MAPPING,
			{
				"mapping_identity_key": listing_import.compute_mapping_identity_key(account.name, reference),
			},
			["name", "item", "external_sku", "enabled"],
			as_dict=True,
		)

	def make_user(self, label, *roles):
		email, _role = make_role_user(self, label, {})
		frappe.get_doc("User", email).add_roles(*roles)
		frappe.clear_cache(user=email)
		return email

	def run_import(self, header, rows, import_type):
		buffer = io.StringIO()
		writer = csv.writer(buffer)
		writer.writerow(header)
		writer.writerows(rows)
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"{self.unique('item-listings')}.csv",
				"content": buffer.getvalue().encode(),
				"is_private": 1,
			}
		).insert()
		self._files.append(file_doc.name)

		data_import = frappe.new_doc("Data Import")
		data_import.import_type = import_type
		data_import.reference_doctype = "Item"
		data_import.import_file = file_doc.file_url
		data_import.insert()
		# Commit so a failed row's rollback does not take the Data Import with it.
		frappe.db.commit()  # nosemgrep
		self._data_imports.append(data_import.name)
		data_import.start_import()
		return data_import.reload()

	def failure_text(self, data_import):
		logs = frappe.get_all(
			"Data Import Log",
			filters={"data_import": data_import.name, "success": 0},
			fields=["messages", "exception"],
		)
		return " ".join(f"{log.messages} {log.exception}" for log in logs)

	def item_header(self, with_listings=True):
		header = ["item_code", "item_name", "item_group", "stock_uom"]
		if with_listings:
			header += [ACCOUNT_COLUMN, REFERENCE_COLUMN, SKU_COLUMN, ENABLED_COLUMN]
		return header

	def update_header(self):
		return ["name", "item_name", ACCOUNT_COLUMN, REFERENCE_COLUMN, SKU_COLUMN, ENABLED_COLUMN]

	# --- schema and export ----------------------------------------------------------------

	def test_schema_adds_the_listing_table_next_to_the_item_fields(self):
		provider = ItemImportProvider()
		provider.doctype = "Item"
		schema = provider.get_import_fields()
		meta = frappe.get_meta("Item")

		tables = {table["fieldname"]: table for table in schema["child_tables"]}
		self.assertEqual(
			[field.fieldname for field in meta.get_table_fields()],
			[name for name in tables if name != LISTINGS_TABLE],
		)
		listing = tables[LISTINGS_TABLE]
		self.assertEqual([df["fieldname"] for df in listing["fields"]], list(LISTING_FIELDS))
		self.assertTrue(all(df["parent"] == MAPPING for df in listing["fields"]))
		self.assertTrue(any(df["fieldname"] == "item_code" for df in schema["fields"]))
		self.assertFalse(any(df["fieldname"] == LISTINGS_TABLE for df in schema["fields"]))

	def test_user_without_mapping_read_gets_no_table_and_no_rows(self):
		item = self.make_item()
		self.make_mapping(self.make_account(), item, self.unique("hidden"))
		email, _role = make_role_user(self, "Listing No Mapping Read", {"Item": ["read"]})
		provider = ItemImportProvider()
		provider.doctype = "Item"

		with self.set_user(email):
			schema = provider.get_import_fields()
			rows = provider.get_export_rows([item.name], {LISTINGS_TABLE: list(LISTING_FIELDS)})

		self.assertNotIn(LISTINGS_TABLE, [table["fieldname"] for table in schema["child_tables"]])
		self.assertEqual(rows, {})

	def test_export_has_one_row_per_listing_and_several_per_item(self):
		first, second = self.make_account(), self.make_account()
		item, other = self.make_item(), self.make_item()
		prefix = self.unique("export")
		self.make_mapping(first, item, f"{prefix}-a", external_sku="sku-a")
		self.make_mapping(second, item, f"{prefix}-b")
		self.make_mapping(first, other, f"{prefix}-c")
		provider = ItemImportProvider()
		provider.doctype = "Item"

		rows = provider.get_export_rows(
			[item.name, other.name], {LISTINGS_TABLE: ["marketplace_account", "external_reference", "external_sku"]}
		)

		self.assertEqual(len(rows[item.name][LISTINGS_TABLE]), 2)
		self.assertEqual(len(rows[other.name][LISTINGS_TABLE]), 1)
		by_reference = {row["external_reference"]: row for row in rows[item.name][LISTINGS_TABLE]}
		self.assertEqual(by_reference[f"{prefix}-a"]["external_sku"], "sku-a")
		self.assertEqual(
			set(by_reference[f"{prefix}-a"]), {"marketplace_account", "external_reference", "external_sku"}
		)

	def test_export_returns_only_listings_the_platform_user_may_read(self):
		talabat, noon = self.make_account(), self.make_account("Noon")
		item = self.make_item()
		prefix = self.unique("export-platform")
		self.make_mapping(talabat, item, f"{prefix}-talabat")
		self.make_mapping(noon, item, f"{prefix}-noon")
		email = self.make_user("Listing Export Platform", "Talabat User")
		provider = ItemImportProvider()
		provider.doctype = "Item"

		with self.set_user(email):
			rows = provider.get_export_rows([item.name], {LISTINGS_TABLE: ["external_reference"]})

		references = {row["external_reference"] for row in rows[item.name][LISTINGS_TABLE]}
		self.assertEqual(references, {f"{prefix}-talabat"})

	# --- import through the real Data Import ----------------------------------------------

	def test_item_only_file_imports_in_every_import_type(self):
		# Regression: a file without listing columns behaves as before.
		code = self.unique("TEST-ILI-PLAIN")
		data_import = self.run_import(
			self.item_header(False), [[code, "Plain", ITEM_GROUP, UOM]], INSERT_TYPE
		)
		self.assertEqual(data_import.status, "Success", self.failure_text(data_import))
		self._items.append(code)
		self._tracked.append(("Item", code))
		self.assertEqual(frappe.db.get_value("Item", code, "item_name"), "Plain")

		data_import = self.run_import(["name", "item_name"], [[code, "Plain 2"]], UPDATE_TYPE)
		self.assertEqual(data_import.status, "Success", self.failure_text(data_import))
		self.assertEqual(frappe.db.get_value("Item", code, "item_name"), "Plain 2")

		data_import = self.run_import(["name", "item_name"], [[code, "Plain 3"]], UPSERT_TYPE)
		self.assertEqual(data_import.status, "Success", self.failure_text(data_import))
		self.assertEqual(frappe.db.get_value("Item", code, "item_name"), "Plain 3")

		# Frappe's get_diff is never empty here: update_record copies the document with copy_doc,
		# which clears name and the child row names, so a no-change Update is a Success too
		# (Frappe 16.52). The Item data stays as it was.
		data_import = self.run_import(["name", "item_name"], [[code, "Plain 3"]], UPDATE_TYPE)
		self.assertEqual(data_import.status, "Success", self.failure_text(data_import))
		self.assertEqual(frappe.db.get_value("Item", code, "item_name"), "Plain 3")

		# A file without listing columns never creates a listing, in any import type.
		self.assertEqual(frappe.db.count("Marketplace Product Mapping", {"item": code}), 0)

	def test_new_listing_creates_one_mapping_with_the_doctype_defaults(self):
		account = self.make_account()
		code, reference = self.unique("TEST-ILI-NEW"), self.unique("ref")
		data_import = self.run_import(
			self.item_header(),
			[[code, "New", ITEM_GROUP, UOM, account.name, reference, "sku-1", ""]],
			INSERT_TYPE,
		)
		self._items.append(code)
		self._tracked.append(("Item", code))

		self.assertEqual(data_import.status, "Success", self.failure_text(data_import))
		mapping = self.mapping_of(account, reference)
		self.assertEqual(mapping.item, code)
		self.assertEqual(mapping.external_sku, "sku-1")
		# A blank Enabled on a new listing takes the doctype default (1).
		self.assertEqual(mapping.enabled, 1)

	def test_listing_on_the_same_item_updates_in_place_and_blank_enabled_keeps_it(self):
		account, item = self.make_account(), self.make_item()
		reference = self.unique("ref")
		self.make_mapping(account, item, reference, enabled=0, external_sku="old")
		before = self.mapping_of(account, reference).name

		data_import = self.run_import(
			self.update_header(), [[item.name, "", account.name, reference, "new", ""]], UPDATE_TYPE
		)

		self.assertEqual(data_import.status, "Success", self.failure_text(data_import))
		mapping = self.mapping_of(account, reference)
		self.assertEqual(mapping.name, before)
		self.assertEqual(mapping.external_sku, "new")
		# Data Import drops a blank Check cell, so the stored value is kept.
		self.assertEqual(mapping.enabled, 0)

	def test_update_with_the_id_header_finds_the_item_and_raises_no_false_warning(self):
		# The provider schema maps the ID header to ``name``; the ID must still reach item_code.
		account, item = self.make_account(), self.make_item()
		reference = self.unique("ref")
		self.make_mapping(account, item, reference, external_sku="old")

		data_import = self.run_import(
			["ID", "item_name", ACCOUNT_COLUMN, REFERENCE_COLUMN, SKU_COLUMN],
			[[item.name, "Renamed", account.name, reference, "new"]],
			UPDATE_TYPE,
		)

		self.assertEqual(data_import.status, "Success", self.failure_text(data_import))
		self.assertFalse(data_import.template_warnings)
		self.assertEqual(frappe.db.get_value("Item", item.name, "item_name"), "Renamed")
		self.assertEqual(self.mapping_of(account, reference).external_sku, "new")

	def test_blank_item_code_cell_falls_back_to_the_id_cell_per_row(self):
		# Both columns present, Item Code blank on this row: the ID cell names the Item.
		account, item = self.make_account(), self.make_item()
		reference = self.unique("ref")
		self.make_mapping(account, item, reference, external_sku="old")

		data_import = self.run_import(
			["item_code", "ID", "item_name", ACCOUNT_COLUMN, REFERENCE_COLUMN, SKU_COLUMN],
			[["", item.name, "Renamed", account.name, reference, "new"]],
			UPDATE_TYPE,
		)

		self.assertEqual(data_import.status, "Success", self.failure_text(data_import))
		self.assertFalse(data_import.template_warnings)
		self.assertEqual(self.mapping_of(account, reference).external_sku, "new")

	def test_item_id_differing_only_by_case_and_spaces_raises_no_false_warning(self):
		account, item = self.make_account(), self.make_item()
		reference = self.unique("ref")
		self.make_mapping(account, item, reference, external_sku="old")

		data_import = self.run_import(
			["name", "item_name", ACCOUNT_COLUMN, REFERENCE_COLUMN, SKU_COLUMN],
			[[f"  {item.name.lower()}  ", "Renamed", account.name, reference, "new"]],
			UPDATE_TYPE,
		)

		self.assertEqual(data_import.status, "Success", self.failure_text(data_import))
		self.assertFalse(data_import.template_warnings)
		self.assertEqual(self.mapping_of(account, reference).external_sku, "new")

	def test_update_with_spaces_around_the_id_cell_finds_the_item(self):
		# Frappe does not strip Data cells and MariaDB keeps edge spaces in comparisons,
		# so import_row must hand the stripped ID to update_record.
		account, item = self.make_account(), self.make_item()
		reference = self.unique("ref")
		self.make_mapping(account, item, reference, external_sku="old")

		for header_id in ("name", "item_code"):
			data_import = self.run_import(
				[header_id, "item_name", ACCOUNT_COLUMN, REFERENCE_COLUMN, SKU_COLUMN],
				[[f"  {item.name}  ", "Renamed", account.name, reference, "new"]],
				UPDATE_TYPE,
			)

			self.assertEqual(data_import.status, "Success", self.failure_text(data_import))
			self.assertFalse(data_import.template_warnings)
			self.assertEqual(frappe.db.get_value("Item", item.name, "item_name"), "Renamed")
			self.assertEqual(self.mapping_of(account, reference).external_sku, "new")
			frappe.db.set_value("Item", item.name, "item_name", item.name)
			frappe.db.set_value(MAPPING, self.mapping_of(account, reference).name, "external_sku", "old")

	def test_id_item_code_headers_map_to_item_code_but_not_in_the_field_picker(self):
		from frappe.core.doctype.data_import.importer import build_fields_dict_for_column_matching

		mapping = build_fields_dict_for_column_matching("Item")
		label = frappe.get_meta("Item").get_field("item_code").label
		for header in ("item_code", f"ID ({label})", "{} ({})".format(frappe._("ID"), frappe._(label))):
			self.assertEqual(mapping[header].fieldname, "item_code", header)
		self.assertEqual(mapping["ID"].fieldname, "name")

		provider = ItemImportProvider()
		provider.doctype = "Item"
		with patch.object(frappe.local, "form_dict", frappe._dict(cmd=listing_import.PICKER_CMD)):
			fields = provider.get_import_fields()["fields"]
		self.assertEqual([df["fieldname"] for df in fields].count("item_code"), 1)

	def test_insert_or_update_updates_an_existing_item_and_inserts_a_new_one(self):
		account, item = self.make_account(), self.make_item()
		old_reference, new_reference = self.unique("ref"), self.unique("ref")
		self.make_mapping(account, item, old_reference, external_sku="old")
		code = self.unique("TEST-ILI-UPSERT")
		header = ["name", "item_name", "item_group", "stock_uom", ACCOUNT_COLUMN, REFERENCE_COLUMN, SKU_COLUMN]

		data_import = self.run_import(
			header,
			[
				[item.name, "Upserted", ITEM_GROUP, UOM, account.name, old_reference, "new"],
				[code, "Fresh", ITEM_GROUP, UOM, account.name, new_reference, "fresh"],
			],
			UPSERT_TYPE,
		)
		self._items.append(code)
		self._tracked.append(("Item", code))

		self.assertEqual(data_import.status, "Success", self.failure_text(data_import))
		self.assertEqual(frappe.db.get_value("Item", item.name, "item_name"), "Upserted")
		self.assertEqual(self.mapping_of(account, old_reference).external_sku, "new")
		self.assertEqual(frappe.db.get_value("Item", code, "item_name"), "Fresh")
		self.assertEqual(self.mapping_of(account, new_reference).item, code)

	def test_import_row_translates_name_to_item_code_unless_item_code_is_set(self):
		class Recorder:
			import_type = UPDATE

			def update_record(self, doc, raise_if_no_changes=True):
				self.doc = dict(doc)
				return frappe.get_doc("Item", doc["item_code"])

		item = self.make_item()
		provider = ItemImportProvider()
		provider.doctype = "Item"

		importer = Recorder()
		provider.import_row(importer, {"name": item.name})
		self.assertEqual(importer.doc, {"item_code": item.name})

		importer = Recorder()
		provider.import_row(importer, {"name": "OTHER", "item_code": item.name})
		self.assertEqual(importer.doc, {"item_code": item.name})

	def test_reference_with_surrounding_spaces_matches_the_existing_mapping(self):
		account, item = self.make_account(), self.make_item()
		reference = self.unique("ref")
		self.make_mapping(account, item, reference, external_sku="old")

		data_import = self.run_import(
			self.update_header(), [[item.name, "", account.name, f"  {reference}  ", "new", ""]], UPDATE_TYPE
		)

		self.assertEqual(data_import.status, "Success", self.failure_text(data_import))
		self.assertEqual(frappe.db.count(MAPPING, {"marketplace_account": account.name}), 1)
		self.assertEqual(self.mapping_of(account, reference).external_sku, "new")

	def test_reimporting_an_unedited_export_changes_no_mapping(self):
		account, item = self.make_account(), self.make_item()
		reference = self.unique("ref")
		mapping = self.make_mapping(account, item, reference, external_sku="sku", enabled=0)
		versions = frappe.db.count("Version", {"ref_doctype": MAPPING, "docname": mapping.name})
		modified = frappe.db.get_value(MAPPING, mapping.name, "modified")

		exporter = Exporter(
			"Item",
			export_fields={
				"Item": ["name", "item_name"],
				LISTINGS_TABLE: ["marketplace_account", "external_reference", "external_sku", "enabled"],
			},
			export_data=True,
			export_filters={"name": item.name},
		)
		csv_array = exporter.get_csv_array()
		self.assertEqual(len(csv_array), 2)
		data_import = self.run_import(csv_array[0], csv_array[1:], UPDATE_TYPE)

		self.assertEqual(data_import.status, "Success", self.failure_text(data_import))
		self.assertEqual(frappe.db.count("Version", {"ref_doctype": MAPPING, "docname": mapping.name}), versions)
		self.assertEqual(frappe.db.get_value(MAPPING, mapping.name, "modified"), modified)

	def test_listing_mapped_to_another_item_blocks_the_import_before_anything_is_written(self):
		account, owner = self.make_account(), self.make_item()
		reference = self.unique("ref")
		self.make_mapping(account, owner, reference)
		code = self.unique("TEST-ILI-OTHER")

		data_import = self.run_import(
			self.item_header(),
			[[code, "Other", ITEM_GROUP, UOM, account.name, reference, "", ""]],
			INSERT_TYPE,
		)

		self.assertEqual(data_import.status, "Pending")
		self.assertIn(reference, data_import.template_warnings)
		self.assertFalse(frappe.db.exists("Item", code))
		self.assertEqual(self.mapping_of(account, reference).item, owner.name)

	def test_pair_twice_in_one_file_blocks_the_import(self):
		account = self.make_account()
		reference = self.unique("ref")
		first, second = self.unique("TEST-ILI-DUP"), self.unique("TEST-ILI-DUP")

		data_import = self.run_import(
			self.item_header(),
			[
				[first, "One", ITEM_GROUP, UOM, account.name, reference, "", ""],
				[second, "Two", ITEM_GROUP, UOM, account.name, f" {reference}", "", ""],
			],
			INSERT_TYPE,
		)

		self.assertEqual(data_import.status, "Pending")
		self.assertIn("appears more than once", data_import.template_warnings)
		self.assertFalse(frappe.db.exists("Item", first))

	def shopify_header(self):
		return [
			"item_code",
			"item_name",
			"item_group",
			"stock_uom",
			ACCOUNT_COLUMN,
			REFERENCE_COLUMN,
			PRODUCT_ID_COLUMN,
			VARIANT_ID_COLUMN,
		]

	def run_shopify_row(self, reference, product_id, variant_id):
		account = self.make_account("Shopify")
		code = self.unique("TEST-ILI-SHOP")
		data_import = self.run_import(
			self.shopify_header(),
			[[code, "Shop", ITEM_GROUP, UOM, account.name, reference, product_id, variant_id]],
			INSERT_TYPE,
		)
		self._items.append(code)
		self._tracked.append(("Item", code))
		return data_import, code

	def test_shopify_reference_that_is_not_a_variant_gid_blocks_the_import(self):
		data_import, code = self.run_shopify_row("12345", "gid://shopify/Product/1", "12345")

		self.assertEqual(data_import.status, "Pending")
		self.assertIn("gid://shopify/ProductVariant/", data_import.template_warnings)
		self.assertFalse(frappe.db.exists("Item", code))

	def test_new_shopify_listing_without_product_id_blocks_the_import(self):
		data_import, code = self.run_shopify_row("gid://shopify/ProductVariant/11", "", "gid://shopify/ProductVariant/11")

		self.assertEqual(data_import.status, "Pending")
		self.assertIn("External Product ID is required", data_import.template_warnings)
		self.assertFalse(frappe.db.exists("Item", code))

	def test_shopify_product_id_that_is_not_a_product_gid_blocks_the_import(self):
		data_import, _code = self.run_shopify_row(
			"gid://shopify/ProductVariant/11", "gid://shopify/ProductVariant/9", "gid://shopify/ProductVariant/11"
		)

		self.assertEqual(data_import.status, "Pending")
		self.assertIn("gid://shopify/Product/", data_import.template_warnings)

	def test_new_shopify_listing_without_variant_id_blocks_the_import(self):
		data_import, code = self.run_shopify_row("gid://shopify/ProductVariant/11", "gid://shopify/Product/1", "")

		self.assertEqual(data_import.status, "Pending")
		self.assertIn("External Variant ID is required", data_import.template_warnings)
		self.assertFalse(frappe.db.exists("Item", code))

	def test_valid_shopify_listing_imports(self):
		data_import, code = self.run_shopify_row(
			"gid://shopify/ProductVariant/11", "gid://shopify/Product/1", "gid://shopify/ProductVariant/11"
		)

		self.assertEqual(data_import.status, "Success", self.failure_text(data_import))
		self.assertTrue(frappe.db.exists("Item", code))

	def test_talabat_and_noon_listings_without_product_or_variant_id_are_never_blocked(self):
		for platform in ("Talabat", "Noon"):
			account = self.make_account(platform)
			code = self.unique("TEST-ILI-PLAT")
			data_import = self.run_import(
				self.shopify_header(),
				[[code, platform, ITEM_GROUP, UOM, account.name, self.unique("plain-ref"), "", ""]],
				INSERT_TYPE,
			)
			self._items.append(code)
			self._tracked.append(("Item", code))

			self.assertEqual(data_import.status, "Success", f"{platform}: {self.failure_text(data_import)}")

	def test_talabat_sku_differing_from_reference_warns_without_blocking(self):
		account = self.make_account("Talabat")
		other = self.make_account("Noon")
		entry = {"account": account.name, "reference": "ref-1", "row": 3, "sku": "sku-1", "key": "k1"}
		same = {**entry, "row": 4, "sku": "ref-1", "key": "k2"}
		blank = {**entry, "row": 5, "sku": None, "key": "k3"}
		noon = {**entry, "row": 6, "account": other.name, "key": "k4"}

		warnings = listing_import._platform_warnings([entry, same, blank, noon], {})

		self.assertEqual([w["row"] for w in warnings], [3])
		self.assertEqual(warnings[0]["type"], "info")
		self.assertIn("sku-1", warnings[0]["message"])

	def test_talabat_sku_mismatch_does_not_stop_the_import(self):
		account = self.make_account("Talabat")
		code = self.unique("TEST-ILI-SKU")

		data_import = self.run_import(
			self.item_header(),
			[[code, "Sku", ITEM_GROUP, UOM, account.name, self.unique("ref"), "different-sku", ""]],
			INSERT_TYPE,
		)

		self.assertEqual(data_import.status, "Success", self.failure_text(data_import))

	def test_row_whose_reference_belongs_to_another_item_fails_and_the_item_change_rolls_back(self):
		# validate() blocks this case first; it is switched off here to reach the row-level guard
		# (a mapping the user cannot read is invisible to validate()).
		account, owner, target = self.make_account(), self.make_item(), self.make_item()
		reference = self.unique("ref")
		self.make_mapping(account, owner, reference)

		with patch.object(ItemImportProvider, "validate", return_value=[]):
			data_import = self.run_import(
				self.update_header(),
				[[target.name, "Changed name", account.name, reference, "", ""]],
				UPDATE_TYPE,
			)

		self.assertEqual(data_import.status, "Error")
		self.assertIn("already mapped", self.failure_text(data_import))
		self.assertEqual(frappe.db.get_value("Item", target.name, "item_name"), target.item_name)
		self.assertEqual(self.mapping_of(account, reference).item, owner.name)

	# --- permissions (import_row called directly: the user needs no Item permission) ------

	def stub_importer(self):
		class StubImporter:
			import_type = UPDATE

			def update_record(self, doc, raise_if_no_changes=True):
				# Like Frappe, read the Item ID from item_code (import_row translates ``name``).
				return frappe.get_doc("Item", doc["item_code"])

		return StubImporter()

	def import_listing_as(self, email, item, row):
		provider = ItemImportProvider()
		provider.doctype = "Item"
		with self.set_user(email):
			return provider.import_row(self.stub_importer(), {"name": item.name, LISTINGS_TABLE: [row]})

	def test_platform_user_cannot_create_another_platforms_listing(self):
		noon, talabat = self.make_account("Noon"), self.make_account()
		item = self.make_item()
		email = self.make_user("Listing Import Platform", "Talabat User")
		reference = self.unique("ref")

		with self.assertRaises(frappe.PermissionError):
			self.import_listing_as(
				email, item, {"marketplace_account": noon.name, "external_reference": reference}
			)
		self.assertIsNone(self.mapping_of(noon, reference))

		# Control: the user's own platform works through the same path.
		own = self.unique("own")
		self.import_listing_as(email, item, {"marketplace_account": talabat.name, "external_reference": own})
		self.assertEqual(self.mapping_of(talabat, own).item, item.name)

	def test_platform_user_cannot_update_another_platforms_listing(self):
		noon, item = self.make_account("Noon"), self.make_item()
		reference = self.unique("ref")
		self.make_mapping(noon, item, reference, external_sku="old")
		email = self.make_user("Listing Update Platform", "Talabat User")

		with self.assertRaises(frappe.PermissionError):
			self.import_listing_as(
				email,
				item,
				{"marketplace_account": noon.name, "external_reference": reference, "external_sku": "new"},
			)
		self.assertEqual(self.mapping_of(noon, reference).external_sku, "old")

	def test_row_error_names_the_other_item_only_to_a_user_who_may_read_its_mapping(self):
		noon, owner, target = self.make_account("Noon"), self.make_item(), self.make_item()
		reference = self.unique("ref")
		self.make_mapping(noon, owner, reference)
		email = self.make_user("Listing Hidden Owner", "Talabat User")

		with self.assertRaises(frappe.ValidationError) as hidden:
			self.import_listing_as(email, target, {"marketplace_account": noon.name, "external_reference": reference})
		self.assertNotIn(owner.name, str(hidden.exception))
		self.assertIn("different Item", str(hidden.exception))

		with self.assertRaises(frappe.ValidationError) as visible:
			provider = ItemImportProvider()
			provider.doctype = "Item"
			row = {"marketplace_account": noon.name, "external_reference": reference}
			provider.import_row(self.stub_importer(), {"name": target.name, LISTINGS_TABLE: [row]})
		self.assertIn(owner.name, str(visible.exception))
