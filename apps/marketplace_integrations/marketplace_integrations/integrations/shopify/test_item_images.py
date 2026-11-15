# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Every test mocks ShopifyClient.execute() and the image download (``http_request``): no live
Shopify or CDN call. The worker commits per Item, so class rollback does not cover the fixtures:
tearDown removes them."""

import base64
import random
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify import item_images
from marketplace_integrations.integrations.shopify.test_products import make_item, make_shopify_account
from marketplace_integrations.tests.utils import make_role_user

EXECUTE_TARGET = "marketplace_integrations.integrations.shopify.client.ShopifyClient.execute"
HTTP_TARGET = "marketplace_integrations.integrations.shopify.item_images.http_request"
ENQUEUE_TARGET = "frappe.enqueue"
PNG = base64.b64decode(
	"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
CDN = "https://cdn.shopify.com/s/files/1/0000/files"


def _variant_gid():
	return f"gid://shopify/ProductVariant/{random.randint(10**8, 10**9)}"


def _response(status=200, content_type="image/png", url=f"{CDN}/x.png", content=None):
	# Distinct bytes per URL: save_file() reuses an existing File by content hash, which would hand a
	# second Item the first Item's file_url.
	if content is None:
		content = PNG + url.encode()
	return SimpleNamespace(
		status_code=status, headers={"Content-Type": content_type}, content=content, url=url
	)


def _download_ok(method, url, **kwargs):
	return _response(url=url)


class FakeShopify:
	"""Answers the image ``nodes(ids:)`` query from ``{variant gid: node}``; a gid without an entry
	comes back as a null node. ``reverse`` answers in the opposite order (match by id, not position)."""

	def __init__(self):
		self.nodes = {}
		self.calls = []
		self.reverse = False

	def add(self, gid, variant_url=None, product_url=None):
		media = [{"image": {"url": variant_url}}] if variant_url else []
		featured = {"image": {"url": product_url}} if product_url else None
		self.nodes[gid] = {"id": gid, "media": {"nodes": media}, "product": {"featuredMedia": featured}}

	def __call__(self, query, variables=None):
		assert "... on ProductVariant" in query and "MediaImage" in query
		ids = variables["ids"]
		self.calls.append(ids)
		nodes = [self.nodes.get(gid) for gid in ids]
		return {"nodes": nodes[::-1] if self.reverse else nodes}


class ItemImageFixtures:
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.account = make_shopify_account(f"Item Image Account {self.suffix}")
		self.shopify = FakeShopify()
		self.items = []
		self.job_ids = []

	def tearDown(self):
		for job_id in self.job_ids:
			frappe.cache().delete_value(item_images._status_cache_key(job_id))
		frappe.db.delete("Marketplace Sync Log", {"marketplace_account": self.account.name})
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": self.account.name})
		for code in self.items:
			frappe.db.delete("File", {"attached_to_doctype": "Item", "attached_to_name": code})
			frappe.delete_doc("Item", code, ignore_permissions=True, force=True)
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Shopify Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDown()

	def _item(self, marker, image=None, disabled=0):
		code = f"Item Image {marker} {self.suffix}"
		make_item(code)
		self.items.append(code)
		frappe.db.set_value("Item", code, {"image": image, "disabled": disabled})
		return code

	def _mapping(self, item, gid=None, account=None, enabled=1):
		gid = gid or _variant_gid()
		return frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": (account or self.account).name,
				"item": item,
				"external_reference": gid,
				"external_variant_id": gid,
				"external_product_id": "gid://shopify/Product/1",
				"enabled": enabled,
			}
		).insert()

	def _run(self, http=None, user="Administrator"):
		job_id = frappe.generate_hash(length=16)
		self.job_ids.append(job_id)
		item_images._set_status(job_id, {"owner": user, "status": "queued", "processed": 0})
		with (
			patch(EXECUTE_TARGET, side_effect=self.shopify),
			patch(HTTP_TARGET, side_effect=http or _download_ok) as mocked_http,
			self.set_user(user),
		):
			item_images.pull_missing_images(job_id, user, self.account.name)
		self.http = mocked_http
		return job_id

	def _image_bytes(self, item):
		"""The bytes of the File that Item.image points to (the stored name may carry a hash suffix)."""
		file_url = frappe.db.get_value("Item", item, "image")
		file_doc = frappe.get_doc(
			"File", {"file_url": file_url, "attached_to_doctype": "Item", "attached_to_name": item}
		)
		# get_content() returns text when the bytes happen to decode; read the raw bytes instead.
		with open(file_doc.get_full_path(), "rb") as handle:
			return handle.read()

	def _result(self, job_id):
		state = frappe.cache().get_value(item_images._status_cache_key(job_id))
		return state, {row["mapping_name"]: row for row in (state.get("result") or {}).get("results", [])}

	def _logs(self):
		return frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_account": self.account.name, "sync_type": "Item Image"},
			fields=["name", "status", "trigger_source", "request_data", "response_data", "traceback"],
		)


class IntegrationTestPullMissingImages(ItemImageFixtures, IntegrationTestCase):
	def test_variant_image_becomes_a_public_file_on_the_item(self):
		item = self._item("Variant")
		mapping = self._mapping(item)
		self.shopify.add(mapping.external_reference, variant_url=f"{CDN}/variant.png?v=1")

		job_id = self._run()

		state, rows = self._result(job_id)
		self.assertEqual(state["status"], "completed")
		self.assertTrue(rows[item]["ok"])
		file_url = frappe.db.get_value("Item", item, "image")
		self.assertTrue(file_url.startswith("/files/"))
		self.assertNotIn("cdn.shopify.com", file_url)
		file_row = frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Item", "attached_to_name": item},
			fields=["file_url", "is_private", "attached_to_field"],
		)
		# Exactly one File row, already attached to the `image` field so Frappe's on_update hook adds
		# no second (corrupted) copy; the disk bytes equal the downloaded bytes.
		self.assertEqual(
			[(r.file_url, r.is_private, r.attached_to_field) for r in file_row], [(file_url, 0, "image")]
		)
		self.assertEqual(self._image_bytes(item), PNG + f"{CDN}/variant.png?v=1".encode())
		self.assertEqual(self.http.call_args.args[:2], ("GET", f"{CDN}/variant.png?v=1"))

	def test_featured_product_image_is_the_fallback(self):
		item = self._item("Featured")
		mapping = self._mapping(item)
		self.shopify.add(mapping.external_reference, product_url=f"{CDN}/featured.png")

		self._run()

		self.assertTrue(frappe.db.get_value("Item", item, "image"))
		self.assertEqual(self.http.call_args.args[1], f"{CDN}/featured.png")

	def test_nodes_are_matched_by_id_not_position(self):
		first, second = self._item("First"), self._item("Second")
		m1, m2 = self._mapping(first), self._mapping(second)
		self.shopify.add(m1.external_reference, variant_url=f"{CDN}/one.png")
		self.shopify.add(m2.external_reference, variant_url=f"{CDN}/two.png")
		self.shopify.reverse = True

		self._run()

		self.assertEqual(self._image_bytes(first), PNG + f"{CDN}/one.png".encode())
		self.assertEqual(self._image_bytes(second), PNG + f"{CDN}/two.png".encode())

	def test_item_with_several_mappings_is_processed_once_from_the_first_by_name(self):
		item = self._item("Multi")
		mappings = [self._mapping(item), self._mapping(item)]
		first = min(mappings, key=lambda m: m.name)
		other = max(mappings, key=lambda m: m.name)
		self.shopify.add(first.external_reference, variant_url=f"{CDN}/first.png")
		self.shopify.add(other.external_reference, variant_url=f"{CDN}/other.png")

		self._run()

		self.assertEqual(self.shopify.calls, [[first.external_reference]])
		self.assertEqual(self.http.call_count, 1)
		self.assertEqual(self._image_bytes(item), PNG + f"{CDN}/first.png".encode())

	def test_disabled_mappings_and_disabled_items_are_not_processed(self):
		disabled_mapping = self._mapping(self._item("DisabledMapping"), enabled=0)
		disabled_item = self._mapping(self._item("DisabledItem", disabled=1))
		for mapping in (disabled_mapping, disabled_item):
			self.shopify.add(mapping.external_reference, variant_url=f"{CDN}/x.png")

		job_id = self._run()

		self.assertEqual(self.shopify.calls, [])
		self.assertEqual(self.http.call_count, 0)
		self.assertEqual(self._result(job_id)[0]["result"]["total"], 0)

	def test_existing_item_image_is_never_changed(self):
		item = self._item("HasImage", image="/files/mine.png")
		mapping = self._mapping(item)
		self.shopify.add(mapping.external_reference, variant_url=f"{CDN}/new.png")

		self._run()

		self.assertEqual(frappe.db.get_value("Item", item, "image"), "/files/mine.png")
		self.assertEqual(self.shopify.calls, [])

	def test_image_gained_between_candidate_read_and_write_is_skipped(self):
		item = self._item("Race")
		mapping = self._mapping(item)
		self.shopify.add(mapping.external_reference, variant_url=f"{CDN}/race.png")

		def download(method, url, **kwargs):
			frappe.db.set_value("Item", item, "image", "/files/someone-else.png")
			return _response()

		job_id = self._run(http=download)

		self.assertEqual(frappe.db.get_value("Item", item, "image"), "/files/someone-else.png")
		row = self._result(job_id)[1][item]
		self.assertTrue(row["skipped"])
		self.assertIn("already has an image", row["detail"])
		self.assertFalse(frappe.db.exists("File", {"attached_to_doctype": "Item", "attached_to_name": item}))

	def test_each_failure_reason_is_reported_and_other_items_still_complete(self):
		cases = {
			"NoImage": (None, None, None, "No image on Shopify", True),
			"Missing": ("missing", None, None, "Variant not found on Shopify", True),
			"Http": ("http://cdn.example.com/a.png", None, _response(), "not https", False),
			"NotFound": (f"{CDN}/nf.png", None, _response(status=404), "HTTP 404", False),
			"Html": (f"{CDN}/h.png", None, _response(content_type="text/html"), "not an image", False),
			"Svg": (f"{CDN}/s.svg", None, _response(content_type="image/svg+xml"), "SVG images", False),
			"Redirect": (
				f"{CDN}/r.png",
				None,
				_response(url="http://insecure.example.com/r.png"),
				"not https",
				False,
			),
		}
		good = self._item("Good")
		mapping = self._mapping(good)
		self.shopify.add(mapping.external_reference, variant_url=f"{CDN}/good.png")
		responses = {}
		expected = {}
		for marker, (url, _p, response, reason, skipped) in cases.items():
			item = self._item(marker)
			gid = self._mapping(item).external_reference
			if url != "missing":
				self.shopify.add(gid, variant_url=url)
			if response is not None:
				responses[url] = response
			expected[item] = (reason, skipped)

		job_id = self._run(http=lambda method, url, **kwargs: responses.get(url) or _response(url=url))

		_state, rows = self._result(job_id)
		self.assertTrue(rows[good]["ok"])
		self.assertTrue(frappe.db.get_value("Item", good, "image"))
		for item, (reason, skipped) in expected.items():
			row = rows[item]
			self.assertFalse(row["ok"])
			self.assertEqual(bool(row.get("skipped")), skipped, item)
			self.assertIn(reason, row.get("detail") or row.get("error"))
			self.assertFalse(frappe.db.get_value("Item", item, "image"))
		result = _state["result"]
		self.assertEqual((result["succeeded"], result["failed"], result["skipped_mappings"]), (1, 5, 2))

	def test_an_item_save_failure_rolls_back_only_that_item(self):
		bad, good = self._item("SaveFails"), self._item("SaveWorks")
		for item in (bad, good):
			self.shopify.add(
				self._mapping(item).external_reference, variant_url=f"{CDN}/{frappe.scrub(item)}.png"
			)
		real_save = frappe.model.document.Document.save

		def save(doc, *args, **kwargs):
			if doc.doctype == "Item" and doc.name == bad:
				frappe.throw("Item rejected")
			return real_save(doc, *args, **kwargs)

		with patch("frappe.model.document.Document.save", save):
			job_id = self._run()

		rows = self._result(job_id)[1]
		self.assertFalse(rows[bad]["ok"])
		self.assertIn("Item rejected", rows[bad]["error"])
		self.assertFalse(frappe.db.get_value("Item", bad, "image"))
		self.assertTrue(rows[good]["ok"])
		self.assertTrue(frappe.db.get_value("Item", good, "image"))


class IntegrationTestPullMissingImagesLog(ItemImageFixtures, IntegrationTestCase):
	def test_a_run_writes_exactly_one_success_log(self):
		item = self._item("Log")
		self.shopify.add(self._mapping(item).external_reference, variant_url=f"{CDN}/log.png")

		job_id = self._run()

		(log,) = self._logs()
		self.assertEqual((log.status, log.trigger_source), ("Success", "Manual"))
		self.assertEqual(frappe.parse_json(log.request_data), {"job_id": job_id, "owner": "Administrator"})
		response = frappe.parse_json(log.response_data)
		self.assertEqual((response["total"], response["updated"], response["failed"]), (1, 1, 0))

	def test_a_failed_row_makes_the_log_an_error_with_the_row(self):
		item = self._item("LogFail")
		self.shopify.add(self._mapping(item).external_reference, variant_url=f"{CDN}/f.png")

		self._run(http=lambda *a, **k: _response(status=500))

		(log,) = self._logs()
		self.assertEqual(log.status, "Error")
		self.assertEqual(frappe.parse_json(log.response_data)["failed_rows"][0]["mapping_name"], item)

	def test_a_skipped_row_alone_is_still_success(self):
		item = self._item("LogSkip")
		self.shopify.add(self._mapping(item).external_reference)

		self._run()

		(log,) = self._logs()
		self.assertEqual(log.status, "Success")

	def test_whole_job_failure_sets_failed_status_and_writes_an_error_log(self):
		item = self._item("Whole")
		self._mapping(item)
		job_id = frappe.generate_hash(length=16)
		self.job_ids.append(job_id)

		with patch(EXECUTE_TARGET, side_effect=frappe.ValidationError("Shopify exploded")):
			item_images.pull_missing_images(job_id, "Administrator", self.account.name)

		state = frappe.cache().get_value(item_images._status_cache_key(job_id))
		self.assertEqual(state["status"], "failed")
		self.assertIn("Shopify exploded", state["error"])
		(log,) = self._logs()
		self.assertEqual(log.status, "Error")
		self.assertIn("Shopify exploded", log.traceback)

	def test_an_exception_with_an_empty_message_is_still_an_error(self):
		job_id = frappe.generate_hash(length=16)
		self.job_ids.append(job_id)
		# The worker rolls back on a whole-job failure: commit the account first, as the real run does.
		frappe.db.commit()

		with patch(
			"marketplace_integrations.integrations.shopify.item_images.products.get_account",
			side_effect=frappe.PermissionError(),
		):
			item_images.pull_missing_images(job_id, "Administrator", self.account.name)

		state = frappe.cache().get_value(item_images._status_cache_key(job_id))
		self.assertEqual(state["status"], "failed")
		self.assertEqual(state["error"], "PermissionError")
		(log,) = self._logs()
		self.assertEqual(log.status, "Error")
		self.assertIn("PermissionError", log.traceback)

	def test_job_timeout_is_re_raised_not_recorded_against_an_item(self):
		from rq.timeouts import JobTimeoutException

		item = self._item("Timeout")
		self.shopify.add(self._mapping(item).external_reference, variant_url=f"{CDN}/t.png")

		with self.assertRaises(JobTimeoutException):
			self._run(http=lambda *a, **k: (_ for _ in ()).throw(JobTimeoutException()))

		(log,) = self._logs()
		self.assertEqual(log.status, "Error")
		self.assertFalse(frappe.db.get_value("Item", item, "image"))


class IntegrationTestQueuePullMissingImages(ItemImageFixtures, IntegrationTestCase):
	def _queue(self, user="Administrator", enqueue_return="enqueued", account=None):
		with (
			patch(EXECUTE_TARGET) as mocked_execute,
			patch(HTTP_TARGET) as mocked_http,
			patch(ENQUEUE_TARGET, return_value=enqueue_return) as mocked_enqueue,
			self.set_user(user),
		):
			try:
				return item_images.queue_pull_missing_images(account or self.account.name), mocked_enqueue
			finally:
				mocked_execute.assert_not_called()
				mocked_http.assert_not_called()

	def test_queues_a_deduplicated_job_with_no_shopify_call(self):
		result, mocked_enqueue = self._queue()

		job = result["jobs"][0]
		self.job_ids.append(job["job_id"])
		self.assertEqual(job["account"], self.account.name)
		kwargs = mocked_enqueue.call_args.kwargs
		self.assertEqual(kwargs["queue"], "long")
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["job_id"], f"marketplace-shopify-item-images-{self.account.name}")
		self.assertEqual(item_images.get_pull_status(job["job_id"])["status"], "queued")

	def test_a_second_click_while_running_enqueues_nothing_new(self):
		with self.assertRaisesRegex(frappe.ValidationError, "already running"):
			self._queue(enqueue_return=None)

	def test_non_shopify_account_is_refused(self):
		self.account.db_set("platform", "Talabat", update_modified=False)

		with self.assertRaises(frappe.ValidationError):
			self._queue()

	def test_shopify_user_without_item_write_can_queue_and_run(self):
		email, _role = make_role_user(self, "Item Image Shopify", {"Marketplace Account": ["write"]})
		frappe.get_doc("User", email).add_roles("Shopify User")
		item = self._item("NoItemWrite")
		self.shopify.add(self._mapping(item).external_reference, variant_url=f"{CDN}/p.png")

		result, _enqueue = self._queue(user=email)
		self.job_ids.append(result["jobs"][0]["job_id"])
		with self.set_user(email):
			self.assertFalse(frappe.has_permission("Item", "write"))
		job_id = self._run(user=email)

		self.assertTrue(frappe.db.get_value("Item", item, "image"))
		self.assertEqual(self._result(job_id)[0]["status"], "completed")

	def test_talabat_user_is_refused_on_a_shopify_account(self):
		# The platform row filter hides the Shopify account from a Talabat-only user.
		email, _role = make_role_user(self, "Item Image Talabat", {"Marketplace Account": ["write"]})
		frappe.get_doc("User", email).add_roles("Talabat User")

		with self.assertRaises(frappe.PermissionError):
			self._queue(user=email)

	def test_user_without_a_platform_role_is_refused(self):
		email, _role = make_role_user(self, "Item Image No Platform", {"Marketplace Account": ["write"]})

		with self.assertRaises(frappe.PermissionError):
			self._queue(user=email)

	def test_worker_rechecks_account_access_when_the_role_was_removed_after_queueing(self):
		email, _role = make_role_user(self, "Item Image Recheck", {"Marketplace Account": ["write"]})
		user = frappe.get_doc("User", email)
		user.add_roles("Shopify User")
		item = self._item("Recheck")
		self.shopify.add(self._mapping(item).external_reference, variant_url=f"{CDN}/r.png")
		result, _enqueue = self._queue(user=email)
		self.job_ids.append(result["jobs"][0]["job_id"])
		# The worker commits and runs in another process: commit the fixtures, then revoke the role.
		frappe.db.commit()
		user.remove_roles("Shopify User")
		frappe.clear_cache(user=email)
		frappe.db.commit()

		job_id = self._run(user=email)

		state, _rows = self._result(job_id)
		self.assertEqual(state["status"], "failed")
		self.assertFalse(frappe.db.get_value("Item", item, "image"))
		self.assertEqual(self.shopify.calls, [])
		(log,) = self._logs()
		self.assertEqual(log.status, "Error")


class IntegrationTestPullStatus(ItemImageFixtures, IntegrationTestCase):
	def test_another_users_job_id_is_refused(self):
		job_id = frappe.generate_hash(length=16)
		self.job_ids.append(job_id)
		item_images._set_status(job_id, {"owner": "someone@example.com", "status": "running"})

		with self.assertRaises(frappe.PermissionError):
			item_images.get_pull_status(job_id)

	def test_owner_gets_the_state_without_the_owner_key(self):
		job_id = frappe.generate_hash(length=16)
		self.job_ids.append(job_id)
		item_images._set_status(job_id, {"owner": "Administrator", "status": "running"})

		self.assertEqual(item_images.get_pull_status(job_id), {"status": "running"})
