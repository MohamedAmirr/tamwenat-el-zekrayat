// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Generic paginated table backed by an async data source. No Talabat-specific
// logic belongs here - this is reusable for any future "live remote list with
// an inline editable column" need (see docs/talabat-integration/architecture.md).
//
// A global component: all markup/styling lives in
// public/css/remote_data_grid.css (a real asset, not JS-injected), required
// alongside this file. Visually modeled on Frappe's list view; Frappe CSS
// variables only, so it follows the active theme. The table sits in a scroll
// container so it stays usable on narrow screens (responsive rules in the CSS).
// Built on frappe's globally-available primitives (frappe.ui.form.make_control
// for cells, frappe.toast for notifications) - frappe.DataTable exists but is
// only bundled with the report/web-form bundles, not desk pages.
//
// Usage:
//   frappe.require([
//     "/assets/talabat_integration/js/remote_data_grid.js",
//     "/assets/talabat_integration/css/remote_data_grid.css",
//   ]).then(() => {
//     new frappe.ui.RemoteDataGrid({
//       wrapper: $div,
//       columns: [
//         {fieldname: "sku", label: "SKU"},
//         {fieldname: "item", label: "Item", editable: "Link", options: "Item",
//          get_query: () => ({filters: {...}})},  // optional Link query filter
//       ],
//       fetch_page: (page_number, page_size) => frappe.call(...).then(r => ({rows: r.message.rows, has_more: r.message.has_more})),
//       on_change: (row, fieldname, value) => { ... },  // may return a Promise
//       on_error: (error) => { ... },     // optional fetch failure callback
//       page_size: 20,                  // optional, default 20
//       page_size_options: [20, 50, 100], // optional, default [20, 50, 100]
//       confirm_changes: true,          // optional, default false
//     });
//   });
//
// Editing model:
//   - confirm_changes: false (default) - auto-save: on_change fires as soon as
//     the cell's value changes.
//   - confirm_changes: true - explicit save: changing a cell reveals a save
//     button (Enter also commits). If on_change returns a promise that
//     rejects, the cell reverts to its last saved value.

frappe.provide("frappe.ui");

frappe.ui.RemoteDataGrid = class RemoteDataGrid {
	constructor({
		wrapper,
		columns,
		fetch_page,
		on_change,
		on_error,
		page_size,
		page_size_options,
		confirm_changes,
	}) {
		this.wrapper = $(wrapper);
		this.columns = columns;
		this.fetch_page = fetch_page;
		this.on_change = on_change || function () {};
		this.on_error = on_error || function () {};
		this.confirm_changes = !!confirm_changes;
		this.page_size = page_size || 20;
		// Copy before mutating below - page_size_options may be a caller-owned
		// array (e.g. a module-level constant), and push()/sort() below would
		// otherwise mutate it in place out from under the caller.
		this.page_size_options = page_size_options ? [...page_size_options] : [20, 50, 100];
		if (!this.page_size_options.includes(this.page_size)) {
			this.page_size_options.push(this.page_size);
			this.page_size_options.sort((a, b) => a - b);
		}
		this.page = 1;
		// Incremented per request so a slow earlier response can never
		// overwrite a newer page's rows (rapid Prev/Next clicks).
		this._request_seq = 0;
		// row object -> {fieldname: $cell}, so update_cell() can update a
		// derived/read-only column's display after an editable column in the
		// same row changes, without re-fetching/re-rendering the whole page.
		this._row_cells = new WeakMap();
		this.make();
	}

	make() {
		this.wrapper.empty();
		// Scroll container keeps the table usable on narrow screens without
		// squashing columns (the table itself must never clip - it would cut
		// off the Link fields' dropdowns).
		this.$scroll_wrapper = $('<div class="remote-data-grid-scroll"></div>').appendTo(
			this.wrapper
		);
		this.$table = $('<table class="remote-data-grid"></table>').appendTo(this.$scroll_wrapper);
		this.$thead = $("<thead></thead>").appendTo(this.$table);
		this.$tbody = $("<tbody></tbody>").appendTo(this.$table);

		let header_row = $("<tr></tr>").appendTo(this.$thead);
		this.columns.forEach((col) => {
			$("<th></th>").text(col.label).appendTo(header_row);
		});

		this.$pagination = $('<div class="remote-data-grid-pagination"></div>').appendTo(
			this.wrapper
		);

		// Rows-per-page selector (left side of the paging footer).
		this.$page_size = $('<select class="remote-data-grid-page-size-select"></select>');
		this.page_size_options.forEach((size) => {
			$("<option></option>").attr("value", size).text(size).appendTo(this.$page_size);
		});
		this.$page_size.val(this.page_size).on("change", () => {
			this.page_size = parseInt(this.$page_size.val(), 10);
			this.go(1);
		});
		$('<div class="remote-data-grid-page-size"></div>')
			.append($("<span></span>").text(__("Rows per page")), this.$page_size)
			.appendTo(this.$pagination);

		// Prev / page indicator / Next (right side of the paging footer).
		this.$prev = $('<button class="btn btn-default btn-sm"></button>')
			.text(__("Prev"))
			.on("click", () => this.go(this.page - 1));
		this.$next = $('<button class="btn btn-default btn-sm"></button>')
			.text(__("Next"))
			.on("click", () => this.go(this.page + 1));
		this.$page_indicator = $('<span class="remote-data-grid-page-indicator"></span>');
		$('<div class="remote-data-grid-paging"></div>')
			.append(this.$prev, this.$page_indicator, this.$next)
			.appendTo(this.$pagination);

		this.go(1);
	}

	go(page_number) {
		if (page_number < 1) return;
		this.page = page_number;
		let seq = ++this._request_seq;
		this.$tbody.empty();
		this.$prev.prop("disabled", true);
		this.$next.prop("disabled", true);
		this.$page_indicator.text(__("Loading..."));
		// The .catch()/reject handler below covers only the fetch_page()
		// promise itself - it must NOT also wrap render_page()'s rendering
		// (rows.forEach(render_row)/make_link_cell), or a client-side
		// rendering bug there gets misreported as "Unable to load data" and
		// handed to on_error() as if it were a fetch failure.
		Promise.resolve(this.fetch_page(page_number, this.page_size)).then(
			(result) => {
				if (seq !== this._request_seq) return; // superseded by a newer page request
				this.render_page(result);
			},
			(error) => {
				if (seq !== this._request_seq) return;
				this.render_error(error);
			}
		);
	}

	render_page({ rows, has_more }) {
		this.has_more = has_more;
		this.$prev.prop("disabled", this.page <= 1);
		this.$next.prop("disabled", !has_more);
		this.$page_indicator.text(__("Page {0}", [this.page]));
		if (!rows.length) {
			$('<tr class="remote-data-grid-empty"></tr>')
				.append($("<td></td>").attr("colspan", this.columns.length).text(__("No Data")))
				.appendTo(this.$tbody);
			return;
		}
		rows.forEach((row) => this.render_row(row));
	}

	render_error(error) {
		this.has_more = false;
		this.$prev.prop("disabled", this.page <= 1);
		this.$next.prop("disabled", true);
		this.$page_indicator.text(__("Unable to load"));
		$('<tr class="remote-data-grid-empty"></tr>')
			.append(
				$("<td></td>")
					.attr("colspan", this.columns.length)
					.text(__("Unable to load data"))
			)
			.appendTo(this.$tbody);
		this.on_error(error);
	}

	render_row(row) {
		let $row = $("<tr></tr>").appendTo(this.$tbody);
		let cells = {};
		this.columns.forEach((col) => {
			let $cell = $("<td></td>").appendTo($row);
			cells[col.fieldname] = $cell;
			if (col.editable === "Link") {
				this.make_link_cell($cell, row, col);
			} else {
				// Ellipsis only on plain text cells - an editable cell must never
				// clip, or the Link's dropdown gets cut off at the cell boundary.
				$cell.addClass("remote-data-grid-text");
				$cell.text(row[col.fieldname] == null ? "" : row[col.fieldname]);
			}
		});
		this._row_cells.set(row, cells);
	}

	// Updates a single read-only/derived column's display for one already-
	// rendered row (e.g. a "status" column computed from an editable column
	// that just changed), without re-fetching or re-rendering the whole page.
	// Only supports plain text columns - editable (Link) cells manage their
	// own DOM state via make_link_cell()'s closures and aren't touched here.
	update_cell(row, fieldname, value) {
		row[fieldname] = value;
		let cells = this._row_cells.get(row);
		let $cell = cells && cells[fieldname];
		if (!$cell) return;
		let col = this.columns.find((c) => c.fieldname === fieldname);
		if (col && col.editable === "Link") return;
		$cell.text(value == null ? "" : value);
	}

	make_link_cell($cell, row, col) {
		let $editable = $('<div class="remote-data-grid-editable"></div>').appendTo($cell);
		let control = frappe.ui.form.make_control({
			df: {
				fieldtype: "Link",
				fieldname: col.fieldname,
				options: col.options,
				// Optional caller-supplied Link query (filters/query) so the
				// dropdown can exclude choices the caller wants to avoid.
				get_query: col.get_query ? () => col.get_query(row) : undefined,
			},
			parent: $editable.get(0),
			only_input: true,
			render_input: true,
		});
		let saved_value = row[col.fieldname] || "";
		// This control has no backing Frappe document. set_value() therefore
		// updates the model asynchronously and leaves the input blank; set_input()
		// is the control API for rendering a server-returned value directly.
		control.set_input(saved_value);

		// Explicit save button, only shown when the cell has unsaved edits
		// (confirm_changes mode). Click or Enter commits; Escape reverts.
		let $save_btn = $('<button class="btn btn-primary btn-xs remote-data-grid-save-btn"></button>')
			.attr("type", "button")
			.attr("title", __("Save"))
			.text("✓")
			.appendTo($editable)
			.hide();

		let commit = () => {
			let value = control.get_value();
			row[col.fieldname] = value;
			$save_btn.prop("disabled", true);
			let result = this.on_change(row, col.fieldname, value);
			if (result && result.then) {
				result.then(
					() => {
						saved_value = value;
						$save_btn.prop("disabled", false).hide();
					},
					() => {
						// Save failed: revert the cell to its last saved value
						// so the UI never shows a mapping the server rejected.
						row[col.fieldname] = saved_value;
						control.set_input(saved_value);
						$save_btn.prop("disabled", false).hide();
					}
				);
			} else {
				saved_value = value;
				$save_btn.hide();
			}
		};
		$save_btn.on("click", commit);

		let on_value_changed = () => {
			let value = control.get_value();
			if (value == null) value = "";
			row[col.fieldname] = value;
			if (this.confirm_changes) {
				$save_btn.toggle(value !== saved_value).prop("disabled", false);
			} else {
				this.on_change(row, col.fieldname, value);
			}
		};
		control.$input.on("change", on_value_changed);
		// frappe's Link control sets the input programmatically when a
		// suggestion is picked, and DOM `change` only fires later on blur -
		// without `awesomplete-selectcomplete` the pick would appear to do
		// nothing until the user clicks elsewhere.
		control.$input.on("awesomplete-selectcomplete", on_value_changed);
		control.$input.on("keydown", (e) => {
			if (e.key === "Enter" && $save_btn.is(":visible")) {
				e.preventDefault();
				commit();
			} else if (e.key === "Escape") {
				row[col.fieldname] = saved_value;
				control.set_input(saved_value);
				$save_btn.hide();
			}
		});

		return control;
	}

};
