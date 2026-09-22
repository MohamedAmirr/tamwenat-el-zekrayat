// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Sibling page to marketplace-talabat-catalog-mapping (#105) - never imports
// or calls into that page's own JS, or api/talabat_catalog.py. Discovery
// here is background-sweep based (see
// integrations/shopify/products.py::run_catalog_sweep()), not interactive
// page-by-page browsing like Talabat's: an account picker plus one "Run
// Catalog Sweep" action queues a job, progress is shown as "N processed so
// far" (Shopify's cursor pagination never exposes a total, see
// products.py's module docstring), and the finished result (a flat list of
// Unmapped/Deleted/Variant-less exceptions) is handed to RemoteDataGrid as a
// single-shot, already-fetched dataset - no core RemoteDataGrid change is
// needed for that (gap-check #3): fetch_page() is already an opaque
// caller-supplied callback, so a closure that slices the in-memory result
// array and always resolves the final page's has_more correctly is enough.
frappe.pages["marketplace-shopify-catalog-mapping"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Shopify Catalog Mapping"),
		single_column: true,
	});
	const account_field = page.add_field({
		fieldtype: "Link",
		fieldname: "marketplace_account",
		options: "Marketplace Account",
		label: __("Marketplace Account"),
		get_query: () => ({ filters: { platform: "Shopify" } }),
		change: () => reset_for_account(account_field.get_value()),
	});
	const $container = $('<div class="marketplace-shopify-catalog-mapping-container"></div>').appendTo(
		page.body
	);
	$("<p class=\"text-muted mb-3\"></p>")
		.text(
			__(
				"Shopify Connection stores API credentials. Marketplace Account identifies the Shopify store. Running a catalog sweep walks the full live Shopify variant catalog plus this account's saved mappings, and reports unmapped, deleted, and variant-less exceptions - it never changes anything on Shopify itself."
			)
		)
		.appendTo($container);
	const $status = $('<div class="text-muted mb-2"></div>').appendTo($container);
	const $grid_wrapper = $("<div></div>").appendTo($container);

	let grid = null;
	let current_account = null;
	let account_request_seq = 0;
	let current_job_id = null;

	page.set_primary_action(__("Run Catalog Sweep"), run_sweep, "refresh");

	function reset_for_account(marketplace_account) {
		if (marketplace_account === current_account) return;
		current_account = marketplace_account;
		current_job_id = null;
		account_request_seq++;
		$status.text("");
		$grid_wrapper.empty();
		grid = null;
	}

	function run_sweep() {
		const marketplace_account = account_field.get_value();
		if (!marketplace_account) {
			frappe.toast({ message: __("Select a marketplace account first."), indicator: "orange" });
			return;
		}
		const seq = ++account_request_seq;
		current_job_id = null;
		$grid_wrapper.empty();
		grid = null;
		$status.text(__("Queuing catalog sweep..."));

		frappe
			.call({
				method: "marketplace_integrations.api.shopify_products.queue_catalog_sweep",
				args: { account: marketplace_account },
			})
			.then((r) => {
				if (seq !== account_request_seq) return;
				const job = r.message;
				if (!job || !job.job_id) {
					$status.text(__("Could not start the catalog sweep."));
					return;
				}
				current_job_id = job.job_id;
				watch_sweep(marketplace_account, job.job_id, seq);
			})
			.catch(() => {
				if (seq === account_request_seq) $status.text(__("Could not start the catalog sweep."));
			});
	}

	function watch_sweep(marketplace_account, job_id, seq) {
		let timer;
		const stop = () => {
			clearInterval(timer);
			if (frappe.realtime && frappe.realtime.off) {
				frappe.realtime.off("marketplace_shopify_catalog_sweep_progress", on_progress);
			}
		};
		const on_progress = (status) => {
			if (!status || status.job_id !== job_id) return;
			if (seq !== account_request_seq || job_id !== current_job_id) return stop();
			if (status.status === "completed") {
				stop();
				render_results(marketplace_account, seq, status.result || {});
			} else if (status.status === "failed") {
				stop();
				$status.text(status.error || __("The catalog sweep failed."));
			} else {
				$status.text(__("Catalog sweep running: {0} processed so far.", [status.processed || 0]));
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
					if (seq === account_request_seq) $status.text(__("Could not retrieve the catalog sweep status."));
				});
		};
		if (frappe.realtime && frappe.realtime.on) {
			frappe.realtime.on("marketplace_shopify_catalog_sweep_progress", on_progress);
		}
		timer = setInterval(check_status, 1500);
		check_status();
	}

	function toast_mapping_saved(external_reference, item) {
		frappe.toast({
			message: __("Mapping saved: {0} → {1}", [
				frappe.utils.escape_html(external_reference),
				frappe.utils.escape_html(item),
			]),
			indicator: "green",
		});
	}

	function render_results(marketplace_account, seq, result) {
		if (seq !== account_request_seq) return;
		const exceptions = result.exceptions || [];
		const total_exceptions = result.total_exceptions != null ? result.total_exceptions : exceptions.length;
		let status_text = exceptions.length
			? __("Catalog sweep complete: {0} exception(s) found.", [total_exceptions])
			: __("Catalog sweep complete: no exceptions found.");
		// Review finding #4: the server caps the returned/cached/published
		// exception list at MAX_EXCEPTIONS_RETURNED (500) even though the sweep
		// itself still walks/processes the whole catalog - surface that here so
		// an operator on a large first sweep doesn't mistake "500 rows shown"
		// for "500 rows total".
		if (result.truncated) {
			status_text += " " + __("Showing first {0} of {1}.", [exceptions.length, total_exceptions]);
		}
		// Review finding #4: a descriptive-field refresh failure is caught,
		// rolled back to its own savepoint, and logged server-side
		// (_refresh_mapping_descriptive_fields()) - it otherwise never surfaces
		// anywhere. Keep this simple: a count and a pointer to the Error Log,
		// not a new exception category or per-row detail.
		if (result.refresh_failures) {
			status_text +=
				" " +
				__("{0} row(s) failed to refresh, see Error Log.", [result.refresh_failures]);
		}
		$status.text(status_text);
		$grid_wrapper.empty();

		frappe
			.require([
				"/assets/marketplace_integrations/js/remote_data_grid.js",
				"/assets/marketplace_integrations/css/remote_data_grid.css",
				"/assets/marketplace_integrations/css/marketplace_shopify_catalog_mapping.css",
			])
			.then(() => {
				if (seq !== account_request_seq) return;
				grid = new frappe.ui.RemoteDataGrid({
					wrapper: $grid_wrapper,
					columns: [
						{ fieldname: "category", label: __("Exception") },
						{ fieldname: "external_reference", label: __("Variant GID") },
						{ fieldname: "external_sku", label: __("SKU") },
						{ fieldname: "external_barcode", label: __("Barcode") },
						{ fieldname: "product_title", label: __("Shopify Product") },
						{ fieldname: "item", label: __("Item"), editable: "Link", options: "Item" },
						{
							fieldname: "actions",
							label: __("Actions"),
							render: ($cell, row) => {
								if (!row.mapping) return;
								$("<a class=\"btn btn-default btn-xs\"></a>")
									.text(__("Open mapping"))
									.attr("href", `/app/marketplace-product-mapping/${encodeURIComponent(row.mapping)}`)
									.attr("target", "_blank")
									.appendTo($cell);
							},
						},
					],
					// Single-shot dataset: fetch_page() slices the already-fetched
					// exceptions array in memory instead of calling the server again -
					// see this file's module comment / gap-check #3.
					fetch_page: (page_number, page_size) => {
						const start = (page_number - 1) * page_size;
						return Promise.resolve({
							rows: exceptions.slice(start, start + page_size),
							has_more: start + page_size < exceptions.length,
						});
					},
					confirm_changes: true,
					on_change: (row, fieldname, value) => {
						if (fieldname !== "item") return;
						if (!value) return Promise.reject();
						return frappe
							.call({
								method: "marketplace_integrations.api.shopify_products.upsert_mapping",
								args: {
									account: marketplace_account,
									external_reference: row.external_reference,
									item: value,
									external_product_id: row.external_product_id,
									external_sku: row.external_sku,
									external_barcode: row.external_barcode,
								},
							})
							.then((r) => {
								if (seq !== account_request_seq) return;
								toast_mapping_saved(row.external_reference, value);
								if (grid) {
									row.mapping = r.message.name;
									grid.update_cell(row, "actions");
								}
							});
					},
				});
			});
	}
};
