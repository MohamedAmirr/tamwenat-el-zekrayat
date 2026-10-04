// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// #168 "Marketplace Products": pick one Marketplace Account and see its Needs
// Mapping items - the ERPNext Item is assigned inline. Each platform's list is
// sourced from that platform's own existing endpoints, never a parallel
// implementation:
//   - Talabat: live catalog via api.talabat_catalog.get_catalog_page (rows with
//     no Item are shown; saved via upsert_mapping).
//   - Shopify: the same background catalog sweep as the Shopify Catalog Mapping
//     page (queue_catalog_sweep / get_catalog_sweep_status), showing its
//     "Unmapped" exceptions (saved via shopify_products.upsert_mapping).
//   - Noon: partner SKUs seen on this account's order imports that have no
//     mapping yet (api.noon_catalog.get_unmapped_order_skus). This is order-
//     derived only - Noon catalog export is blocked (see
//     docs/noon-integration/catalog-export-capability.md) and nothing here calls
//     Noon.
// The per-platform Catalog Mapping pages and the Marketplace Product Mapping
// list are left untouched and remain linked from the workspace.
frappe.pages["marketplace-products"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Marketplace Products"),
		single_column: true,
	});
	const account_field = page.add_field({
		fieldtype: "Link",
		fieldname: "marketplace_account",
		options: "Marketplace Account",
		label: __("Marketplace Account"),
		get_query: () => ({ filters: { enabled: 1 } }),
		change: () => load_account(account_field.get_value()),
	});
	const $container = $('<div class="marketplace-products-container"></div>').appendTo(page.body);
	$('<p class="text-muted mb-3"></p>')
		.text(
			__(
				"Needs Mapping lists marketplace products that have no ERPNext Item yet. Choose an Item to map it. Mapped products drop out of this list; the Marketplace Product Mapping list keeps every saved mapping for diagnostics."
			)
		)
		.appendTo($container);
	const $status = $('<div class="text-muted mb-2"></div>').appendTo($container);
	const $grid_wrapper = $("<div></div>").appendTo($container);

	let grid = null;
	let current_account = null;
	let account_request_seq = 0;
	let current_job_id = null;
	let unmapped_rows = [];

	page.clear_primary_action();

	function reset() {
		account_request_seq++;
		current_job_id = null;
		unmapped_rows = [];
		grid = null;
		$status.text("");
		$grid_wrapper.empty();
		page.clear_primary_action();
	}

	function load_account(marketplace_account) {
		if (marketplace_account === current_account) return;
		current_account = marketplace_account;
		reset();
		if (!marketplace_account) return;
		const seq = account_request_seq;

		frappe
			.call({
				method: "frappe.client.get_value",
				args: {
					doctype: "Marketplace Account",
					filters: marketplace_account,
					fieldname: ["platform"],
				},
			})
			.then((r) => {
				if (seq !== account_request_seq) return;
				const platform = r.message && r.message.platform;
				if (platform === "Talabat") return render_talabat(marketplace_account, seq);
				if (platform === "Shopify") return render_shopify(marketplace_account, seq);
				if (platform === "Noon") return render_noon(marketplace_account, seq);
				$status.text(__("This platform has no Needs Mapping source."));
			});
	}

	function ensure_grid_assets() {
		return frappe.require([
			"/assets/marketplace_integrations/js/remote_data_grid.js",
			"/assets/marketplace_integrations/css/remote_data_grid.css",
		]);
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

	// A save can empty the page it was on (its last unmapped row was just
	// mapped), and RemoteDataGrid would then render "No Data" for that page.
	// Step back to the nearest earlier page that still has rows; page 1 is
	// always shown. Probes with fetch_page() before go() so the grid only renders
	// a page that has rows.
	function refresh_grid_after_save() {
		if (grid) show_nearest_page_with_rows(grid, grid.page);
	}

	function show_nearest_page_with_rows(target, page_number) {
		if (page_number <= 1) return target.go(1);
		return Promise.resolve(target.fetch_page(page_number, target.page_size)).then(
			(result) =>
				result.rows.length
					? target.go(page_number)
					: show_nearest_page_with_rows(target, page_number - 1),
			// On a probe failure, render the page normally so the grid shows its own error state.
			() => target.go(page_number)
		);
	}

	// Single-shot dataset, sliced in memory: the same approach the Shopify
	// Catalog Mapping page uses for its sweep results.
	function list_fetcher(get_rows) {
		return (page_number, page_size) => {
			const rows = get_rows();
			const start = (page_number - 1) * page_size;
			return Promise.resolve({
				rows: rows.slice(start, start + page_size),
				has_more: start + page_size < rows.length,
			});
		};
	}

	function make_item_grid(columns, fetch_page, save) {
		$grid_wrapper.empty();
		grid = new frappe.ui.RemoteDataGrid({
			wrapper: $grid_wrapper,
			columns: [
				...columns,
				{ fieldname: "item", label: __("Item"), editable: "Link", options: "Item" },
			],
			fetch_page,
			confirm_changes: true,
			on_change: (row, fieldname, value) => {
				if (fieldname !== "item") return;
				if (!value) return Promise.reject();
				return save(row, value);
			},
		});
	}

	// ---- Talabat -------------------------------------------------------------

	function render_talabat(marketplace_account, seq) {
		$status.text(__("Rows that already have an Item are hidden on each page."));
		return ensure_grid_assets().then(() => {
			if (seq !== account_request_seq) return;
			make_item_grid(
				[
					{ fieldname: "sku", label: __("Talabat SKU") },
					{ fieldname: "title", label: __("Title") },
					{ fieldname: "price", label: __("Price") },
				],
				(page_number, page_size) =>
					frappe
						.call({
							method: "marketplace_integrations.api.talabat_catalog.get_catalog_page",
							args: { marketplace_account, page: page_number, page_size },
						})
						.then((r) => ({
							rows: (r.message.rows || []).filter((row) => !row.item),
							has_more: r.message.has_more,
						})),
				(row, value) =>
					frappe
						.call({
							method: "marketplace_integrations.api.talabat_catalog.upsert_mapping",
							args: {
								marketplace_account,
								external_reference: row.sku,
								item: value,
							},
						})
						.then(() => {
							if (seq !== account_request_seq) return;
							toast_mapping_saved(row.sku, value);
							refresh_grid_after_save();
						})
			);
		});
	}

	// ---- Noon ----------------------------------------------------------------

	function render_noon(marketplace_account, seq) {
		$status.text(
			__("Partner SKUs seen on this account's order imports that have no Item mapping yet.")
		);
		return Promise.all([
			ensure_grid_assets(),
			frappe.call({
				method: "marketplace_integrations.api.noon_catalog.get_unmapped_order_skus",
				args: { account: marketplace_account },
			}),
		]).then(([, r]) => {
			if (seq !== account_request_seq) return;
			unmapped_rows = (r.message || []).map((sku) => ({ sku }));
			if (!unmapped_rows.length)
				$status.text(__("No unmapped Noon partner SKUs were found on order imports."));
			make_item_grid(
				[{ fieldname: "sku", label: __("Noon Partner SKU") }],
				list_fetcher(() => unmapped_rows),
				(row, value) =>
					frappe
						.call({
							method: "marketplace_integrations.api.noon_catalog.upsert_mapping",
							args: {
								account: marketplace_account,
								external_reference: row.sku,
								item: value,
							},
						})
						.then(() => {
							if (seq !== account_request_seq) return;
							toast_mapping_saved(row.sku, value);
							unmapped_rows = unmapped_rows.filter(
								(candidate) => candidate.sku !== row.sku
							);
							refresh_grid_after_save();
						})
			);
		});
	}

	// ---- Shopify -------------------------------------------------------------

	function render_shopify(marketplace_account, seq) {
		$status.text(
			__("Run a catalog sweep to find Shopify variants that have no Item mapping.")
		);
		page.set_primary_action(
			__("Run Catalog Sweep"),
			() => run_sweep(marketplace_account),
			"refresh"
		);
	}

	function run_sweep(marketplace_account) {
		const seq = ++account_request_seq;
		current_job_id = null;
		unmapped_rows = [];
		grid = null;
		$grid_wrapper.empty();
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
				if (seq === account_request_seq)
					$status.text(__("Could not start the catalog sweep."));
			});
	}

	// Same shared watch as marketplace_shopify_catalog_mapping.js (realtime
	// progress with a polling fallback), see public/js/shopify_catalog_sweep_watch.js.
	function watch_sweep(marketplace_account, job_id, seq) {
		frappe
			.require("/assets/marketplace_integrations/js/shopify_catalog_sweep_watch.js")
			.then(() => {
				frappe.ui.watchShopifyCatalogSweep({
					job_id,
					is_current: () => seq === account_request_seq && job_id === current_job_id,
					on_progress: (processed) =>
						$status.text(
							__("Catalog sweep running: {0} processed so far.", [processed])
						),
					on_complete: (result) =>
						render_shopify_results(marketplace_account, seq, result),
					on_failed: (error) => $status.text(error || __("The catalog sweep failed.")),
					on_error: () => {
						if (seq === account_request_seq)
							$status.text(__("Could not retrieve the catalog sweep status."));
					},
				});
			});
	}

	function render_shopify_results(marketplace_account, seq, result) {
		if (seq !== account_request_seq) return;
		const exceptions = result.exceptions || [];
		unmapped_rows = exceptions.filter((row) => row.category === "Unmapped");
		let status_text = unmapped_rows.length
			? __("Catalog sweep complete: {0} variant(s) need a mapping.", [unmapped_rows.length])
			: __("Catalog sweep complete: no unmapped variants found.");
		// The server caps the returned exception list (see products.py); say so
		// rather than let a truncated list look complete.
		if (result.truncated) {
			status_text +=
				" " +
				__("Showing first {0} of {1} exceptions.", [
					exceptions.length,
					result.total_exceptions,
				]);
		}
		$status.text(status_text);

		ensure_grid_assets().then(() => {
			if (seq !== account_request_seq) return;
			make_item_grid(
				[
					{ fieldname: "external_reference", label: __("Variant GID") },
					{ fieldname: "external_sku", label: __("SKU") },
					{ fieldname: "product_title", label: __("Shopify Product") },
				],
				list_fetcher(() => unmapped_rows),
				(row, value) =>
					frappe
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
						.then(() => {
							if (seq !== account_request_seq) return;
							toast_mapping_saved(row.external_reference, value);
							unmapped_rows = unmapped_rows.filter((candidate) => candidate !== row);
							refresh_grid_after_save();
						})
			);
		});
	}
};
