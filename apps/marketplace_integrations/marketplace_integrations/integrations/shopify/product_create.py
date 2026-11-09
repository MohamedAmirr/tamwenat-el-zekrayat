# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Create a draft Shopify product from an ERPNext Item and map it (#494).

Sends title (``item_name``) and status DRAFT through ``productCreate``, then
sets the default variant's SKU to ``item_code`` through
``productVariantsBulkUpdate``, then writes the Marketplace Product Mapping
through ``products.upsert_mapping()``. Price and stock reach Shopify later
through the existing #106 price push and #107 inventory sync.

Shopify has no idempotency key for ``productCreate``. A retry after an unknown
outcome can therefore create a duplicate product, so this module never retries
on its own: any failure after Shopify may have created the product tells staff
to check Shopify first (and names the GIDs when they are known).
"""

import hashlib

import frappe
from frappe import _
from frappe.utils import cstr
from frappe.utils.synchronization import filelock

from marketplace_integrations.integrations.shopify import products
from marketplace_integrations.integrations.shopify.client import ShopifyClient

MAPPING_DOCTYPE = "Marketplace Product Mapping"

ERROR_LOG_TITLE = "Shopify product create failed"
# Long enough for a concurrent job's Shopify calls; the waiter then sees the mapping and refuses.
LOCK_TIMEOUT = 120
RESULT_TTL = 60 * 60
ERROR_MESSAGE_MAX_LENGTH = 500

_RQ_JOB_ID_PREFIX = "shopify_create_product"

_PRODUCT_CREATE_MUTATION = """
mutation MarketplaceProductCreate($product: ProductCreateInput!) {
  productCreate(product: $product) {
    product {
      id
      variants(first: 1) {
        nodes {
          id
        }
      }
    }
    userErrors {
      field
      message
    }
  }
}
"""

_SET_SKU_MUTATION = """
mutation MarketplaceProductCreateSetSku($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants {
      id
    }
    userErrors {
      field
      message
    }
  }
}
"""


def job_id_for(account, item):
	return f"{_RQ_JOB_ID_PREFIX}::{account}::{item}"


def queue_product_create(account, item):
	"""Authorize, run local checks only (no Shopify call) and enqueue the
	create. A second queue for the same (account, item) while one is queued or
	running is rejected."""
	account_doc, connection = products.get_account(account)
	account_doc.check_permission("write")
	if not account_doc.enabled:
		frappe.throw(_("Marketplace Account {0} is disabled.").format(account_doc.name))
	if not connection.enabled:
		frappe.throw(_("Shopify Connection {0} is disabled.").format(connection.name))
	if not frappe.has_permission(MAPPING_DOCTYPE, ptype="create"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	item = cstr(item).strip()
	if not item:
		frappe.throw(_("Item is required."))
	item_doc = frappe.get_doc("Item", item)
	item_doc.check_permission("read")
	_check_item_eligible(item_doc)

	if frappe.db.exists(MAPPING_DOCTYPE, {"marketplace_account": account_doc.name, "item": item_doc.name}):
		frappe.throw(
			_("Item {0} is already mapped on Marketplace Account {1}.").format(item_doc.name, account_doc.name)
		)

	owner = frappe.session.user
	# The RQ job id is fixed per (account, item) for dedup. The status id is random per
	# request, so a rejected duplicate can never touch the running job's status or owner.
	result_id = frappe.generate_hash(length=16)
	_set_status(result_id, {"owner": owner, "status": "queued"})
	job = frappe.enqueue(
		create_product,
		queue="long",
		timeout=600,
		job_id=job_id_for(account_doc.name, item_doc.name),
		deduplicate=True,
		job_id_for_result=result_id,
		owner=owner,
		account=account_doc.name,
		item=item_doc.name,
	)
	if job is None:
		frappe.cache().delete_value(_cache_key(result_id))
		frappe.throw(
			_("A Shopify product create is already running for Item {0} on Marketplace Account {1}.").format(
				item_doc.name, account_doc.name
			)
		)
	return {"job_id": result_id, "status": "queued"}


def get_product_create_status(job_id):
	state = frappe.cache().get_value(_cache_key(job_id))
	if not state or state.get("owner") != frappe.session.user:
		frappe.throw(_("Product create job not found."), frappe.PermissionError)
	return {key: value for key, value in state.items() if key != "owner"}


def list_product_create_accounts():
	"""Enabled Shopify accounts the current user may create products on
	(Marketplace Account ``write``), for the Item form dialog. Empty when the
	user cannot create mappings."""
	if not frappe.has_permission(MAPPING_DOCTYPE, ptype="create") or not frappe.has_permission(
		"Marketplace Account", ptype="read"
	):
		return []
	accounts = frappe.get_list(
		"Marketplace Account",
		filters={"platform": "Shopify", "enabled": 1},
		fields=["name", "account_name"],
		order_by="account_name asc",
		limit_page_length=0,
	)
	return [row for row in accounts if frappe.has_permission("Marketplace Account", "write", doc=row.name)]


def create_product(job_id_for_result, owner, account, item):
	"""Background worker. Runs as the user who queued it. A per-(account, item)
	file lock serialises the already-mapped check, the Shopify create and the
	mapping write, so two near-simultaneous jobs cannot both create a product:
	the second waits, then sees the mapping and refuses."""
	# `progress` records what is known when something fails: only a failure after Shopify
	# was called is logged (a routine refusal is not an error worth an Error Log row).
	progress = {"shopify_called": False, "product_gid": None, "variant_gid": None}
	try:
		_update_status(job_id_for_result, owner, status="running")
		with filelock(_lock_name(account, item), timeout=LOCK_TIMEOUT):
			changes = _create_locked(account, item, progress)
		_update_status(job_id_for_result, owner, **changes)
	except Exception as exc:
		if progress["shopify_called"]:
			_log_failure(account, item, progress, frappe.get_traceback())
		_update_status(job_id_for_result, owner, status="failed", error=cstr(exc)[:ERROR_MESSAGE_MAX_LENGTH * 2])


def _log_failure(account, item, progress, details):
	frappe.log_error(
		title=ERROR_LOG_TITLE,
		message=f"account={account} item={item} product={progress['product_gid']} variant={progress['variant_gid']}\n\n{details}",
	)


def _lock_name(account, item):
	return f"shopify_product_create_{hashlib.sha1(f'{account}::{item}'.encode()).hexdigest()}"


def _create_locked(account, item, progress):
	"""Runs under the lock. Returns the status changes for a finished job, or raises."""
	account_doc, connection = products.get_account(account)
	item_doc = frappe.get_doc("Item", item)
	# Re-check: permissions, the account, Item or mapping may have changed since queueing.
	account_doc.check_permission("write")
	if not frappe.has_permission(MAPPING_DOCTYPE, ptype="create"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	item_doc.check_permission("read")
	if not account_doc.enabled:
		frappe.throw(_("Marketplace Account {0} is disabled.").format(account_doc.name))
	if not connection.enabled:
		frappe.throw(_("Shopify Connection {0} is disabled.").format(connection.name))
	_check_item_eligible(item_doc)
	if frappe.db.exists(MAPPING_DOCTYPE, {"marketplace_account": account_doc.name, "item": item_doc.name}):
		frappe.throw(
			_("Item {0} is already mapped on Marketplace Account {1}.").format(item_doc.name, account_doc.name)
		)
	client = ShopifyClient(connection.name)

	progress["shopify_called"] = True
	try:
		created = client.execute(
			_PRODUCT_CREATE_MUTATION,
			{"product": {"title": item_doc.item_name, "status": "DRAFT"}},
		)
	except Exception as exc:
		raise ProductCreateError(
			_(
				"Shopify did not confirm the product create ({0}). It may have been created: check Shopify for a product titled {1} before retrying."
			).format(cstr(exc)[:ERROR_MESSAGE_MAX_LENGTH], item_doc.item_name)
		) from exc

	payload = (created or {}).get("productCreate") or {}
	_raise_user_errors(payload.get("userErrors"))
	product = payload.get("product") or {}
	product_gid = cstr(product.get("id")).strip()
	nodes = ((product.get("variants") or {}).get("nodes")) or []
	variant_gid = cstr((nodes[0] or {}).get("id")).strip() if nodes else ""
	if not product_gid or not variant_gid:
		raise ProductCreateError(
			_(
				"Shopify's response did not include the product and variant ids. It may have been created: check Shopify for a product titled {0} before retrying."
			).format(item_doc.item_name)
		)
	progress["product_gid"] = product_gid
	progress["variant_gid"] = variant_gid

	# A SKU failure still maps the product: both GIDs are known, and the mapping makes the
	# duplicate guard refuse a second create. The user fixes the SKU in Shopify.
	sku_error = _set_sku(client, product_gid, variant_gid, item_doc.name)

	try:
		mapping = products.upsert_mapping(
			account_doc.name,
			variant_gid,
			item_doc.name,
			external_product_id=product_gid,
			external_sku=None if sku_error else item_doc.name,
		)
		# Commit the mapping before the status write so the rollback below only ever
		# discards a failed mapping write.
		frappe.db.commit()
	except Exception as exc:
		frappe.db.rollback()
		message = _(
			"Shopify created product {0} (variant {1}) but saving the mapping failed: {2}. Check Shopify before retrying."
		).format(product_gid, variant_gid, cstr(exc)[:ERROR_MESSAGE_MAX_LENGTH])
		if sku_error:
			message = f"{message} {_('Setting its SKU also failed: {0}.').format(sku_error)}"
		raise ProductCreateError(message) from exc

	result = {"mapping": mapping["name"], "product_id": product_gid, "variant_id": variant_gid}
	if sku_error:
		_log_failure(account, item, progress, f"SKU step failed: {sku_error}")
		return {
			"status": "failed",
			"mapped": True,
			"result": result,
			"error": _(
				"Shopify created product {0} (variant {1}) and it is mapped, but setting its SKU failed: {2}. Fix the SKU in Shopify; do not create it again."
			).format(product_gid, variant_gid, sku_error),
		}
	return {"status": "completed", "result": result}


class ProductCreateError(Exception):
	pass


def _check_item_eligible(item_doc):
	if item_doc.disabled:
		frappe.throw(_("Item {0} is disabled.").format(item_doc.name))
	if item_doc.has_variants:
		frappe.throw(_("Item {0} is a template and cannot be created on Shopify.").format(item_doc.name))
	if item_doc.variant_of:
		frappe.throw(_("Item {0} is an Item variant and cannot be created on Shopify.").format(item_doc.name))
	if frappe.db.exists("Product Bundle", {"new_item_code": item_doc.name}):
		frappe.throw(_("Item {0} is a Product Bundle and cannot be created on Shopify.").format(item_doc.name))
	if not item_doc.is_stock_item:
		frappe.throw(_("Item {0} is not a stock Item.").format(item_doc.name))


def _set_sku(client, product_gid, variant_gid, sku):
	"""Return None on success, else an error text."""
	try:
		response = client.execute(
			_SET_SKU_MUTATION,
			{"productId": product_gid, "variants": [{"id": variant_gid, "inventoryItem": {"sku": sku}}]},
		)
	except Exception as exc:
		return cstr(exc)[:ERROR_MESSAGE_MAX_LENGTH]
	payload = (response or {}).get("productVariantsBulkUpdate") or {}
	message = _format_user_errors(payload.get("userErrors"))
	if message:
		return message
	returned = {node.get("id") for node in (payload.get("productVariants") or []) if isinstance(node, dict)}
	if variant_gid not in returned:
		return _("Shopify did not confirm the SKU update.")
	return None


def _format_user_errors(user_errors):
	messages = [
		cstr(error.get("message")).strip()
		for error in (user_errors or [])
		if isinstance(error, dict) and cstr(error.get("message")).strip()
	]
	return "; ".join(messages)[:ERROR_MESSAGE_MAX_LENGTH]


def _raise_user_errors(user_errors):
	if user_errors:
		message = _format_user_errors(user_errors)
		raise ProductCreateError(message or _("Shopify did not accept the product."))


def _cache_key(job_id):
	return f"marketplace_integrations:shopify:product_create_job:{job_id}"


def _set_status(job_id, state):
	frappe.cache().set_value(_cache_key(job_id), state, expires_in_sec=RESULT_TTL)


def _update_status(job_id, owner, **changes):
	state = frappe.cache().get_value(_cache_key(job_id)) or {"owner": owner}
	state.update(changes)
	_set_status(job_id, state)
