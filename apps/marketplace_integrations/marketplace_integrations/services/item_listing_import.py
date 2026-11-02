# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Marketplace listings on the native Item Data Import and Export (#373).

Frappe's Data Import calls an ``ImportProvider`` registered under ``data_import_providers`` in
``hooks.py``. This one adds a ``Marketplace Listing`` table to Item: one row per Marketplace
Product Mapping of the Item. Marketplace Product Mapping stays the only store of the marketplace
identifiers; nothing is added to the Item doctype. Shape follows ERPNext's
``erpnext/utilities/party_import_provider.py`` (a Customer plus its Contacts and Addresses).

* The table is offered (and exported) only to a user who may read Marketplace Product Mapping.
* A listing is found by ``mapping_identity_key`` (never by the raw text, which matches
  case-insensitively on MariaDB). A new pair creates a mapping, a pair already on this Item is
  updated in place when something changed, a pair on another Item fails the row. Listings
  missing from the file are never touched.
* Every mapping write is a normal document operation (permission checks on), so DocPerm and the
  platform ``has_permission`` hook (#322) decide. Data Import commits once per record, so the
  Item and its listings commit or roll back together.
"""

import frappe
from frappe import _
from frappe.core.doctype.data_import.import_provider import ImportProvider
from frappe.core.doctype.data_import.importer import INSERT, UPDATE
from frappe.core.doctype.data_import.value_mapping import INVALID_VALUES
from frappe.model import display_fieldtypes, no_value_fields
from frappe.utils import cint, cstr
from frappe.utils.data import escape_html

from marketplace_integrations.integrations.shopify.catalog_push import (
	_PRODUCT_GID_RE,
	_VARIANT_GID_RE,
)
from marketplace_integrations.marketplace_integrations.doctype.marketplace_product_mapping.marketplace_product_mapping import (
	compute_mapping_identity_key,
)

MAPPING = "Marketplace Product Mapping"
LISTINGS_TABLE = "marketplace_listings"
# The columns of the table, in file order. The Item comes from the parent row.
LISTING_FIELDS = (
	"marketplace_account",
	"external_reference",
	"external_product_id",
	"external_sku",
	"external_barcode",
	"external_variant_id",
	"sales_uom",
	"enabled",
)
# The identity pair is never rewritten on an existing listing.
IDENTITY_FIELDS = ("marketplace_account", "external_reference")
LOOKUP_BATCH_SIZE = 500
# Item is autoname ``field:item_code``: the Item Code is its ID.
ITEM_ID_FIELD = "item_code"
# The whitelisted method that serves the schema to the Export Data field picker.
PICKER_CMD = "frappe.core.doctype.data_import.data_import.get_import_fields"


class ItemImportProvider(ImportProvider):
	def get_import_fields(self) -> dict:
		"""Item's own fields and tables (as without a provider), plus the listings table."""
		schema = {
			"fields": _with_id_header_aliases(self.doctype, _doctype_docfields(self.doctype)),
			"child_tables": [
				{
					"fieldname": tf.fieldname,
					"label": _(tf.label or tf.fieldname),
					"fields": _doctype_docfields(tf.options),
				}
				for tf in frappe.get_meta(self.doctype).get_table_fields()
			],
		}
		if frappe.has_permission(MAPPING, "read"):
			schema["child_tables"].append(
				{
					"fieldname": LISTINGS_TABLE,
					"label": _("Marketplace Listing"),
					"fields": [df for df in _doctype_docfields(MAPPING) if df["fieldname"] in LISTING_FIELDS],
				}
			)
		return schema

	def get_export_rows(self, names, tables):
		"""One row per listing of the exported Items, limited to the listings the user may read."""
		fieldnames = tables.get(LISTINGS_TABLE)
		if not fieldnames or not names or not frappe.has_permission(MAPPING, "read"):
			return {}
		# get_list, not get_all: the exporter does not check these rows, the platform row filter (#322) must.
		rows = frappe.get_list(
			MAPPING,
			filters={"item": ["in", list(names)]},
			fields=["item", *fieldnames],
			order_by="marketplace_account asc, external_reference asc",
			limit_page_length=0,
		)
		out = {}
		for row in rows:
			out.setdefault(row.item, {}).setdefault(LISTINGS_TABLE, []).append(
				{fieldname: row.get(fieldname) for fieldname in fieldnames}
			)
		return out

	def validate(self, import_file):
		"""Warnings for listing rows.

	Blocking: a pair twice in the file, a pair on another Item, malformed or missing Shopify
	identifiers. Not blocking (``type: info``): a Talabat External SKU that differs from the reference.
	"""
		columns = _listing_columns(import_file)
		if not columns.get("marketplace_account") or not columns.get("external_reference"):
			return []

		entries = _listing_entries(import_file, columns)
		if not entries:
			return []

		warnings = []
		rows_by_key = {}
		for entry in entries:
			rows_by_key.setdefault(entry["key"], []).append(entry["row"])
		for entry in entries:
			rows = rows_by_key[entry["key"]]
			if len(rows) > 1 and entry["row"] != rows[0]:
				warnings.append(
					{
						"row": entry["row"],
						"message": _(
							"External reference {0} of Marketplace Account {1} appears more than once in this file (rows {2})."
						).format(
							escape_html(entry["reference"]),
							escape_html(entry["account"]),
							", ".join(str(row) for row in rows),
						),
					}
				)

		existing = _existing_mappings(list(rows_by_key))
		existing_items = _existing_items(import_file, {entry["item_id"] for entry in entries})
		for entry in entries:
			mapped_to = existing.get(entry["key"])
			if not mapped_to:
				continue
			this_item = existing_items.get(cstr(entry["item_id"]).lower())
			if mapped_to != this_item:
				warnings.append(
					{
						"row": entry["row"],
						"message": _already_mapped_message(entry["account"], entry["reference"], mapped_to),
					}
				)
		return warnings + _platform_warnings(entries, existing)

	def import_row(self, importer, doc):
		"""Persist the Item per Import Type, then create or update its listings."""
		listing_rows = doc.pop(LISTINGS_TABLE, None) or []
		# A provider schema maps the ID header to ``name``; without a provider Frappe maps it to
		# Item's autoname field, which is what Importer.update_record / upsert_record read.
		name = doc.pop("name", None)
		# Frappe does not strip Data cells; assign the stripped value validate looked up.
		item_id = _resolve_item_id(doc.get(ITEM_ID_FIELD), name)
		if item_id:
			doc[ITEM_ID_FIELD] = item_id
		item, import_action = self._persist_item(importer, doc, bool(listing_rows))
		for row in listing_rows:
			_save_listing(item, row)
		return item, import_action

	def _persist_item(self, importer, doc, has_listing_rows):
		if importer.import_type == INSERT:
			return importer.insert_record(doc), None
		if importer.import_type == UPDATE:
			# A row may only add or change listings, without changing the Item.
			return importer.update_record(doc, raise_if_no_changes=not has_listing_rows), None
		return importer.upsert_record(doc)


def _save_listing(item, row):
	account = cstr(row.get("marketplace_account")).strip()
	reference = cstr(row.get("external_reference")).strip()
	if not account or not reference:
		frappe.throw(_("Every Marketplace Listing row needs a Marketplace Account and an External Reference."))

	key = compute_mapping_identity_key(account, reference)
	existing = frappe.db.get_value(MAPPING, {"mapping_identity_key": key}, ["name", "item"], as_dict=True)
	if not existing:
		values = {
			fieldname: row.get(fieldname) for fieldname in LISTING_FIELDS if not _is_blank(row.get(fieldname))
		}
		frappe.get_doc({"doctype": MAPPING, **values, "item": item.name}).insert()
		return

	if existing.item != item.name:
		# Name the other Item only to a user who may read that mapping.
		other = existing.item if frappe.has_permission(MAPPING, "read", doc=existing.name) else None
		frappe.throw(_already_mapped_message(account, reference, other))

	mapping = frappe.get_doc(MAPPING, existing.name)
	changed = False
	for fieldname in LISTING_FIELDS:
		value = row.get(fieldname)
		# A blank cell leaves the stored value alone (Data Import drops blank cells).
		if fieldname in IDENTITY_FIELDS or _is_blank(value):
			continue
		if _differs(fieldname, mapping.get(fieldname), value):
			mapping.set(fieldname, value)
			changed = True
	if changed:
		mapping.save()


def _resolve_item_id(item_code, name):
	"""The Item ID of a row: the Item Code cell, else the ID cell, else None.

	import_row and the validate step share this rule so they cannot drift.
	"""
	for value in (item_code, name):
		if not _is_blank(value):
			return cstr(value).strip()
	return None


def _is_blank(value):
	return value is None or (isinstance(value, str) and not value.strip())


def _differs(fieldname, stored, value):
	if fieldname == "enabled":
		return cint(stored) != cint(value)
	return cstr(stored) != cstr(value)


def _already_mapped_message(account, reference, other_item):
	if other_item:
		return _(
			"External reference {0} of Marketplace Account {1} is already mapped to Item {2}. Change it on the mapping form."
		).format(escape_html(cstr(reference)), escape_html(cstr(account)), escape_html(cstr(other_item)))
	return _(
		"External reference {0} of Marketplace Account {1} is already mapped to a different Item. Change it on the mapping form."
	).format(escape_html(cstr(reference)), escape_html(cstr(account)))


def _listing_columns(import_file):
	"""The file's listing columns by fieldname."""
	columns = {}
	for column in import_file.header.columns:
		df = column.df
		if column.skip_import or not df or not df.get("child_table_df"):
			continue
		if df.child_table_df.fieldname == LISTINGS_TABLE:
			columns[df.fieldname] = column
	return columns


def _listing_entries(import_file, columns):
	"""Raw (account, reference) pairs of the file's listing rows with the Item row they belong to.

	Child rows follow the Item row that has values in the parent columns, the same rule the
	importer uses; the cells are read as they are, nothing is parsed twice.
	"""
	header = import_file.header
	parent_indexes = header.get_column_indexes(import_file.doctype)
	# The ID column is ``item_code`` or, through the provider schema, ``name`` (the ID header).
	id_columns = {
		column.df.fieldname: column.index
		for column in header.columns
		if column.df
		and not column.skip_import
		and column.df.parent == import_file.doctype
		and column.df.fieldname in (ITEM_ID_FIELD, "name")
	}

	entries = []
	item_id = None
	for row in import_file.data:
		if any(row.get(index) not in INVALID_VALUES for index in parent_indexes):
			# Per row, as import_row does: the Item Code cell, else the ID cell.
			item_id = _resolve_item_id(
				_cell(row, id_columns.get(ITEM_ID_FIELD)), _cell(row, id_columns.get("name"))
			)
		account = _cell(row, columns["marketplace_account"].index)
		reference = _cell(row, columns["external_reference"].index)
		if not account or not reference:
			continue
		entries.append(
			{
				"row": row.row_number,
				"item_id": item_id,
				"account": account,
				"reference": reference,
				"key": compute_mapping_identity_key(account, reference),
				"product_id": _cell(row, _column_index(columns, "external_product_id")),
				"variant_id": _cell(row, _column_index(columns, "external_variant_id")),
				"sku": _cell(row, _column_index(columns, "external_sku")),
			}
		)
	return entries


def _column_index(columns, fieldname):
	column = columns.get(fieldname)
	return column.index if column else None


def _platforms(entries):
	"""``{lowercased account name: platform}`` of the file's accounts the user may read (one query)."""
	accounts = list({entry["account"] for entry in entries})
	# get_list: an account the user cannot read has no known platform, so no platform check runs.
	rows = frappe.get_list(
		"Marketplace Account",
		filters={"name": ["in", accounts]},
		fields=["name", "platform"],
		limit_page_length=0,
	)
	return {row.name.lower(): row.platform for row in rows}


def _platform_warnings(entries, existing):
	"""Shopify identifier warnings (blocking) and the Talabat SKU notice (``type: info``, not blocking)."""
	platforms = _platforms(entries)
	warnings = []
	for entry in entries:
		platform = platforms.get(entry["account"].lower())
		row, reference = entry["row"], entry["reference"]
		if platform == "Shopify":
			messages = []
			if not _VARIANT_GID_RE.match(reference):
				messages.append(
					_("External Reference {0} must look like gid://shopify/ProductVariant/<number>.").format(
						escape_html(reference)
					)
				)
			product_id = entry["product_id"]
			if product_id and not _PRODUCT_GID_RE.match(product_id):
				messages.append(
					_("External Product ID {0} must look like gid://shopify/Product/<number>.").format(
						escape_html(product_id)
					)
				)
			# A listing that already exists keeps its stored IDs when the cell is blank.
			if entry["key"] not in existing:
				if not product_id:
					messages.append(_("External Product ID is required for a new Shopify listing."))
				if not entry["variant_id"]:
					messages.append(_("External Variant ID is required for a new Shopify listing."))
			warnings += [{"row": row, "message": message} for message in messages]
		elif platform == "Talabat" and entry["sku"] and entry["sku"] != reference:
			warnings.append(
				{
					"row": row,
					"type": "info",
					"message": _(
						"External SKU {0} differs from External Reference {1}. Talabat catalog pushes send the SKU, but orders are matched by the reference."
					).format(escape_html(entry["sku"]), escape_html(reference)),
				}
			)
	return warnings


def _cell(row, index):
	if index is None:
		return None
	value = row.get(index)
	if value in INVALID_VALUES:
		return None
	return cstr(value).strip() or None


def _existing_items(import_file, item_ids):
	"""``{lowercased id: stored name}`` of the existing Items the file will update (one query).

	MariaDB matches names case-insensitively, so the caller compares through the lowercased id.
	"""
	item_ids = [item_id for item_id in item_ids if item_id]
	if not item_ids or import_file.import_type == INSERT:
		return {}
	# get_all, not get_list: an existence check of the IDs the file itself names; nothing is returned to the user.
	names = frappe.get_all(import_file.doctype, filters={"name": ["in", item_ids]}, pluck="name")
	return {name.lower(): name for name in names}


def _existing_mappings(keys):
	"""``{identity key: item}`` of the mappings the user may read, looked up in batches."""
	found = {}
	for start in range(0, len(keys), LOOKUP_BATCH_SIZE):
		rows = frappe.get_list(
			MAPPING,
			filters={"mapping_identity_key": ["in", keys[start : start + LOOKUP_BATCH_SIZE]]},
			fields=["mapping_identity_key", "item"],
			limit_page_length=0,
		)
		found.update({row.mapping_identity_key: row.item for row in rows})
	return found


def _with_id_header_aliases(doctype, fields):
	"""Map the headers ``ID (Item Code)`` and ``ID (<translated Item Code>)`` to ``item_code``.

	Frappe's own matching does this for an autoname field, but not for a provider schema, which
	has no alias slot for parent fields. Each alias is a copy of the Item Code docfield with the
	alias as its label, placed before the real one so the real field still owns the plain
	``item_code`` header. The field picker is served the schema without them (it would list the
	field three times).
	"""
	if frappe.form_dict.get("cmd") == PICKER_CMD:
		return fields
	index = next((i for i, df in enumerate(fields) if df["fieldname"] == ITEM_ID_FIELD), None)
	if index is None:
		return fields
	label = fields[index].get("label") or ITEM_ID_FIELD
	aliases = {
		f"ID ({label})",
		"{} ({})".format(_("ID"), _(label)),
	}
	copies = [{**fields[index], "label": alias} for alias in sorted(aliases)]
	return [*fields[:index], *copies, *fields[index:]]


def _doctype_docfields(doctype):
	"""Non-table importable fields of ``doctype`` as complete docfield dicts."""
	fields = []
	for df in frappe.get_meta(doctype).fields:
		if df.fieldtype in no_value_fields or df.fieldtype in display_fieldtypes:
			continue
		if df.fieldname in ("lft", "rgt") or df.get("is_virtual"):
			continue
		fields.append(df.as_dict())
	return fields
