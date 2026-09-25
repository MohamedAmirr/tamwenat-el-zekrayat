// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Shared job-status + results dialog for the Shopify price-only catalog push
// (#106 decision 7) and, generalized via the optional `status_method`/`labels`
// constructor options (`labels.title` included), the Shopify inventory sync (#107 decision 12).
// Every option defaults to the original price-push behavior/wording. Loaded
// on demand via frappe.require() from marketplace_product_mapping.js
// (single-mapping buttons), marketplace_product_mapping_list.js (bulk
// actions) and marketplace_account.js (Reconcile), so the
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

	function build_results_table($el, results, truncated, total) {
		$el.empty();
		if (!results.length) return;

		const $table = $('<table class="table table-bordered"></table>').appendTo($el);
		const $thead = $("<thead><tr></tr></thead>").appendTo($table);
		[__("Mapping"), __("Status"), __("Error")].forEach((label) => {
			$("<th></th>").text(label).appendTo($thead.find("tr"));
		});
		const $tbody = $("<tbody></tbody>").appendTo($table);
		results.forEach((row) => {
			const $tr = $("<tr></tr>").appendTo($tbody);
			$("<td></td>")
				.append(
					$("<a target=\"_blank\"></a>")
						.attr("href", `/app/marketplace-product-mapping/${encodeURIComponent(row.mapping_name)}`)
						.html(escape(row.mapping_name))
				)
				.appendTo($tr);
			let status_label = __("Error");
			if (row.ok) {
				status_label = __("Synced");
			} else if (row.skipped) {
				status_label = __("Skipped");
			}
			$("<td></td>").text(status_label).appendTo($tr);
			$("<td></td>").text(row.error || "").appendTo($tr);
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
		constructor({ jobs, on_done, poll_interval = 1500, status_method, labels }) {
			this.status_method = status_method || DEFAULT_STATUS_METHOD;
			this.labels = Object.assign({}, DEFAULT_LABELS, labels || {});
			this.jobs = jobs || [];
			this.on_done = on_done;
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

			build_results_table(this.$status, results, truncated, total);
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
})();
