# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""XLSX round-trip import/export for Marketplace Product Mapping.

One row represents one external marketplace listing.  Its business identity
is exactly (marketplace_account, external_reference); Frappe document names
are deliberately never exposed in the workbook.
"""

from collections import Counter

import frappe
from frappe import _
from frappe.desk.utils import provide_binary_file
from frappe.utils import cint, cstr
from frappe.utils.xlsxutils import make_xlsx, read_xlsx_file_from_attached_file

MANAGED_COLUMNS = [
	"marketplace_account",
	"item",
	"external_reference",
	"external_product_id",
	"external_sku",
	"external_barcode",
	"external_variant_id",
	"sales_uom",
	"enabled",
]
MODIFIED_COLUMN = "__modified"
HEADER_ROW = [*MANAGED_COLUMNS, MODIFIED_COLUMN]
XLSX_SHEET_NAME = "Data Export"

OPTIONAL_OPAQUE_FIELDS = (
	"external_product_id",
	"external_sku",
	"external_barcode",
	"external_variant_id",
)
LINK_FIELDS = {
	"marketplace_account": "Marketplace Account",
	"item": "Item",
	"sales_uom": "UOM",
}
MAPPING_FIELDS = ["name", *MANAGED_COLUMNS, "modified"]
TRUE_VALUES = {"1", "yes", "true"}
FALSE_VALUES = {"0", "no", "false"}


def export_mappings_xlsx():
	"""Export one row per Marketplace Product Mapping the user may read."""
	_require_mapping_read_permission()
	mappings = frappe.get_list(
		"Marketplace Product Mapping",
		fields=MAPPING_FIELDS,
		order_by="marketplace_account asc, external_reference asc",
		limit=None,
	)
	rows = [list(HEADER_ROW)]
	for mapping in mappings:
		rows.append(
			[mapping.get(column) if mapping.get(column) is not None else "" for column in MANAGED_COLUMNS]
			+ [cstr(mapping.modified)]
		)

	# "Data Export" is a deliberate Frappe xlsxutils exemption that preserves
	# opaque IDs containing < or > instead of passing them through html2text.
	xlsx_file = make_xlsx(rows, XLSX_SHEET_NAME)
	provide_binary_file("marketplace_product_mappings", "xlsx", xlsx_file.getvalue())


def preview_mappings_import(file_url: str):
	"""Parse and validate an uploaded workbook without writing anything."""
	return _summarize(_evaluate_rows(file_url))


def apply_mappings_import(file_url: str):
	"""Re-parse, re-validate, and write valid new/changed rows.

	Preview validation deliberately covers only this workbook's identity,
	mandatory fields, links, and permissions.  The mapping DocType's real
	insert/save validation remains authoritative at apply time.
	"""
	results = _evaluate_rows(file_url)
	applied = 0
	for result in results:
		if result["status"] not in ("new", "changed"):
			continue

		savepoint_name = "mapping_import_" + frappe.generate_hash(length=10)
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

	summary = _summarize(results)
	summary["applied_count"] = applied
	return summary


def _read_uploaded_workbook(file_url: str):
	"""Read only a File the caller has explicit permission to read."""
	file_doc = frappe.get_doc("File", {"file_url": file_url})
	file_doc.check_permission("read")
	return read_xlsx_file_from_attached_file(filepath=file_doc.get_full_path()) or []


def _evaluate_rows(file_url: str):
	raw_rows = _read_uploaded_workbook(file_url)
	if not raw_rows:
		frappe.throw(_("The uploaded file is empty."))

	column_index = _map_header(raw_rows[0])
	parsed = []
	identities = []
	for row_number, raw_row in enumerate(raw_rows[1:], start=2):
		if _is_blank_row(raw_row):
			continue
		values, parse_errors = _parse_row(raw_row, column_index)
		parsed.append({"row": row_number, "values": values, "parse_errors": parse_errors})
		if values["marketplace_account"] and values["external_reference"]:
			identities.append((values["marketplace_account"], values["external_reference"]))

	duplicate_identities = {identity for identity, count in Counter(identities).items() if count > 1}
	existing = _fetch_existing_mappings(
		[account for account, reference in identities if (account, reference) not in duplicate_identities],
		[reference for account, reference in identities if (account, reference) not in duplicate_identities],
	)

	results = []
	for entry in parsed:
		identity = (entry["values"]["marketplace_account"], entry["values"]["external_reference"])
		if identity in duplicate_identities:
			results.append(
				_error_result(
					entry["row"],
					*identity,
					[
						_(
							"Duplicate Marketplace Account and external_reference pair appears in more than one row of this workbook."
						)
					],
				)
			)
			continue
		results.append(
			_evaluate_row(entry["row"], entry["values"], existing.get(identity), entry["parse_errors"])
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


def _is_blank_row(row):
	return all(cell is None or cstr(cell).strip() == "" for cell in row)


def _cell(row, index):
	return row[index] if index < len(row) else None


def _parse_row(raw_row, column_index):
	values = {}
	for field in ("marketplace_account", "item", "external_reference", "sales_uom"):
		values[field] = _string_cell(_cell(raw_row, column_index[field])).strip()
	# These fields hold opaque platform identifiers.  Preserve their exact
	# value (apart from the empty-cell -> empty-string representation) rather
	# than silently trimming identifiers that may legitimately contain spaces.
	for field in OPTIONAL_OPAQUE_FIELDS:
		values[field] = _string_cell(_cell(raw_row, column_index[field]))
	values["enabled"], error = _parse_enabled(_cell(raw_row, column_index["enabled"]))
	values[MODIFIED_COLUMN] = _string_cell(_cell(raw_row, column_index[MODIFIED_COLUMN])).strip()
	return values, [error] if error else []


def _string_cell(raw):
	"""Keep a legitimate numeric 0 rather than turning it into an empty cell."""
	return "" if raw is None else cstr(raw)


def _parse_enabled(raw):
	if raw is None or cstr(raw).strip() == "":
		return 1, None
	if isinstance(raw, bool):
		return (1 if raw else 0), None
	if isinstance(raw, (int, float)) and raw in (0, 1):
		return int(raw), None
	value = cstr(raw).strip()
	if value.lower() in TRUE_VALUES:
		return 1, None
	if value.lower() in FALSE_VALUES:
		return 0, None
	return None, _("enabled value '{0}' is not recognized; use 1/0, yes/no, or true/false.").format(value)


def _fetch_existing_mappings(accounts, references):
	accounts = list(dict.fromkeys(account for account in accounts if account))
	references = list(dict.fromkeys(reference for reference in references if reference))
	if not accounts or not references:
		return {}
	_require_mapping_read_permission()
	rows = frappe.get_list(
		"Marketplace Product Mapping",
		filters={"marketplace_account": ["in", accounts], "external_reference": ["in", references]},
		fields=MAPPING_FIELDS,
		limit=None,
	)
	return {(row.marketplace_account, row.external_reference): row for row in rows}


def _require_mapping_read_permission():
	"""Prevent select-only users from exporting or probing mapping records."""
	if not frappe.has_permission("Marketplace Product Mapping", "read"):
		frappe.throw(_("Not permitted to read Marketplace Product Mappings."), frappe.PermissionError)


def _evaluate_row(row_number, values, existing, parse_errors):
	account = values["marketplace_account"]
	reference = values["external_reference"]
	errors = list(parse_errors) + _validate_mandatory_and_links(values)
	if existing:
		if not frappe.has_permission("Marketplace Product Mapping", "write", doc=existing.name):
			errors.append(_("Not permitted to update Marketplace Product Mapping {0}.").format(existing.name))
	elif not frappe.has_permission("Marketplace Product Mapping", "create"):
		errors.append(_("Not permitted to create Marketplace Product Mapping."))

	if errors:
		return _error_result(row_number, account, reference, errors)
	if not existing:
		return {
			"row": row_number,
			"marketplace_account": account,
			"external_reference": reference,
			"status": "new",
			"new_values": values,
		}

	diff = _diff_row(values, existing)
	if not diff:
		return {
			"row": row_number,
			"marketplace_account": account,
			"external_reference": reference,
			"status": "unchanged",
		}
	stale_error = _check_not_stale(account, reference, values[MODIFIED_COLUMN], existing.modified)
	if stale_error:
		return _error_result(row_number, account, reference, [stale_error])
	return {
		"row": row_number,
		"marketplace_account": account,
		"external_reference": reference,
		"existing_name": existing.name,
		"status": "changed",
		"diff": diff,
		"new_values": values,
	}


def _validate_mandatory_and_links(values):
	errors = []
	for field in ("marketplace_account", "item", "external_reference"):
		if not values[field]:
			errors.append(_("{0} is mandatory.").format(field))
	for field, doctype in LINK_FIELDS.items():
		value = values[field]
		if value and not frappe.db.exists(doctype, value):
			errors.append(_("{0} '{1}' does not exist as a {2}.").format(field, value, doctype))
	return errors


def _diff_row(values, existing):
	diff = {}
	for field in ("item", *OPTIONAL_OPAQUE_FIELDS, "sales_uom"):
		old_value = cstr(existing.get(field) or "")
		if old_value != values[field]:
			diff[field] = {"old": old_value, "new": values[field]}
	old_enabled = cint(existing.get("enabled") or 0)
	if old_enabled != values["enabled"]:
		diff["enabled"] = {"old": old_enabled, "new": values["enabled"]}
	return diff


def _check_not_stale(account, reference, xlsx_modified, current_modified):
	if not xlsx_modified:
		return _(
			"Missing {0} value for Marketplace Product Mapping {1}/{2}; re-export before editing."
		).format(MODIFIED_COLUMN, account, reference)
	if xlsx_modified != cstr(current_modified):
		return _(
			"Marketplace Product Mapping {0}/{1} was modified after this file was exported; re-export and re-apply your edit."
		).format(account, reference)
	return None


def _error_result(row_number, account, reference, errors):
	return {
		"row": row_number,
		"marketplace_account": account or None,
		"external_reference": reference or None,
		"status": "error",
		"errors": errors,
	}


def _summarize(results):
	counts = {"new": 0, "changed": 0, "unchanged": 0, "error": 0}
	for result in results:
		counts[result["status"]] += 1
	return {"rows": results, "counts": counts}


def _write_row(result):
	values = result["new_values"]
	if result["status"] == "new":
		doc = frappe.new_doc("Marketplace Product Mapping")
	else:
		doc = frappe.get_doc("Marketplace Product Mapping", result["existing_name"])
		# Let Frappe's normal optimistic-concurrency check enforce the same
		# __modified guard again in the small interval between preview and save.
		doc.modified = values[MODIFIED_COLUMN]
	_apply_values(doc, values)
	if result["status"] == "new":
		doc.insert()
	else:
		doc.save()


def _apply_values(doc, values):
	for field in MANAGED_COLUMNS:
		setattr(doc, field, values[field])
