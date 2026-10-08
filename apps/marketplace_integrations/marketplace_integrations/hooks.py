from marketplace_integrations.integrations.shopify.constants import SHOPIFY_INVENTORY_SWEEP_CRON

app_name = "marketplace_integrations"
app_title = "Marketplace Integrations"
app_publisher = "Tamwenat El Zekrayat"
app_description = "ERPNext integration with multiple marketplace platforms (Talabat, Noon, Instashop, Shopify) for Tamwenat El Zekrayat"
app_email = "m7mdd.amirr@gmail.com"
app_license = "mit"

# Apps
# ------------------

required_apps = ["erpnext"]

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "marketplace_integrations",
# 		"logo": "/assets/marketplace_integrations/logo.png",
# 		"title": "Marketplace Integrations",
# 		"route": "/marketplace_integrations",
# 		"has_permission": "marketplace_integrations.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/marketplace_integrations/css/marketplace_integrations.css"
# app_include_js = "/assets/marketplace_integrations/js/marketplace_integrations.js"

# include js, css files in header of web template
# web_include_css = "/assets/marketplace_integrations/css/marketplace_integrations.css"
# web_include_js = "/assets/marketplace_integrations/js/marketplace_integrations.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "marketplace_integrations/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
doctype_js = {"Sales Order": "public/js/sales_order.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "marketplace_integrations/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "marketplace_integrations.utils.jinja_methods",
# 	"filters": "marketplace_integrations.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "marketplace_integrations.install.before_install"
# after_install = "marketplace_integrations.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "marketplace_integrations.uninstall.before_uninstall"
# after_uninstall = "marketplace_integrations.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "marketplace_integrations.utils.before_app_install"
# after_app_install = "marketplace_integrations.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "marketplace_integrations.utils.before_app_uninstall"
# after_app_uninstall = "marketplace_integrations.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "marketplace_integrations.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "marketplace_integrations.notifications.get_notification_config"

# Awesome Bar
# -----------
# Extra search results: list of dicts with label, description, route, index.
# route: ["List", "ToDo"], "/desk/docs/some/page", or "https://example.com"
# awesomebar_search = ["marketplace_integrations.search.awesomebar_results"]

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# Hourly: retry Shopify Webhook Delivery rows stuck at status="Error" that no
# Shopify-initiated redelivery would ever retrigger (webhook_event() already
# responded 200 before the later ASYNC failure) - see
# integrations/shopify/webhooks.py's retry_errored_webhook_deliveries()
# docstring. Bounded by MAX_WEBHOOK_DELIVERY_ATTEMPTS in that same module, not
# a cron-level concern.
#
# Every 2 minutes (cron, since there is no built-in "every_n_minutes" bucket):
# the Shopify inventory changed-Bin sweep (#107 decision 1) -
# integrations/shopify/inventory_sync.py's sweep_changed_bins(). Cheap by
# construction (no Shopify HTTP call, only Bin/mapping/watermark reads plus
# an enqueue) - see that function's own docstring.
#
# Daily: the Shopify inventory full reconciliation (#107 decision 2) -
# integrations/shopify/inventory_sync.py's reconcile_all_accounts(). Same
# no-Shopify-HTTP-call guarantee; recovers anything the sweep's watermark
# could miss.
#
# Neither of these two is a doc_events entry - both are cron/interval
# polling, never a document hook. api/test_talabat_catalog.py's
# test_no_catalog_document_events_are_registered depends on doc_events
# staying empty for every app; do not add one here.
scheduler_events = {
	"hourly": [
		"marketplace_integrations.integrations.shopify.webhooks.retry_errored_webhook_deliveries",
		"marketplace_integrations.integrations.noon.sweep.sweep_all_accounts",
		"marketplace_integrations.integrations.noon.orders.poll_all_accounts",
		"marketplace_integrations.services.sales_order_sync_debounce.sweep_stuck_sales_order_reconciliations",
	],
	"cron": {
		SHOPIFY_INVENTORY_SWEEP_CRON: [
			"marketplace_integrations.integrations.shopify.inventory_sync.sweep_changed_bins",
		],
		"* * * * *": [
			"marketplace_integrations.services.sales_order_sync_debounce.sweep_due_sales_order_syncs",
		],
	},
	"daily": [
		"marketplace_integrations.integrations.shopify.inventory_sync.reconcile_all_accounts",
		"marketplace_integrations.integrations.shopify.orders.reconcile_all_accounts_orders",
		"marketplace_integrations.marketplace_integrations.doctype.marketplace_sync_log.marketplace_sync_log.clear_old_logs",
		# #153 (experimental): daily scan of existing shipment AWBs against
		# Noon's ListReturnReferences - see integrations/noon/returns.py's
		# module docstring for the unverified awb_nr assumption this relies on.
		"marketplace_integrations.integrations.noon.returns.scan_awbs_all_accounts",
	],
}

# Testing
# -------

# before_tests = "marketplace_integrations.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "marketplace_integrations.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "marketplace_integrations.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "marketplace_integrations.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Fixtures
# --------
# Sales Order Item.rate/price_list_rate need more than the site's 2-decimal
# currency_precision so `sales_order_sync.py`'s effective_rate (#85 fix A:
# fulfilled_total_price / fulfilled_quantity) survives ERPNext's own
# `round_floats_in()` inside `calculate_taxes_and_totals()` without being
# truncated back to 2dp before `amount = rate * qty` is computed - otherwise
# a fulfilled amount that does not divide evenly by quantity at 2dp (e.g.
# 9.99 / 2) can never reconstruct exactly, no matter what precision the
# Python-side division itself uses. See `services/sales_order_sync.py`'s
# module docstring and `docs/frappe-conventions.md` for the verified detail.
#
# #136: flags refund evidence (#109) in the UI without any new doc_events -
# a declarative Notification on Marketplace Order (event=Value Change,
# value_changed=refunds, condition=doc.refunds) that Frappe core's own
# Notification controller wires and fires on save; see
# test_no_catalog_document_events_are_registered in
# api/test_talabat_catalog.py, which this fixture does not affect.
#
# #135: opening a Sales Order created by `services/sales_order_sync.py` gave
# zero indication it came from Talabat/Shopify vs. being created manually -
# two Custom Fields on core Sales Order fix that: `custom_marketplace_order`
# (Link to Marketplace Order, set explicitly by
# `services/sales_order_sync.py::_apply_snapshot()` on every sync, both
# platforms) and `custom_marketplace_platform` (Data, `fetch_from:
# custom_marketplace_order.platform`, populated automatically once the first
# field is set and the document is saved/inserted - confirmed to fire on a
# purely server-side `.insert()`/`.save()`, so no separate assignment is
# needed for it).
fixtures = [
	{
		"dt": "Property Setter",
		"filters": [
			[
				"name",
				"in",
				[
					"Sales Order Item-rate-precision",
					"Sales Order Item-price_list_rate-precision",
				],
			]
		],
	},
	{
		"dt": "Notification",
		"filters": [["name", "in", ["Refund Detected on Marketplace Order"]]],
	},
	{
		"dt": "Custom Field",
		"filters": [
			[
				"name",
				"in",
				[
					"Sales Order-custom_marketplace_order",
					"Sales Order-custom_marketplace_platform",
				],
			]
		],
	},
	# #139: the Shopify Reconciliation dashboard's Script Reports (Order Import
	# failures, and the reconciliation summary that replaced the dashboard's
	# original 5 Number Cards with one date-filterable report) ship as native
	# Script Reports instead of a hand-built custom Page or Number Cards - see
	# marketplace_integrations/report/shopify_order_import_failures/ and
	# .../report/shopify_reconciliation_summary/, both of which call the same
	# services/shopify_reconciliation.py query functions this dashboard has
	# always used, so there is one source of truth for each query, not
	# multiple implementations that could drift. Section 1 (catalog sweeps)
	# is not reproduced here at all - the Workspace links straight to the
	# existing "Marketplace Shopify Catalog Mapping" page instead. The
	# dashboard's original "Shopify Refund Evidence Gaps" and general-purpose
	# "Shopify Refunded Orders" Script Reports were later removed as redundant
	# with the Reconciliation Summary's Total Refunded Amount metric, replaced
	# by a "Show Refunded Orders" quick filter button on the Marketplace Order
	# list view (marketplace_integrations/doctype/marketplace_order/
	# marketplace_order_list.js) instead of a Script Report. The dashboard's
	# "Shopify Reconciliation" Workspace itself ships as a module file
	# (marketplace_integrations/workspace/shopify_reconciliation/), not a
	# fixture - frappe's migrate-time orphan-entity sweep
	# (frappe.model.sync.remove_orphan_entities) deletes any public Workspace
	# with no matching on-disk .../workspace/<name>/<name>.json file, which a
	# fixtures/workspace.json entry can't satisfy. Script Reports follow the
	# same app-level-folder convention as doctype/ and page/ - every
	# report/<name>/<name>.json under the module is synced automatically by
	# bench migrate, no fixtures/hooks.py wiring needed. There is no longer a
	# "Number Card" fixtures entry: the 5 Number Cards this dashboard used to
	# ship (Shopify Catalog Push Errors, Shopify Inventory Sync Errors,
	# Shopify Webhook Deliveries: Error/Exhausted Retries, Marketplace
	# Orders: Sync Error) are superseded by the "Shopify Reconciliation
	# Summary" report above, which reproduces their exact filter logic as its
	# first 5 (date-filter-independent, current-state) metric rows.
]

# Request Events
# ----------------
before_request = [
	"marketplace_integrations.api.request.normalize_talabat_webhook_authorization",
]
# after_request = ["marketplace_integrations.utils.after_request"]

# Job Events
# ----------
# before_job = ["marketplace_integrations.utils.before_job"]
# after_job = ["marketplace_integrations.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"marketplace_integrations.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
