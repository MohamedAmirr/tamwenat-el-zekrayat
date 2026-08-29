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

	let $status = $('<div class="text-muted" style="margin-bottom: 8px;"></div>').appendTo(page.body);
	let $grid_wrapper = $("<div></div>").appendTo(page.body);
	let grid = null;

	function load_vendor(vendor) {
		$status.text("");
		$grid_wrapper.empty();
		grid = null;
		if (!vendor) return;

		frappe.require("/assets/talabat_integration/js/remote_data_grid.js").then(() => {
			grid = new frappe.ui.RemoteDataGrid({
				wrapper: $grid_wrapper,
				columns: [
					{ fieldname: "sku", label: __("Talabat SKU") },
					{ fieldname: "title", label: __("Title") },
					{ fieldname: "price", label: __("Price") },
					{ fieldname: "status", label: __("Status") },
					{ fieldname: "item", label: __("Item"), editable: "Link", options: "Item" },
				],
				fetch_page: (page_number) => fetch_catalog_page(vendor, page_number),
				on_change: (row, fieldname, value) => {
					if (fieldname !== "item") return;
					frappe.call({
						method: "talabat_integration.api.catalog.upsert_mapping",
						args: { vendor: vendor, talabat_sku: row.sku, item: value },
						callback: () => {
							frappe.show_alert({
								message: __("Mapping saved"),
								indicator: "green",
							});
							grid.go(grid.page);
						},
					});
				},
			});
		});
	}

	function fetch_catalog_page(vendor, page_number) {
		return frappe
			.call({
				method: "talabat_integration.api.catalog.get_vendor_catalog_page",
				args: { vendor: vendor, page: page_number },
			})
			.then((r) => {
				let rows = (r.message.rows || []).map((row) => ({
					...row,
					status: row.item ? "Mapped" : "Not Mapped",
				}));
				return { rows: rows, has_more: r.message.has_more };
			})
			.catch(() => {
				$status.text(__("Failed to fetch Talabat's catalog for this vendor."));
				return { rows: [], has_more: false };
			});
	}
};
