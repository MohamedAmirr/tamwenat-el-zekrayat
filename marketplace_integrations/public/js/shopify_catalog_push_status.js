// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Shared job-status + results dialog for the Shopify price-only catalog push
// (#106 decision 7) and, generalized via the optional `status_method`/`labels`
// constructor options (`labels.title` included), the Shopify inventory sync (#107 decision 12).
// Every option defaults to the original price-push behavior/wording. Loaded
// on demand via frappe.require() from marketplace_product_mapping.js
// (single-mapping buttons), mapping_sync_actions.js (bulk routing, shared by
// the mapping list and the Item form's Marketplace Listings tab) and
// marketplace_account.js (Reconcile), so the
// polling/rendering logic exists in exactly one place - Desk loads each
// doctype's own .js file independently, so it can't be shared by ordinary
// module import the way the two Node test files share a require().
//
// One push request can enqueue more than one background job (one per
// affected Marketplace Account, per api/shopify_catalog_push.py) - this
// dialog polls every job independently and aggregates their results/counts
// into one combined view, only calling `on_done` once every job has reached
// a terminal status ("completed" or "failed").
(function () {
	function escape(value) {
		return frappe.utils.escape_html(value == null ? "" : String(value));
	}

	function build_results_table($el, results, truncated, total, result_link_route, result_column_label, status_labels, detail_column_label) {
		$el.empty();
		if (!results.length) return;

		const $table = $('<table class="table table-bordered"></table>').appendTo($el);
		const $thead = $("<thead><tr></tr></thead>").appendTo($table);
		[result_column_label || __("Mapping"), __("Status"), detail_column_label || __("Error")].forEach((label) => {
			$("<th></th>").text(label).appendTo($thead.find("tr"));
		});
		const $tbody = $("<tbody></tbody>").appendTo($table);
		const route = result_link_route || "marketplace-product-mapping";
		results.forEach((row) => {
			const $tr = $("<tr></tr>").appendTo($tbody);
			$("<td></td>")
				.append(
					$("<a target=\"_blank\"></a>")
						.attr("href", `/app/${route}/${encodeURIComponent(row.mapping_name)}`)
						.html(escape(row.mapping_name))
				)
				.appendTo($tr);
			const names = Object.assign(
				{ ok: __("Synced"), skipped: __("Skipped"), error: __("Error") },
				status_labels || {}
			);
			let status_label = names.error;
			if (row.ok) {
				status_label = names.ok;
			} else if (row.skipped) {
				status_label = names.skipped;
			}
			$("<td></td>").text(status_label).appendTo($tr);
			// `detail` is the one-line text of a caller whose rows carry one.
			$("<td></td>").text(row.detail || row.error || "").appendTo($tr);
		});
		if (truncated) {
			$('<p class="text-muted"></p>')
				.text(__("Showing first {0} of {1} result(s).", [results.length, total]))
				.appendTo($el);
		}
	}

	// Bounded consecutive-poll-error retry budget (review finding #3): a
	// single transient poll error (network blip, brief cache unavailability)
	// must not permanently abandon a job the server-side worker is very
	// likely still running - only give up after this many CONSECUTIVE
	// failures for the same job. Reset to 0 on any successful poll.
	const MAX_CONSECUTIVE_POLL_ERRORS = 3;

	const DEFAULT_STATUS_METHOD =
		"marketplace_integrations.api.shopify_catalog_push.get_catalog_push_status";

	// Wording defaults (the price push). Callers may override any subset via
	// the `labels` option. Functions so __() is evaluated at render time.
	const DEFAULT_LABELS = {
		title: () => __("Pushing to Shopify"),
		nothing: () => __("Nothing to push."),
		running: (counts) =>
			__("Pushing... {0} succeeded, {1} failed, {2} skipped so far.", counts),
		complete: (counts) => __("Push complete: {0} succeeded, {1} failed, {2} skipped.", counts),
		account_failed: (account, error) =>
			__("Push failed for account {0}: {1}", [account, error]),
		poll_failed: () => __("Could not retrieve the catalog push status."),
	};

	frappe.ui.ShopifyCatalogPushStatus = class ShopifyCatalogPushStatus {
		constructor({ jobs, on_done, on_close, poll_interval = 1500, status_method, labels, result_link_route, result_column_label, status_labels, detail_column_label }) {
			this.status_method = status_method || DEFAULT_STATUS_METHOD;
			this.labels = Object.assign({}, DEFAULT_LABELS, labels || {});
			// Both default to the original price-push/inventory-sync behavior:
			// a link into Marketplace Product Mapping under a "Mapping" column.
			// A caller whose result rows are a different doctype (e.g. #109's
			// order import, whose rows are Marketplace Order names) overrides
			// both so the table links/labels correctly instead of pointing at a
			// nonexistent Marketplace Product Mapping document.
			this.result_link_route = result_link_route;
			this.result_column_label = result_column_label;
			// Optional per-caller names for the Status cell ({ok, skipped, error}) and the
			// last column's heading, for rows that are not "Synced / Skipped / Error".
			this.status_labels = status_labels;
			this.detail_column_label = detail_column_label;
			this.jobs = jobs || [];
			this.on_done = on_done;
			this.on_close = on_close;
			this.poll_interval = poll_interval;
			this.job_states = {};
			this.poll_error_counts = {};
			this.finished = false;
			this.stopped = false;

			this.dialog = new frappe.ui.Dialog({
				title: this.labels.title(),
				fields: [{ fieldtype: "HTML", fieldname: "status_html" }],
			});
			this.$status = this.dialog.fields_dict.status_html.$wrapper;
			// Stop polling once the dialog is hidden/closed (Esc, navigating
			// away, ...) - without this, closing the dialog left the poll loop
			// running indefinitely against a detached DOM wrapper, issuing a
			// get_catalog_push_status call every poll_interval per job for as
			// long as that job hadn't reached a terminal state (review finding
			// #3). frappe.ui.Dialog's own "hide.bs.modal" handler calls this
			// onhide hook, so this doesn't need its own bootstrap event binding.
			this.dialog.onhide = () => {
				this.stopped = true;
				if (this.on_close) this.on_close();
			};

			if (!this.jobs.length) {
				this.dialog.show();
				$('<p class="text-muted"></p>').text(this.labels.nothing()).appendTo(this.$status);
				return;
			}

			this.dialog.show();
			this.render();
			this.jobs.forEach((job) => this.poll(job));
		}

		poll(job) {
			const check = () => {
				if (this.stopped) return;
				frappe
					.call({
						method: this.status_method,
						args: { job_id: job.job_id },
					})
					.then((r) => {
						if (this.stopped) return;
						this.poll_error_counts[job.job_id] = 0;
						this.job_states[job.job_id] = r.message || {};
						this.render();
						this.maybe_finish();
						// Stop THIS job's own loop once it is terminal, independent
						// of other jobs - otherwise a finished job keeps being
						// polled (and a later transient error or an expired cache
						// entry could overwrite its completed result).
						if (!["completed", "failed"].includes(this.job_states[job.job_id].status)) {
							setTimeout(check, this.poll_interval);
						}
					})
					.catch(() => {
						if (this.stopped) return;
						const error_count = (this.poll_error_counts[job.job_id] || 0) + 1;
						this.poll_error_counts[job.job_id] = error_count;
						if (error_count < MAX_CONSECUTIVE_POLL_ERRORS) {
							// Transient error - retry rather than giving up on the
							// first failure, since the push job is very likely
							// still running server-side and will still write
							// results.
							setTimeout(check, this.poll_interval);
							return;
						}
						this.job_states[job.job_id] = {
							status: "failed",
							error: this.labels.poll_failed(),
						};
						this.render();
						this.maybe_finish();
					});
			};
			check();
		}

		maybe_finish() {
			const all_done = this.jobs.every((job) => {
				const state = this.job_states[job.job_id];
				return state && ["completed", "failed"].includes(state.status);
			});
			if (all_done && !this.finished) {
				this.finished = true;
				if (this.on_done) this.on_done();
			}
		}

		render() {
			this.$status.empty();
			let results = [];
			let total = 0;
			let truncated = false;
			let succeeded = 0;
			let failed = 0;
			let skipped = 0;
			let any_running = false;
			const failed_jobs = [];

			this.jobs.forEach((job) => {
				const state = this.job_states[job.job_id] || { status: "queued" };
				if (!["completed", "failed"].includes(state.status)) any_running = true;
				if (state.status === "failed") failed_jobs.push({ account: job.account, error: state.error });

				const result = state.result || {};
				if (result.results) {
					results = results.concat(result.results);
					total += result.total || 0;
					truncated = truncated || !!result.truncated;
					succeeded += result.succeeded || 0;
					failed += result.failed || 0;
					skipped += result.skipped_mappings || 0;
				}
			});

			$("<p></p>")
				.text(
					any_running
						? this.labels.running([succeeded, failed, skipped])
						: this.labels.complete([succeeded, failed, skipped])
				)
				.appendTo(this.$status);

			failed_jobs.forEach((entry) => {
				$('<p class="text-danger"></p>')
					.text(this.labels.account_failed(entry.account, entry.error || ""))
					.appendTo(this.$status);
			});

			// The table gets its own container: build_results_table() empties what it is given, which
			// would otherwise wipe the totals line and the failed-job lines drawn above.
			build_results_table($("<div></div>").appendTo(this.$status), results, truncated, total, this.result_link_route, this.result_column_label, this.status_labels, this.detail_column_label);
		}
	};

	// Constructor options for the Shopify inventory sync (#107), kept here so
	// the three callers (mapping form, mapping list, Marketplace Account) share
	// one definition: `new frappe.ui.ShopifyCatalogPushStatus({ jobs,
	// ...frappe.ui.ShopifyCatalogPushStatus.INVENTORY_OPTIONS })`.
	frappe.ui.ShopifyCatalogPushStatus.INVENTORY_OPTIONS = {
		status_method: "marketplace_integrations.api.shopify_inventory.get_inventory_sync_status",
		labels: {
			title: () => __("Syncing Shopify inventory"),
			nothing: () => __("Nothing to sync."),
			running: (counts) =>
				__("Syncing... {0} succeeded, {1} failed, {2} skipped so far.", counts),
			complete: (counts) =>
				__("Sync complete: {0} succeeded, {1} failed, {2} skipped.", counts),
			account_failed: (account, error) =>
				__("Sync failed for account {0}: {1}", [account, error]),
			poll_failed: () => __("Could not retrieve the inventory sync status."),
		},
	};

	// Constructor options for the Shopify missing-image pull (#526). Result rows are Item codes
	// (`mapping_name` carries the Item), so the table links to /app/item/<code>; a pulled image
	// counts as "Updated".
	frappe.ui.ShopifyCatalogPushStatus.IMAGE_OPTIONS = {
		status_method: "marketplace_integrations.api.shopify_item_images.get_pull_missing_images_status",
		result_link_route: "item",
		result_column_label: __("Item"),
		status_labels: { ok: __("Updated") },
		detail_column_label: __("Details"),
		labels: {
			title: () => __("Pulling images from Shopify"),
			nothing: () => __("Nothing to pull."),
			running: (counts) => __("Pulling... {0} updated, {1} failed, {2} skipped so far.", counts),
			complete: (counts) => __("Pull complete: {0} updated, {1} failed, {2} skipped.", counts),
			account_failed: (account, error) =>
				__("Image pull failed for account {0}: {1}", [account, error]),
			poll_failed: () => __("Could not retrieve the image pull status."),
		},
	};

	// Constructor options for the Shopify order import (#109 review finding
	// #7 - get_order_import_status() was never wired to any UI). Result rows
	// here are Marketplace Order names, not Marketplace Product Mapping
	// names, hence the overridden result_link_route/result_column_label.
	frappe.ui.ShopifyCatalogPushStatus.ORDER_IMPORT_OPTIONS = {
		status_method: "marketplace_integrations.api.shopify_orders.get_order_import_status",
		result_link_route: "marketplace-order",
		result_column_label: __("Order"),
		labels: {
			title: () => __("Retrieving Shopify orders"),
			nothing: () => __("Nothing to retrieve."),
			running: (counts) =>
				__("Retrieving... {0} succeeded, {1} failed, {2} skipped so far.", counts),
			complete: (counts) => __("Retrieval complete: {0} succeeded, {1} failed, {2} skipped.", counts),
			account_failed: (account, error) =>
				__("Retrieval failed for account {0}: {1}", [account, error]),
			poll_failed: () => __("Could not retrieve the order import status."),
		},
	};
})();
