if (typeof frappe !== "undefined") {
	frappe.listview_settings["Marketplace Order"] = {
		// Not all in_list_view: the indicator reads erpnext_sync_status and the
		// status formatter reads the Shopify financial/fulfillment statuses
		// (hidden doctype fields, fetched here regardless).
		add_fields: ["erpnext_sync_status", "external_financial_status", "external_fulfillment_status", "platform"],
		get_indicator: get_order_indicator,
		formatters: {
			external_status(value, df, doc) {
				const text = frappe.utils.escape_html(platform_status_text(doc));
				return `<span class="ellipsis" title="${text}">${text}</span>`;
			},
		},
		onload(listview) {
			listview.page.add_inner_button(__("Needs Attention"), () => {
				frappe.set_route("query-report", "Marketplace Needs Attention");
			});
			listview.page.add_inner_button(__("Retrieve from Talabat"), () => {
				show_talabat_retrieval_dialog(listview);
			});
			listview.page.add_inner_button(__("Retrieve from Shopify"), () => {
				show_shopify_retrieval_dialog(listview);
			});
			listview.page.add_inner_button(__("Retrieve from Noon"), () => {
				show_noon_retrieval_dialog(listview);
			});
			listview.page.add_inner_button(__("Show Refunded Orders"), () => {
				show_refunded_orders(listview);
			});
			if (frappe.model.can_write("Marketplace Order")) {
				listview.page.add_actions_menu_item(__("Retry ERPNext Sync"), () => {
					retry_erpnext_sync(listview);
				});
			}
		},
	};
}

// #169: one colour per ERPNext sync state so a merchant can scan the list for
// problems without opening each order.
const ORDER_SYNC_STATUS_COLORS = {
	Synced: "green",
	"Not Synced": "grey",
	"Modified After Sync": "orange",
	"Sync Error": "red",
};

function get_order_indicator(doc) {
	const status = doc.erpnext_sync_status || "Not Synced";
	return [__(status), ORDER_SYNC_STATUS_COLORS[status] || "grey", `erpnext_sync_status,=,${status}`];
}

// Shopify's meaningful state is its financial + fulfillment status; the other
// platforms have one external_status. Returned unescaped - callers escape.
function platform_status_text(doc) {
	if (doc.platform === "Shopify") {
		const parts = [doc.external_financial_status, doc.external_fulfillment_status].filter(Boolean);
		if (parts.length) return parts.join(" / ");
	}
	return doc.external_status || "";
}

// Frappe stores the server's message in responseJSON._server_messages: a JSON
// array of JSON strings, each carrying a `message`. Decode entities and drop
// markup here; the summary escapes it again at render time.
const ORDER_HTML_ENTITIES = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" };

function order_server_message_text(error) {
	const raw = error && error.responseJSON && error.responseJSON._server_messages;
	if (!raw) return "";
	try {
		return JSON.parse(raw)
			.map((entry) => JSON.parse(entry).message || "")
			.map((message) =>
				String(message)
					.replace(/&(amp|lt|gt|quot|#39);/g, (entity) => ORDER_HTML_ENTITIES[entity])
					.replace(/<[^>]*>/g, "")
					.trim()
			)
			.filter(Boolean)
			.join(" ");
	} catch (parse_error) {
		return "";
	}
}

// sync_to_erpnext returns an expected blocker as {ok: false, error} instead of
// raising, so a fulfilled call is only a success when `ok` is not false.
function summarize_retry_results(names, results) {
	const succeeded = [];
	const failed = [];
	results.forEach((result, index) => {
		if (result.status === "fulfilled") {
			const value = (result.value && result.value.message) || {};
			if (value.ok === false) {
				failed.push({ name: names[index], text: value.error || __("The sync was not completed.") });
			} else {
				succeeded.push(names[index]);
			}
		} else {
			const reason = result.reason;
			failed.push({
				name: names[index],
				text: order_server_message_text(reason) || (reason && reason.message) || __("The request failed."),
			});
		}
	});
	return { succeeded, failed };
}

function format_retry_summary({ succeeded, failed }) {
	const lines = [__("Succeeded: {0}", [succeeded.length]), __("Failed: {0}", [failed.length])].map(
		(line) => `<p>${frappe.utils.escape_html(line)}</p>`
	);
	if (failed.length) {
		lines.push(
			"<ul>" +
				failed
					.map((item) => `<li>${frappe.utils.escape_html(item.name)}: ${frappe.utils.escape_html(item.text)}</li>`)
					.join("") +
				"</ul>"
		);
	}
	return {
		title: __("Retry ERPNext Sync"),
		indicator: failed.length ? "orange" : "green",
		message: lines.join(""),
	};
}

const RETRY_CONCURRENCY = 3;

// Like Promise.allSettled over `items.map(task)`, but with at most `limit`
// tasks in flight; results keep the input order.
function settle_with_concurrency(items, limit, task) {
	const results = new Array(items.length);
	let next = 0;
	function run_next() {
		if (next >= items.length) return Promise.resolve();
		const index = next++;
		return Promise.resolve()
			.then(() => task(items[index]))
			.then(
				(value) => {
					results[index] = { status: "fulfilled", value };
				},
				(reason) => {
					results[index] = { status: "rejected", reason };
				}
			)
			.then(run_next);
	}
	const workers = Array.from({ length: Math.min(limit, items.length) }, run_next);
	return Promise.all(workers).then(() => results);
}

function retry_erpnext_sync(listview) {
	const names = listview.get_checked_items(true);
	if (!names.length) return;
	return settle_with_concurrency(names, RETRY_CONCURRENCY, (name) =>
		frappe.call({
			method: "marketplace_integrations.api.marketplace_orders.sync_to_erpnext",
			args: { marketplace_order: name },
			silent: true,
		})
	).then((results) => {
		listview.clear_checked_items();
		listview.refresh();
		frappe.msgprint(format_retry_summary(summarize_retry_results(names, results)));
	});
}

// Account-level Noon import: queued on the long queue (deduplicated per
// account), so there is no date range - same endpoint as the Marketplace
// Account form's Noon import button.
function show_noon_retrieval_dialog(listview) {
	const dialog = new frappe.ui.Dialog({
		title: __("Retrieve Noon Orders"),
		fields: [
			{
				fieldname: "account",
				fieldtype: "Link",
				label: __("Marketplace Account"),
				options: "Marketplace Account",
				get_query: () => ({ filters: { platform: "Noon", enabled: 1 } }),
				reqd: 1,
			},
		],
		primary_action_label: __("Retrieve"),
		primary_action(values) {
			dialog.hide();
			frappe
				.call({
					method: "marketplace_integrations.api.noon_orders.import_orders",
					args: { account: values.account },
					freeze: true,
					freeze_message: __("Queuing Noon order import..."),
				})
				.then((response) => {
					frappe.msgprint(format_noon_import_result(response.message));
					listview.refresh();
				});
		},
	});
	dialog.show();
}

function format_noon_import_result(result) {
	const queued = Boolean(result && result.queued);
	return {
		title: __("Noon Retrieval"),
		indicator: queued ? "green" : "orange",
		message: queued
			? __("Noon order import queued. Refresh Marketplace Orders to see the results once it finishes.")
			: __("A Noon order import is already queued or running for this account."),
	};
}

// Replaces the removed "Shopify Refund Evidence Gaps"/"Shopify Refunded
// Orders" Script Reports (#139 redundant with the "Shopify Reconciliation
// Summary" report's own Total Refunded Amount metric) with a plain list view
// quick filter: platform = Shopify AND external_financial_status in
// (REFUNDED, PARTIALLY_REFUNDED) - frappe.listview.filter_area.add() takes an
// array of [doctype, fieldname, condition, value] tuples, matching Frappe's
// own list view filter API.
function show_refunded_orders(listview) {
	listview.filter_area.add([
		["Marketplace Order", "platform", "=", "Shopify"],
		["Marketplace Order", "external_financial_status", "in", ["REFUNDED", "PARTIALLY_REFUNDED"]],
	]);
}

function show_shopify_retrieval_dialog(listview) {
	const dialog = new frappe.ui.Dialog({
		title: __("Retrieve Shopify Orders"),
		fields: [
			{ fieldname: "account", fieldtype: "Link", label: __("Marketplace Account"), options: "Marketplace Account", get_query: () => ({ filters: { platform: "Shopify", enabled: 1 } }), reqd: 1 },
			{ fieldname: "start_date", fieldtype: "Date", label: __("Start Date"), reqd: 1 },
			{ fieldname: "end_date", fieldtype: "Date", label: __("End Date"), reqd: 1 },
		],
		primary_action_label: __("Retrieve"),
		primary_action(values) {
			const range = build_utc_range(values);
			if (range.error) { frappe.msgprint(range.error); return; }
			dialog.hide();
			frappe.call({
				method: "marketplace_integrations.api.shopify_orders.queue_order_import",
				args: { account: values.account, start: range.start, end: range.end },
			}).then((response) => {
				const job_id = response.message.job_id;
				frappe.show_alert({ message: __("Shopify order import queued: {0}", [job_id]), indicator: "blue" });
				// Review finding #7: get_order_import_status() was never wired to
				// any UI beyond this one-shot "queued" toast - reuse the same
				// job-status polling dialog #106/#107 already built for the
				// catalog push/inventory sync jobs, rather than a new one.
				frappe.require("/assets/marketplace_integrations/js/shopify_catalog_push_status.js").then(() => {
					new frappe.ui.ShopifyCatalogPushStatus({
						jobs: [{ job_id, account: values.account }],
						on_done: () => listview.refresh(),
						...frappe.ui.ShopifyCatalogPushStatus.ORDER_IMPORT_OPTIONS,
					});
				});
			});
		},
	});
	dialog.show();
}

function show_talabat_retrieval_dialog(listview) {
	const dialog = new frappe.ui.Dialog({
		title: __("Retrieve Talabat Orders"),
		fields: [
			{
				fieldname: "account",
				fieldtype: "Link",
				label: __("Marketplace Account"),
				options: "Marketplace Account",
				get_query: () => ({ filters: { platform: "Talabat", enabled: 1 } }),
				reqd: 1,
			},
			{
				fieldname: "start_date",
				fieldtype: "Date",
				label: __("Start Date"),
				description: __("Retrieved from the beginning of this UTC date."),
				reqd: 1,
			},
			{
				fieldname: "end_date",
				fieldtype: "Date",
				label: __("End Date"),
				description: __("Retrieved through the end of this UTC date."),
				reqd: 1,
			},
		],
		primary_action_label: __("Retrieve"),
		primary_action(values) {
			const range = build_utc_range(values);
			if (range.error) {
				frappe.msgprint(range.error);
				return;
			}
			dialog.hide();
			frappe
				.call({
					method: "marketplace_integrations.api.talabat_orders.import_vendor_orders",
					args: { account: values.account, start: range.start, end: range.end },
					freeze: true,
					freeze_message: __("Retrieving orders from Talabat..."),
				})
				.then((response) => {
					frappe.msgprint(format_import_result(response.message));
					listview.refresh();
				});
		},
	});
	dialog.show();
}

function build_utc_range(values) {
	const start_date = values.start_date;
	const end_date = values.end_date;
	if (!is_iso_date(start_date) || !is_iso_date(end_date)) {
		return { error: __("Start Date and End Date must be valid dates.") };
	}
	if (start_date > end_date) {
		return { error: __("Start Date must be before or equal to End Date.") };
	}
	return { start: `${start_date}T00:00:00Z`, end: `${end_date}T23:59:59Z` };
}

function is_iso_date(value) {
	if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
		return false;
	}
	return new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value;
}

function format_import_result(result) {
	const errors = result.errors || [];
	return {
		title: __("Talabat Retrieval Complete"),
		indicator: errors.length ? "orange" : "green",
		message: errors.length
			? __("Retrieved {0} orders. {1} could not be imported.", [result.count, errors.length])
			: __("Retrieved {0} orders.", [result.count]),
	};
}

if (typeof module !== "undefined") {
	module.exports = {
		build_utc_range,
		format_import_result,
		format_noon_import_result,
		format_retry_summary,
		get_order_indicator,
		platform_status_text,
		retry_erpnext_sync,
		settle_with_concurrency,
		show_refunded_orders,
		summarize_retry_results,
	};
}
