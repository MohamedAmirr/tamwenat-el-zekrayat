// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Shopify Reconciliation dashboard's custom Page (#139 sections 1, 4, 6) -
// the sections a static Workspace Number Card can't express: a live cache
// read plus an action button (Section 1), a filtered log table (Section 4),
// and a live cross-field query (Section 6). Sections 2, 3, 5 are the
// Workspace's own Number Cards (fixtures/number_card.json), not duplicated
// here.
//
// Section 1's "Run sweep now" reuses the exact queue_catalog_sweep() ->
// watch_sweep() realtime-polling pattern already implemented in
// marketplace_shopify_catalog_mapping.js (frappe.realtime event
// "marketplace_shopify_catalog_sweep_progress") rather than reinventing it -
// adapted here to run per-account (this page shows every enabled Shopify
// account at once, not one account picked from a dropdown).

// Module-scope (not inside on_page_load's closure) so shopify_reconciliation_dashboard.test.js
// can require() and unit-test the escaping decision directly, the same way
// shopify_catalog_push_status.js's helpers are tested.
function comment_when(datetime_str) {
	return frappe.datetime.comment_when
		? frappe.datetime.comment_when(datetime_str)
		: frappe.utils.escape_html(datetime_str);
}

// Builds the "Catalog Sweeps" status-cell message and says how it must be
// inserted into the DOM: mode "html" for the success message, which embeds a
// real frappe-timestamp <span> from comment_when() and must go through
// .html() to render (not .text(), which was the bug - it showed the span
// markup as literal escaped text instead of a rendered timestamp element);
// mode "text" for the failed/no-sweep-yet messages, which may carry a raw
// API-controlled error string and must stay escaped via .text().
//
// Safe to use .html() for the success message: comment_when()'s markup is
// built only from last_sweep.completed_at (a server-generated datetime,
// never an API/user-controlled string), and every other value interpolated
// into the message (counts, totals) is a plain integer, never a raw string -
// so nothing user/API-controlled is ever inserted as HTML.
function build_last_sweep_message(last_sweep) {
	if (!last_sweep) {
		return { mode: "text", content: __("No sweep has run yet.") };
	}
	if (last_sweep.status === "failed") {
		return { mode: "text", content: __("Last sweep failed: {0}", [last_sweep.error || ""]) };
	}
	const result = last_sweep.result || {};
	const exceptions = result.exceptions || [];
	const counts = {};
	exceptions.forEach((row) => {
		counts[row.category] = (counts[row.category] || 0) + 1;
	});
	const age = last_sweep.completed_at ? comment_when(last_sweep.completed_at) : __("unknown");
	let text = __("Last swept {0}. {1} exception(s) (Unmapped: {2}, Deleted: {3}, Variant-less: {4}).", [
		age,
		result.total_exceptions != null ? result.total_exceptions : exceptions.length,
		counts["Unmapped"] || 0,
		counts["Deleted"] || 0,
		counts["Variant-less"] || 0,
	]);
	if (result.truncated) {
		text += " " + __("(list truncated)");
	}
	if (result.refresh_failures) {
		text += " " + __("{0} row(s) failed to refresh, see Error Log.", [result.refresh_failures]);
	}
	return { mode: "html", content: text };
}

frappe.pages["shopify-reconciliation-dashboard"].on_page_load = function (wrapper) {
	// Standard page container styling (margin/padding matching this app's other
	// custom Pages, e.g. marketplace_shopify_catalog_mapping.css) - without
	// this the container div has no CSS of its own and renders edge-to-edge
	// instead of centered/contained like the rest of Desk.
	frappe.require("/assets/marketplace_integrations/css/shopify_reconciliation_dashboard.css");

	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Shopify Reconciliation Dashboard"),
		single_column: true,
	});
	const $container = $('<div class="shopify-reconciliation-dashboard-container"></div>').appendTo(page.body);

	const $sweep_section = $("<div></div>").appendTo($container);
	const $order_import_section = $("<div></div>").appendTo($container);
	const $refund_section = $("<div></div>").appendTo($container);

	page.set_primary_action(__("Refresh"), () => load_all(), "refresh");

	load_all();

	function load_all() {
		render_sweep_section($sweep_section);
		render_order_import_section($order_import_section);
		render_refund_section($refund_section);
	}

	// --- Section 1: unmapped variants / last catalog sweep, per account ---

	function render_sweep_section($el) {
		$el.empty();
		$("<h4></h4>").text(__("Catalog Sweeps")).appendTo($el);
		const $intro = $('<p class="text-muted"></p>').appendTo($el);
		$intro.text(
			__("Last catalog sweep per enabled Shopify account. See the ")
		);
		$('<a></a>')
			.attr("href", "/app/marketplace-shopify-catalog-mapping")
			.text(__("Marketplace Shopify Catalog Mapping"))
			.appendTo($intro);
		$intro.append(document.createTextNode(" " + __("page for live exception detail.")));
		const $table = $('<table class="table table-bordered"></table>').appendTo($el);
		const $tbody = $("<tbody></tbody>").appendTo($table);

		frappe
			.call({
				method: "frappe.client.get_list",
				args: {
					doctype: "Marketplace Account",
					filters: { platform: "Shopify", enabled: 1 },
					fields: ["name"],
					limit_page_length: 0,
				},
			})
			.then((r) => {
				const accounts = (r.message || []).map((row) => row.name);
				if (!accounts.length) {
					$tbody.append($("<tr><td></td></tr>").find("td").text(__("No enabled Shopify accounts.")).end());
					return;
				}
				accounts.forEach((account_name) => render_account_row($tbody, account_name));
			});
	}

	function render_account_row($tbody, account_name) {
		const $row = $("<tr></tr>").appendTo($tbody);
		const $name_cell = $("<td></td>").text(account_name).appendTo($row);
		const $status_cell = $("<td></td>").text(__("Loading...")).appendTo($row);
		const $action_cell = $("<td></td>").appendTo($row);
		const $button = $('<button class="btn btn-default btn-xs"></button>')
			.text(__("Run sweep now"))
			.appendTo($action_cell);

		load_last_sweep(account_name, $status_cell);

		$button.on("click", () => {
			$button.prop("disabled", true);
			$status_cell.text(__("Queuing catalog sweep..."));
			frappe
				.call({
					method: "marketplace_integrations.api.shopify_products.queue_catalog_sweep",
					args: { account: account_name },
				})
				.then((r) => {
					const job = r.message;
					if (!job || !job.job_id) {
						$status_cell.text(__("Could not start the catalog sweep."));
						$button.prop("disabled", false);
						return;
					}
					watch_sweep(job.job_id, account_name, $status_cell, $button);
				})
				.catch(() => {
					$status_cell.text(__("Could not start the catalog sweep."));
					$button.prop("disabled", false);
				});
		});
	}

	function load_last_sweep(account_name, $status_cell) {
		frappe
			.call({
				method: "marketplace_integrations.api.shopify_products.get_last_catalog_sweep",
				args: { account: account_name },
			})
			.then((r) => render_last_sweep($status_cell, r.message))
			.catch(() => $status_cell.text(__("Could not retrieve the last sweep status.")));
	}

	function render_last_sweep($status_cell, last_sweep) {
		const message = build_last_sweep_message(last_sweep);
		// message.mode "html" carries a real frappe-timestamp <span> (built by
		// build_last_sweep_message/comment_when below) and must be inserted with
		// .html() so it renders as an element, not literal text; "text" carries
		// a plain-string message (e.g. a failed-job error, which may be an
		// API-controlled string) and must stay escaped via .text().
		if (message.mode === "html") {
			$status_cell.html(message.content);
		} else {
			$status_cell.text(message.content);
		}
	}

	function watch_sweep(job_id, account_name, $status_cell, $button) {
		let timer;
		const stop = () => {
			clearInterval(timer);
			if (frappe.realtime && frappe.realtime.off) {
				frappe.realtime.off("marketplace_shopify_catalog_sweep_progress", on_progress);
			}
			$button.prop("disabled", false);
		};
		const on_progress = (status) => {
			if (!status || status.job_id !== job_id) return;
			if (status.status === "completed") {
				stop();
				load_last_sweep(account_name, $status_cell);
			} else if (status.status === "failed") {
				stop();
				$status_cell.text(status.error || __("The catalog sweep failed."));
			} else {
				$status_cell.text(__("Catalog sweep running: {0} processed so far.", [status.processed || 0]));
			}
		};
		const check_status = () => {
			frappe
				.call({
					method: "marketplace_integrations.api.shopify_products.get_catalog_sweep_status",
					args: { job_id },
				})
				.then((r) => on_progress({ job_id, ...(r.message || {}) }))
				.catch(() => {
					stop();
					$status_cell.text(__("Could not retrieve the catalog sweep status."));
				});
		};
		if (frappe.realtime && frappe.realtime.on) {
			frappe.realtime.on("marketplace_shopify_catalog_sweep_progress", on_progress);
		}
		timer = setInterval(check_status, 1500);
		check_status();
	}

	// --- Section 4: recent Order Import failures ---

	function render_order_import_section($el) {
		$el.empty();
		$("<h4></h4>").text(__("Recent Order Import Failures")).appendTo($el);
		frappe
			.call({ method: "marketplace_integrations.api.shopify_reconciliation.get_order_import_failures" })
			.then((r) => {
				const rows = r.message || [];
				if (!rows.length) {
					$('<p class="text-muted"></p>').text(__("No Order Import failures.")).appendTo($el);
					return;
				}
				const $table = $('<table class="table table-bordered"></table>').appendTo($el);
				const $thead = $("<thead><tr></tr></thead>").appendTo($table);
				[__("Log"), __("Trigger Source"), __("Attempted At"), __("Error")].forEach((label) => {
					$("<th></th>").text(label).appendTo($thead.find("tr"));
				});
				const $tbody = $("<tbody></tbody>").appendTo($table);
				rows.forEach((row) => {
					const $tr = $("<tr></tr>").appendTo($tbody);
					$("<td></td>")
						.append(
							$("<a target=\"_blank\"></a>")
								.attr("href", `/app/marketplace-sync-log/${encodeURIComponent(row.name)}`)
								.text(row.name)
						)
						.appendTo($tr);
					$("<td></td>").text(row.trigger_source || "").appendTo($tr);
					$("<td></td>").text(frappe.datetime.str_to_user(row.attempted_at) || "").appendTo($tr);
					$("<td></td>").text(row.traceback || "").appendTo($tr);
				});
			});
	}

	// --- Section 6: refund evidence gaps ---

	function render_refund_section($el) {
		$el.empty();
		$("<h4></h4>").text(__("Refund Evidence Gaps")).appendTo($el);
		$('<p class="text-muted"></p>')
			.text(__("Shopify orders whose financial status shows a refund, but with no refund recorded."))
			.appendTo($el);
		frappe
			.call({ method: "marketplace_integrations.api.shopify_reconciliation.get_refund_evidence_gaps" })
			.then((r) => {
				const rows = r.message || [];
				if (!rows.length) {
					$('<p class="text-muted"></p>').text(__("No refund evidence gaps.")).appendTo($el);
					return;
				}
				const $table = $('<table class="table table-bordered"></table>').appendTo($el);
				const $thead = $("<thead><tr></tr></thead>").appendTo($table);
				[__("Order"), __("Financial Status"), __("Last Platform Update")].forEach((label) => {
					$("<th></th>").text(label).appendTo($thead.find("tr"));
				});
				const $tbody = $("<tbody></tbody>").appendTo($table);
				rows.forEach((row) => {
					const $tr = $("<tr></tr>").appendTo($tbody);
					$("<td></td>")
						.append(
							$("<a target=\"_blank\"></a>")
								.attr("href", `/app/marketplace-order/${encodeURIComponent(row.name)}`)
								.text(row.order_code || row.name)
						)
						.appendTo($tr);
					$("<td></td>").text(row.external_financial_status || "").appendTo($tr);
					$("<td></td>").text(frappe.datetime.str_to_user(row.last_platform_update_on) || "").appendTo($tr);
				});
			});
	}
};

if (typeof module !== "undefined") {
	module.exports = { build_last_sweep_message };
}
