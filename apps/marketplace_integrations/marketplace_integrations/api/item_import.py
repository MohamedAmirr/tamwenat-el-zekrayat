# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""XLSX round-trip import/export for ERPNext Item master data.

One worksheet, one row per Item - see
docs/marketplace-integrations/architecture.md ("Product management imports"
> "Item workbook") for the agreed shape: export -> edit -> upload ->
server-side preview (no writes) -> explicit apply -> re-parse + re-validate +
write.

item_code is the stable business key: an existing item_code updates that
Item, an unknown one creates a new Item. Changing/renaming an Item Code (i.e.
editing item_code on a row that used to reference a different Item) is not
supported - every row is resolved purely by the item_code value it currently
holds, never by "whatever used to be on this row". This premise requires
Stock Settings -> Item Naming By = "Item Code" - see
_require_item_code_naming() below.

barcode manages exactly one primary Item Barcode child row (the one with the
lowest idx) - this is not a general multi-barcode/UOM-conversion importer.

Authorization uses plain Item read/create/write permissions throughout - no
project-specific role is hard-coded here.
"""

from collections import Counter

import frappe
from frappe import _
from frappe.desk.utils import provide_binary_file
from frappe.utils import cint, cstr
from frappe.utils.xlsxutils import make_xlsx, read_xlsx_file_from_attached_file

from marketplace_integrations.services import item_import_jobs

# Managed columns, in worksheet column order. item_code is the stable
# identity column; __modified is bookkeeping for the stale-write guard below,
# not a field the user is meant to hand-edit.
MANAGED_COLUMNS = [
	"item_code",
	"item_name",
	"description",
	"item_group",
	"brand",
	"stock_uom",
	"disabled",
	"barcode",
]
MODIFIED_COLUMN = "__modified"
HEADER_ROW = [*MANAGED_COLUMNS, MODIFIED_COLUMN]

# Core Item fields that are reqd=1 - required for a create.
MANDATORY_FIELDS = ("item_code", "item_group", "stock_uom")

# Managed Link fields -> target DocType. Unknown values are row errors and
# are never auto-created.
LINK_FIELDS = {
	"item_group": "Item Group",
	"brand": "Brand",
	"stock_uom": "UOM",
}
ITEM_CODES_PER_BARCODE_QUERY = 500

# disabled cell parsing: blank/None is deliberately treated as *disabled*
# (fail safe - a row a user forgot to fill the column in for should not
# silently enable an Item), not as "falsy -> enabled". Only these exact
# (case-insensitive) strings are recognized; anything else is a row error
# rather than being silently coerced.
DISABLED_TRUE_VALUES = {"1", "yes", "true"}
DISABLED_FALSE_VALUES = {"0", "no", "false"}

# frappe.utils.xlsxutils.make_xlsx() runs handle_html() on every string cell
# and silently html2text-converts anything that looks like it contains a tag,
# UNLESS the worksheet name is one of two hard-coded exemptions ("Data Import
# Template", "Data Export") - confirmed directly against the installed
# frappe/utils/xlsxutils.py. Item.description is a Text Editor (HTML) field,
# so without this exemption every export/re-import round trip of a non-empty
# description would come back "changed" even when nothing was edited. Using
# "Data Export" as the sheet name is a deliberate reuse of that exemption,
# not a display choice.
XLSX_SHEET_NAME = "Data Export"

ITEM_FIELDS = [
	"name",
	"item_code",
	"item_name",
	"description",
	"item_group",
	"brand",
	"stock_uom",
	"disabled",
	"modified",
]


@frappe.whitelist()
def export_items_xlsx():
	"""Export one XLSX worksheet with one row per Item the user can read."""
	provide_binary_file("items", "xlsx", _export_items_xlsx_bytes())


@frappe.whitelist(methods=["POST"])
def apply_items_import(file_url: str):
	"""Re-parse, re-validate, and synchronously apply a workbook."""
	_require_item_code_naming()
	return _apply_items_import(file_url)


@frappe.whitelist()
def get_items_import_job_status(job_id: str):
	"""Return the requesting user's short-lived Item import job state."""
	return item_import_jobs.get_status(job_id)


@frappe.whitelist(methods=["POST"])
def queue_items_xlsx_export():
	"""Queue an Item workbook export for the requesting user."""
	return item_import_jobs.queue_export()


@frappe.whitelist(methods=["POST"])
def queue_items_import_apply(file_url: str):
	_require_item_code_naming()
	_get_uploaded_file(file_url)
	return item_import_jobs.queue_apply(file_url)


def _export_items_xlsx_bytes():
	"""Build one XLSX worksheet with one row per Item the user can read."""
	items = frappe.get_list(
		"Item",
		fields=ITEM_FIELDS,
		order_by="item_code asc",
		limit_page_length=0,
	)
	first_barcodes = _fetch_first_barcodes([item.item_code for item in items])

	rows = [list(HEADER_ROW)]
	for item in items:
		barcode_row = first_barcodes.get(item.item_code)
		rows.append(
			[
				item.item_code,
				item.item_name or "",
				item.description or "",
				item.item_group or "",
				item.brand or "",
				item.stock_uom or "",
				cint(item.disabled),
				barcode_row.barcode if barcode_row else "",
				cstr(item.modified),
			]
		)

	xlsx_file = make_xlsx(rows, XLSX_SHEET_NAME)
	return xlsx_file.getvalue()


@frappe.whitelist(methods=["POST"])
def preview_items_import(file_url: str):
	"""Parse and validate an uploaded workbook without writing anything."""
	_require_item_code_naming()
	return _summarize(_evaluate_rows(file_url))


def _apply_items_import(file_url: str, progress_callback=None):
	"""Re-parse, re-validate, and write an uploaded workbook.

	Never trusts a client-supplied preview result: runs the exact same
	parse/validate pass preview_items_import() does, then writes only the
	rows that come out as "new" or "changed" with no errors. A row that
	fails during the actual write (e.g. a validation rule preview didn't
	anticipate, or an after_insert/on_update hook like Item.add_price()
	raising partway through) is reported as an error, not raised - it does
	not abort rows already written earlier in the same request, and its own
	partial writes are rolled back to a per-row savepoint so it doesn't leave
	a half-written Item behind despite being reported as "error".
	"""
	_require_item_code_naming()
	results = _evaluate_rows(file_url)
	applied = 0
	for processed, result in enumerate(results, start=1):
		if result["status"] in ("new", "changed"):
			# Per-row savepoint (frappe.db.savepoint()/rollback(save_point=...)/
			# release_savepoint() - frappe/database/database.py) rather than the
			# swallow-on-catch savepoint(catch=...) context-manager wrapper also
			# defined there: that wrapper discards the exception on rollback,
			# but we need the exact message to report against this row. Without
			# this, a row that fails partway through doc.insert()/doc.save()
			# (e.g. a child table row already written before an after_insert
			# hook raises) would leave that partial write committed even though
			# the row is reported as "error" and excluded from applied_count.
			savepoint_name = "item_import_" + frappe.generate_hash(length=10)
			frappe.db.savepoint(savepoint_name)
			try:
				_write_row(result)
			except (frappe.ValidationError, frappe.PermissionError, frappe.DuplicateEntryError) as exc:
				frappe.db.rollback(save_point=savepoint_name)
				result["status"] = "error"
				result["errors"] = [str(exc)]
				result.pop("diff", None)
				result.pop("new_values", None)
			else:
				frappe.db.release_savepoint(savepoint_name)
				result["applied"] = True
				applied += 1
		if progress_callback:
			progress_callback(processed, len(results))

	summary = _summarize(results)
	summary["applied_count"] = applied
	return summary


# --- Naming-series guard -----------------------------------------------------


def _require_item_code_naming():
	"""Fail loudly rather than silently corrupting data.

	erpnext.stock.doctype.item.item.Item.autoname() overwrites item_code
	with the generated naming-series value whenever Stock Settings ->
	Item Naming By is "Naming Series" (confirmed directly against the
	installed erpnext/stock/doctype/item/item.py). That silently breaks this
	importer's "item_code is the stable business key" premise - every apply
	would look like a brand new item_code and create a duplicate Item
	instead of updating the one just written. Naming Series mode is
	deliberately not supported here; the site must use Item Naming By =
	"Item Code" for this importer to be safe to use at all.
	"""
	if frappe.db.get_default("item_naming_by") != "Item Code":
		frappe.throw(
			_(
				"This importer requires Stock Settings > Item Naming By to be set to "
				"'Item Code'. Naming Series-based item codes are not supported, because "
				"ERPNext overwrites item_code with the generated series name on save, "
				"which would silently break this importer's item_code-based identity."
			)
		)


# --- File access --------------------------------------------------------


def _get_uploaded_file(file_url: str):
	"""Load and permission-check an uploaded File without parsing it.

	frappe.utils.xlsxutils.read_xlsx_file_from_attached_file() itself does
	`frappe.get_doc("File", {"file_url": file_url})` with no type check and
	no permission check - a non-str file_url (e.g. a list/dict from a raw
	JSON request body) would turn that exact-match lookup into an arbitrary
	filter, and even a valid file_url would be read regardless of whether
	the caller may actually read that File. The `file_url: str` annotation
	on the whitelisted callers makes frappe's own validate_argument_types
	reject non-str values before this ever runs; loading the File doc
	ourselves and calling check_permission("read") (which raises
	frappe.PermissionError on failure) closes the missing-permission-check
	gap. Queueing uses this lightweight check so workbook parsing remains in
	the long-running worker.
	"""
	file_doc = frappe.get_doc("File", {"file_url": file_url})
	file_doc.check_permission("read")
	return file_doc


def _read_uploaded_workbook(file_url: str):
	"""Read an already permission-checked workbook by its resolved path."""
	file_doc = _get_uploaded_file(file_url)
	return read_xlsx_file_from_attached_file(filepath=file_doc.get_full_path()) or []


# --- Parsing --------------------------------------------------------------


def _evaluate_rows(file_url: str):
	raw_rows = _read_uploaded_workbook(file_url)
	if not raw_rows:
		frappe.throw(_("The uploaded file is empty."))

	column_index = _map_header(raw_rows[0])

	parsed = []
	item_codes = []
	for row_number, raw_row in enumerate(raw_rows[1:], start=2):
		if _is_blank_row(raw_row):
			continue

		values, parse_errors = _parse_row(raw_row, column_index)
		if not values.get("item_name"):
			# Mirror erpnext.stock.doctype.item.item.Item.validate()'s own
			# "item_name defaults to item_code when blank" so preview's diff
			# matches what apply's real Item.insert()/save() will store.
			values["item_name"] = values["item_code"]

		parsed.append({"row": row_number, "values": values, "parse_errors": parse_errors})
		if values["item_code"]:
			item_codes.append(values["item_code"])

	# A duplicate non-blank item_code within the same workbook must never
	# reach a DB lookup/diff/write for either of its rows - which row "wins"
	# would be silently order-dependent (a phantom "modified after export"
	# error for whichever row is evaluated second, or a raw
	# DuplicateEntryError from a second insert attempt at apply time).
	# Report every such row as its own clear error up front instead.
	duplicate_codes = {code for code, count in Counter(item_codes).items() if count > 1}
	lookup_codes = [code for code in item_codes if code not in duplicate_codes]

	existing_items = _fetch_existing_items(lookup_codes)
	existing_barcodes = _fetch_first_barcodes(list(existing_items.keys()))
	known_links = _fetch_known_links(parsed)

	results = []
	for entry in parsed:
		item_code = entry["values"]["item_code"]
		if item_code and item_code in duplicate_codes:
			results.append(
				_error_result(
					entry["row"],
					item_code,
					[
						_("Duplicate item_code '{0}' appears in more than one row of this workbook.").format(
							item_code
						)
					],
				)
			)
			continue
		results.append(
			_evaluate_row(
				entry["row"],
				entry["values"],
				existing_items,
				existing_barcodes,
				known_links,
				entry["parse_errors"],
			)
		)
	return results


def _map_header(header_row):
	labels = [cstr(cell).strip() if cell is not None else "" for cell in header_row]
	column_index = {}
	for column in HEADER_ROW:
		if column not in labels:
			frappe.throw(_("Missing expected column '{0}' in the uploaded file.").format(column))
		column_index[column] = labels.index(column)
	return column_index


def _cell(row, index):
	return row[index] if index < len(row) else None


def _is_blank_row(row):
	return all(cell is None or cstr(cell).strip() == "" for cell in row)


def _parse_row(raw_row, column_index):
	"""Parse one raw row into MANAGED_COLUMNS values, plus a list of any
	parse-level errors (currently just an unrecognized `disabled` cell)."""
	values = {}
	errors = []
	for column in MANAGED_COLUMNS:
		raw = _cell(raw_row, column_index[column])
		if column == "disabled":
			value, error = _parse_disabled(raw)
			if error:
				errors.append(error)
			values[column] = value
		else:
			values[column] = _normalize(raw)
	values[MODIFIED_COLUMN] = cstr(_cell(raw_row, column_index[MODIFIED_COLUMN]) or "").strip()
	return values, errors


def _normalize(raw):
	return cstr(raw or "").strip()


def _parse_disabled(raw):
	"""blank/None -> 1 (disabled, fail-safe); recognized truthy/falsy
	strings (case-insensitive) -> 1/0; anything else -> a row error rather
	than a silent, potentially wrong, coercion.

	Returns (value, error) - value is None when error is set.
	"""
	if raw is None:
		return 1, None
	if isinstance(raw, bool):
		return (1 if raw else 0), None
	if isinstance(raw, (int, float)):
		if raw in (0, 1):
			return int(raw), None
		return None, _("disabled value '{0}' is not recognized; use 1/0, yes/no, or true/false.").format(raw)

	text = cstr(raw).strip()
	if text == "":
		return 1, None
	lowered = text.lower()
	if lowered in DISABLED_TRUE_VALUES:
		return 1, None
	if lowered in DISABLED_FALSE_VALUES:
		return 0, None
	return None, _("disabled value '{0}' is not recognized; use 1/0, yes/no, or true/false.").format(text)


# --- Diffing / validation ---------------------------------------------------
#
# This validation is best-effort, not a substitute for erpnext's own
# Item.validate()/.insert()/.save(). It only hand-checks mandatory fields and
# Link-target existence, deliberately not attempting to replicate every
# ERPNext-side rule (e.g. the stock_uom-vs-Bin check, barcode
# uniqueness/check-digit validation, custom mandatory fields, HTML
# sanitization that can change the stored value, or item_group changes
# rewriting item_defaults). A row previewed here as "new"/"changed" with no
# error can still be rejected, or stored with different sanitized values,
# once apply's real Item.validate()/.insert()/.save() actually runs - apply
# is the sole authority on whether a write succeeds and what gets stored.
# See docs/frappe-conventions.md ("XLSX export/import") for the same note,
# which future importers (e.g. #36's mapping workbook) should read before
# rediscovering this gap themselves.


def _fetch_existing_items(item_codes):
	if not item_codes:
		return {}
	rows = frappe.get_list(
		"Item",
		filters={"item_code": ["in", item_codes]},
		fields=ITEM_FIELDS,
		limit_page_length=0,
	)
	return {row.item_code: row for row in rows}


def _fetch_first_barcodes(item_codes):
	item_codes = [code for code in item_codes if code]
	if not item_codes:
		return {}
	first_by_parent = {}
	for offset in range(0, len(item_codes), ITEM_CODES_PER_BARCODE_QUERY):
		rows = frappe.get_all(
			"Item Barcode",
			filters={
				"parent": ["in", item_codes[offset : offset + ITEM_CODES_PER_BARCODE_QUERY]],
				"parenttype": "Item",
			},
			fields=["parent", "barcode", "idx"],
			order_by="parent asc, idx asc",
		)
		for row in rows:
			first_by_parent.setdefault(row.parent, row)
	return first_by_parent


def _fetch_known_links(parsed):
	known_links = {}
	for field, doctype in LINK_FIELDS.items():
		values = list({entry["values"].get(field) for entry in parsed if entry["values"].get(field)})
		known_links[field] = (
			set(frappe.get_all(doctype, filters={"name": ["in", values]}, pluck="name")) if values else set()
		)
	return known_links


def _evaluate_row(row_number, values, existing_items, existing_barcodes, known_links, parse_errors=()):
	item_code = values["item_code"]
	if not item_code:
		return _error_result(row_number, None, [_("item_code is required.")])

	errors = list(parse_errors) + _validate_mandatory_and_links(values, known_links)

	existing = existing_items.get(item_code)
	if existing:
		if not frappe.has_permission("Item", "write", doc=existing.name):
			errors.append(_("Not permitted to update Item {0}.").format(item_code))
	elif not frappe.has_permission("Item", "create"):
		errors.append(_("Not permitted to create Item {0}.").format(item_code))

	if errors:
		return _error_result(row_number, item_code, errors)

	if not existing:
		return {"row": row_number, "item_code": item_code, "status": "new", "new_values": values}

	diff = _diff_row(values, existing, existing_barcodes.get(item_code))
	if not diff:
		return {"row": row_number, "item_code": item_code, "status": "unchanged"}

	stale_error = _check_not_stale(item_code, values[MODIFIED_COLUMN], existing.modified)
	if stale_error:
		return _error_result(row_number, item_code, [stale_error])

	return {
		"row": row_number,
		"item_code": item_code,
		"status": "changed",
		"diff": diff,
		"new_values": values,
	}


def _validate_mandatory_and_links(values, known_links):
	errors = []
	for field in MANDATORY_FIELDS:
		if not values.get(field):
			errors.append(_("{0} is mandatory.").format(field))
	for field, target_doctype in LINK_FIELDS.items():
		value = values.get(field)
		if value and value not in known_links[field]:
			errors.append(_("{0} '{1}' does not exist as a {2}.").format(field, value, target_doctype))
	return errors


def _diff_row(values, existing, existing_barcode_row):
	diff = {}
	for field in ("item_name", "description", "item_group", "brand", "stock_uom"):
		# Both sides are normalized the same way (stripped) before comparing
		# - values[field] already went through _normalize()'s strip() at
		# parse time, so leaving the DB side unstripped would make an Item
		# whose stored value merely has incidental leading/trailing
		# whitespace round-trip as "changed" on every unedited export,
		# breaking the "unedited export -> zero writes" guarantee.
		old_value = cstr(existing.get(field) or "").strip()
		new_value = values.get(field, "")
		if old_value != new_value:
			diff[field] = {"old": old_value, "new": new_value}

	old_disabled = cint(existing.get("disabled") or 0)
	new_disabled = cint(values.get("disabled") or 0)
	if old_disabled != new_disabled:
		diff["disabled"] = {"old": old_disabled, "new": new_disabled}

	old_barcode = cstr(existing_barcode_row.barcode if existing_barcode_row else "").strip()
	new_barcode = values.get("barcode", "")
	if old_barcode != new_barcode:
		diff["barcode"] = {"old": old_barcode, "new": new_barcode}

	return diff


def _check_not_stale(item_code, xlsx_modified, current_modified):
	if not xlsx_modified:
		return _("Missing {0} value for existing Item {1}; re-export before editing.").format(
			MODIFIED_COLUMN, item_code
		)
	if xlsx_modified != cstr(current_modified):
		return _(
			"Item {0} was modified after this file was exported; re-export and re-apply your edit."
		).format(item_code)
	return None


def _error_result(row_number, item_code, errors):
	return {"row": row_number, "item_code": item_code, "status": "error", "errors": errors}


def _summarize(results):
	counts = {"new": 0, "changed": 0, "unchanged": 0, "error": 0}
	for result in results:
		counts[result["status"]] = counts.get(result["status"], 0) + 1
	return {"rows": results, "counts": counts}


# --- Writing ----------------------------------------------------------------


def _write_row(result):
	values = result["new_values"]
	item_code = result["item_code"]

	if result["status"] == "new":
		doc = frappe.new_doc("Item")
		doc.item_code = item_code
		_apply_scalar_fields(doc, values)
		_set_first_barcode(doc, values.get("barcode", ""))
		doc.insert()
	else:
		doc = frappe.get_doc("Item", item_code)
		# Optimistic-lock defense-in-depth: _check_not_stale() above already
		# rejects a row whose __modified doesn't match what we just read, but
		# setting doc.modified from the workbook value means Frappe's own
		# check_if_latest() (the same mechanism the Desk UI relies on) also
		# independently re-enforces it at save() time, in case this Item
		# changed again in the narrow window between that check and this
		# write.
		doc.modified = values[MODIFIED_COLUMN]
		_apply_scalar_fields(doc, values)
		_set_first_barcode(doc, values.get("barcode", ""))
		doc.save()


def _apply_scalar_fields(doc, values):
	doc.item_name = values.get("item_name") or None
	doc.description = values.get("description") or None
	doc.item_group = values.get("item_group")
	doc.brand = values.get("brand") or None
	doc.stock_uom = values.get("stock_uom")
	doc.disabled = cint(values.get("disabled") or 0)


def _set_first_barcode(doc, barcode_value):
	"""Manage exactly the first (lowest-idx) Item Barcode row.

	Any additional barcode rows a document already has are left untouched -
	multiple barcodes are explicitly out of scope for this importer.
	"""
	barcode_value = cstr(barcode_value or "").strip()
	rows = sorted(doc.get("barcodes") or [], key=lambda row: row.idx or 0)

	if barcode_value:
		if rows:
			rows[0].barcode = barcode_value
		else:
			doc.append("barcodes", {"barcode": barcode_value})
	elif rows:
		# Clearing the managed barcode column removes the primary row rather
		# than leaving it with a blank (and invalid, since barcode is
		# reqd=1) value.
		first = rows[0]
		doc.set("barcodes", [row for row in doc.get("barcodes") if row is not first])
