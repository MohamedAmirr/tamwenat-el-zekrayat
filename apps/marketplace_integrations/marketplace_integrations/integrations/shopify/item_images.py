# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Manual "Pull Missing Images" for a Shopify Marketplace Account (#526).

Downloads the Shopify image of each mapped Item that has none and stores it as a public File
attached to the Item (``Item.image`` = that File's ``file_url``; no Shopify CDN URL is kept).
Existing Item images are never replaced.

Shape (same as the other manual Shopify jobs):

- The request (``queue_pull_missing_images``) only authorizes and enqueues: Marketplace Account
  ``write`` on the Shopify account (document level, so the platform row filter applies). No Item
  permission is required: a Shopify User may run it without Item write. No Shopify call is made
  in the request.
- The worker re-checks the same permission (it runs as the enqueuing user), reads the candidates,
  looks up the variants' images in ``nodes(ids:)`` chunks and, per Item and after the download
  (no lock held across HTTP), saves the File and the Item in its own savepoint, committed per Item.
- Progress and the final payload are cached in the shape ``ShopifyCatalogPushStatus`` reads;
  every run also writes one ``Marketplace Sync Log`` row.

Image source fields: docs/shopify/README.md, "Item images".
"""

import mimetypes
import os
import re
from urllib.parse import unquote, urlparse

import frappe
from frappe import _
from frappe.utils import cstr, get_files_path, now_datetime
from frappe.utils.file_manager import get_content_hash, get_file_data_from_hash
from rq.timeouts import BaseTimeoutException

from marketplace_integrations.integrations.http import http_request
from marketplace_integrations.integrations.shopify import products
from marketplace_integrations.integrations.shopify.client import ShopifyClient
from marketplace_integrations.integrations.shopify.constants import SHOPIFY_NODES_LOOKUP_CHUNK_SIZE

MAPPING_DOCTYPE = "Marketplace Product Mapping"
RESULT_TTL = 60 * 60
IMAGE_PROGRESS_EVENT = "marketplace_shopify_item_image_pull_progress"
MAX_RESULTS_RETURNED = 500
ERROR_MESSAGE_MAX_LENGTH = 500
PROGRESS_UPDATE_INTERVAL = 10
# RQ job id prefix: one pull per account at a time (deduplicated).
_RQ_JOB_ID_PREFIX = "marketplace-shopify-item-images"

# `nodes` is typed to the union `[Node]!`, hence `... on ProductVariant`. Fields: docs/shopify/README.md,
# "Item images" (`ProductVariant.image` and `Product.featuredImage` are deprecated, not used).
_IMAGE_NODES_QUERY = """
query ShopifyVariantImages($ids: [ID!]!) {
  nodes(ids: $ids) {
    id
    ... on ProductVariant {
      media(first: 1) {
        nodes {
          ... on MediaImage {
            image {
              url
            }
          }
        }
      }
      product {
        featuredMedia {
          ... on MediaImage {
            image {
              url
            }
          }
        }
      }
    }
  }
}
"""


class _Skip(Exception):
	"""Nothing to do for this Item; the message is the reason."""


class _Fail(Exception):
	"""This Item failed for a known reason; the message is shown as is."""


# --- Request ----------------------------------------------------------------


def queue_pull_missing_images(account_name):
	"""Authorize and enqueue one deduplicated image pull for a Shopify account. Returns
	``{"jobs": [{"account", "job_id"}]}``; throws when a pull is already queued/running."""
	account_doc, _connection = products.get_account(account_name)
	# Document-level write (the platform row filter applies). Deliberately no Item permission check.
	account_doc.check_permission("write")

	owner = frappe.session.user
	job_id = frappe.generate_hash(length=16)
	# Seed the "queued" entry BEFORE enqueueing so a fast worker can't publish "running" first.
	_set_status(job_id, {"owner": owner, "status": "queued", "processed": 0})
	job = frappe.enqueue(
		pull_missing_images,
		queue="long",
		timeout=3600,
		job_id=f"{_RQ_JOB_ID_PREFIX}-{account_doc.name}",
		deduplicate=True,
		job_id_for_result=job_id,
		owner=owner,
		account_name=account_doc.name,
	)
	if job is None:
		frappe.cache().delete_value(_status_cache_key(job_id))
		frappe.throw(
			_("A missing-image pull is already running for Marketplace Account {0}.").format(account_doc.name)
		)
	return {"jobs": [{"account": account_doc.name, "job_id": job_id}]}


def get_pull_status(job_id):
	state = frappe.cache().get_value(_status_cache_key(job_id))
	if not state or state.get("owner") != frappe.session.user:
		frappe.throw(_("Image pull job not found."), frappe.PermissionError)
	return {key: value for key, value in state.items() if key != "owner"}


# --- Worker -----------------------------------------------------------------


def pull_missing_images(job_id_for_result, owner, account_name):
	"""Background job body. Returns the result payload."""
	attempted_at = now_datetime()
	results = []
	failure = None
	failure_traceback = None
	try:
		_report(job_id_for_result, owner, status="running", processed=0, result=_result_payload([]))
		account_doc, connection = products.get_account(account_name)
		# Re-check: the job runs as the enqueuing user, whose access may have changed since queueing.
		account_doc.check_permission("write")
		client = ShopifyClient(connection.name)

		candidates = _candidates(account_doc.name)
		# Commit before any HTTP call so no read snapshot/lock is held across it.
		frappe.db.commit()

		for chunk in _chunked(candidates, SHOPIFY_NODES_LOOKUP_CHUNK_SIZE):
			data = client.execute(_IMAGE_NODES_QUERY, {"ids": [row.external_reference for row in chunk]})
			# Match by each node's own id, never by position.
			nodes_by_id = {node["id"]: node for node in data.get("nodes") or [] if node and node.get("id")}
			for row in chunk:
				results.append(_pull_one(row, nodes_by_id.get(row.external_reference)))
				if len(results) % PROGRESS_UPDATE_INTERVAL == 0:
					_report(
						job_id_for_result,
						owner,
						status="running",
						processed=len(results),
						result=_result_payload(results),
					)

		payload = _result_payload(results)
		_report(job_id_for_result, owner, status="completed", processed=len(results), result=payload)
		return payload
	except BaseTimeoutException:
		# Before the broad handler: a job timeout subclasses Exception and must not be recorded
		# against whichever Item was in flight.
		failure = cstr(_("The image pull timed out."))
		failure_traceback = frappe.get_traceback()
		# Drop half-done work so the sync log commit in `finally` does not persist it.
		frappe.db.rollback()
		_report(job_id_for_result, owner, status="failed", error=failure)
		raise
	except Exception as exc:
		# An exception such as PermissionError can carry an empty message: fall back to its class name
		# so the status error and the Sync Log are never blank.
		failure = (cstr(exc) or type(exc).__name__)[:ERROR_MESSAGE_MAX_LENGTH]
		failure_traceback = frappe.get_traceback()
		frappe.db.rollback()
		frappe.log_error(title="Shopify item image pull failed", message=failure_traceback)
		_report(job_id_for_result, owner, status="failed", error=failure)
	finally:
		_write_sync_log(
			account_name, job_id_for_result, owner, attempted_at, results, failure, failure_traceback
		)


def _candidates(account_name):
	"""Enabled mappings of the account whose Item is enabled and has no image: one per Item, the
	first by mapping name."""
	# Trusted read: queue_pull_missing_images() and the worker's account write check authorized the
	# caller; the Item and mapping rows are only used to choose what to pull, never returned as is.
	mappings = frappe.get_all(
		MAPPING_DOCTYPE,
		filters={"marketplace_account": account_name, "enabled": 1, "item": ["is", "set"]},
		fields=["name", "item", "external_reference"],
		order_by="name asc",
		limit_page_length=0,
	)
	item_names = list(dict.fromkeys(row.item for row in mappings))
	eligible = set()
	for chunk in _chunked(item_names, 1000):
		# Trusted read, same authorization as above.
		for item in frappe.get_all(
			"Item",
			filters={"name": ["in", chunk], "disabled": 0},
			fields=["name", "image"],
			limit_page_length=0,
		):
			if not item.image:
				eligible.add(item.name)

	seen = set()
	candidates = []
	for row in mappings:
		if row.item in eligible and row.item not in seen and row.external_reference:
			seen.add(row.item)
			candidates.append(row)
	return candidates


def _pull_one(row, node):
	item = row.item
	try:
		url = _image_url(node)
		content, content_type = _download(url)
		return _store_image(item, url, content, content_type)
	except _Skip as skip:
		return {"mapping_name": item, "ok": False, "skipped": True, "detail": cstr(skip)}
	except BaseTimeoutException:
		raise
	except _Fail as fail:
		return {"mapping_name": item, "ok": False, "error": cstr(fail)}
	except Exception as exc:
		frappe.log_error(
			title="Shopify item image pull: Item failed", message=f"item={item}\n\n{frappe.get_traceback()}"
		)
		# Keep the log row if a later whole-job failure rolls back.
		frappe.db.commit()
		return {"mapping_name": item, "ok": False, "error": cstr(exc)[:ERROR_MESSAGE_MAX_LENGTH]}


def _image_url(node):
	"""The variant's own media image, else its product's featured media image."""
	if not node:
		raise _Skip(_("Variant not found on Shopify"))
	for media in (node.get("media") or {}).get("nodes") or []:
		url = (((media or {}).get("image")) or {}).get("url")
		if url:
			return url
	featured = (node.get("product") or {}).get("featuredMedia") or {}
	url = (featured.get("image") or {}).get("url")
	if url:
		return url
	raise _Skip(_("No image on Shopify"))


def _download(url):
	if urlparse(url).scheme != "https":
		raise _Fail(_("The image URL is not https."))
	response = http_request("GET", url, service_name="Shopify")
	# requests follows redirects: the final URL must still be https.
	if urlparse(cstr(response.url)).scheme != "https":
		raise _Fail(_("The image URL is not https."))
	if not 200 <= response.status_code < 300:
		raise _Fail(_("The image download failed with HTTP {0}.").format(response.status_code))
	content_type = cstr(response.headers.get("Content-Type")).split(";")[0].strip().lower()
	if content_type == "image/svg+xml":
		raise _Fail(_("SVG images are not accepted."))
	if not content_type.startswith("image/"):
		raise _Fail(_("The download is not an image."))
	return response.content, content_type


def _store_image(item, url, content, content_type):
	"""Attach the downloaded image to the Item. Runs in its own savepoint, AFTER the download, so no
	lock is held across HTTP; committed per Item."""
	savepoint = "shopify_item_image_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(savepoint)
	file_doc = None
	file_reused = True
	try:
		# Row lock taken here, after the download, so no lock is held across HTTP.
		item_doc = frappe.get_doc("Item", item, for_update=True)
		if item_doc.image:
			frappe.db.release_savepoint(savepoint)
			# End the transaction so the Item row lock is not held while the next download runs.
			frappe.db.commit()
			raise _Skip(_("Item already has an image"))
		# The File insert reuses an existing File's file_url on a content-hash match; remember whether
		# it writes a new disk file, because a savepoint rollback leaves it on disk.
		file_reused = bool(get_file_data_from_hash(get_content_hash(content), is_private=0))
		file_doc = _insert_image_file(item, _filename(item, url, content_type), content)
		item_doc.image = file_doc.file_url
		# The caller was authorized by Marketplace Account write in the request and again in the
		# worker, not by Item write. Only `image` changes; normal Item validation still runs.
		item_doc.flags.ignore_permissions = True
		item_doc.save()
	except _Skip:
		raise
	except BaseException:
		# Includes a job timeout: roll back this Item's work before anything propagates.
		frappe.db.rollback(save_point=savepoint)
		if file_doc and not file_reused:
			_remove_disk_file(file_doc)
		raise
	frappe.db.release_savepoint(savepoint)
	frappe.db.commit()
	return {"mapping_name": item, "ok": True}


def _insert_image_file(item, file_name, content):
	"""Insert a public File holding ``content``, already attached to the Item's ``image`` field.

	Not ``frappe.utils.file_manager.save_file()``: it writes the file, then File.before_insert re-reads
	it as text (File.get_content decodes with windows-1252) and rewrites it, which corrupts binary
	images; and without ``attached_to_field="image"`` the Item's on_update hook
	(file/utils.py::attach_files_to_document) inserts a second File. ``ignore_permissions`` mirrors
	what save_file() sets on its File: the caller was authorized on the Marketplace Account."""
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"attached_to_doctype": "Item",
			"attached_to_name": item,
			"attached_to_field": "image",
			"is_private": 0,
			"content": content,
		}
	)
	file_doc.insert(ignore_permissions=True)
	return file_doc


def _remove_disk_file(file_doc):
	"""Delete the file the File insert just wrote after the File row was rolled back. Best effort: a
	leftover file is harmless, a failure here must not hide the real error."""
	try:
		path = get_files_path(os.path.basename(file_doc.file_url), is_private=0)
		if os.path.exists(path):
			os.remove(path)
	except Exception:
		frappe.log_error(
			title="Shopify item image pull: could not remove file", message=frappe.get_traceback()
		)


def _filename(item, url, content_type):
	base = os.path.basename(unquote(urlparse(url).path))
	base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
	if not os.path.splitext(base)[1]:
		base = f"{frappe.scrub(item)}{mimetypes.guess_extension(content_type) or '.img'}"
	return base


def _chunked(items, size):
	items = list(items)
	for start in range(0, len(items), size):
		yield items[start : start + size]


# --- Status and Sync Log ----------------------------------------------------


def _result_payload(results):
	total = len(results)
	return {
		"results": results[:MAX_RESULTS_RETURNED],
		"total": total,
		"succeeded": sum(1 for row in results if row["ok"]),
		"failed": sum(1 for row in results if not row["ok"] and not row.get("skipped")),
		"skipped_mappings": sum(1 for row in results if row.get("skipped")),
		"truncated": total > MAX_RESULTS_RETURNED,
	}


def _write_sync_log(account_name, job_id, owner, attempted_at, results, failure, failure_traceback):
	"""One committed ``Marketplace Sync Log`` row per run, also for a whole-job failure. Never raises
	(a failed log write goes to the Error Log)."""
	try:
		payload = _result_payload(results)
		failed_rows = [row for row in results if not row["ok"] and not row.get("skipped")]
		frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Item Image",
				"marketplace_account": account_name,
				"status": "Error" if failure is not None or failed_rows else "Success",
				"trigger_source": "Manual",
				"attempted_at": attempted_at,
				"completed_at": now_datetime(),
				"request_data": frappe.as_json({"job_id": job_id, "owner": owner}),
				"response_data": frappe.as_json(
					{
						"total": payload["total"],
						"updated": payload["succeeded"],
						"failed": payload["failed"],
						"skipped": payload["skipped_mappings"],
						"failed_rows": failed_rows[:MAX_RESULTS_RETURNED],
					}
				),
				"traceback": failure_traceback if failure is not None else None,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(
			title="Shopify item image pull: failed to write sync log", message=frappe.get_traceback()
		)
	else:
		frappe.db.commit()


def _report(job_id, owner, **changes):
	if not job_id:
		return
	state = frappe.cache().get_value(_status_cache_key(job_id)) or {"owner": owner}
	state.update(changes)
	_set_status(job_id, state)
	frappe.publish_realtime(
		IMAGE_PROGRESS_EVENT,
		{"job_id": job_id, **{key: value for key, value in state.items() if key != "owner"}},
		user=owner,
	)


def _set_status(job_id, state):
	frappe.cache().set_value(_status_cache_key(job_id), state, expires_in_sec=RESULT_TTL)


def _status_cache_key(job_id):
	return f"marketplace_integrations:shopify:item_image_pull_job:{job_id}"
