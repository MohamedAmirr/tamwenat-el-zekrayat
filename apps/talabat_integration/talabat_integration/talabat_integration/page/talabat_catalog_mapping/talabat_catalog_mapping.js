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
	// Incremented on every load_vendor() call so a slow response for a
	// previously-selected vendor (mapped-items fetch, or the frappe.require
	// callback that builds the grid) can never land after the user has since
	// picked a different vendor and overwrite this page's state for it.
	let vendor_request_seq = 0;
	page.set_primary_action(__("Add manual mapping"), show_manual_mapping_dialog, "add");
	page.set_secondary_action(
		__("Refresh"),
		() => {
			let vendor = vendor_field.get_value();
			if (!vendor) return;
			// Bypasses load_vendor()'s current_vendor equality guard so
			// re-fetching the same vendor (e.g. after a failed load) works.
			current_vendor = null;
			load_vendor(vendor);
		},
		"refresh"
	);

	// Talabat SKUs/titles and Item names typed or picked here are never
	// server-side XSS-sanitized before reaching this page (talabat_sku is an
	// opaque external identifier with ignore_xss_filter: 1 - see
	// frappe-conventions.md), and frappe.toast()/show_alert() render
	// message.message straight into the DOM. Any value sourced from Talabat's
	// live catalog or user typing must be escaped before it's interpolated
	// into a toast message.
	function esc(value) {
		return frappe.utils.escape_html(value == null ? "" : String(value));
	}

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
						message: __("Item {0} is already mapped to SKU {1}.", [
							esc(values.item),
							esc(existing_sku),
						]),
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
							message: __("Mapping saved: {0} → {1}", [
								esc(values.talabat_sku),
								esc(values.item),
							]),
							indicator: "green",
						});
						// A manually-added mapping is, by definition, for a SKU
						// the live catalog page didn't return (that's this
						// dialog's whole purpose - see architecture.md) so it
						// can't be reflected in an on-page row. No grid refetch
						// needed here.
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
		let seq = ++vendor_request_seq;
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
				if (seq !== vendor_request_seq) return; // superseded by a newer vendor selection

				// Wait for the vendor's full mapping map before the grid can be
				// interacted with - otherwise the duplicate-item guard (both the
				// Link get_query exclusion and the on_change/dialog checks below)
				// is a no-op until this resolves, and a fast user can hit the
				// server's raw item_vendor_key unique-index error instead of the
				// intended client-side toast.
				return fetch_mapped_items(vendor, seq)
					.catch((err) => {
						console.warn("Failed to fetch existing Talabat mappings for vendor", vendor, err);
						if (seq !== vendor_request_seq) return;
						$status.text(
							__(
								"Failed to fetch this vendor's existing mappings; duplicate-mapping checks may be incomplete until you refresh."
							)
						);
					})
					.then(() => {
						if (seq !== vendor_request_seq) return; // superseded while mapped items were loading

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
								fetch_catalog_page(vendor, seq, page_number, page_size),
							on_error: () => {
								// Guard against a stale in-flight request for a
								// since-superseded vendor selection overwriting $status
								// for the vendor the user has since switched to.
								if (seq !== vendor_request_seq) return;
								$status.text(__("Failed to fetch Talabat's catalog for this vendor."));
							},
							confirm_changes: true,
							on_change: (row, fieldname, value) => {
								if (fieldname !== "item") return;
								if (!value) {
									// Item is a mandatory field on Talabat Product
									// Mapping - sending an empty value would surface a
									// raw "Item is required" framework error. This page
									// doesn't support unmapping, so fail clearly instead.
									frappe.toast({
										message: __(
											"Use a value to update the mapping; unmapping isn't supported here yet."
										),
										indicator: "orange",
									});
									return Promise.reject();
								}
								let existing_sku = mapped_item_by_item.get(value);
								if (existing_sku && existing_sku !== row.sku) {
									frappe.toast({
										message: __("Item {0} is already mapped to SKU {1}.", [
											esc(value),
											esc(existing_sku),
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
										if (seq !== vendor_request_seq) return; // superseded by a newer vendor selection
										set_mapped_item(row.sku, value);
										frappe.toast({
											message: __("Mapping saved: {0} → {1}", [esc(row.sku), esc(value)]),
											indicator: "green",
										});
										// Update just this row's derived Status display
										// in place - the grid already has the value it
										// just saved, no need to re-fetch Talabat's live
										// catalog (and a full grid.go() refetch/re-render
										// would also nuke any other row's staged-but-
										// unsaved edit in confirm_changes mode).
										if (grid) {
											grid.update_cell(row, "status", "Mapped");
										}
									});
							},
						});
					});
			});
	}

	function fetch_mapped_items(vendor, seq) {
		return frappe
			.call({
				method: "talabat_integration.api.catalog.get_vendor_mapped_items",
				args: { vendor: vendor },
			})
			.then((r) => {
				if (seq !== vendor_request_seq) return; // superseded by a newer vendor selection
				mapped_item_by_item.clear();
				Object.entries(r.message || {}).forEach(([item, sku]) => {
					mapped_item_by_item.set(item, sku);
				});
			});
	}

	function fetch_catalog_page(vendor, seq, page_number, page_size) {
		return frappe
			.call({
				method: "talabat_integration.api.catalog.get_vendor_catalog_page",
				args: { vendor: vendor, page: page_number, page_size: page_size },
			})
			.then((r) => {
				// Guard against a stale in-flight request for a
				// since-superseded vendor selection overwriting $status for
				// the vendor the user has since switched to.
				if (seq === vendor_request_seq) $status.text("");
				let rows = (r.message.rows || []).map((row) => ({
					...row,
					status: row.item ? "Mapped" : "Not Mapped",
				}));
				return { rows: rows, has_more: r.message.has_more };
			});
	}
};
