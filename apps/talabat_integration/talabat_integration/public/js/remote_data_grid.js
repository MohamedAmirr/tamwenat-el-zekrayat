// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Generic paginated table backed by an async data source. No Talabat-specific
// logic belongs here - this is reusable for any future "live remote list with
// an inline editable column" need (see docs/talabat-integration/architecture.md).
//
// Usage:
//   new frappe.ui.RemoteDataGrid({
//     wrapper: $div,
//     columns: [
//       {fieldname: "sku", label: "SKU"},
//       {fieldname: "item", label: "Item", editable: "Link", options: "Item"},
//     ],
//     fetch_page: (page_number) => frappe.call(...).then(r => ({rows: r.message.rows, has_more: r.message.has_more})),
//     on_change: (row, fieldname, value) => { ... },
//   });

frappe.provide("frappe.ui");

frappe.ui.RemoteDataGrid = class RemoteDataGrid {
	constructor({ wrapper, columns, fetch_page, on_change }) {
		this.wrapper = $(wrapper);
		this.columns = columns;
		this.fetch_page = fetch_page;
		this.on_change = on_change || function () {};
		this.page = 1;
		this.make();
	}

	make() {
		this.wrapper.empty();
		this.$table = $('<table class="table table-bordered"></table>').appendTo(this.wrapper);
		this.$thead = $("<thead></thead>").appendTo(this.$table);
		this.$tbody = $("<tbody></tbody>").appendTo(this.$table);
		this.$pagination = $('<div class="remote-data-grid-pagination" style="margin-top: 8px;"></div>').appendTo(
			this.wrapper
		);

		let header_row = $("<tr></tr>").appendTo(this.$thead);
		this.columns.forEach((col) => {
			$("<th></th>").text(col.label).appendTo(header_row);
		});

		this.$prev = $('<button class="btn btn-xs btn-default">Prev</button>')
			.on("click", () => this.go(this.page - 1))
			.appendTo(this.$pagination);
		this.$next = $('<button class="btn btn-xs btn-default" style="margin-left: 4px;">Next</button>')
			.on("click", () => this.go(this.page + 1))
			.appendTo(this.$pagination);

		this.go(1);
	}

	go(page_number) {
		if (page_number < 1) return;
		this.page = page_number;
		this.$tbody.empty();
		Promise.resolve(this.fetch_page(page_number)).then(({ rows, has_more }) => {
			this.has_more = has_more;
			this.$prev.prop("disabled", this.page <= 1);
			this.$next.prop("disabled", !has_more);
			rows.forEach((row) => this.render_row(row));
		});
	}

	render_row(row) {
		let $row = $("<tr></tr>").appendTo(this.$tbody);
		this.columns.forEach((col) => {
			let $cell = $("<td></td>").appendTo($row);
			if (col.editable === "Link") {
				this.make_link_cell($cell, row, col);
			} else {
				$cell.text(row[col.fieldname] == null ? "" : row[col.fieldname]);
			}
		});
	}

	make_link_cell($cell, row, col) {
		let control = frappe.ui.form.make_control({
			df: {
				fieldtype: "Link",
				fieldname: col.fieldname,
				options: col.options,
			},
			parent: $cell.get(0),
			only_input: true,
			render_input: true,
		});
		control.set_value(row[col.fieldname] || "");
		control.$input.on("change", () => {
			let value = control.get_value();
			row[col.fieldname] = value;
			this.on_change(row, col.fieldname, value);
		});
	}
};
