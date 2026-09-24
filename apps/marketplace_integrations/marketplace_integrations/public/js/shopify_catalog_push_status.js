// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Shared job-status + results dialog for the Shopify price-only catalog push
// (#106 decision 7). Loaded on demand via frappe.require() from both
// marketplace_product_mapping.js (single-mapping "Push to Shopify" button)
// and marketplace_product_mapping_list.js (bulk "Push selected"), so the
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

	frappe.ui.ShopifyCatalogPushStatus = class ShopifyCatalogPushStatus {
		constructor({ jobs, on_done, poll_interval = 1500 }) {
			this.jobs = jobs || [];
			this.on_done = on_done;
			this.poll_interval = poll_interval;
			this.job_states = {};
			this.poll_error_counts = {};
			this.finished = false;
			this.stopped = false;

			this.dialog = new frappe.ui.Dialog({
				title: __("Pushing to Shopify"),
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
				$('<p class="text-muted"></p>').text(__("Nothing to push.")).appendTo(this.$status);
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
						method: "marketplace_integrations.api.shopify_catalog_push.get_catalog_push_status",
						args: { job_id: job.job_id },
					})
					.then((r) => {
						if (this.stopped) return;
						this.poll_error_counts[job.job_id] = 0;
						this.job_states[job.job_id] = r.message || {};
						this.render();
						this.maybe_finish();
						if (!this.finished) setTimeout(check, this.poll_interval);
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
							error: __("Could not retrieve the catalog push status."),
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
						? __("Pushing... {0} succeeded, {1} failed, {2} skipped so far.", [succeeded, failed, skipped])
						: __("Push complete: {0} succeeded, {1} failed, {2} skipped.", [succeeded, failed, skipped])
				)
				.appendTo(this.$status);

			failed_jobs.forEach((entry) => {
				$('<p class="text-danger"></p>')
					.text(__("Push failed for account {0}: {1}", [entry.account, entry.error || ""]))
					.appendTo(this.$status);
			});

			build_results_table(this.$status, results, truncated, total);
		}
	};
})();
