# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Talabat asynchronous "Update Products" catalog feedback processor.

This module finalizes ``Marketplace Product Mapping.sync_status`` for the
exact Talabat catalog job a webhook callback reports on. It is deliberately
trigger-source agnostic: it finalizes whatever mapping currently has
``last_sync_reference == payload["job_id"]``, regardless of whether that job
was submitted by a manual single push, a manual bulk push
(``integrations/talabat/catalog_sync.py``), or a future reconciliation job.

Talabat's OpenAPI documents no formal enum for the CSV ``feedback`` column -
only an example success string. The fail-closed V1 rule here is intentional:
only the exact documented success string is ever treated as success. Do not
"improve" this without a matching issue/architecture change - see
docs/marketplace-integrations/architecture.md.

This module never polls Talabat's Beta job-status endpoint
(``GET /v2/chains/{chain_id}/catalog/jobs/{job_id}``) - the webhook-delivered
CSV is the only source of truth consumed here.
"""

import csv
import io
from collections import defaultdict
from urllib.parse import urljoin

import frappe
from frappe import _
from frappe.utils import cstr

from marketplace_integrations.integrations.talabat.catalog_sync import effective_outbound_sku
from marketplace_integrations.integrations.talabat.client import http_request

CATALOG_FEEDBACK_STATUS = "COMPLETED"
SUCCESS_FEEDBACK = "Product Assignment Successful"

# Generous but bounded: a feedback CSV is one row per submitted product, and a
# single catalog batch is capped at 10,000 products (see catalog_sync.py). 25MB
# comfortably covers that while still bounding worst-case memory/parse cost.
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_REDIRECTS = 5
DOWNLOAD_TIMEOUT_SECONDS = 30
LAST_ERROR_MAX_LENGTH = 1000
# Bound how many mapping names a single ops-visibility log line spells out;
# the count itself is always logged regardless of this cap.
MAX_LOGGED_MISSING_NAMES = 50
_REDIRECT_STATUS_CODES = (301, 302, 303, 307, 308)


class CatalogFeedbackError(frappe.ValidationError):
	"""A processor-level failure. Raising this must never leave a partially
	applied mapping mutation - every write happens only after every prior
	validation/download/parse step has already succeeded."""


def process_catalog_feedback(payload):
	"""Finalize Marketplace Product Mapping sync status for one Talabat catalog job.

	``payload`` is the raw webhook body already authenticated and schema-checked
	by ``api/talabat_webhooks.py::catalog_update_feedback`` before enqueue. This
	function re-derives and re-validates everything it needs from the payload
	itself rather than trusting any caller-side resolution, since a background
	job may run detached from the request that enqueued it.
	"""
	# The public webhook request is intentionally handled as Guest. Its
	# signature has already been validated before this job is enqueued, so the
	# trusted integration worker must not inherit Guest's document permissions
	# - same rationale as integrations/talabat/order_events.py's
	# process_order_event().
	frappe.set_user("Administrator")

	job_id = cstr(payload.get("job_id")).strip()
	status = cstr(payload.get("status")).strip()
	platform_vendor_id = cstr(payload.get("platform_vendor_id")).strip()
	download_url = cstr(payload.get("download_url")).strip()

	if not job_id:
		raise CatalogFeedbackError(_("Talabat catalog feedback payload is missing job_id."))
	if status != CATALOG_FEEDBACK_STATUS:
		raise CatalogFeedbackError(
			_("Talabat catalog feedback job {0} has unexpected status {1!r}; expected {2!r}.").format(
				job_id, status, CATALOG_FEEDBACK_STATUS
			)
		)
	if not platform_vendor_id:
		raise CatalogFeedbackError(
			_("Talabat catalog feedback job {0} is missing platform_vendor_id.").format(job_id)
		)

	account = _resolve_account(platform_vendor_id)
	if not account:
		raise CatalogFeedbackError(
			_(
				"Talabat catalog feedback job {0} does not match exactly one enabled Talabat account for vendor {1}."
			).format(job_id, platform_vendor_id)
		)

	rows = _download_feedback_csv(download_url)
	_apply_feedback(account, job_id, platform_vendor_id, rows)


def _resolve_account(platform_vendor_id):
	"""Resolve exactly one enabled Talabat account for this vendor id.

	Mirrors ``api/talabat_webhooks.py``'s account resolution shape, but this
	runs independently inside the background job rather than trusting the
	webhook's own resolution, since the two can run detached from each other.
	"""
	accounts = frappe.get_all(
		"Marketplace Account",
		filters={
			"platform": "Talabat",
			"enabled": 1,
			"connection_doctype": "Talabat Connection",
			"external_account_id": platform_vendor_id,
		},
		fields=["name"],
	)
	if len(accounts) != 1:
		return None
	return accounts[0].name


def _download_feedback_csv(download_url):
	"""Download and parse the callback-provided CSV. Never sends Talabat OAuth/
	client credentials - this is a plain, unauthenticated GET to whatever URL
	Talabat's callback provided, following at most MAX_REDIRECTS HTTPS hops."""
	url = download_url
	if not url.lower().startswith("https://"):
		raise CatalogFeedbackError(_("Talabat catalog feedback download_url must use HTTPS."))

	for _hop in range(MAX_REDIRECTS + 1):
		with _download_request(url) as response:
			if response.status_code in _REDIRECT_STATUS_CODES:
				location = response.headers.get("Location")
				if not location:
					raise CatalogFeedbackError(
						_("Talabat catalog feedback download redirected without a Location header.")
					)
				# Location may legitimately be a relative reference (RFC 7231)
				# - resolve it against the current URL before applying the
				# HTTPS-only scheme check, rather than rejecting every
				# relative redirect outright.
				resolved = urljoin(url, location)
				if not resolved.lower().startswith("https://"):
					raise CatalogFeedbackError(
						_("Talabat catalog feedback download redirected to a non-HTTPS URL.")
					)
				url = resolved
				continue

			if not response.ok:
				raise CatalogFeedbackError(
					_("Failed to download Talabat catalog feedback: HTTP {0}.").format(response.status_code)
				)
			return _read_bounded_csv(response)

	raise CatalogFeedbackError(_("Talabat catalog feedback download exceeded the maximum redirect count."))


def _download_request(url):
	"""Reuse the app's single shared bounded-timeout/network-error request
	wrapper (``integrations/talabat/client.py``'s ``http_request``) instead of
	a second ad-hoc ``requests`` call site. No Authorization header is
	attached - this is not a Talabat Partner API request.

	Returned as a context manager (``requests.Response`` supports
	``with``/``__exit__`` natively) so every code path - a redirect hop whose
	body is never read, a successful parse, an oversize-abort, or a parse
	error - always releases the underlying connection back to the pool."""
	return http_request(
		"GET",
		url,
		timeout=DOWNLOAD_TIMEOUT_SECONDS,
		stream=True,
		allow_redirects=False,
	)


def _read_bounded_csv(response):
	content_length = response.headers.get("Content-Length")
	if content_length is not None:
		try:
			if int(content_length) > MAX_DOWNLOAD_BYTES:
				raise CatalogFeedbackError(
					_("Talabat catalog feedback download exceeds the maximum allowed size.")
				)
		except ValueError:
			pass

	chunks = []
	total = 0
	try:
		for chunk in response.iter_content(chunk_size=65536):
			if not chunk:
				continue
			total += len(chunk)
			if total > MAX_DOWNLOAD_BYTES:
				raise CatalogFeedbackError(
					_("Talabat catalog feedback download exceeds the maximum allowed size.")
				)
			chunks.append(chunk)
	except CatalogFeedbackError:
		raise
	except Exception as error:
		raise CatalogFeedbackError(
			_("Failed to read Talabat catalog feedback download: {0}").format(error)
		) from error

	try:
		text = b"".join(chunks).decode("utf-8-sig")
	except UnicodeDecodeError as error:
		raise CatalogFeedbackError(_("Talabat catalog feedback CSV is not valid UTF-8.")) from error

	try:
		return list(csv.DictReader(io.StringIO(text)))
	except csv.Error as error:
		raise CatalogFeedbackError(
			_("Failed to parse Talabat catalog feedback CSV: {0}").format(error)
		) from error


def _apply_feedback(account, job_id, platform_vendor_id, rows):
	"""Finalize only rows exactly correlated by account + SKU + job_id.

	Correlation matches the CSV's ``sku`` column against each mapping's
	*effective outbound* Talabat sku - the exact same value
	``catalog_sync.py``'s ``build_catalog_product`` actually sent
	(``external_sku`` when non-blank, else ``external_reference``) - per this
	feature's documented correlation contract, see
	docs/marketplace-integrations/architecture.md.

	Every mapping this account owns is fetched exactly once, upfront, instead
	of one ``frappe.get_all``/``frappe.db.get_value`` pair per CSV row (a real
	N+1: up to ~20,000 round trips for a 10,000-row batch). Fetching the whole
	account (not just this job's mappings) rather than filtering by
	``last_sync_reference`` up front is a deliberate choice, still a single
	query: it is what lets a CSV row's sku be classified as "unknown to us"
	versus "matches a real mapping, but that mapping is on a different/newer
	job" as two distinct ops signals, and it reproduces the exact same
	ambiguity handling the previous per-row lookup had (a sku shared by more
	than one mapping's effective outbound sku resolves to no match).
	"""
	mappings = frappe.get_all(
		"Marketplace Product Mapping",
		filters={"marketplace_account": account},
		fields=["name", "external_sku", "external_reference", "last_sync_reference"],
	)

	by_effective_sku = defaultdict(list)
	job_names = set()
	for mapping in mappings:
		by_effective_sku[effective_outbound_sku(mapping.external_sku, mapping.external_reference)].append(
			mapping
		)
		if cstr(mapping.last_sync_reference).strip() == job_id:
			job_names.add(mapping.name)

	matched = 0
	vendor_mismatch = 0
	sku_unknown = 0
	stale_job = 0
	applied_names = set()

	for row in rows:
		row_vendor_id = cstr(row.get("vendorId")).strip()
		if row_vendor_id != platform_vendor_id:
			vendor_mismatch += 1
			continue

		sku = cstr(row.get("sku")).strip()
		candidates = by_effective_sku.get(sku) if sku else None
		if not candidates or len(candidates) != 1:
			# Either genuinely unknown to this account, or ambiguous (more
			# than one mapping shares this effective outbound sku) - both
			# resolve to no match rather than guessing.
			sku_unknown += 1
			continue

		mapping = candidates[0]
		if cstr(mapping.last_sync_reference).strip() != job_id:
			# A real mapping, but it has since been resubmitted under a
			# different job (or was never on this job) - never overwrite
			# another job's current state.
			stale_job += 1
			continue

		if not _finalize_mapping(
			mapping.name, job_id, cstr(row.get("feedback")).strip(), cstr(row.get("details")).strip()
		):
			# Lost a race against a concurrent re-push (e.g. #91 bulk push)
			# that moved this mapping onto a different job between the
			# snapshot read above and this write.
			stale_job += 1
			continue

		applied_names.add(mapping.name)
		matched += 1

	missing_names = sorted(job_names - applied_names)
	_log_feedback_summary(job_id, matched, vendor_mismatch, sku_unknown, stale_job, missing_names)


def _log_feedback_summary(job_id, matched, vendor_mismatch, sku_unknown, stale_job, missing_names):
	"""Log ops-visibility counts whenever anything didn't cleanly finalize.

	``missing_names`` are mappings still ``last_sync_reference == job_id``
	that never appeared anywhere in the CSV at all - they are left exactly as
	they were (typically ``Queued``) and logged here so a stuck job is
	visible rather than silently invisible.
	"""
	if not (vendor_mismatch or sku_unknown or stale_job or missing_names):
		return

	missing_suffix = ""
	if missing_names:
		shown = ", ".join(missing_names[:MAX_LOGGED_MISSING_NAMES])
		if len(missing_names) > MAX_LOGGED_MISSING_NAMES:
			shown += ", ..."
		missing_suffix = f": {shown}"

	frappe.logger().warning(
		"Talabat catalog feedback job %s: matched %s row(s), skipped %s vendor-mismatch row(s), "
		"skipped %s row(s) with unknown/ambiguous sku, skipped %s row(s) matched to a mapping on a "
		"different/stale job, %s mapping(s) queued for this job never appeared in the CSV and remain "
		"unfinalized%s.",
		job_id,
		matched,
		vendor_mismatch,
		sku_unknown,
		stale_job,
		len(missing_names),
		missing_suffix,
	)


def _finalize_mapping(mapping_name, job_id, feedback, details):
	"""Atomically finalize one mapping only if it is still on this exact job.

	A single conditional ``UPDATE`` - rather than a separate
	``frappe.db.get_value`` read of ``last_sync_reference`` followed by an
	unconditional ``frappe.db.set_value`` write - closes a race window: a
	concurrent re-push (e.g. #91's bulk push) could move this mapping onto a
	fresh job/``Queued`` state between the read and the write, and this stale
	job's feedback must never overwrite that fresher state. Returns True only
	when a row was actually updated, i.e. the mapping was still on this exact
	job at the moment of the write.
	"""
	if feedback == SUCCESS_FEEDBACK:
		sync_status, last_error = "Synced", None
	else:
		sync_status, last_error = "Error", _build_last_error(feedback, details)

	# update_modified is deliberately skipped here, matching every other
	# system-driven sync-state write in this app (catalog_sync.py's
	# _mark_queued()/_mark_error() and this module's previous
	# frappe.db.set_value(..., update_modified=False) calls) - this is
	# background reconciliation of sync status, not a user edit.
	frappe.db.sql(
		"""
		UPDATE `tabMarketplace Product Mapping`
		SET `sync_status` = %s, `last_error` = %s
		WHERE `name` = %s AND `last_sync_reference` = %s
		""",
		(sync_status, last_error, mapping_name, job_id),
	)
	updated = frappe.db._cursor.rowcount == 1
	if updated:
		# frappe.db.set_value() clears this automatically; a raw UPDATE must
		# do the same so a subsequent frappe.get_cached_doc() can't return a
		# pre-update copy of this mapping.
		frappe.clear_document_cache("Marketplace Product Mapping", mapping_name)
	return updated


def _build_last_error(feedback, details):
	"""Talabat documents no feedback enum, so any non-success value (including
	blank) is treated as Error rather than assumed successful. The stored text
	is plain data - Marketplace Product Mapping.last_error is a read-only Small
	Text field, which Desk escapes by default; this must not be interpolated
	into any new HTML-rendering UI sink without explicit escaping."""
	message = feedback or _("Talabat did not confirm success for this listing.")
	if details:
		message = f"{message}: {details}"
	return message[:LAST_ERROR_MAX_LENGTH]
