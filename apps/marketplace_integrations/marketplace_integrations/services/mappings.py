# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.query_builder import DocType
from frappe.utils import cstr

from marketplace_integrations.marketplace_integrations.doctype.marketplace_product_mapping.marketplace_product_mapping import (
	compute_mapping_identity_key,
)


def resolve_item(marketplace_account, external_reference):
	"""Resolve the ERPNext Item mapped to an external marketplace listing.

	This is the one runtime entry point platform-specific adapter code (e.g.
	Talabat order/catalog handling) should call to go from
	"this marketplace account said this external reference" to "this ERPNext
	Item" - per docs/marketplace-integrations/architecture.md's "exact
	mapping resolution" boundary.

	Resolution is exact and deliberately dumb: it looks up the single stored
	Marketplace Product Mapping row whose (marketplace_account,
	external_reference) pair matches, via the same normalized identity key
	the doctype itself enforces uniqueness on. There is no fuzzy/partial
	matching and no fallback to external_sku/external_barcode/
	external_variant_id if external_reference doesn't match - core mapping
	code must not guess which identifier a platform's external_reference
	actually is (SKU, barcode, product ID, variant ID, ...); that decision is
	entirely the calling platform adapter's responsibility when it decides
	what to pass as external_reference in the first place.

	Only *enabled* mappings resolve, and only when the mapping's Marketplace
	Account is *also* enabled - a disabled mapping OR a disabled account is
	treated the same as no mapping at all, so callers fail closed rather than
	silently continuing to sync/fulfil against a mapping an operator has
	intentionally turned off, or whose whole account has been disabled while
	its individual mappings were left enabled.

	Blank/missing marketplace_account or external_reference are treated as
	"no mapping" rather than an error - this function normalizes its own
	inputs and returns None early for either, rather than propagating
	compute_mapping_identity_key()'s frappe.throw() (that throw is correct
	for the doctype's own before_save() write-path guard, but this is a
	read-only resolver whose documented contract is "Item or None").

	Returns the mapped Item's name (str), or None if no matching mapping
	with both the mapping and its account enabled exists for this exact
	pair.
	"""
	marketplace_account = cstr(marketplace_account).strip()
	external_reference = cstr(external_reference).strip()
	if not marketplace_account or not external_reference:
		return None

	key = compute_mapping_identity_key(marketplace_account, external_reference)

	MPM = DocType("Marketplace Product Mapping")
	MA = DocType("Marketplace Account")

	result = (
		frappe.qb.from_(MPM)
		.join(MA)
		.on(MA.name == MPM.marketplace_account)
		.select(MPM.item)
		.where(MPM.mapping_identity_key == key)
		.where(MPM.enabled == 1)
		.where(MA.enabled == 1)
		.limit(1)
		.run(as_dict=False)
	)
	return result[0][0] if result else None
