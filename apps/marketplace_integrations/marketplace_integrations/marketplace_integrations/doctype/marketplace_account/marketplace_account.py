# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr


def compute_account_identity_key(platform, external_account_id):
	"""Unambiguous, fixed-length encoding for the (platform, external_account_id)
	secondary-uniqueness key.

	Same rationale as Talabat Product Mapping's compute_item_vendor_key(): a
	NUL-separated hash rather than a plain "-"-joined string (real collision
	risk - platform-issued external account IDs are not guaranteed to avoid
	any particular separator character) or a raw JSON encoding
	(frappe.as_json's default ensure_ascii=True blows up non-ASCII content
	into \\uXXXX escapes, and even the pure-ASCII case eats into the field's
	length budget for no reason). A SHA-256 hash is always a fixed 64 hex
	chars regardless of input content or length, and the NUL byte can't
	appear in either value undetected the way a printable separator could.

	Both inputs are normalized (stringified + stripped) HERE, inside the key
	computation itself, rather than relying on the caller to have already
	normalized them. Frappe's own Select-field normalization
	(`_validate_selects()`) runs in `_validate()`, which fires AFTER
	`before_save()` - i.e. after this function has already been called for
	the value that ends up persisted. If this function trusted the raw
	pre-normalization value, `platform=" Talabat "` would be hashed
	untrimmed but stored trimmed ("Talabat"), silently decoupling the key
	from the row it's supposed to identify. `_validate_selects()` is also
	skipped entirely under `frappe.flags.in_import`, so Data Import is
	another path that would keep a raw value.  Normalizing in one place
	that's on every call path (rather than depending on validation order)
	is what keeps this closed.
	"""
	platform = cstr(platform).strip()
	external_account_id = cstr(external_account_id).strip()

	# cstr(None) returns "" rather than the literal string "None" - but
	# don't silently hash empty strings either. Both fields are `reqd` in
	# normal Desk/API flow, so this should be unreachable there; it only
	# guards against a future bulk-write path that bypasses mandatory-field
	# validation (e.g. `ignore_mandatory=True` in a migration patch) and
	# would otherwise let two such rows collide on an identical "empty"
	# key rather than failing loudly.
	if not platform or not external_account_id:
		frappe.throw(_("Platform and External Account ID are required to compute the account identity key."))

	digest_input = f"{platform}\x00{external_account_id}".encode()
	return hashlib.sha256(digest_input).hexdigest()


class MarketplaceAccount(Document):
	def validate(self):
		self.validate_connection()
		self.validate_unique_account_identity()

	def before_save(self):
		self.account_identity_key = compute_account_identity_key(self.platform, self.external_account_id)

	def validate_connection(self):
		if not self.connection_doctype:
			frappe.throw(_("Connection DocType is required."))

		if not self.connection:
			frappe.throw(_("Connection is required."))

		if not frappe.db.exists(self.connection_doctype, self.connection):
			frappe.throw(
				_("Connection {0} does not exist as a {1}.").format(self.connection, self.connection_doctype)
			)

	def validate_unique_account_identity(self):
		# account_identity_key isn't set yet at this point (before_save runs
		# after validate) - compute the candidate directly for this friendly
		# pre-check. The field's own DB unique index is the atomic backstop
		# for the concurrent case; this is just a cleaner error for the
		# common case.
		candidate_key = compute_account_identity_key(self.platform, self.external_account_id)
		duplicate = frappe.db.exists(
			"Marketplace Account",
			{
				"account_identity_key": candidate_key,
				"name": ["!=", self.name or ""],
			},
		)
		if duplicate:
			frappe.throw(
				_(
					"A Marketplace Account for platform {0} and external account {1} already exists ({2})."
				).format(cstr(self.platform).strip(), cstr(self.external_account_id).strip(), duplicate)
			)
