# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from io import BytesIO
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.xlsxutils import make_xlsx
from openpyxl import load_workbook

from marketplace_integrations.api import item_import

# This test module lives outside any doctype/ folder, so
# UnitTestCase.setUpClass() can't derive a doctype from its path
# (frappe.tests.classes.unit_test_case._get_doctype_from_module() looks for a
# "doctype" path segment and finds none) - cls.doctype stays None and
# IntegrationTestCase's auto-test-record generation never runs for this
# module at all. No IGNORE_TEST_RECORD_DEPENDENCIES dance is needed here.

ITEM_GROUP = "Products"
UOM = "Nos"


class IntegrationTestItemImport(IntegrationTestCase):
	def setUp(self):
		self._counter = 0

	def unique_code(self, prefix):
		self._counter += 1
		return f"{prefix}-{frappe.generate_hash(length=6)}"

	def make_item(self, item_code=None, barcode=None, **kwargs):
		item_code = item_code or self.unique_code("TEST-II")
		doc = {
			"doctype": "Item",
			"item_code": item_code,
			"item_group": ITEM_GROUP,
			"stock_uom": UOM,
		}
		doc.update(kwargs)
		if barcode:
			doc["barcodes"] = [{"barcode": barcode}]
		item = frappe.get_doc(doc).insert()
		item.reload()
		return item

	def make_user(self, roles=("All",)):
		"""A plain user with only the given roles - every real Frappe user
		also ends up with the standard "Desk User"/"Guest" baseline roles
		(frappe.get_roles()), which is enough to grant an implicit "select"
		(Link-lookup) permission on common doctypes like Item even with zero
		explicit read/write/create - see docs/frappe-conventions.md. That
		doesn't affect the create/write authorization this importer actually
		enforces, which is what these tests exercise."""
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"item-import-{frappe.generate_hash(length=10)}@example.com",
				"first_name": "Item Import Test User",
				"send_welcome_email": 0,
				"roles": [{"role": role} for role in roles],
			}
		).insert()
		return user

	def export_bytes(self):
		# frappe.response is a LocalProxy (frappe.utils.local.LocalProxy) onto
		# frappe.local.response - mutate it in place via attribute/item
		# access, never reassign `frappe.response` itself, or the proxy
		# binding is replaced with a plain dict for the rest of the process.
		item_import.export_items_xlsx()
		content = frappe.response["filecontent"]
		frappe.response.filecontent = None
		frappe.response.type = None
		frappe.response.filename = None
		return content

	def make_file(self, content, filename="items.xlsx", is_private=1):
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": filename,
				"content": content,
				"is_private": is_private,
			}
		).insert()
		return file_doc.file_url

	def read_sheet(self, content):
		wb = load_workbook(filename=BytesIO(content), data_only=True)
		ws = wb.active
		return [[cell.value for cell in row] for row in ws.iter_rows()]

	def build_xlsx(self, rows):
		data = [list(item_import.HEADER_ROW), *rows]
		return make_xlsx(data, item_import.XLSX_SHEET_NAME).getvalue()

	def row_for(self, sheet_rows, item_code):
		header = sheet_rows[0]
		idx = header.index("item_code")
		for row in sheet_rows[1:]:
			if row[idx] == item_code:
				return dict(zip(header, row, strict=True))
		return None

	def blank_row(self, item_code, **overrides):
		"""Build a HEADER_ROW-shaped row with every managed column blank
		except item_code, then apply overrides by column name."""
		values = dict.fromkeys(item_import.HEADER_ROW, "")
		values["item_code"] = item_code
		values["disabled"] = 0
		values.update(overrides)
		return [values[col] for col in item_import.HEADER_ROW]

	# --- export -------------------------------------------------------

	def test_export_has_one_row_per_item_with_expected_columns(self):
		item_a = self.make_item(barcode="1234567890123")
		item_b = self.make_item(item_name="Item B Name", description="Plain text description")

		sheet_rows = self.read_sheet(self.export_bytes())

		self.assertEqual(sheet_rows[0], item_import.HEADER_ROW)

		row_a = self.row_for(sheet_rows, item_a.item_code)
		self.assertIsNotNone(row_a)
		self.assertEqual(row_a["item_code"], item_a.item_code)
		self.assertEqual(row_a["item_name"], item_a.item_name)
		self.assertEqual(row_a["item_group"], ITEM_GROUP)
		self.assertEqual(row_a["stock_uom"], UOM)
		self.assertEqual(row_a["disabled"], 0)
		self.assertEqual(row_a["barcode"], "1234567890123")
		self.assertEqual(row_a["__modified"], str(item_a.modified))

		row_b = self.row_for(sheet_rows, item_b.item_code)
		self.assertIsNotNone(row_b)
		self.assertEqual(row_b["item_name"], "Item B Name")
		self.assertEqual(row_b["description"], "Plain text description")
		# xlsxwriter writes an empty string as a blank cell, which openpyxl
		# reads back as None rather than "" - both mean "no barcode" here.
		self.assertFalse(row_b["barcode"])

	# --- preview / round trip -------------------------------------------

	def test_reuploading_unedited_export_reports_all_unchanged_and_writes_nothing(self):
		item = self.make_item(barcode="9990001112223")
		before_modified = frappe.db.get_value("Item", item.name, "modified")

		file_url = self.make_file(self.export_bytes())
		result = item_import.preview_items_import(file_url)

		row = next(r for r in result["rows"] if r["item_code"] == item.item_code)
		self.assertEqual(row["status"], "unchanged")
		self.assertEqual(result["counts"]["error"], 0)

		after_modified = frappe.db.get_value("Item", item.name, "modified")
		self.assertEqual(str(before_modified), str(after_modified))

	def test_html_description_round_trips_unchanged(self):
		"""Regression test for the "Data Export" sheet-name html2text
		exemption (docs/frappe-conventions.md, XLSX export/import) actually
		working end to end with real HTML content, not just in isolation."""
		item = self.make_item(description="<p>Bold <b>text</b></p>")

		file_url = self.make_file(self.export_bytes())
		result = item_import.preview_items_import(file_url)

		row = next(r for r in result["rows"] if r["item_code"] == item.item_code)
		self.assertEqual(row["status"], "unchanged")

	def test_item_with_incidental_whitespace_round_trips_unchanged(self):
		"""An Item whose stored value merely has incidental leading/trailing
		whitespace (never edited through this importer) must not look
		"changed" just because the uploaded cell gets stripped and the DB
		value doesn't - both sides of the comparison must be normalized the
		same way."""
		item = self.make_item(item_name="Whitespace Item")
		# Bypass controller validation to store a value with real whitespace,
		# standing in for pre-existing data that predates this importer. No
		# frappe.db.commit() needed (or wanted - it would defeat per-test
		# transaction rollback): a read in the same transaction/connection
		# already sees this uncommitted UPDATE.
		frappe.db.set_value("Item", item.name, "item_name", "  Whitespace Item  ")

		file_url = self.make_file(self.export_bytes())
		result = item_import.preview_items_import(file_url)

		row = next(r for r in result["rows"] if r["item_code"] == item.item_code)
		self.assertEqual(row["status"], "unchanged")

	def test_editing_field_previews_exact_diff_and_apply_updates_only_that_item(self):
		item_a = self.make_item(item_name="Original Name A")
		item_b = self.make_item(item_name="Original Name B")
		item_b_modified_before = frappe.db.get_value("Item", item_b.name, "modified")

		wb = load_workbook(filename=BytesIO(self.export_bytes()))
		ws = wb.active
		header = [cell.value for cell in ws[1]]
		item_code_col = header.index("item_code") + 1
		item_name_col = header.index("item_name") + 1
		for row in ws.iter_rows(min_row=2):
			if row[item_code_col - 1].value == item_a.item_code:
				row[item_name_col - 1].value = "Edited Name A"
				break

		buf = BytesIO()
		wb.save(buf)
		file_url = self.make_file(buf.getvalue())

		preview = item_import.preview_items_import(file_url)
		row_a = next(r for r in preview["rows"] if r["item_code"] == item_a.item_code)
		self.assertEqual(row_a["status"], "changed")
		self.assertEqual(row_a["diff"]["item_name"], {"old": "Original Name A", "new": "Edited Name A"})

		row_b = next(r for r in preview["rows"] if r["item_code"] == item_b.item_code)
		self.assertEqual(row_b["status"], "unchanged")

		apply_result = item_import.apply_items_import(file_url)
		applied_row_a = next(r for r in apply_result["rows"] if r["item_code"] == item_a.item_code)
		self.assertEqual(applied_row_a["status"], "changed")
		self.assertTrue(applied_row_a["applied"])

		item_a.reload()
		self.assertEqual(item_a.item_name, "Edited Name A")

		item_b_modified_after = frappe.db.get_value("Item", item_b.name, "modified")
		self.assertEqual(str(item_b_modified_before), str(item_b_modified_after))

	def test_preview_never_writes_to_item_or_item_barcode(self):
		item = self.make_item(item_name="Preview Only", barcode="1112223334445")
		before_modified = frappe.db.get_value("Item", item.name, "modified")
		before_barcode = frappe.db.get_value(
			"Item Barcode", {"parent": item.name, "parenttype": "Item"}, "barcode"
		)

		row = self.blank_row(
			item.item_code,
			item_name="Should Not Persist",
			item_group=ITEM_GROUP,
			stock_uom=UOM,
			barcode="0000000000000",
			__modified=str(item.modified),
		)
		file_url = self.make_file(self.build_xlsx([row]))

		preview = item_import.preview_items_import(file_url)
		self.assertEqual(preview["rows"][0]["status"], "changed")

		after_modified = frappe.db.get_value("Item", item.name, "modified")
		after_barcode = frappe.db.get_value(
			"Item Barcode", {"parent": item.name, "parenttype": "Item"}, "barcode"
		)
		self.assertEqual(str(before_modified), str(after_modified))
		self.assertEqual(before_barcode, after_barcode)

	# --- create -----------------------------------------------------------

	def test_new_item_code_creates_item_when_mandatory_and_links_valid(self):
		new_code = self.unique_code("TEST-II-NEW")
		row = self.blank_row(
			new_code,
			item_name="Brand New Item",
			item_group=ITEM_GROUP,
			stock_uom=UOM,
			barcode="5556667778889",
		)
		file_url = self.make_file(self.build_xlsx([row]))

		preview = item_import.preview_items_import(file_url)
		self.assertEqual(preview["rows"][0]["status"], "new")

		result = item_import.apply_items_import(file_url)
		self.assertEqual(result["applied_count"], 1)
		self.assertTrue(frappe.db.exists("Item", new_code))

		created = frappe.get_doc("Item", new_code)
		self.assertEqual(created.item_name, "Brand New Item")
		self.assertEqual(created.item_group, ITEM_GROUP)
		self.assertEqual(created.stock_uom, UOM)
		self.assertEqual(len(created.barcodes), 1)
		self.assertEqual(created.barcodes[0].barcode, "5556667778889")

	def test_unknown_item_group_brand_uom_are_row_errors_not_autocreated(self):
		new_code = self.unique_code("TEST-II-BADLINK")
		bogus_group = self.unique_code("Bogus-Group")
		bogus_brand = self.unique_code("Bogus-Brand")
		bogus_uom = self.unique_code("Bogus-UOM")

		row = self.blank_row(
			new_code,
			item_name="Should Not Be Created",
			item_group=bogus_group,
			brand=bogus_brand,
			stock_uom=bogus_uom,
		)
		file_url = self.make_file(self.build_xlsx([row]))

		result = item_import.apply_items_import(file_url)
		row_result = result["rows"][0]
		self.assertEqual(row_result["status"], "error")
		self.assertEqual(result["applied_count"], 0)

		self.assertFalse(frappe.db.exists("Item", new_code))
		self.assertFalse(frappe.db.exists("Item Group", bogus_group))
		self.assertFalse(frappe.db.exists("Brand", bogus_brand))
		self.assertFalse(frappe.db.exists("UOM", bogus_uom))

	# --- stale __modified ---------------------------------------------------

	def test_stale_modified_row_is_rejected_and_does_not_overwrite(self):
		item = self.make_item(item_name="Stale Test Item")
		stale_modified = str(item.modified)

		# Simulate a newer edit made in ERPNext after the file was exported.
		item.item_name = "Changed In ERPNext After Export"
		item.save()
		item.reload()
		self.assertNotEqual(str(item.modified), stale_modified)

		row = self.blank_row(
			item.item_code,
			item_name="Changed In ERPNext After Export",
			item_group=ITEM_GROUP,
			stock_uom=UOM,
			disabled=1,
			__modified=stale_modified,
		)
		file_url = self.make_file(self.build_xlsx([row]))

		result = item_import.apply_items_import(file_url)
		row_result = result["rows"][0]
		self.assertEqual(row_result["status"], "error")
		self.assertEqual(result["applied_count"], 0)

		item.reload()
		self.assertEqual(item.disabled, 0)

	# --- barcode primary-row management --------------------------------

	def test_clearing_barcode_column_removes_the_managed_row(self):
		item = self.make_item(item_name="Has Barcode", barcode="7778889990001")
		item.reload()
		self.assertEqual(len(item.barcodes), 1)

		row = self.blank_row(
			item.item_code,
			item_name=item.item_name,
			item_group=ITEM_GROUP,
			stock_uom=UOM,
			barcode="",
			__modified=str(item.modified),
		)
		file_url = self.make_file(self.build_xlsx([row]))

		result = item_import.apply_items_import(file_url)
		self.assertEqual(result["applied_count"], 1)

		item.reload()
		self.assertEqual(len(item.barcodes), 0)

	# --- structural / row-shape edge cases ------------------------------

	def test_blank_row_skipped_and_missing_item_code_is_a_row_error(self):
		new_code = self.unique_code("TEST-II-VALID")
		valid_row = self.blank_row(new_code, item_name="Valid Row", item_group=ITEM_GROUP, stock_uom=UOM)
		blank_row = ["" for _ in item_import.HEADER_ROW]
		bad_row = self.blank_row("", item_name="No Item Code Here")

		file_url = self.make_file(self.build_xlsx([valid_row, blank_row, bad_row]))
		result = item_import.preview_items_import(file_url)

		# The fully blank row is skipped entirely, not reported as any status.
		self.assertEqual(len(result["rows"]), 2)
		statuses = {r["item_code"]: r["status"] for r in result["rows"]}
		self.assertEqual(statuses[new_code], "new")
		self.assertEqual(statuses[None], "error")

	def test_missing_expected_column_raises(self):
		bad_header = [c for c in item_import.HEADER_ROW if c != "__modified"]
		data = [bad_header, ["x"] * len(bad_header)]
		content = make_xlsx(data, item_import.XLSX_SHEET_NAME).getvalue()
		file_url = self.make_file(content)

		with self.assertRaises(frappe.ValidationError):
			item_import.preview_items_import(file_url)

	def test_duplicate_item_code_in_workbook_is_reported_as_row_error_without_writes(self):
		new_code = self.unique_code("TEST-II-DUP")
		row_1 = self.blank_row(new_code, item_name="First Dup Row", item_group=ITEM_GROUP, stock_uom=UOM)
		row_2 = self.blank_row(new_code, item_name="Second Dup Row", item_group=ITEM_GROUP, stock_uom=UOM)
		file_url = self.make_file(self.build_xlsx([row_1, row_2]))

		preview = item_import.preview_items_import(file_url)
		for row in preview["rows"]:
			self.assertEqual(row["status"], "error")
			self.assertIn("Duplicate item_code", row["errors"][0])

		result = item_import.apply_items_import(file_url)
		self.assertEqual(result["applied_count"], 0)
		self.assertFalse(frappe.db.exists("Item", new_code))

	# --- disabled cell parsing (finding 11) ------------------------------

	def test_disabled_blank_cell_means_disabled(self):
		new_code = self.unique_code("TEST-II-DISABLED-BLANK")
		row = self.blank_row(
			new_code, item_name="Blank Disabled", item_group=ITEM_GROUP, stock_uom=UOM, disabled=""
		)
		file_url = self.make_file(self.build_xlsx([row]))

		result = item_import.apply_items_import(file_url)
		self.assertEqual(result["applied_count"], 1)
		created = frappe.get_doc("Item", new_code)
		self.assertEqual(created.disabled, 1)

	def test_disabled_recognizes_truthy_and_falsy_strings(self):
		cases = (("1", 1), ("true", 1), ("YES", 1), ("0", 0), ("false", 0), ("No", 0))
		for value, expected in cases:
			with self.subTest(value=value):
				new_code = self.unique_code("TEST-II-DISABLED")
				row = self.blank_row(
					new_code,
					item_name=f"Disabled {value}",
					item_group=ITEM_GROUP,
					stock_uom=UOM,
					disabled=value,
				)
				file_url = self.make_file(self.build_xlsx([row]))

				result = item_import.apply_items_import(file_url)
				self.assertEqual(result["applied_count"], 1)
				created = frappe.get_doc("Item", new_code)
				self.assertEqual(created.disabled, expected)

	def test_disabled_garbage_value_is_a_row_error_not_silently_coerced(self):
		new_code = self.unique_code("TEST-II-DISABLED-GARBAGE")
		row = self.blank_row(
			new_code, item_name="Garbage Disabled", item_group=ITEM_GROUP, stock_uom=UOM, disabled="maybe"
		)
		file_url = self.make_file(self.build_xlsx([row]))

		result = item_import.apply_items_import(file_url)
		self.assertEqual(result["rows"][0]["status"], "error")
		self.assertIn("not recognized", result["rows"][0]["errors"][0])
		self.assertFalse(frappe.db.exists("Item", new_code))

	# --- Stock Settings > Item Naming By guard (finding 2) ----------------

	def test_preview_and_apply_reject_naming_series_item_naming_by(self):
		with self.change_settings("Stock Settings", item_naming_by="Naming Series"):
			new_code = self.unique_code("TEST-II-NAMESERIES")
			row = self.blank_row(
				new_code, item_name="Should Not Be Evaluated", item_group=ITEM_GROUP, stock_uom=UOM
			)
			file_url = self.make_file(self.build_xlsx([row]))

			with self.assertRaises(frappe.ValidationError):
				item_import.preview_items_import(file_url)
			with self.assertRaises(frappe.ValidationError):
				item_import.apply_items_import(file_url)

	# --- file_url type validation and File read permission (finding 3) ----

	def test_file_url_must_be_a_string(self):
		with self.assertRaises(TypeError):
			item_import.preview_items_import(file_url=["not", "a", "string"])
		with self.assertRaises(TypeError):
			item_import.apply_items_import(file_url={"file_url": "x"})

	def test_preview_and_apply_require_read_permission_on_the_uploaded_file(self):
		self.make_item()
		# Administrator-owned, private, not shared, not attached to
		# anything - frappe.core.doctype.file.file.has_permission() denies
		# read to anyone else in that situation.
		file_url = self.make_file(self.export_bytes(), is_private=1)
		unauthorized_reader = self.make_user()

		with self.set_user(unauthorized_reader.name):
			with self.assertRaises(frappe.PermissionError):
				item_import.preview_items_import(file_url)
			with self.assertRaises(frappe.PermissionError):
				item_import.apply_items_import(file_url)

	# --- batch lookups and progress ---------------------------------------

	def test_link_targets_are_looked_up_once_per_link_doctype(self):
		parsed = [
			{"values": {"item_group": ITEM_GROUP, "brand": "Brand A", "stock_uom": UOM}},
			{"values": {"item_group": ITEM_GROUP, "brand": "Brand B", "stock_uom": UOM}},
		]

		with mock.patch.object(item_import.frappe, "get_all", return_value=[]) as get_all:
			item_import._fetch_known_links(parsed)

		self.assertEqual(get_all.call_count, len(item_import.LINK_FIELDS))
		self.assertEqual(
			{call.args[0] for call in get_all.call_args_list}, set(item_import.LINK_FIELDS.values())
		)

	def test_barcode_lookup_splits_large_item_sets_into_bounded_queries(self):
		item_codes = [
			f"TEST-II-BARCODE-{index}" for index in range(item_import.ITEM_CODES_PER_BARCODE_QUERY + 1)
		]

		with mock.patch.object(item_import.frappe, "get_all", return_value=[]) as get_all:
			item_import._fetch_first_barcodes(item_codes)

		self.assertEqual(get_all.call_count, 2)
		self.assertEqual(len(get_all.call_args_list[0].kwargs["filters"]["parent"][1]), 500)
		self.assertEqual(len(get_all.call_args_list[1].kwargs["filters"]["parent"][1]), 1)

	def test_apply_progress_counts_every_processed_row_including_errors(self):
		valid_code = self.unique_code("TEST-II-PROGRESS")
		invalid_code = self.unique_code("TEST-II-PROGRESS-INVALID")
		rows = [
			self.blank_row(valid_code, item_name="Valid", item_group=ITEM_GROUP, stock_uom=UOM),
			self.blank_row(invalid_code, item_name="Invalid", item_group="Missing Group", stock_uom=UOM),
		]
		progress = []

		result = item_import._apply_items_import(
			self.make_file(self.build_xlsx(rows)),
			progress_callback=lambda complete, total: progress.append((complete, total)),
		)

		self.assertEqual(result["applied_count"], 1)
		self.assertEqual(progress, [(1, 2), (2, 2)])

	def test_queued_apply_checks_file_permission_without_parsing_in_the_web_request(self):
		with (
			mock.patch.object(item_import, "_get_uploaded_file") as get_uploaded_file,
			mock.patch.object(item_import, "_read_uploaded_workbook") as read_uploaded_workbook,
			mock.patch.object(
				item_import.item_import_jobs, "queue_apply", return_value={"job_id": "job-token"}
			),
		):
			result = item_import.queue_items_import_apply("/private/files/items.xlsx")

		self.assertEqual(result, {"job_id": "job-token"})
		get_uploaded_file.assert_called_once_with("/private/files/items.xlsx")
		read_uploaded_workbook.assert_not_called()

	# --- POST-only whitelisting (finding 4) --------------------------------

	def test_preview_and_apply_are_post_only(self):
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[item_import.preview_items_import], ["POST"]
		)
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[item_import.apply_items_import], ["POST"]
		)
		# export_items_xlsx is read-only - deliberately left at the default
		# (every method), unlike the two writes above.
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[item_import.export_items_xlsx],
			["GET", "POST", "PUT", "DELETE"],
		)
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[item_import.queue_items_xlsx_export], ["POST"]
		)
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[item_import.queue_items_import_apply], ["POST"]
		)

	# --- authorization via real Item permissions, not Administrator (finding 5) --

	def test_read_only_item_role_can_call_preview_and_apply_but_apply_writes_nothing(self):
		item = self.make_item(item_name="Original Name")
		stock_user = self.make_user(roles=("All", "Stock User"))  # Item read=1, write=0, create=0

		row = self.blank_row(
			item.item_code,
			item_name="Edited By Stock User",
			item_group=ITEM_GROUP,
			stock_uom=UOM,
			__modified=str(item.modified),
		)
		file_url = self.make_file(self.build_xlsx([row]), is_private=0)

		with self.set_user(stock_user.name):
			preview = item_import.preview_items_import(file_url)
			apply_result = item_import.apply_items_import(file_url)

		# Both calls complete (no PermissionError bubbles out of the
		# endpoint) - the write itself is what gets rejected, via the same
		# frappe.has_permission("Item", "write", ...) check preview and
		# apply share, actually exercised here through a real non-System-
		# Manager role rather than being unreachable dead code.
		preview_row = preview["rows"][0]
		self.assertEqual(preview_row["status"], "error")
		self.assertIn("Not permitted to update", preview_row["errors"][0])

		apply_row = apply_result["rows"][0]
		self.assertEqual(apply_row["status"], "error")
		self.assertEqual(apply_result["applied_count"], 0)

		item.reload()
		self.assertEqual(item.item_name, "Original Name")

	def test_user_without_item_role_is_rejected_for_both_create_and_write(self):
		existing_item = self.make_item(item_name="Original Name")
		new_code = self.unique_code("TEST-II-NOPERM-NEW")
		no_item_role_user = self.make_user()  # only the default "All" role

		rows = [
			self.blank_row(
				existing_item.item_code,
				item_name="Attempted Edit",
				item_group=ITEM_GROUP,
				stock_uom=UOM,
				__modified=str(existing_item.modified),
			),
			self.blank_row(new_code, item_name="Attempted Create", item_group=ITEM_GROUP, stock_uom=UOM),
		]
		file_url = self.make_file(self.build_xlsx(rows), is_private=0)

		with self.set_user(no_item_role_user.name):
			preview = item_import.preview_items_import(file_url)
			apply_result = item_import.apply_items_import(file_url)

		for result in (preview, apply_result):
			statuses = {r["item_code"]: r["status"] for r in result["rows"]}
			self.assertEqual(statuses[existing_item.item_code], "error")
			self.assertEqual(statuses[new_code], "error")

		self.assertEqual(apply_result["applied_count"], 0)
		self.assertFalse(frappe.db.exists("Item", new_code))
		existing_item.reload()
		self.assertEqual(existing_item.item_name, "Original Name")

	# --- per-row savepoint rollback on write failure (finding 1) ----------

	def test_apply_rolls_back_partial_write_on_row_failure_without_discarding_prior_rows(self):
		good_code = self.unique_code("TEST-II-GOOD")
		bad_code = self.unique_code("TEST-II-BAD")
		good_row = self.blank_row(good_code, item_name="Good Item", item_group=ITEM_GROUP, stock_uom=UOM)
		bad_row = self.blank_row(bad_code, item_name="Bad Item", item_group=ITEM_GROUP, stock_uom=UOM)
		file_url = self.make_file(self.build_xlsx([good_row, bad_row]))

		real_write_row = item_import._write_row

		def write_row_simulating_partial_failure(result):
			if result["item_code"] != bad_code:
				return real_write_row(result)

			# Simulate a row whose write already reached the DB (e.g. the
			# Item row itself, or a child table row inserted before an
			# after_insert/on_update hook like Item.add_price() raises)
			# before ultimately failing. db_insert() bypasses controller
			# validation entirely, standing in for whatever partial write
			# Frappe's own document lifecycle would otherwise have already
			# performed by the time such a hook runs.
			partial = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": bad_code,
					"name": bad_code,
					"item_group": ITEM_GROUP,
					"stock_uom": UOM,
					"item_name": "Bad Item",
				}
			)
			partial.db_insert()
			raise frappe.ValidationError("Simulated failure after partial write")

		with mock.patch.object(item_import, "_write_row", side_effect=write_row_simulating_partial_failure):
			result = item_import.apply_items_import(file_url)

		good = next(r for r in result["rows"] if r["item_code"] == good_code)
		bad = next(r for r in result["rows"] if r["item_code"] == bad_code)
		self.assertEqual(good["status"], "new")
		self.assertTrue(good.get("applied"))
		self.assertEqual(bad["status"], "error")
		self.assertEqual(result["applied_count"], 1)

		self.assertTrue(frappe.db.exists("Item", good_code))
		# The simulated partial write must have been rolled back to the
		# per-row savepoint, not left committed just because the row is
		# reported as an error and excluded from applied_count.
		self.assertFalse(frappe.db.exists("Item", bad_code))
