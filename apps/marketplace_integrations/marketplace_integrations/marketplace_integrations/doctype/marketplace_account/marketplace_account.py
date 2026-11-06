# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr

from marketplace_integrations.integrations.shopify.locations import fetch_shop_and_active_locations

# connection_doctype is hidden/read-only on the form - a user never types it
# directly (#103). This is the one place platform -> Connection DocType is
# decided; each platform resolves to its own Connection DocType.
PLATFORM_CONNECTION_DOCTYPES = {
	"Talabat": "Talabat Connection",
	"Shopify": "Shopify Connection",
	"Noon": "Noon Connection",
}


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


def shipping_account_problem(account_name, company):
	"""Why ``account_name`` cannot receive a Shopify shipping charge, or None when it can.

	Shared by Marketplace Account validation and the Sales Order sync (the account
	can be disabled or changed after the Marketplace Account was saved).  ERPNext
	itself only checks that the account belongs to the document's company.
	"""
	values = frappe.db.get_value(
		"Account",
		account_name,
		["company", "is_group", "disabled", "root_type", "account_type"],
		as_dict=True,
	)
	if not values:
		return _("ERPNext Shipping Income Account {0} does not exist.").format(account_name)
	if not company:
		return _("Set the ERPNext Company before choosing the ERPNext Shipping Income Account.")
	if values.company != company:
		return _("ERPNext Shipping Income Account {0} does not belong to Company {1}.").format(
			account_name, company
		)
	if values.is_group:
		return _("ERPNext Shipping Income Account {0} is a group account; choose a leaf account.").format(
			account_name
		)
	if values.disabled:
		return _("ERPNext Shipping Income Account {0} is disabled.").format(account_name)
	if values.root_type != "Income" and values.account_type not in ("Tax", "Chargeable"):
		return _(
			"ERPNext Shipping Income Account {0} must be an Income account or have the Tax or Chargeable account type."
		).format(account_name)
	return None


def clearing_account_problem(account_name, company):
	"""Why ``account_name`` cannot be the clearing account of a mirrored Shopify payment, or None when it can.

	Runs on Marketplace Account validation and again when a payment is mirrored (the account
	can be disabled or changed after the Marketplace Account was saved).  Account types are
	those ERPNext v16 offers for ``paid_to`` on a Receive Payment Entry (Bank and Cash).
	"""
	values = frappe.db.get_value(
		"Account",
		account_name,
		["company", "is_group", "disabled", "account_type", "account_currency"],
		as_dict=True,
	)
	if not values:
		return _("Clearing Account {0} does not exist.").format(account_name)
	if not company:
		return _("Set the ERPNext Company before choosing a Clearing Account.")
	if values.company != company:
		return _("Clearing Account {0} does not belong to Company {1}.").format(account_name, company)
	if values.is_group:
		return _("Clearing Account {0} is a group account; choose a leaf account.").format(account_name)
	if values.disabled:
		return _("Clearing Account {0} is disabled.").format(account_name)
	if values.account_type not in ("Bank", "Cash"):
		return _("Clearing Account {0} must be a Bank or Cash account.").format(account_name)
	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	if values.account_currency != company_currency:
		return _("Clearing Account {0} is in {1}; it must be in the Company currency {2}.").format(
			account_name, values.account_currency, company_currency
		)
	return None


class MarketplaceAccount(Document):
	def __init__(self, *args, **kwargs):
		"""connection_doctype needs to be defaulted here, in __init__, not in
		validate() - Frappe's own Document.insert()/save() call
		_validate_links() (which validates the connection Dynamic Link field
		against connection_doctype, and throws "Connection DocType must be
		set first" if it's blank) BEFORE before_insert/before_validate/
		validate ever run - none of those controller hooks fire early enough
		to fill it in first. __init__ is the one thing that always runs
		before any of that, since it's what constructs the in-memory document
		in the first place (this is also an established pattern elsewhere in
		this Frappe/ERPNext ecosystem, e.g. purchase_order.py/stock_entry.py
		overriding __init__ for their own per-instance setup).

		Safe for the "load an existing document from DB" constructor path too
		(frappe.get_doc(doctype, name)): super().__init__() already runs
		load_from_db() before returning in that case, so connection_doctype
		is already populated from the DB by the time set_connection_doctype()
		runs and its own "already set" guard makes it a no-op.

		Known gap (#103 review finding #8): this only covers the
		dict-construction path (`frappe.get_doc({...})`) and the
		load-from-db path. `frappe.new_doc("Marketplace Account")` followed by
		plain attribute assignment (`doc.platform = "Shopify"`) does NOT reach
		this with `platform` already set - `__init__` runs first, with
		`platform` still blank, before the caller ever assigns it. That
		construction pattern is a genuine gap in this defaulting: it needs the
		caller to set `connection_doctype` explicitly (or call
		`set_connection_doctype()` again after assigning `platform`, before
		`insert()`), because by the time `validate()` re-runs the same
		defaulting (see below), core's `_validate_links()` has already fired
		and thrown on the still-blank `connection_doctype`. See
		`docs/frappe-conventions.md`'s note on `_validate_links()` running
		before controller hooks for the general version of this lesson."""
		super().__init__(*args, **kwargs)
		self.set_connection_doctype()

	def validate(self):
		# Belt-and-suspenders re-defaulting (#103 review finding #8): covers
		# any construction path that reaches validate() with `platform` set
		# but `connection_doctype` still blank. __init__ remains the primary
		# fix (it's what stops core's _validate_links() throwing before this
		# ever runs) - see set_connection_doctype()'s docstring for the one
		# construction pattern this still can't save.
		self.set_connection_doctype()
		self.update_connection_doctype_on_platform_change()
		self.validate_connection()
		self.validate_warehouse()
		self.validate_shipping_account()
		self.validate_payment_gateways()
		self.validate_shipping_method_rules()
		# validate_shopify_account() must run BEFORE validate_unique_account_identity():
		# for a new Shopify account, external_account_id starts out blank and
		# is only populated from the live-confirmed shop GID inside
		# validate_shopify_shop() (see its docstring) - the uniqueness
		# precheck below needs that populated value, not the blank one, to
		# compute a meaningful candidate identity key.
		self.validate_shopify_account()
		self.validate_noon_account()
		self.validate_unique_account_identity()

	def before_save(self):
		self.external_account_id = cstr(self.external_account_id).strip()
		self.account_identity_key = compute_account_identity_key(self.platform, self.external_account_id)

	def set_connection_doctype(self):
		"""Default connection_doctype from platform whenever it's blank - it's
		hidden/read-only on the form, so nothing else ever fills it in for a
		normal Desk-created document. Deliberately only fills a *blank* value,
		never overrides one a caller already set explicitly (e.g. a future
		migration patch, or a test building a deliberately mismatched
		fixture) - this stays a pure default, not a forced override.

		A platform CHANGE on an existing document (not just a blank value) is
		handled separately by update_connection_doctype_on_platform_change() -
		this method's own "already set" guard is deliberately left as pure
		defaulting behavior, not overloaded to also detect a change."""
		if self.connection_doctype:
			return
		expected = PLATFORM_CONNECTION_DOCTYPES.get(self.platform)
		if expected:
			self.connection_doctype = expected

	def update_connection_doctype_on_platform_change(self):
		"""#103 review finding #4: switching `platform` on an existing account
		(e.g. Talabat -> Shopify) left connection_doctype stuck at its old
		value - set_connection_doctype() only ever fills a *blank* value, so
		an existing, already-populated connection_doctype was never revisited.
		That stranded the document: connection_doctype is hidden/read-only on
		the form, so nothing else could fix it, and `connection` (whose
		Dynamic Link target is the now-wrong connection_doctype) would fail
		validation with an unhelpful error.

		This explicitly detects a platform VALUE CHANGE on an existing
		(not new) document via has_value_changed(), and re-derives
		connection_doctype from the new platform - intentionally overriding
		whatever connection_doctype already held, unlike
		set_connection_doctype()'s blank-only default."""
		if self.is_new():
			return
		if not self.has_value_changed("platform"):
			return
		expected = PLATFORM_CONNECTION_DOCTYPES.get(self.platform)
		if expected:
			self.connection_doctype = expected

	def validate_connection(self):
		if not self.connection_doctype:
			frappe.throw(_("Connection DocType is required."))

		if not self.connection:
			frappe.throw(_("Connection is required."))

		if not frappe.db.exists(self.connection_doctype, self.connection):
			frappe.throw(
				_("Connection {0} does not exist as a {1}.").format(self.connection, self.connection_doctype)
			)

	def validate_warehouse(self):
		# Deliberately lighter-weight than services/account_warehouse.py's
		# validate_account_warehouse() (#85 review finding #7): this is a
		# basic save-time sanity check (leaf-vs-group only) that applies
		# regardless of which sync flow ever uses this Warehouse, while the
		# stricter check (disabled, Company match) only makes sense in the
		# context of one specific sync operation - a Company mismatch isn't
		# an error at save time for a Warehouse that will only ever be used
		# for catalog sync, which doesn't require a Company at all. Do not
		# fold the two together; they intentionally check different things.
		if self.warehouse and frappe.db.get_value("Warehouse", self.warehouse, "is_group"):
			frappe.throw(_("Marketplace Account Warehouse must be a leaf Warehouse, not a group Warehouse."))

	def validate_shipping_account(self):
		"""The Shopify shipping income account must be a usable account of this
		account's Company (see ``shipping_account_problem()``)."""
		if self.platform != "Shopify" or not self.erpnext_shipping_account:
			return
		problem = shipping_account_problem(self.erpnext_shipping_account, self.erpnext_company)
		if problem:
			frappe.throw(problem)

	def validate_payment_gateways(self):
		"""Trim each gateway, allow one row per gateway, and check each clearing account
		(see ``clearing_account_problem()``).  Shopify only."""
		if self.platform != "Shopify":
			return
		seen = set()
		for row in self.shopify_payment_gateways or []:
			row.shopify_gateway = cstr(row.shopify_gateway).strip()
			if not row.shopify_gateway:
				frappe.throw(_("Row #{0}: Shopify Gateway is required.").format(row.idx))
			if row.shopify_gateway in seen:
				frappe.throw(
					_("Row #{0}: Shopify Gateway {1} is listed more than once.").format(
						row.idx, row.shopify_gateway
					)
				)
			seen.add(row.shopify_gateway)
			problem = clearing_account_problem(row.clearing_account, self.erpnext_company)
			if problem:
				frappe.throw(_("Row #{0}: {1}").format(row.idx, problem))

	def validate_shipping_method_rules(self):
		"""Trim each shipping title, allow one rule per title and no negative days.  Shopify only."""
		if self.platform != "Shopify":
			return
		seen = set()
		for row in self.shopify_shipping_method_rules or []:
			row.shipping_title = cstr(row.shipping_title).strip()
			if not row.shipping_title:
				frappe.throw(_("Row #{0}: Shipping Title is required.").format(row.idx))
			if row.shipping_title in seen:
				frappe.throw(
					_("Row #{0}: Shipping Title {1} is listed more than once.").format(
						row.idx, row.shipping_title
					)
				)
			seen.add(row.shipping_title)
			if cint(row.delivery_days) < 0:
				frappe.throw(_("Row #{0}: Delivery Days cannot be negative.").format(row.idx))

	def validate_noon_account(self):
		"""Bind a Noon account to the selected connection's project code locally."""
		if self.platform != "Noon":
			return

		if self.connection_doctype != "Noon Connection":
			frappe.throw(_("A Noon Marketplace Account must use a Noon Connection."))

		project_code = frappe.db.get_value("Noon Connection", self.connection, "project_code")
		if not project_code:
			frappe.throw(_("The selected Noon Connection must have a project code."))
		if self.external_account_id != project_code:
			frappe.throw(
				_("External Account ID must exactly match the selected Noon Connection's project code.")
			)
		if self.noon_country_code:
			from marketplace_integrations.integrations.noon.constants import COUNTRY_CURRENCY

			if self.noon_country_code not in COUNTRY_CURRENCY:
				frappe.throw(_("Noon Country Code must be ae, sa, or eg."))

		if not self.is_new() and (
			self.has_value_changed("connection") or self.has_value_changed("noon_warehouse_code")
		):
			# The stored Check Noon Warehouse answer belongs to the old warehouse: the user checks again.
			self.noon_fulfillment_system_code = None
			self.noon_warehouse_active = 0
			self.noon_warehouse_checked_on = None

		if self.noon_warehouse_code and (
			self.is_new()
			or self.has_value_changed("connection")
			or self.has_value_changed("noon_warehouse_code")
		):
			from marketplace_integrations.integrations.noon.catalog import validate_warehouse_code

			validate_warehouse_code(self.connection, self.noon_warehouse_code)

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

	def validate_shopify_account(self):
		"""Shopify-only cross-checks against the real store (#103): the shop
		GID and, if one is set, the inventory Location are both verified via a
		single live, read-only Admin GraphQL call
		(integrations/shopify/locations.py's fetch_shop_and_active_locations()).
		Both `shop { id }` and `locations { ... }` are read-only queries, so
		running this automatically inside validate()/save() is safe under this
		project's read-vs-write rule - unlike any future Shopify *mutation*,
		which must stay behind an explicit user-triggered action instead. (#107's
		automatic inventory sync - the scheduled changed-Bin sweep and daily
		reconciliation, in integrations/shopify/inventory_sync.py - IS such a
		mutation with no explicit user action; it is a deliberate, user-approved
		exception to this rule, not a contradiction of it.)

		This is a no-op for every non-Shopify account (in particular, existing
		Talabat accounts save exactly as before).

		#103 review finding #1: the live call is also skipped on an existing
		document's save when nothing Shopify-relevant changed - a save that
		only touches Warehouse/Price List/Customer/enabled on an
		already-verified account must not re-trigger a live Shopify call
		(this also matters for services/sales_order_sync.py's
		_get_or_create_customer(), which calls account.save() after only
		setting erpnext_customer - see review finding #2/its test below)."""
		if self.platform != "Shopify":
			return

		if self.connection_doctype != "Shopify Connection":
			frappe.throw(_("A Shopify Marketplace Account must use a Shopify Connection."))

		if not self._shopify_live_check_required():
			return

		info = fetch_shop_and_active_locations(self.connection)
		self.validate_shopify_shop(info["shop_id"])
		self.validate_shopify_location(info["active_locations"])

	def _shopify_live_check_required(self):
		"""New documents always need the live check (nothing has ever been
		verified yet). For an existing document, only re-run it when one of
		the three fields the live call actually verifies against has
		changed - connection (which Connection to call), external_account_id
		(e.g. something unexpected setting it directly via API, bypassing the
		read-only field on the form; see validate_shopify_shop()), or
		shopify_location_gid (which Location to verify). Any other field
		changing (Warehouse, Price List, Customer, enabled, ...) does not need
		a fresh live call."""
		if self.is_new():
			return True
		return (
			self.has_value_changed("connection")
			or self.has_value_changed("external_account_id")
			or self.has_value_changed("shopify_location_gid")
		)

	def validate_shopify_shop(self, live_shop_id):
		"""external_account_id always ends up equal to the linked Shopify
		Connection's own live shop GID by the time this returns.

		UX change (per user request): external_account_id is no longer
		manually typed/pasted for Shopify - it is always (re-)derived from the
		live-confirmed shop GID here, mirroring how shopify_location_name is
		always re-derived from live data below.

		#103 review finding #2: a disagreeing (non-blank) external_account_id
		used to throw a "does not match" error as a safety net - but the field
		is read-only on the form for Shopify accounts, so that error had no
		recovery path for a legitimate case (e.g. repointing an existing
		account at a different Connection, or converting an old Talabat
		account to Shopify and carrying over its stale vendor ID). Since the
		field is never user-editable for Shopify, there's no longer a reason
		to preserve a stale/wrong stored value over the live-verified one -
		self-heal it instead of throwing.
		"""
		if not live_shop_id:
			frappe.throw(
				_("Could not verify Shopify Connection {0}'s own shop - no shop id was returned.").format(
					self.connection
				)
			)
		self.external_account_id = live_shop_id

	def validate_shopify_location(self, active_locations):
		"""shopify_location_gid is optional (same "no fallback" philosophy as
		Warehouse) - but if one is set, it must be one of the store's currently
		*active* Locations. shopify_location_name is always re-derived from
		the live result, never trusted as a stored/typed value, so it can never
		drift from what the GID actually resolves to."""
		if not self.shopify_location_gid:
			self.shopify_location_name = None
			return

		match = next(
			(location for location in active_locations if location.get("id") == self.shopify_location_gid),
			None,
		)
		if not match:
			frappe.throw(
				_("Shopify Location {0} is not an active Location on {1}.").format(
					self.shopify_location_gid, self.connection
				)
			)
		self.shopify_location_name = match.get("name")
