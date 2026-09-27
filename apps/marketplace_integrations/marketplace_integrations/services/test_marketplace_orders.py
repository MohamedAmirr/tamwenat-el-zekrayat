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
			except TypeError, ValueError:
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
