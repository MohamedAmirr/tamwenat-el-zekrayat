frappe.pages["talabat-catalog-mapping"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Talabat Catalog Mapping"),
		single_column: true,
	});

	let vendor_field = page.add_field({
		fieldtype: "Link",
		fieldname: "vendor",
		options: "Talabat Vendor",
		label: __("Vendor"),
		change: () => load_vendor(vendor_field.get_value()),
	});

	// .layout-main-section has no padding of its own on Frappe pages (list
	// pages get theirs from .frappe-list's margins) - same spacing here, from
	// this page's stylesheet (responsive rules included).
	let $container = $('<div class="talabat-catalog-mapping-container"></div>').appendTo(page.body);
	let $status = $('<div class="text-muted mb-2"></div>').appendTo($container);
	let $grid_wrapper = $("<div></div>").appendTo($container);
	let grid = null;
	// item -> talabat_sku for every mapping of the selected vendor; keeps
	// already-mapped Items out of the picker and blocks re-mapping a product
	// that is mapped to a different SKU.
	let mapped_item_by_item = new Map();

	let current_vendor = null;
	page.set_primary_action(__("Add manual mapping"), show_manual_mapping_dialog, "add");

	function get_available_item_query(current_sku) {
		let taken = Array.from(mapped_item_by_item.entries())
			.filter(([, sku]) => sku !== current_sku)
			.map(([item]) => item);
		return taken.length ? { filters: { name: ["not in", taken] } } : {};
	}

	function set_mapped_item(sku, item) {
		for (let [mapped_item, mapped_sku] of mapped_item_by_item) {
			if (mapped_sku === sku) mapped_item_by_item.delete(mapped_item);
		}
		if (item) mapped_item_by_item.set(item, sku);
	}

	function show_manual_mapping_dialog() {
		let vendor = vendor_field.get_value();
		if (!vendor) {
			frappe.toast({ message: __("Select a vendor before adding a mapping."), indicator: "orange" });
			return;
		}

		let dialog = new frappe.ui.Dialog({
			title: __("Add manual mapping"),
			fields: [
				{
					fieldname: "talabat_sku",
					fieldtype: "Data",
					label: __("Talabat SKU"),
					reqd: 1,
				},
				{
					fieldname: "item",
					fieldtype: "Link",
					options: "Item",
					label: __("ERPNext Item"),
					reqd: 1,
					get_query: () => get_available_item_query(),
				},
			],
			primary_action_label: __("Save mapping"),
			primary_action: (values) => {
				let existing_sku = mapped_item_by_item.get(values.item);
				if (existing_sku && existing_sku !== values.talabat_sku) {
					frappe.toast({
						message: __("Item {0} is already mapped to SKU {1}.", [values.item, existing_sku]),
						indicator: "red",
					});
					return;
				}
				return frappe
					.call({
						method: "talabat_integration.api.catalog.upsert_mapping",
						args: { vendor: vendor, talabat_sku: values.talabat_sku, item: values.item },
						freeze: true,
						freeze_message: __("Saving mapping..."),
					})
					.then(() => {
						set_mapped_item(values.talabat_sku, values.item);
						dialog.hide();
						frappe.toast({
							message: __("Mapping saved: {0} → {1}", [values.talabat_sku, values.item]),
							indicator: "green",
						});
						if (grid) grid.go(grid.page);
					});
			},
		});
		dialog.show();
	}

	function load_vendor(vendor) {
		// Page-field Link controls fire `change` twice per pick - once on
		// selection (frappe's programmatic trigger) and once on the following
		// blur (the native change after typing) - which would rebuild the grid
		// and refetch the catalog twice per selection.
		if (vendor === current_vendor) return;
		current_vendor = vendor;
		mapped_item_by_item.clear();
		$status.text("");
		$grid_wrapper.empty();
		grid = null;
		if (!vendor) return;

		frappe
			.require([
				"/assets/talabat_integration/js/remote_data_grid.js",
				"/assets/talabat_integration/css/remote_data_grid.css",
				"/assets/talabat_integration/css/talabat_catalog_mapping.css",
			])
			.then(() => {
				grid = new frappe.ui.RemoteDataGrid({
					wrapper: $grid_wrapper,
					columns: [
						{ fieldname: "sku", label: __("Talabat SKU") },
						{ fieldname: "title", label: __("Title") },
						{ fieldname: "price", label: __("Price") },
						{ fieldname: "status", label: __("Status") },
						{
							fieldname: "item",
							label: __("Item"),
							editable: "Link",
							options: "Item",
							// Keep already-mapped Items out of the dropdown so
							// the same product can't be mapped to a second SKU.
							get_query: (row) => get_available_item_query(row.sku),
						},
					],
					fetch_page: (page_number, page_size) =>
						fetch_catalog_page(vendor, page_number, page_size),
					on_error: () => $status.text(__("Failed to fetch Talabat's catalog for this vendor.")),
					confirm_changes: true,
					on_change: (row, fieldname, value) => {
						if (fieldname !== "item") return;
						let existing_sku = mapped_item_by_item.get(value);
						if (existing_sku && existing_sku !== row.sku) {
							frappe.toast({
								message: __("Item {0} is already mapped to SKU {1}.", [
									value,
									existing_sku,
								]),
								indicator: "red",
							});
							// Rejecting reverts the cell to its saved value.
							return Promise.reject();
						}
						return frappe
							.call({
								method: "talabat_integration.api.catalog.upsert_mapping",
								args: { vendor: vendor, talabat_sku: row.sku, item: value },
							})
							.then(() => {
								set_mapped_item(row.sku, value);
								frappe.toast({
									message: __("Mapping saved: {0} → {1}", [row.sku, value]),
									indicator: "green",
								});
								grid.go(grid.page);
							});
					},
				});
			});
	}

	function fetch_catalog_page(vendor, page_number, page_size) {
		return frappe
			.call({
				method: "talabat_integration.api.catalog.get_vendor_catalog_page",
				args: { vendor: vendor, page: page_number, page_size: page_size },
			})
			.then((r) => {
				$status.text("");
				mapped_item_by_item.clear();
				(r.message.mappings || []).forEach((m) => {
					if (m.item) mapped_item_by_item.set(m.item, m.talabat_sku);
				});
				let rows = (r.message.rows || []).map((row) => ({
					...row,
					status: row.item ? "Mapped" : "Not Mapped",
				}));
				return { rows: rows, has_more: r.message.has_more };
			});
	}
};
