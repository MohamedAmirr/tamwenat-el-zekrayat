# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import csv
import io
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.talabat import catalog_feedback
from marketplace_integrations.integrations.talabat.catalog_feedback import (
	CatalogFeedbackError,
	process_catalog_feedback,
)

CSV_COLUMNS = [
	"vendorId",
	"sku",
	"barcode",
	"images",
	"categories",
	"price",
	"isSoldByWeight",
	"baseWeight",
	"averageWeightPerPiece",
	"title::en_GB",
	"feedback",
	"details",
]


def _csv_bytes(rows):
	buffer = io.StringIO()
	writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS)
	writer.writeheader()
	for row in rows:
		full_row = dict.fromkeys(CSV_COLUMNS, "")
		full_row.update(row)
		writer.writerow(full_row)
	return buffer.getvalue().encode("utf-8")


def _download_response(rows, status_code=200, headers=None, redirect_to=None):
	response = MagicMock()
	response.status_code = status_code
	response.ok = 200 <= status_code < 400 and status_code not in (301, 302, 303, 307, 308)
	response.headers = headers or {}
	if redirect_to:
		response.headers = {**response.headers, "Location": redirect_to}
	body = _csv_bytes(rows)

	def iter_content(chunk_size=65536):
		for start in range(0, len(body), chunk_size):
			yield body[start : start + chunk_size]

	response.iter_content.side_effect = iter_content
	# catalog_feedback.py uses the response as `with http_request(...) as response:`
	# to guarantee the connection is always released - a bare MagicMock's default
	# __enter__ returns a *different* child mock, not this configured instance
	# (see docs/frappe-conventions.md), so it must be wired explicitly.
	response.__enter__.return_value = response
	response.__exit__.return_value = False
	return response


class IntegrationTestTalabatCatalogFeedback(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Catalog Feedback Connection {self.suffix}",
				"client_id": "test-client-id",
				"client_secret": "test-client-secret",
				"chain_id": f"test-chain-{self.suffix}",
				"enabled": 1,
			}
		).insert()
		self.account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Catalog Feedback Account {self.suffix}",
				"platform": "Talabat",
				"external_account_id": f"vendor-{self.suffix}",
				"connection_doctype": "Talabat Connection",
				"connection": self.connection.name,
				"enabled": 1,
			}
		).insert()
		self.item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"TEST-CATALOG-FEEDBACK-{self.suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}
		).insert()

	def _make_mapping(self, sku, job_id, sync_status="Queued", **kwargs):
		doc = {
			"doctype": "Marketplace Product Mapping",
			"marketplace_account": self.account.name,
			"item": self.item.name,
			"external_reference": sku,
			"enabled": 1,
			"sync_status": sync_status,
			"last_sync_reference": job_id,
		}
		doc.update(kwargs)
		return frappe.get_doc(doc).insert()

	def _payload(self, job_id="job-001", download_url="https://example.com/feedback/job.csv", **overrides):
		payload = {
			"job_id": job_id,
			"status": "COMPLETED",
			"platform_vendor_id": self.account.external_account_id,
			"download_url": download_url,
		}
		payload.update(overrides)
		return payload

	def _run(self, payload, rows, **response_kwargs):
		with patch.object(
			catalog_feedback, "http_request", return_value=_download_response(rows, **response_kwargs)
		):
			process_catalog_feedback(payload)

	# -- schema / status validation --------------------------------------

	def test_non_completed_status_raises_and_does_not_mutate_mapping(self):
		mapping = self._make_mapping("sku-1", "job-001")

		with patch.object(catalog_feedback, "http_request") as request:
			with self.assertRaises(CatalogFeedbackError):
				process_catalog_feedback(self._payload(status="PENDING"))
			request.assert_not_called()

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Queued")

	def test_missing_job_id_raises(self):
		with self.assertRaises(CatalogFeedbackError):
			process_catalog_feedback(self._payload(job_id=""))

	def test_unknown_vendor_raises_and_does_not_mutate_mapping(self):
		mapping = self._make_mapping("sku-1", "job-001")

		with self.assertRaises(CatalogFeedbackError):
			process_catalog_feedback(self._payload(platform_vendor_id="unknown-vendor"))

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Queued")

	def test_non_https_download_url_raises_without_requesting(self):
		with patch.object(catalog_feedback, "http_request") as request:
			with self.assertRaises(CatalogFeedbackError):
				process_catalog_feedback(self._payload(download_url="http://example.com/job.csv"))
			request.assert_not_called()

	# -- correlation -------------------------------------------------------

	def test_success_feedback_marks_mapping_synced(self):
		mapping = self._make_mapping("sku-1", "job-001")
		self._run(
			self._payload(job_id="job-001"),
			[
				{
					"vendorId": self.account.external_account_id,
					"sku": "sku-1",
					"feedback": "Product Assignment Successful",
				}
			],
		)

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Synced")
		self.assertFalse(mapping.last_error)

	def test_unknown_feedback_marks_mapping_error(self):
		mapping = self._make_mapping("sku-1", "job-001")
		self._run(
			self._payload(job_id="job-001"),
			[
				{
					"vendorId": self.account.external_account_id,
					"sku": "sku-1",
					"feedback": "Product Assignment Failed",
					"details": '{"category": "not allowed"}',
				}
			],
		)

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Error")
		self.assertIn("Product Assignment Failed", mapping.last_error)
		self.assertIn("not allowed", mapping.last_error)

	def test_blank_feedback_marks_mapping_error_not_synced(self):
		mapping = self._make_mapping("sku-1", "job-001")
		self._run(
			self._payload(job_id="job-001"),
			[{"vendorId": self.account.external_account_id, "sku": "sku-1", "feedback": ""}],
		)

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Error")

	def test_vendor_mismatch_row_is_skipped(self):
		mapping = self._make_mapping("sku-1", "job-001")
		self._run(
			self._payload(job_id="job-001"),
			[{"vendorId": "some-other-vendor", "sku": "sku-1", "feedback": "Product Assignment Successful"}],
		)

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Queued")

	def test_stale_job_id_does_not_overwrite_newer_mapping_state(self):
		mapping = self._make_mapping("sku-1", "job-002", sync_status="Queued")
		self._run(
			self._payload(job_id="job-001"),
			[
				{
					"vendorId": self.account.external_account_id,
					"sku": "sku-1",
					"feedback": "Product Assignment Successful",
				}
			],
		)

		mapping.reload()
		# The mapping has since been resubmitted under job-002; job-001's
		# feedback must not finalize it.
		self.assertEqual(mapping.sync_status, "Queued")
		self.assertEqual(mapping.last_sync_reference, "job-002")

	def test_mapping_missing_from_csv_remains_queued(self):
		queued_but_absent = self._make_mapping("sku-absent", "job-001")
		present = self._make_mapping("sku-present", "job-001")
		self._run(
			self._payload(job_id="job-001"),
			[
				{
					"vendorId": self.account.external_account_id,
					"sku": "sku-present",
					"feedback": "Product Assignment Successful",
				}
			],
		)

		queued_but_absent.reload()
		present.reload()
		self.assertEqual(queued_but_absent.sync_status, "Queued")
		self.assertEqual(present.sync_status, "Synced")

	def test_correlates_on_non_blank_external_sku_not_external_reference(self):
		# external_sku differs from external_reference - this is what
		# catalog_sync.py's build_catalog_product actually sends as the
		# outbound Talabat sku (external_sku wins when non-blank), so the
		# feedback CSV's sku column must correlate against external_sku here,
		# not external_reference.
		mapping = self._make_mapping("ref-100", "job-001", external_sku="outbound-sku-100")
		self._run(
			self._payload(job_id="job-001"),
			[
				{
					"vendorId": self.account.external_account_id,
					"sku": "outbound-sku-100",
					"feedback": "Product Assignment Successful",
				}
			],
		)

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Synced")

	def test_mapping_with_non_blank_external_sku_is_not_matched_by_its_external_reference(self):
		# A feedback row whose sku equals this mapping's external_reference
		# (but not its real outbound external_sku) must never be treated as
		# feedback for this mapping - Talabat never actually received that
		# value as this mapping's sku.
		mapping = self._make_mapping("ref-200", "job-001", external_sku="outbound-sku-200")
		self._run(
			self._payload(job_id="job-001"),
			[
				{
					"vendorId": self.account.external_account_id,
					"sku": "ref-200",
					"feedback": "Product Assignment Successful",
				}
			],
		)

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Queued")

	def test_blank_external_sku_still_correlates_on_external_reference(self):
		# No external_sku set - the effective outbound sku falls back to
		# external_reference, same as build_catalog_product.
		mapping = self._make_mapping("sku-1", "job-001")
		self._run(
			self._payload(job_id="job-001"),
			[
				{
					"vendorId": self.account.external_account_id,
					"sku": "sku-1",
					"feedback": "Product Assignment Successful",
				}
			],
		)

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Synced")

	def test_other_account_mapping_with_same_sku_is_not_touched(self):
		other_connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Other Catalog Feedback Connection {self.suffix}",
				"client_id": "test-client-id",
				"client_secret": "test-client-secret",
				"chain_id": f"test-chain-other-{self.suffix}",
				"enabled": 1,
			}
		).insert()
		other_account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Other Catalog Feedback Account {self.suffix}",
				"platform": "Talabat",
				"external_account_id": f"other-vendor-{self.suffix}",
				"connection_doctype": "Talabat Connection",
				"connection": other_connection.name,
				"enabled": 1,
			}
		).insert()
		other_mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": other_account.name,
				"item": self.item.name,
				"external_reference": "shared-sku",
				"enabled": 1,
				"sync_status": "Queued",
				"last_sync_reference": "job-001",
			}
		).insert()
		mine = self._make_mapping("shared-sku", "job-001")

		self._run(
			self._payload(job_id="job-001"),
			[
				{
					"vendorId": self.account.external_account_id,
					"sku": "shared-sku",
					"feedback": "Product Assignment Successful",
				}
			],
		)

		mine.reload()
		other_mapping.reload()
		self.assertEqual(mine.sync_status, "Synced")
		self.assertEqual(other_mapping.sync_status, "Queued")

	# -- download/parse failures --------------------------------------------

	def test_download_http_failure_does_not_mutate_any_mapping(self):
		mapping = self._make_mapping("sku-1", "job-001")
		with patch.object(
			catalog_feedback, "http_request", return_value=_download_response([], status_code=500)
		):
			with self.assertRaises(CatalogFeedbackError):
				process_catalog_feedback(self._payload(job_id="job-001"))

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Queued")

	def test_download_exceeding_max_size_raises_without_mutating_mapping(self):
		mapping = self._make_mapping("sku-1", "job-001")
		with patch.object(catalog_feedback, "MAX_DOWNLOAD_BYTES", 10):
			with patch.object(
				catalog_feedback,
				"http_request",
				return_value=_download_response(
					[{"vendorId": self.account.external_account_id, "sku": "sku-1", "feedback": "x" * 100}]
				),
			):
				with self.assertRaises(CatalogFeedbackError):
					process_catalog_feedback(self._payload(job_id="job-001"))

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Queued")

	def test_redirect_chain_exceeding_cap_raises(self):
		with patch.object(catalog_feedback, "MAX_REDIRECTS", 1):
			with patch.object(
				catalog_feedback,
				"http_request",
				return_value=_download_response(
					[], status_code=302, redirect_to="https://example.com/still-redirecting"
				),
			):
				with self.assertRaises(CatalogFeedbackError):
					process_catalog_feedback(self._payload())

	def test_redirect_to_non_https_url_is_rejected(self):
		with patch.object(
			catalog_feedback,
			"http_request",
			return_value=_download_response(
				[], status_code=302, redirect_to="http://example.com/insecure.csv"
			),
		):
			with self.assertRaises(CatalogFeedbackError):
				process_catalog_feedback(self._payload())

	# -- no Beta polling ------------------------------------------------------

	def test_never_calls_talabat_client_request(self):
		mapping = self._make_mapping("sku-1", "job-001")
		with patch(
			"marketplace_integrations.integrations.talabat.client.TalabatClient.request",
			side_effect=AssertionError("must not call the Talabat Partner API from the feedback processor"),
		):
			self._run(
				self._payload(job_id="job-001"),
				[
					{
						"vendorId": self.account.external_account_id,
						"sku": "sku-1",
						"feedback": "Product Assignment Successful",
					}
				],
			)

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Synced")

	# -- external text is stored as plain data, not markup -------------------

	def test_html_like_feedback_is_stored_as_literal_text(self):
		mapping = self._make_mapping("sku-1", "job-001")
		self._run(
			self._payload(job_id="job-001"),
			[
				{
					"vendorId": self.account.external_account_id,
					"sku": "sku-1",
					"feedback": "<script>alert(1)</script>",
					"details": "<img src=x onerror=alert(2)>",
				}
			],
		)

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Error")
		# Stored verbatim as plain data - Marketplace Product Mapping.last_error
		# is a read-only Small Text field, which Desk escapes by default (see
		# docs/frappe-conventions.md); this test only guards that our own code
		# neither executes nor otherwise interpolates it into markup.
		self.assertIn("<script>alert(1)</script>", mapping.last_error)
		self.assertIn("<img src=x onerror=alert(2)>", mapping.last_error)

	# -- runs as a trusted system user, not the calling Guest -----------------

	def test_runs_as_administrator_even_when_enqueued_from_a_guest_session(self):
		# The public webhook that enqueues this job is allow_guest=True, so
		# frappe.enqueue() would otherwise propagate a Guest session into this
		# trusted background worker - which cannot write documents at all.
		mapping = self._make_mapping("sku-1", "job-001")
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")

		self._run(
			self._payload(job_id="job-001"),
			[
				{
					"vendorId": self.account.external_account_id,
					"sku": "sku-1",
					"feedback": "Product Assignment Successful",
				}
			],
		)

		self.assertEqual(frappe.session.user, "Administrator")
		mapping.reload()
		self.assertEqual(mapping.sync_status, "Synced")

	# -- relative redirect targets --------------------------------------------

	def test_relative_redirect_location_is_resolved_against_current_url(self):
		mapping = self._make_mapping("sku-1", "job-001")
		redirect_response = _download_response([], status_code=302, redirect_to="/signed/job.csv")
		success_response = _download_response(
			[
				{
					"vendorId": self.account.external_account_id,
					"sku": "sku-1",
					"feedback": "Product Assignment Successful",
				}
			]
		)

		with patch.object(
			catalog_feedback, "http_request", side_effect=[redirect_response, success_response]
		) as request:
			process_catalog_feedback(
				self._payload(job_id="job-001", download_url="https://example.com/feedback/job")
			)

		self.assertEqual(request.call_count, 2)
		# The relative Location must be resolved against the current absolute
		# URL, not rejected as non-HTTPS just because it has no scheme/host.
		second_call_url = request.call_args_list[1].args[1]
		self.assertEqual(second_call_url, "https://example.com/signed/job.csv")

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Synced")

	# -- atomic stale-job guard -------------------------------------------------

	def test_finalize_mapping_returns_false_and_does_not_write_on_concurrent_job_change(self):
		mapping = self._make_mapping("sku-1", "job-001")
		# Simulate a concurrent re-push (e.g. #91 bulk push) moving this
		# mapping onto a fresh job between a snapshot read and this write.
		frappe.db.set_value("Marketplace Product Mapping", mapping.name, "last_sync_reference", "job-002")

		updated = catalog_feedback._finalize_mapping(
			mapping.name, "job-001", catalog_feedback.SUCCESS_FEEDBACK, ""
		)

		self.assertFalse(updated)
		mapping.reload()
		self.assertEqual(mapping.sync_status, "Queued")
		self.assertEqual(mapping.last_sync_reference, "job-002")

	def test_finalize_mapping_returns_true_and_writes_when_job_still_matches(self):
		mapping = self._make_mapping("sku-1", "job-001")

		updated = catalog_feedback._finalize_mapping(
			mapping.name, "job-001", catalog_feedback.SUCCESS_FEEDBACK, ""
		)

		self.assertTrue(updated)
		mapping.reload()
		self.assertEqual(mapping.sync_status, "Synced")

	# -- ops-visibility logging --------------------------------------------------

	def test_logs_distinct_counts_for_unknown_sku_versus_mapping_on_a_different_job(self):
		self._make_mapping("sku-1", "job-001")
		self._make_mapping("sku-stale", "job-002")

		with patch.object(catalog_feedback.frappe, "logger") as mock_logger:
			self._run(
				self._payload(job_id="job-001"),
				[
					{
						"vendorId": self.account.external_account_id,
						"sku": "sku-stale",
						"feedback": "Product Assignment Successful",
					},
					{
						"vendorId": self.account.external_account_id,
						"sku": "sku-does-not-exist",
						"feedback": "Product Assignment Successful",
					},
				],
			)

		mock_logger.return_value.warning.assert_called_once()
		args = mock_logger.return_value.warning.call_args.args
		# (fmt, job_id, matched, vendor_mismatch, sku_unknown, stale_job, missing_count, missing_suffix)
		self.assertEqual(args[1], "job-001")
		matched, vendor_mismatch, sku_unknown, stale_job = args[2], args[3], args[4], args[5]
		self.assertEqual(matched, 0)
		self.assertEqual(vendor_mismatch, 0)
		self.assertEqual(sku_unknown, 1)
		self.assertEqual(stale_job, 1)

	def test_logs_missing_mapping_names_that_never_appeared_in_csv(self):
		absent = self._make_mapping("sku-absent", "job-001")

		with patch.object(catalog_feedback.frappe, "logger") as mock_logger:
			self._run(self._payload(job_id="job-001"), [])

		mock_logger.return_value.warning.assert_called_once()
		args = mock_logger.return_value.warning.call_args.args
		missing_count, missing_suffix = args[6], args[7]
		self.assertEqual(missing_count, 1)
		self.assertIn(absent.name, missing_suffix)
