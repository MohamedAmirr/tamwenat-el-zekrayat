# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.utils.password import get_decrypted_password

LEGACY_DOCTYPE = "Talabat Settings"
MIGRATED_CONNECTION_NAME = "Talabat Settings (Migrated)"
MIGRATION_SOURCE = "talabat_settings"


def execute():
	"""One-time backfill: create a single Talabat Connection from the legacy
	Talabat Settings Single doctype (talabat_integration app), when those
	settings are actually configured.

	Deliberately does NOT touch or delete Talabat Settings itself. This patch
	was written while talabat_integration still coexisted with
	marketplace_integrations during the Talabat migration (now complete and
	the legacy app removed, per docs/marketplace-integrations/architecture.md's
	"Talabat migration" section); it remains registered and must keep running
	unchanged for any site (e.g. production) that has not yet applied it.

	Idempotent: re-running (e.g. bench migrate re-running a partially-applied
	patch, or running on a site where the legacy talabat_integration app was
	never installed) must not create a duplicate Talabat Connection or blow up.
	Guards:
	- returns early if the legacy DocType is not installed or this app's own
	  migration has not created "Talabat Connection" yet. Talabat Settings is
	  a Single DocType, so its values live in tabSingles rather than a
	  tabTalabat Settings table.
	- returns early if Talabat Settings isn't actually configured (missing
	  client_id/client_secret/chain_id)
	- returns early if this patch has already created its marker-tagged
	  Talabat Connection; a user-created display label must not suppress the
	  backfill
	"""
	if not frappe.db.exists("DocType", LEGACY_DOCTYPE) or not frappe.db.table_exists("Talabat Connection"):
		return

	settings = frappe.get_single(LEGACY_DOCTYPE)
	client_id = (settings.client_id or "").strip()
	chain_id = (settings.chain_id or "").strip()
	if not client_id or not chain_id:
		# Not configured - nothing meaningful to migrate.
		return

	# Read the secret via Frappe's password API directly (not
	# settings.get_password(), which is just a thin wrapper around this same
	# function) - raise_exception=False so an unset/undecryptable secret is
	# treated as "not configured" rather than blowing up the patch. Never
	# logged - only ever held in a local variable and handed straight to the
	# new Talabat Connection's own Password field.
	client_secret = get_decrypted_password(
		LEGACY_DOCTYPE, LEGACY_DOCTYPE, "client_secret", raise_exception=False
	)
	if not client_secret:
		return

	if frappe.db.exists("Talabat Connection", {"legacy_migration_source": MIGRATION_SOURCE}):
		# Already migrated.
		return

	frappe.get_doc(
		{
			"doctype": "Talabat Connection",
			"connection_name": MIGRATED_CONNECTION_NAME,
			"client_id": client_id,
			"client_secret": client_secret,
			"chain_id": chain_id,
			"environment": settings.environment or "Sandbox",
			"legacy_migration_source": MIGRATION_SOURCE,
			"enabled": 1,
		}
	).insert(ignore_permissions=True)
