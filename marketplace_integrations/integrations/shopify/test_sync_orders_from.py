# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The Shopify "Sync Orders From" cutoff (``Marketplace Account.shopify_orders_sync_from``).

Every Shopify call is mocked. The System Settings time zone is patched to a non-UTC zone
(``frappe.utils.data.get_system_timezone``) so a UTC-versus-site-zone mix-up fails: the default
fixture order is created at 10:00 UTC, which is 15:30 in Asia/Kolkata.
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify import orders
from marketplace_integrations.integrations.shopify.sync_start import (
	OrderBeforeSyncStart,
	clamp_listing_start,
	is_before_sync_start,
)
from marketplace_integrations.integrations.shopify.test_orders import (
	EXECUTE_TARGET,
	_order_gid,
	make_order_payload,
)
from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.tests.utils import make_role_user

SITE_TZ = "frappe.utils.data.get_system_timezone"
# 2026-09-27T10:00:00Z in Asia/Kolkata.
CREATED_LOCAL = "2026-09-27 15:30:00"
CREATED_LOCAL_PLUS_ONE_SECOND = "2026-09-27 15:30:01"


@contextmanager
def as_user(user):
	previous = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(previous)


class IntegrationTestSyncOrdersFrom(IntegrationTestCase):
	def setUp(self):
		patcher = patch(SITE_TZ, return_value="Asia/Kolkata")
		patcher.start()
		self.addCleanup(patcher.stop)
		self.account = make_shopify_account(f"Sync From {frappe.generate_hash(length=8)}")

	def tearDown(self):
		frappe.set_user("Administrator")
		# Several paths commit (failure logs, reconcile), so delete explicitly, orders before
		# the account (naming-series revert gotcha) and commit.
		for name in frappe.get_all(
			"Marketplace Order", filters={"marketplace_account": self.account.name}, pluck="name"
		):
			frappe.delete_doc("Marketplace Order", name, ignore_permissions=True, force=True)
		frappe.db.delete("Marketplace Sync Log", {"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Shopify Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _set_cutoff(self, value):
		self.account.db_set("shopify_orders_sync_from", value, update_modified=False)

	def _row(self, gid):
		return frappe.db.get_value("Marketplace Order", {"external_order_id": gid}, "name")

	def _import(self, gid, **overrides):
		with patch(EXECUTE_TARGET, return_value={"order": make_order_payload(gid, **overrides)}):
			return orders.import_order(self.account.name, gid)

	# -- helper ------------------------------------------------------------------------------

	def test_is_before_sync_start_compares_strictly(self):
		self.assertFalse(is_before_sync_start(self.account, CREATED_LOCAL))  # no cutoff
		self._set_cutoff(CREATED_LOCAL)
		self.assertFalse(is_before_sync_start(self.account, CREATED_LOCAL))  # exactly at the cutoff
		self.assertTrue(is_before_sync_start(self.account, "2026-09-27 15:29:59"))
		self.assertFalse(is_before_sync_start(self.account, None))
		self.assertTrue(is_before_sync_start(self.account.name, "2026-09-27 15:29:59"))  # by name

	def test_clamp_listing_start_never_goes_before_the_cutoff(self):
		self.assertEqual(
			clamp_listing_start(self.account, "2026-09-01 00:00:00"), frappe.utils.get_datetime("2026-09-01")
		)
		self._set_cutoff("2026-09-20 00:00:00")
		self.assertEqual(
			clamp_listing_start(self.account, "2026-09-01 00:00:00"),
			frappe.utils.get_datetime("2026-09-20 00:00:00"),
		)
		self.assertEqual(
			clamp_listing_start(self.account, "2026-10-01 00:00:00"),
			frappe.utils.get_datetime("2026-10-01 00:00:00"),
		)

	# -- import_order ------------------------------------------------------------------------

	def test_empty_cutoff_imports_exactly_as_before(self):
		gid = _order_gid(frappe.generate_hash(length=8))
		order = self._import(gid)
		self.assertEqual(order.doctype, "Marketplace Order")
		self.assertEqual(
			frappe.utils.get_datetime(order.received_on), frappe.utils.get_datetime(CREATED_LOCAL)
		)

	def test_order_before_the_cutoff_creates_no_row(self):
		self._set_cutoff(CREATED_LOCAL_PLUS_ONE_SECOND)
		gid = _order_gid(frappe.generate_hash(length=8))
		with self.assertRaises(OrderBeforeSyncStart):
			self._import(gid)
		self.assertIsNone(self._row(gid))

	def test_order_created_exactly_at_the_cutoff_is_imported(self):
		self._set_cutoff(CREATED_LOCAL)
		gid = _order_gid(frappe.generate_hash(length=8))
		self.assertTrue(self._import(gid))
		self.assertTrue(self._row(gid))

	def test_received_on_is_compared_in_site_time_not_utc(self):
		# The order was created at 10:00 UTC = 15:30 site time, so a 10:00:01 site-time cutoff is
		# before it. Compared as raw UTC wall-clock digits it would wrongly be skipped.
		self._set_cutoff("2026-09-27 10:00:01")
		gid = _order_gid(frappe.generate_hash(length=8))
		self.assertTrue(self._import(gid))

	def test_existing_row_keeps_updating_after_the_cutoff_is_set(self):
		gid = _order_gid(frappe.generate_hash(length=8))
		self._import(gid)
		self._set_cutoff("2026-12-01 00:00:00")  # far after the order's creation

		updated = self._import(gid, updated_at="2026-09-27T11:00:00Z", displayFinancialStatus="REFUNDED")

		self.assertEqual(updated.external_financial_status, "REFUNDED")
		self.assertEqual(frappe.db.count("Marketplace Order", {"external_order_id": gid}), 1)

	# -- range import / reconcile ------------------------------------------------------------

	def _range_nodes(self, *gids):
		return {
			"orders": {
				"pageInfo": {"hasNextPage": False},
				"nodes": [{"id": gid, "updatedAt": "2026-09-28T10:00:00Z"} for gid in gids],
			}
		}

	def test_range_import_counts_old_orders_as_skipped_not_errors(self):
		self._set_cutoff("2026-09-27 00:00:00")  # site time; the "new" order is 15:30 that day
		old_gid = _order_gid(frappe.generate_hash(length=8))
		new_gid = _order_gid(frappe.generate_hash(length=8))

		def fake_execute(query, variables=None):
			if "ShopifyOrderIds" in query:
				return self._range_nodes(old_gid, new_gid)
			gid = variables["id"]
			created = "2026-09-20T10:00:00Z" if gid == old_gid else "2026-09-27T10:00:00Z"
			return {"order": make_order_payload(gid, createdAt=created)}

		skipped = []
		with patch(EXECUTE_TARGET, side_effect=fake_execute):
			client = orders.ShopifyClient(self.account.connection)
			imported, errors = orders.import_orders_updated_between(
				self.account, client, "2026-09-01T00:00:00Z", skipped=skipped
			)

		self.assertEqual(errors, [])
		self.assertEqual(len(imported), 1)
		self.assertEqual(skipped, [old_gid])
		self.assertIsNone(self._row(old_gid))
		self.assertTrue(self._row(new_gid))
		self.assertFalse(
			frappe.db.exists(
				"Marketplace Sync Log", {"marketplace_account": self.account.name, "status": "Error"}
			)
		)

	def test_reconcile_lists_from_the_cutoff_and_keeps_the_watermark(self):
		self.account.db_set("shopify_orders_reconciled_through", "2026-09-01 00:00:00", update_modified=False)
		self._set_cutoff("2026-09-20 00:00:00")

		with patch.object(orders, "import_orders_updated_between", return_value=([], [])) as mocked:
			orders.reconcile_all_accounts_orders(self.account.name)

		# 2026-09-20 00:00 Asia/Kolkata is 2026-09-19 18:30 UTC.
		self.assertEqual(mocked.call_args.args[2], "2026-09-19T18:30:00Z")
		# The run still advances the watermark from "now" as usual; changing the cutoff alone does
		# not reset it.
		self.assertGreater(
			frappe.db.get_value(
				"Marketplace Account", self.account.name, "shopify_orders_reconciled_through"
			),
			frappe.utils.get_datetime("2026-09-02 00:00:00"),
		)

	def test_reconcile_keeps_a_newer_watermark_over_an_older_cutoff(self):
		self.account.db_set("shopify_orders_reconciled_through", "2026-09-25 00:00:00", update_modified=False)
		self._set_cutoff("2026-09-20 00:00:00")

		with patch.object(orders, "import_orders_updated_between", return_value=([], [])) as mocked:
			orders.reconcile_all_accounts_orders(self.account.name)

		self.assertEqual(mocked.call_args.args[2], "2026-09-24T18:30:00Z")

	def test_nightly_summary_reports_skipped_orders(self):
		self._set_cutoff("2026-09-20 00:00:00")

		def fake_import(account, client, start, skipped=None, **kwargs):
			skipped.extend(["a", "b"])
			return [], []

		with patch.object(orders, "import_orders_updated_between", side_effect=fake_import):
			orders.reconcile_all_accounts_orders(self.account.name, catch_up_unlinked=True)

		log = frappe.get_last_doc(
			"Marketplace Sync Log",
			filters={"marketplace_account": self.account.name, "sync_type": "Order Import"},
		)
		self.assertEqual(frappe.parse_json(log.response_data).get("skipped_before_sync_start"), 2)

	def test_manual_range_result_shows_skipped_count(self):
		job = f"job-{frappe.generate_hash(length=6)}"

		def fake_import(account, client, start, end=None, skipped=None, **kwargs):
			skipped.extend(["a", "b", "c"])
			return [], []

		with patch.object(orders, "import_orders_updated_between", side_effect=fake_import):
			orders.run_order_import(job, "Administrator", self.account.name, "s", "e")

		state = frappe.cache().get_value(orders._import_cache_key(job))
		self.assertEqual(state["result"]["skipped_mappings"], 3)
		self.assertEqual(state["result"]["failed"], 0)

	def test_manual_range_ending_before_the_cutoff_is_refused(self):
		self._set_cutoff("2026-09-20 00:00:00")
		with patch.object(orders.frappe, "enqueue") as enqueue:
			with self.assertRaisesRegex(frappe.ValidationError, "Sync Orders From"):
				orders.queue_order_import(self.account.name, "2026-09-01 00:00:00", "2026-09-19 23:59:59")
		enqueue.assert_not_called()

	def test_manual_range_reaching_the_cutoff_starts_at_the_cutoff(self):
		self._set_cutoff("2026-09-20 00:00:00")
		with patch.object(orders.frappe, "enqueue") as enqueue:
			orders.queue_order_import(self.account.name, "2026-09-01 00:00:00", "2026-09-20 00:00:00")
		self.assertEqual(enqueue.call_args.kwargs["start"], "2026-09-19T18:30:00Z")
		self.assertEqual(enqueue.call_args.kwargs["end"], "2026-09-19T18:30:00Z")

	def test_manual_range_without_a_cutoff_is_unchanged(self):
		with patch.object(orders.frappe, "enqueue") as enqueue:
			orders.queue_order_import(self.account.name, "2026-09-01 00:00:00", "2026-09-02 00:00:00")
		self.assertEqual(enqueue.call_args.kwargs["start"], "2026-08-31T18:30:00Z")

	# -- timestamp conversion ----------------------------------------------------------------

	def test_naive_datetime_is_read_in_the_site_time_zone(self):
		self.assertEqual(orders._as_shopify_timestamp(CREATED_LOCAL), "2026-09-27T10:00:00Z")
		self.assertEqual(
			orders._as_shopify_timestamp(frappe.utils.get_datetime(CREATED_LOCAL)), "2026-09-27T10:00:00Z"
		)

	def test_naive_datetime_is_unchanged_when_the_site_is_utc(self):
		with patch(SITE_TZ, return_value="UTC"):
			self.assertEqual(orders._as_shopify_timestamp(CREATED_LOCAL), "2026-09-27T15:30:00Z")

	def test_aware_datetime_is_not_shifted_again(self):
		self.assertEqual(orders._as_shopify_timestamp("2026-09-27T10:00:00Z"), "2026-09-27T10:00:00Z")

	def test_conversion_does_not_depend_on_the_server_clock_zone(self):
		import os
		import time

		previous = os.environ.get("TZ")
		os.environ["TZ"] = "America/New_York"
		time.tzset()
		try:
			self.assertEqual(orders._as_shopify_timestamp(CREATED_LOCAL), "2026-09-27T10:00:00Z")
		finally:
			if previous is None:
				os.environ.pop("TZ", None)
			else:
				os.environ["TZ"] = previous
			time.tzset()


class IntegrationTestSyncOrdersFromPermission(IntegrationTestCase):
	"""The field is permission level 1: System Manager edits it, platform users only read it."""

	def setUp(self):
		self.account = make_shopify_account(f"Sync From Perm {frappe.generate_hash(length=8)}")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Shopify Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _user(self, label, role):
		email, _role = make_role_user(self, label, {})
		frappe.get_doc("User", email).add_roles(role)
		frappe.clear_cache(user=email)
		return email

	def _save_cutoff_as(self, user, value):
		with as_user(user):
			doc = frappe.get_doc("Marketplace Account", self.account.name)
			doc.shopify_orders_sync_from = value
			doc.save()
		return frappe.db.get_value("Marketplace Account", self.account.name, "shopify_orders_sync_from")

	def test_shopify_user_cannot_change_the_cutoff(self):
		user = self._user("Cutoff Shopify Staff", "Shopify User")
		with as_user(user):
			doc = frappe.get_doc("Marketplace Account", self.account.name)
			self.assertNotIn(1, doc.get_permlevel_access("write"))
			self.assertIn(1, doc.get_permlevel_access("read"))
		self.assertIsNone(self._save_cutoff_as(user, "2026-09-20 00:00:00"))

	def test_system_manager_can_change_the_cutoff(self):
		user = self._user("Cutoff Manager", "System Manager")
		with as_user(user):
			doc = frappe.get_doc("Marketplace Account", self.account.name)
			self.assertIn(1, doc.get_permlevel_access("write"))
		self.assertEqual(str(self._save_cutoff_as(user, "2026-09-20 00:00:00")), "2026-09-20 00:00:00")
