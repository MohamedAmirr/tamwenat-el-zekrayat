# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the shared upsert_marketplace_order() write model
(services/marketplace_orders.py), in particular #109's concurrent-first-
insert race fallback - the UniqueValidationError branch that reloads and
re-applies the incoming payload to the winning row rather than raising."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_datetime

from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.services import marketplace_orders as marketplace_orders_module
from marketplace_integrations.services.marketplace_attention import get_resolved_import_failures
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order


def _is_stale_by_updated_at(incoming_updated_at):
	"""A minimal is_stale() callable in the same shape orders.py's
	import_order() builds: staler than the existing row's stored
	latest_payload["updatedAt"] means "do not apply"."""

	def is_stale(existing):
		stored_payload = existing.latest_payload
		if isinstance(stored_payload, str):
			try:
				stored_payload = frappe.parse_json(stored_payload)
			except (TypeError, ValueError):
				return False
		stored_updated_at = (
			(stored_payload or {}).get("updatedAt") if isinstance(stored_payload, dict) else None
		)
		if not stored_updated_at or not incoming_updated_at:
			return False
		return get_datetime(incoming_updated_at) < get_datetime(stored_updated_at)

	return is_stale


class IntegrationTestUpsertMarketplaceOrderConcurrentRace(IntegrationTestCase):
	def setUp(self):
		self.account = make_shopify_account(f"Order Race {frappe.generate_hash(length=8)}")

	def test_concurrent_first_insert_race_does_not_let_older_payload_win(self):
		# #109's own locked-spec acceptance criterion: "A simulated concurrent
		# -first-insert race (two workers both hitting the UniqueValidationError
		# fallback) does not let the older payload win."
		external_order_id = f"race-order-{frappe.generate_hash(length=8)}"

		def make_payload(updated_at, status):
			return {
				"external_order_id": external_order_id,
				"latest_payload": {"updatedAt": updated_at},
				"external_financial_status": status,
				"items": [],
			}

		newer_payload = make_payload("2026-09-27T12:00:00Z", "PAID")
		older_payload = make_payload("2026-09-27T09:00:00Z", "PENDING")

		# "Worker 1" wins the race outright: its insert lands first,
		# uncontested, via the normal frappe.new_doc()/insert() path.
		winner = upsert_marketplace_order(self.account.name, newer_payload)
		self.assertEqual(winner.external_financial_status, "PAID")
		self.assertEqual(frappe.db.count("Marketplace Order", {"external_order_id": external_order_id}), 1)

		# "Worker 2" simulates having read the identity-key lookup BEFORE
		# worker 1's insert committed (the initial frappe.db.get_value() name
		# lookup returns None despite the row already existing), so it takes
		# the frappe.new_doc()/insert() path too, genuinely colliding with
		# worker 1's already-committed row on the unique order_identity_key
		# index - forced here since a single test process can't otherwise
		# reproduce two overlapping uncommitted transactions.
		real_get_value = frappe.db.get_value
		get_value_calls = {"n": 0}

		def race_get_value(*args, **kwargs):
			get_value_calls["n"] += 1
			if get_value_calls["n"] == 1:
				return None
			return real_get_value(*args, **kwargs)

		real_new_doc = frappe.new_doc

		def race_new_doc(doctype, *args, **kwargs):
			doc = real_new_doc(doctype, *args, **kwargs)
			if doctype == "Marketplace Order":

				def failing_insert(*a, **k):
					raise frappe.UniqueValidationError("simulated concurrent-first-insert race")

				doc.insert = failing_insert
			return doc

		with (
			patch.object(marketplace_orders_module.frappe.db, "get_value", side_effect=race_get_value),
			patch.object(marketplace_orders_module.frappe, "new_doc", side_effect=race_new_doc),
		):
			result = upsert_marketplace_order(
				self.account.name,
				older_payload,
				is_stale=_is_stale_by_updated_at(older_payload["latest_payload"]["updatedAt"]),
			)

		# Worker 2 must have been routed onto worker 1's already-committed
		# row, not created a second one, and the older payload it carried
		# must not have overwritten the winner's fields.
		self.assertEqual(result.name, winner.name)
		result.reload()
		self.assertEqual(result.external_financial_status, "PAID")
		self.assertEqual(frappe.db.count("Marketplace Order", {"external_order_id": external_order_id}), 1)

	def test_unlinked_cancelled_shopify_order_stays_cancelled_on_later_import(self):
		payload = {
			"external_order_id": f"cancelled-{frappe.generate_hash(length=8)}",
			"latest_payload": {"cancelledAt": "2026-10-24T10:00:00Z"},
			"cancelled_at": "2026-10-24 10:00:00",
			"external_financial_status": "PENDING",
			"items": [],
		}
		order = upsert_marketplace_order(self.account.name, payload)
		order.erpnext_sync_status = "Cancelled"
		order.last_sync_error = None
		order.save()

		payload["external_financial_status"] = "REFUNDED"
		payload["latest_payload"] = {"cancelledAt": "2026-10-24T10:00:00Z", "updatedAt": "2026-10-24T11:00:00Z"}
		refreshed = upsert_marketplace_order(self.account.name, payload)

		self.assertEqual(refreshed.erpnext_sync_status, "Cancelled")
		self.assertFalse(refreshed.last_sync_error)

	def test_concurrent_first_insert_race_still_applies_a_genuinely_newer_payload(self):
		# The fallback branch's is_stale re-check must not block a payload
		# that is genuinely newer than what's already stored - only staler
		# payloads are rejected.
		external_order_id = f"race-order-newer-{frappe.generate_hash(length=8)}"

		def make_payload(updated_at, status):
			return {
				"external_order_id": external_order_id,
				"latest_payload": {"updatedAt": updated_at},
				"external_financial_status": status,
				"items": [],
			}

		first_payload = make_payload("2026-09-27T09:00:00Z", "PENDING")
		newer_payload = make_payload("2026-09-27T12:00:00Z", "PAID")

		first = upsert_marketplace_order(self.account.name, first_payload)
		self.assertEqual(first.external_financial_status, "PENDING")

		real_get_value = frappe.db.get_value
		get_value_calls = {"n": 0}

		def race_get_value(*args, **kwargs):
			get_value_calls["n"] += 1
			if get_value_calls["n"] == 1:
				return None
			return real_get_value(*args, **kwargs)

		real_new_doc = frappe.new_doc

		def race_new_doc(doctype, *args, **kwargs):
			doc = real_new_doc(doctype, *args, **kwargs)
			if doctype == "Marketplace Order":

				def failing_insert(*a, **k):
					raise frappe.UniqueValidationError("simulated concurrent-first-insert race")

				doc.insert = failing_insert
			return doc

		with (
			patch.object(marketplace_orders_module.frappe.db, "get_value", side_effect=race_get_value),
			patch.object(marketplace_orders_module.frappe, "new_doc", side_effect=race_new_doc),
		):
			result = upsert_marketplace_order(
				self.account.name,
				newer_payload,
				is_stale=_is_stale_by_updated_at(newer_payload["latest_payload"]["updatedAt"]),
			)

		result.reload()
		self.assertEqual(result.name, first.name)
		self.assertEqual(result.external_financial_status, "PAID")


class IntegrationTestUpsertMarketplaceOrderSnapshotRace(IntegrationTestCase):
	"""A locking lookup (current read) can find a row committed after this
	transaction's read view began; a plain get_doc (consistent read) then raises
	DoesNotExistError. One process cannot hold two transactions, so the
	consistent read is simulated: a plain load raises, a ``for_update`` load works."""

	def setUp(self):
		self.account = make_shopify_account(f"Order Snapshot {frappe.generate_hash(length=8)}")
		self.external_order_id = f"snapshot-order-{frappe.generate_hash(length=8)}"
		self.payload = {
			"external_order_id": self.external_order_id,
			"latest_payload": {"updatedAt": "2026-11-05T10:00:00Z"},
			"external_financial_status": "PENDING",
			"items": [],
		}
		self.order = upsert_marketplace_order(self.account.name, self.payload)

	def _snapshot_blind_get_doc(self, calls):
		real_get_doc = frappe.get_doc

		def get_doc(*args, **kwargs):
			if args and args[0] == "Marketplace Order" and len(args) > 1:
				calls.append(bool(kwargs.get("for_update")))
				if not kwargs.get("for_update"):
					raise frappe.DoesNotExistError("Marketplace Order not visible to this read view")
			return real_get_doc(*args, **kwargs)

		return get_doc

	def test_locked_upsert_loads_the_order_with_a_locking_read(self):
		calls = []
		updated = {**self.payload, "external_financial_status": "PAID"}
		with patch.object(
			marketplace_orders_module.frappe, "get_doc", side_effect=self._snapshot_blind_get_doc(calls)
		):
			result = upsert_marketplace_order(
				self.account.name,
				updated,
				is_stale=_is_stale_by_updated_at("2026-11-05T11:00:00Z"),
			)

		# The patch sits on the global frappe module, so Frappe's own load_doc_before_save()
		# (document.py, for_update=True) during save() is recorded too. Assert that no plain
		# load happened and that the upsert's own load (the first call) was locking.
		self.assertTrue(calls)
		self.assertTrue(calls[0])
		self.assertNotIn(False, calls)
		self.assertEqual(result.name, self.order.name)
		result.reload()
		self.assertEqual(result.external_financial_status, "PAID")
		self.assertEqual(
			frappe.db.count("Marketplace Order", {"external_order_id": self.external_order_id}), 1
		)

	def test_unlocked_upsert_keeps_the_plain_load(self):
		calls = []
		real_get_doc = frappe.get_doc

		def recording_get_doc(*args, **kwargs):
			if args and args[0] == "Marketplace Order" and len(args) > 1:
				calls.append(bool(kwargs.get("for_update")))
			return real_get_doc(*args, **kwargs)

		with patch.object(marketplace_orders_module.frappe, "get_doc", side_effect=recording_get_doc):
			upsert_marketplace_order(self.account.name, self.payload)

		self.assertEqual(calls, [False])

	def test_order_missing_for_a_locking_read_still_raises(self):
		with (
			patch.object(
				marketplace_orders_module.frappe.db, "get_value", return_value="MKTORD-DOES-NOT-EXIST"
			),
			self.assertRaises(frappe.DoesNotExistError),
		):
			upsert_marketplace_order(
				self.account.name, self.payload, is_stale=_is_stale_by_updated_at("2026-11-05T11:00:00Z")
			)


class IntegrationTestUpsertMarketplaceOrderNoopResave(IntegrationTestCase):
	def setUp(self):
		self.account = make_shopify_account(f"Order Noop {frappe.generate_hash(length=8)}")
		self.order_name = None

	def tearDown(self):
		if self.order_name:
			frappe.db.delete("Version", {"ref_doctype": "Marketplace Order", "docname": self.order_name})
			frappe.delete_doc("Marketplace Order", self.order_name, force=True)

	def test_reimporting_identical_payload_leaves_no_version_entry(self):
		# A platform that omits numeric/Check fields (e.g. Noon: weights,
		# customer_latitude, is_preorder) must not flip the stored 0 to None
		# on re-import: Version logs 0 -> None as a change on every refresh.
		payload = {
			"external_order_id": f"noop-order-{frappe.generate_hash(length=8)}",
			"latest_payload": {"id": 1},
			"items": [{"external_item_id": "sku:A:S:I", "product_name": "A", "current_quantity": 2}],
		}
		order = upsert_marketplace_order(self.account.name, payload)
		self.order_name = order.name

		# Document.save() skips Version tracking while frappe.in_test is set.
		with patch.object(frappe, "in_test", False):
			upsert_marketplace_order(self.account.name, payload)

		versions = frappe.get_all(
			"Version",
			filters={"ref_doctype": "Marketplace Order", "docname": order.name},
			fields=["data"],
		)
		self.assertEqual(versions, [], [v.data for v in versions])

	def test_reimporting_identical_payload_does_not_touch_modified(self):
		payload = {
			"external_order_id": f"modified-order-{frappe.generate_hash(length=8)}",
			"received_on": "2026-11-03 23:01:51",
			"order_total": 403,
			"latest_payload": {"id": 1},
			"items": [{"external_item_id": "sku:A:S:I", "product_name": "A", "current_quantity": 1}],
		}
		order = upsert_marketplace_order(self.account.name, payload)
		self.order_name = order.name
		old_modified = "2026-01-01 00:00:00"
		frappe.db.set_value("Marketplace Order", order.name, "modified", old_modified, update_modified=False)

		upsert_marketplace_order(self.account.name, payload)

		stored = frappe.db.get_value("Marketplace Order", order.name, ["modified", "received_on"])
		self.assertEqual(str(stored[0]), old_modified)
		self.assertEqual(str(stored[1]), "2026-11-03 23:01:51")

	def test_a_changed_payload_still_saves_and_updates_modified(self):
		payload = {
			"external_order_id": f"changed-order-{frappe.generate_hash(length=8)}",
			"order_total": 403,
			"latest_payload": {"id": 1},
			"items": [{"external_item_id": "sku:A:S:I", "product_name": "A", "current_quantity": 1}],
		}
		order = upsert_marketplace_order(self.account.name, payload)
		self.order_name = order.name
		old_modified = "2026-01-01 00:00:00"
		frappe.db.set_value("Marketplace Order", order.name, "modified", old_modified, update_modified=False)

		# A changed field, then a new line: each is a real change and must be saved.
		upsert_marketplace_order(self.account.name, {**payload, "order_total": 450})
		self.assertNotEqual(
			str(frappe.db.get_value("Marketplace Order", order.name, "modified")), old_modified
		)
		self.assertEqual(frappe.db.get_value("Marketplace Order", order.name, "order_total"), 450)

		frappe.db.set_value("Marketplace Order", order.name, "modified", old_modified, update_modified=False)
		more_items = [*payload["items"], {"external_item_id": "sku:B:S:I", "product_name": "B"}]
		upsert_marketplace_order(self.account.name, {**payload, "order_total": 450, "items": more_items})
		self.assertNotEqual(
			str(frappe.db.get_value("Marketplace Order", order.name, "modified")), old_modified
		)
		self.assertEqual(frappe.db.count("Marketplace Order Item", {"parent": order.name}), 2)

	def _modified(self, name):
		return str(frappe.db.get_value("Marketplace Order", name, "modified"))

	def _age(self, name):
		old_modified = "2026-01-01 00:00:00"
		frappe.db.set_value("Marketplace Order", name, "modified", old_modified, update_modified=False)
		return old_modified

	def test_empty_platform_reference_equals_the_stored_external_id_fallback(self):
		# before_save stores the External Order ID when the payload has no reference, so an
		# empty payload value on the next pull is not a change.
		payload = {
			"external_order_id": f"ref-order-{frappe.generate_hash(length=8)}",
			"platform_order_reference": "",
			"latest_payload": {"id": 1},
			"items": [],
		}
		order = upsert_marketplace_order(self.account.name, payload)
		self.order_name = order.name
		self.assertEqual(order.platform_order_reference, payload["external_order_id"])
		old_modified = self._age(order.name)

		upsert_marketplace_order(self.account.name, payload)

		self.assertEqual(self._modified(order.name), old_modified)

	def test_cancelled_before_sync_shopify_order_saves_when_its_status_fields_change(self):
		payload = {
			"external_order_id": f"cancel-order-{frappe.generate_hash(length=8)}",
			"latest_payload": {"cancelledAt": "2026-10-24T10:00:00Z"},
			"cancelled_at": "2026-10-24 10:00:00",
			"items": [],
		}
		order = upsert_marketplace_order(self.account.name, payload)
		self.order_name = order.name
		frappe.db.set_value(
			"Marketplace Order",
			order.name,
			{"erpnext_sync_status": "Cancelled", "last_sync_error": "stale"},
			update_modified=False,
		)
		old_modified = self._age(order.name)

		upsert_marketplace_order(self.account.name, payload)

		self.assertNotEqual(self._modified(order.name), old_modified)
		self.assertFalse(frappe.db.get_value("Marketplace Order", order.name, "last_sync_error"))

	def test_noop_pull_records_the_import_time_without_touching_modified(self):
		payload = {
			"external_order_id": f"imported-order-{frappe.generate_hash(length=8)}",
			"latest_payload": {"id": 1},
			"items": [],
		}
		order = upsert_marketplace_order(self.account.name, payload)
		self.order_name = order.name
		old_modified = self._age(order.name)
		frappe.db.set_value(
			"Marketplace Order", order.name, "last_imported_on", "2026-01-01 00:00:00", update_modified=False
		)

		result = upsert_marketplace_order(self.account.name, payload)

		self.assertEqual(self._modified(order.name), old_modified)
		stored = frappe.db.get_value("Marketplace Order", order.name, "last_imported_on")
		self.assertGreater(get_datetime(stored), get_datetime("2026-01-01 00:00:00"))
		self.assertEqual(get_datetime(result.last_imported_on), get_datetime(stored))

	def test_clean_reimport_resolves_an_earlier_order_import_failure(self):
		external_order_id = f"recovery-order-{frappe.generate_hash(length=8)}"
		payload = {"external_order_id": external_order_id, "latest_payload": {"id": 1}, "items": []}
		order = upsert_marketplace_order(self.account.name, payload)
		self.order_name = order.name
		# The order predates the failure and nothing about it changes afterwards.
		self._age(order.name)
		frappe.db.set_value(
			"Marketplace Order",
			order.name,
			{"creation": "2026-01-01 00:00:00", "last_imported_on": "2026-01-01 00:00:00"},
			update_modified=False,
		)
		log = frappe._dict(
			name="LOG-1",
			marketplace_account=self.account.name,
			request_data=frappe.as_json({"order_gid": external_order_id}),
			attempted_at="2026-06-01 00:00:00",
			creation="2026-06-01 00:00:00",
		)
		self.assertEqual(get_resolved_import_failures([log]), set())

		upsert_marketplace_order(self.account.name, payload)

		self.assertEqual(get_resolved_import_failures([log]), {"LOG-1"})
		self.assertEqual(self._modified(order.name), "2026-01-01 00:00:00")
