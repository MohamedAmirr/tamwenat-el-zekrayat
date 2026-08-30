# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.password import get_decrypted_password, set_encrypted_password

from marketplace_integrations.patches.v0_0.create_talabat_connection_from_legacy_settings import (
	MIGRATED_CONNECTION_NAME,
	MIGRATION_SOURCE,
	execute,
)

LEGACY_DOCTYPE = "Talabat Settings"


class IntegrationTestCreateTalabatConnectionFromLegacySettings(IntegrationTestCase):
	def setUp(self):
		self.manual_connection_names = []
		settings = frappe.get_single(LEGACY_DOCTYPE)
		self.original_settings = {
			"client_id": settings.client_id,
			"chain_id": settings.chain_id,
			"environment": settings.environment,
			"client_secret": get_decrypted_password(
				LEGACY_DOCTYPE, LEGACY_DOCTYPE, "client_secret", raise_exception=False
			),
		}
		self.delete_migrated_connections()

	def tearDown(self):
		self.delete_migrated_connections()
		for name in self.manual_connection_names:
			if frappe.db.exists("Talabat Connection", name):
				frappe.delete_doc("Talabat Connection", name, force=True)

		settings = frappe.get_single(LEGACY_DOCTYPE)
		settings.client_id = self.original_settings["client_id"]
		settings.chain_id = self.original_settings["chain_id"]
		settings.environment = self.original_settings["environment"]
		settings.save(ignore_permissions=True)
		frappe.db.delete(
			"__Auth",
			{"doctype": LEGACY_DOCTYPE, "name": LEGACY_DOCTYPE, "fieldname": "client_secret"},
		)
		if self.original_settings["client_secret"]:
			set_encrypted_password(
				LEGACY_DOCTYPE,
				LEGACY_DOCTYPE,
				self.original_settings["client_secret"],
				"client_secret",
			)

	def delete_migrated_connections(self):
		for name in frappe.get_all(
			"Talabat Connection",
			filters={"legacy_migration_source": MIGRATION_SOURCE},
			pluck="name",
		):
			frappe.delete_doc(
				"Talabat Connection",
				name,
				force=True,
			)

	def configure_legacy_settings(self, **kwargs):
		settings = frappe.get_single(LEGACY_DOCTYPE)
		settings.client_id = kwargs.get("client_id", "legacy-client-id")
		settings.client_secret = kwargs.get("client_secret", "legacy-client-secret")
		settings.chain_id = kwargs.get("chain_id", "legacy-chain-id")
		settings.environment = kwargs.get("environment", "Production")
		settings.save()
		return settings

	def test_creates_one_connection_from_configured_settings(self):
		self.configure_legacy_settings()

		execute()

		connection_name = frappe.db.get_value(
			"Talabat Connection", {"legacy_migration_source": MIGRATION_SOURCE}
		)
		self.assertTrue(connection_name)

		connection = frappe.get_doc("Talabat Connection", connection_name)
		self.assertEqual(connection.client_id, "legacy-client-id")
		self.assertEqual(connection.chain_id, "legacy-chain-id")
		self.assertEqual(connection.environment, "Production")
		self.assertEqual(connection.get_password("client_secret"), "legacy-client-secret")

		# Legacy settings must not be touched/deleted by the migration.
		self.assertTrue(frappe.db.exists(LEGACY_DOCTYPE, LEGACY_DOCTYPE))

	def test_idempotent_does_not_duplicate_on_rerun(self):
		self.configure_legacy_settings()

		execute()
		execute()

		count = frappe.db.count("Talabat Connection", {"legacy_migration_source": MIGRATION_SOURCE})
		self.assertEqual(count, 1)

	def test_same_label_manual_connection_does_not_suppress_backfill(self):
		self.configure_legacy_settings()
		manual_connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": MIGRATED_CONNECTION_NAME,
				"client_id": "manual-client-id",
				"client_secret": "manual-client-secret",
				"chain_id": "manual-chain-id",
				"environment": "Sandbox",
			}
		).insert()
		self.manual_connection_names.append(manual_connection.name)

		execute()

		self.assertEqual(
			frappe.db.count("Talabat Connection", {"legacy_migration_source": MIGRATION_SOURCE}), 1
		)

	def test_skips_when_legacy_settings_not_configured(self):
		# chain_id is `reqd: 1` on Talabat Settings itself, so it can't be
		# blanked out via a normal save() the way client_id/client_secret can
		# - leaving it set and blanking client_id is enough to exercise the
		# patch's own "not configured" guard.
		self.configure_legacy_settings(client_id="")

		execute()

		self.assertFalse(
			frappe.db.exists("Talabat Connection", {"legacy_migration_source": MIGRATION_SOURCE})
		)
