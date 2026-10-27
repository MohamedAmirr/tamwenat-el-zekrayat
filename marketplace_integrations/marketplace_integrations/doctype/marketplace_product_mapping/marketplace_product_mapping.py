# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr


def compute_mapping_identity_key(marketplace_account, external_reference):
	"""Unambiguous, fixed-length encoding for the (marketplace_account,
	external_reference) secondary-uniqueness key.

	Same rationale as Marketplace Account's compute_account_identity_key()
	(itself mirroring Talabat Product Mapping's compute_item_vendor_key()): a
	NUL-separated hash rather than a plain "-"-joined string (a real collision
	risk - external references are opaque platform-issued strings not
	guaranteed to avoid any particular separator character) or a raw JSON
	encoding (frappe.as_json's default ensure_ascii=True blows up non-ASCII
	content into \\uXXXX escapes, and even the pure-ASCII case eats into the
	field's length budget for no reason). A SHA-256 hash is always a fixed 64
	hex chars regardless of input content or length, and the NUL byte can't
	appear in either value undetected the way a printable separator could.

	Both inputs are normalized (stringified + stripped) HERE, inside the key
	computation itself, rather than relying on the caller to have already
	normalized them - exactly the lesson from Marketplace Account's #31
	review. `marketplace_account` is a Link field, so Frappe's own
	`_validate_links()` already canonicalizes it against the real document
	name before this controller's `before_save()` ever runs - lower risk, but
	still normalized here defensively/consistently. `external_reference` is a
	free-text Data field with NO such protection: Frappe never trims or
	otherwise normalizes arbitrary Data field values, and there is no
	`_validate_selects()`-equivalent step for it. If this function trusted the
	raw value, `external_reference=" sku-1 "` would be hashed untrimmed but
	persisted as-is (untrimmed, since nothing normalizes Data fields), which
	on its own wouldn't diverge - but any future normalization added to this
	field (or a caller that trims before assigning) would immediately
	reintroduce the exact same key/value divergence bug class documented for
	Marketplace Account's `platform`. Normalizing here, in the one function
	every call path (insert, update, Data Import, resolve_item()) goes
	through, is what keeps this closed regardless of validation order or
	whether some validation step gets skipped.
	"""
	marketplace_account = cstr(marketplace_account).strip()
	external_reference = cstr(external_reference).strip()

	# cstr(None) returns "" rather than the literal string "None" - but don't
	# silently hash empty strings either. Both fields are `reqd` in normal
	# Desk/API flow, so this should be unreachable there; it only guards
	# against a future bulk-write path that bypasses mandatory-field
	# validation (e.g. `ignore_mandatory=True` in a migration patch) and would
	# otherwise let two such rows collide on an identical "empty" key rather
	# than failing loudly.
	if not marketplace_account or not external_reference:
		frappe.throw(
			_("Marketplace Account and External Reference are required to compute the mapping identity key.")
		)

	digest_input = f"{marketplace_account}\x00{external_reference}".encode()
	return hashlib.sha256(digest_input).hexdigest()


class MarketplaceProductMapping(Document):
	def validate(self):
		self.validate_unique_mapping_identity()

	def before_save(self):
		# external_reference is a free-text Data field with no Frappe-side
		# normalization (unlike a Link, which _validate_links() canonicalizes,
		# or a Select, which _validate_selects() trims) - write the stripped
		# value back to the field itself here, not just into the hash, so the
		# STORED value always matches what mapping_identity_key was actually
		# computed from. Otherwise a row can persist external_reference with
		# untrimmed whitespace even though its key was computed from the
		# trimmed value, which would silently break exact-string lookups
		# (Desk list-view filters, frappe.db.get_value by external_reference,
		# Data Import round trips) despite resolve_item() still finding it.
		self.external_reference = cstr(self.external_reference).strip()
		self.mapping_identity_key = compute_mapping_identity_key(
			self.marketplace_account, self.external_reference
		)

	def on_update(self):
		# #364: a saved, enabled mapping re-syncs the account's orders stuck on a missing mapping.
		# Service code, not a doc_events entry (the catalog guard test forbids one for this
		# doctype); it only bumps the sync debounce and never raises.
		from marketplace_integrations.services.sales_order_recheck import bump_orders_for_mapping

		bump_orders_for_mapping(self)

	def validate_unique_mapping_identity(self):
		# mapping_identity_key isn't set yet at this point (before_save runs
		# after validate) - compute the candidate directly for this friendly
		# pre-check. The field's own DB unique index is the atomic backstop
		# for the concurrent case; this is just a cleaner error for the
		# common case.
		#
		# Deliberately NOT checked: uniqueness on (marketplace_account, item).
		# The agreed business model allows multiple external listings in the
		# same Marketplace Account to map to the same ERPNext Item (e.g. a
		# bundle listing and a single-unit listing of the same product) - see
		# docs/marketplace-integrations/architecture.md.
		candidate_key = compute_mapping_identity_key(self.marketplace_account, self.external_reference)
		duplicate = frappe.db.exists(
			"Marketplace Product Mapping",
			{
				"mapping_identity_key": candidate_key,
				"name": ["!=", self.name or ""],
			},
		)
		if duplicate:
			frappe.throw(
				_(
					"A Marketplace Product Mapping for Marketplace Account {0} and external reference {1} already exists ({2})."
				).format(
					cstr(self.marketplace_account).strip(),
					cstr(self.external_reference).strip(),
					duplicate,
				)
			)
