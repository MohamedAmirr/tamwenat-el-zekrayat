frappe.pages["marketplace-talabat-catalog-mapping"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Talabat Catalog Mapping"),
		single_column: true,
	});
	const account_field = page.add_field({
		fieldtype: "Link",
		fieldname: "marketplace_account",
		options: "Marketplace Account",
		label: __("Marketplace Account"),
		get_query: () => ({ filters: { platform: "Talabat" } }),
		change: () => load_account(account_field.get_value()),
	});
	const $container = $('<div class="marketplace-talabat-catalog-mapping-container"></div>').appendTo(
		page.body
	);
	const $status = $('<div class="text-muted mb-2"></div>').appendTo($container);
	const $grid_wrapper = $("<div></div>").appendTo($container);
	let grid = null;
	let current_account = null;
	let account_request_seq = 0;

	page.set_primary_action(__("Add manual mapping"), show_manual_mapping_dialog, "add");
	page.set_secondary_action(
		__("Refresh"),
		() => {
			const account = account_field.get_value();
			if (!account) return;
			current_account = null;
			load_account(account);
		},
		"refresh"
	);

	function toast_mapping_saved(external_reference, item) {
		frappe.toast({
			message: __("Mapping saved: {0} → {1}", [
				frappe.utils.escape_html(external_reference),
				frappe.utils.escape_html(item),
			]),
			indicator: "green",
		});
	}

	function show_manual_mapping_dialog() {
		const marketplace_account = account_field.get_value();
		if (!marketplace_account) {
			frappe.toast({ message: __("Select a marketplace account before adding a mapping."), indicator: "orange" });
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Add manual mapping"),
			fields: [
				{ fieldname: "external_reference", fieldtype: "Data", label: __("Talabat SKU"), reqd: 1 },
				{ fieldname: "item", fieldtype: "Link", options: "Item", label: __("ERPNext Item"), reqd: 1 },
			],
			primary_action_label: __("Save mapping"),
			primary_action: (values) =>
				frappe
					.call({
						method: "marketplace_integrations.api.talabat_catalog.upsert_mapping",
						args: { marketplace_account, external_reference: values.external_reference, item: values.item },
						freeze: true,
						freeze_message: __("Saving mapping..."),
					})
					.then(() => {
						dialog.hide();
						toast_mapping_saved(values.external_reference, values.item);
					}),
		});
		dialog.show();
	}

	function load_account(marketplace_account) {
		if (marketplace_account === current_account) return;
		current_account = marketplace_account;
		const seq = ++account_request_seq;
		$status.text("");
		$grid_wrapper.empty();
		grid = null;
		if (!marketplace_account) return;

		frappe
			.require([
				"/assets/marketplace_integrations/js/remote_data_grid.js",
				"/assets/marketplace_integrations/css/remote_data_grid.css",
				"/assets/marketplace_integrations/css/marketplace_talabat_catalog_mapping.css",
			])
			.then(() => {
				if (seq !== account_request_seq) return;
				grid = new frappe.ui.RemoteDataGrid({
					wrapper: $grid_wrapper,
					columns: [
						{ fieldname: "sku", label: __("Talabat SKU") },
						{ fieldname: "title", label: __("Title") },
						{ fieldname: "price", label: __("Price") },
						{ fieldname: "status", label: __("Status") },
						{ fieldname: "item", label: __("Item"), editable: "Link", options: "Item" },
					],
					fetch_page: (page_number, page_size) =>
						fetch_catalog_page(marketplace_account, seq, page_number, page_size),
					on_error: () => {
						if (seq === account_request_seq) {
							$status.text(__("Failed to fetch Talabat's catalog for this marketplace account."));
						}
					},
					confirm_changes: true,
					on_change: (row, fieldname, value) => {
						if (fieldname !== "item") return;
						if (!value) {
							frappe.toast({
								message: __("Use a value to update the mapping; unmapping isn't supported here yet."),
								indicator: "orange",
							});
							return Promise.reject();
						}
						return frappe
							.call({
								method: "marketplace_integrations.api.talabat_catalog.upsert_mapping",
								args: { marketplace_account, external_reference: row.sku, item: value },
							})
							.then(() => {
								if (seq !== account_request_seq) return;
								toast_mapping_saved(row.sku, value);
								if (grid) grid.update_cell(row, "status", __("Mapped"));
							});
					},
				});
			});
	}

	function fetch_catalog_page(marketplace_account, seq, page_number, page_size) {
		return frappe
			.call({
				method: "marketplace_integrations.api.talabat_catalog.get_catalog_page",
				args: { marketplace_account, page: page_number, page_size },
			})
			.then((r) => {
				if (seq === account_request_seq) $status.text("");
				return {
					rows: (r.message.rows || []).map((row) => ({
						...row,
						status: row.item ? __("Mapped") : __("Not Mapped"),
					})),
					has_more: r.message.has_more,
				};
			});
	}
};
