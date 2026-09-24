# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest import mock

import frappe

from marketplace_integrations.services import item_import_jobs
from marketplace_integrations.testing_utils import CleanupTestCase


class MemoryCache:
	def __init__(self):
		self.values = {}

	def get_value(self, key):
		return self.values.get(key)

	def set_value(self, key, value, expires_in_sec):
		self.values[key] = value


class IntegrationTestItemImportJobs(CleanupTestCase):
	def test_queue_export_stores_an_owner_scoped_result_and_uses_the_long_queue(self):
		cache = MemoryCache()
		with (
			self.set_user("Administrator"),
			mock.patch.object(item_import_jobs.frappe, "cache", return_value=cache),
			mock.patch.object(item_import_jobs.frappe, "generate_hash", return_value="job-token"),
			mock.patch.object(item_import_jobs.frappe, "enqueue") as enqueue,
		):
			result = item_import_jobs.queue_export()

		self.assertEqual(result, {"job_id": "job-token", "status": "queued"})
		self.assertEqual(
			cache.get_value(item_import_jobs._key("job-token")),
			{"owner": "Administrator", "kind": "export", "status": "queued", "progress": 0},
		)
		enqueue.assert_called_once_with(
			item_import_jobs._run_export,
			queue="long",
			timeout=3600,
			job_id="marketplace-item-import-job-token",
			job_id_for_result="job-token",
			owner="Administrator",
			kind="export",
		)

	def test_status_is_visible_only_to_the_user_that_queued_the_job(self):
		cache = MemoryCache()
		cache.values[item_import_jobs._key("job-token")] = {
			"owner": "Administrator",
			"kind": "apply",
			"status": "completed",
			"progress": 100,
		}
		with mock.patch.object(item_import_jobs.frappe, "cache", return_value=cache):
			with self.set_user("Administrator"):
				self.assertEqual(
					item_import_jobs.get_status("job-token"),
					{"kind": "apply", "status": "completed", "progress": 100},
				)
			with self.set_user("Guest"):
				with self.assertRaises(frappe.PermissionError):
					item_import_jobs.get_status("job-token")

	def test_apply_job_publishes_progress_and_returns_the_import_result(self):
		cache = MemoryCache()
		cache.values[item_import_jobs._key("job-token")] = {"owner": "Administrator", "kind": "apply"}
		result = {"counts": {"new": 1}, "rows": [], "applied_count": 1}

		def apply_items_import(file_url, progress_callback):
			self.assertEqual(file_url, "/private/files/items.xlsx")
			progress_callback(1, 2)
			return result

		with (
			mock.patch.object(item_import_jobs.frappe, "cache", return_value=cache),
			mock.patch.object(item_import_jobs.frappe, "publish_realtime") as publish_realtime,
			mock.patch(
				"marketplace_integrations.api.item_import._apply_items_import",
				side_effect=apply_items_import,
			),
		):
			item_import_jobs._run_apply("job-token", "Administrator", "apply", "/private/files/items.xlsx")

		self.assertEqual(
			cache.get_value(item_import_jobs._key("job-token")),
			{
				"owner": "Administrator",
				"kind": "apply",
				"status": "completed",
				"progress": 100,
				"result": result,
			},
		)
		self.assertEqual(publish_realtime.call_count, 3)

	def test_progress_publishes_only_when_the_percentage_changes(self):
		cache = MemoryCache()
		cache.values[item_import_jobs._key("job-token")] = {
			"owner": "Administrator",
			"kind": "apply",
			"status": "running",
			"progress": 0,
		}
		with (
			mock.patch.object(item_import_jobs.frappe, "cache", return_value=cache),
			mock.patch.object(item_import_jobs.frappe, "publish_realtime") as publish_realtime,
		):
			item_import_jobs._update_progress("job-token", "Administrator", "apply", 1, 10_000)
			item_import_jobs._update_progress("job-token", "Administrator", "apply", 2, 10_000)
			item_import_jobs._update_progress("job-token", "Administrator", "apply", 100, 10_000)

		self.assertEqual(cache.get_value(item_import_jobs._key("job-token"))["progress"], 1)
		self.assertEqual(publish_realtime.call_count, 1)

	def test_worker_restores_kind_when_the_queued_state_has_expired(self):
		cache = MemoryCache()
		with (
			mock.patch.object(item_import_jobs.frappe, "cache", return_value=cache),
			mock.patch.object(item_import_jobs.frappe, "publish_realtime"),
		):
			item_import_jobs._update("job-token", "Administrator", "export", status="running", progress=10)

		self.assertEqual(
			cache.get_value(item_import_jobs._key("job-token")),
			{"owner": "Administrator", "kind": "export", "status": "running", "progress": 10},
		)
