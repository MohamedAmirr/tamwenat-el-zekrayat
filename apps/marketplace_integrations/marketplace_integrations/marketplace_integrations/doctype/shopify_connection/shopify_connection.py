# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document

from marketplace_integrations.integrations.shopify.client import get_access_token_cache_key

# A bare hostname only: letters, digits, dots, and hyphens, and not starting
# or ending with a dot. Deliberately stricter than "no scheme/path/whitespace"
# - get_store_base_url() builds the full URL by simply string-prefixing
# "https://" onto this value, so any character with special meaning in a URL
# (e.g. "@", which triggers userinfo/host confusion - "evil.example@attacker"
# resolves to attacker, not evil.example - plus "?", "#", ":") must be
# rejected here rather than only scheme/path/whitespace.
_VALID_SHOP_DOMAIN = re.compile(r"^[A-Za-z0-9.-]+$")


class ShopifyConnection(Document):
	def validate(self):
		self.validate_shop_domain()

	def validate_shop_domain(self):
		"""Reject anything other than a bare hostname - it must be a bare host
		value, since get_store_base_url() builds the full URL by simply
		prefixing "https://". Deliberately does NOT hardcode a
		".myshopify.com" suffix check - the store's real domain format is an
		open question in docs/shopify/README.md, not decided by this issue."""
		shop_domain = (self.shop_domain or "").strip()
		if not shop_domain:
			frappe.throw(_("Shop Domain is mandatory."))

		if shop_domain != self.shop_domain:
			frappe.throw(_("Shop Domain must not carry leading or trailing whitespace."))

		if not _VALID_SHOP_DOMAIN.match(shop_domain):
			frappe.throw(
				_(
					"Shop Domain must be a bare hostname containing only letters, digits, dots, "
					"and hyphens (no scheme, path, port, userinfo, or query/fragment)."
				)
			)

		if shop_domain.startswith(".") or shop_domain.endswith("."):
			frappe.throw(_("Shop Domain must not start or end with a dot."))

	def on_update(self):
		"""A saved connection may have rotated credentials or a changed shop
		domain. Clearing its cached token on every update is safe and prevents
		a stale cached token from being reused against a different store or
		with credentials that no longer authenticate it. Mirrors
		Talabat Connection.on_update()."""
		frappe.cache().delete_value(get_access_token_cache_key(self.name))

	def on_trash(self):
		"""A deleted connection's cached token must not linger and be reused
		if a new connection later reuses the same document name. Mirrors
		Talabat Connection.on_trash()."""
		frappe.cache().delete_value(get_access_token_cache_key(self.name))

	def get_store_base_url(self):
		"""Return this connection's Shopify store base URL. Deliberately just
		URL construction - HTTP calls themselves belong in
		integrations/shopify/client.py's ShopifyClient, not here."""
		return f"https://{self.shop_domain}"
