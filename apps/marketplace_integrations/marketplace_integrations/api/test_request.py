# Copyright (c) 2026, Tamwenat El Zekrayat and contributors

from contextlib import contextmanager
from unittest import TestCase
from unittest.mock import patch

from werkzeug.wrappers import Request

from marketplace_integrations.api import request


def _request(path, authorization):
	return Request.from_values(
		path=path,
		headers={"Authorization": authorization} if authorization is not None else {},
	)


@contextmanager
def _bound_request(web_request):
	request.frappe.local.request = web_request
	try:
		yield
	finally:
		del request.frappe.local.request


class TestTalabatWebhookAuthorization(TestCase):
	def test_normalizes_bearer_authorization_for_talabat_webhook(self):
		web_request = _request(request.TALABAT_ORDER_WEBHOOK_PATH, "Bearer talabat-secret")
		with (
			_bound_request(web_request),
			patch.object(request.frappe, "request", web_request),
		):
			request.normalize_talabat_webhook_authorization()
			self.assertEqual(request.frappe.get_request_header("Authorization"), "talabat-secret")

		self.assertEqual(web_request.environ["HTTP_AUTHORIZATION"], "talabat-secret")

	def test_preserves_authorization_for_non_webhook_routes(self):
		web_request = _request("/api/method/frappe.auth.get_logged_user", "Bearer oauth-token")
		with _bound_request(web_request):
			request.normalize_talabat_webhook_authorization()

		self.assertEqual(web_request.environ["HTTP_AUTHORIZATION"], "Bearer oauth-token")

	def test_preserves_raw_authorization_for_talabat_webhook(self):
		web_request = _request(request.TALABAT_ORDER_WEBHOOK_PATH, "talabat-secret")
		with _bound_request(web_request):
			request.normalize_talabat_webhook_authorization()

		self.assertEqual(web_request.environ["HTTP_AUTHORIZATION"], "talabat-secret")
