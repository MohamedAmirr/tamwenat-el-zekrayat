if (typeof frappe !== "undefined") {
	frappe.ui.form.on("Marketplace Order", {
		refresh(frm) {
			if (frm.is_new()) return;
			frm.set_df_property("items", "read_only", 1);
			frm.set_df_property("external_status", "read_only", 1);
			frm.set_df_property("transport_type", "read_only", 1);
			add_status_indicators(frm);
			load_erpnext_sync_status(frm);

			frm.add_custom_button(__("Refresh from Talabat"), () => {
				frappe.call({
					method: "marketplace_integrations.api.talabat_orders.refresh_order",
					args: { marketplace_order: frm.doc.name },
					freeze: true,
					freeze_message: __("Refreshing from Talabat..."),
				}).then(() => frm.reload_doc());
			});
			add_order_actions(frm);

			load_item_presentation(frm);
		},
	});
}

function load_erpnext_sync_status(frm) {
	frappe.call({
		method: "marketplace_integrations.api.marketplace_orders.get_erpnext_sync_status",
		args: {marketplace_order: frm.doc.name},
	}).then((response) => {
		frm.doc.erpnext_sync_status = response.message.status;
		frm.refresh_field("erpnext_sync_status");
		add_erpnext_sync_action(frm);
	});
}

function add_erpnext_sync_action(frm) {
	if (frm.doc.erpnext_sales_order && frm.doc.erpnext_sync_status === "Synced") return;
	const label = frm.doc.erpnext_sales_order ? __("Update ERPNext Order") : __("Send to ERPNext");
	frm.add_custom_button(label, () => {
		frappe.call({
			method: "marketplace_integrations.api.marketplace_orders.sync_to_erpnext",
			args: {marketplace_order: frm.doc.name},
			freeze: true,
			freeze_message: __("Synchronizing with ERPNext..."),
		}).then((response) => {
			if (!response.message.ok) {
				frappe.msgprint({message: response.message.error, indicator: "red", title: __("ERPNext Sync Blocked")});
			}
			frm.reload_doc();
		});
	});
}

const TALABAT_STATUS_COLORS = {
	RECEIVED: "green",
	ACCEPTED: "green",
	COMPLETED: "green",
	DELIVERED: "green",
	READY_FOR_PICKUP: "green",
	DISPATCHED: "green",
	PENDING: "orange",
	PREPARING: "orange",
	UPDATE_CART: "orange",
	CANCELLED: "red",
	REJECTED: "red",
	FAILED: "red",
};

const ERPNEXT_SYNC_STATUS_COLORS = {
	"Not Synced": "gray",
	Synced: "green",
	"Modified After Sync": "orange",
	"Sync Error": "red",
};

function get_status_indicator(status, color_by_status) {
	return {
		label: status || __("Unavailable"),
		color: color_by_status[status] || "gray",
	};
}

function add_status_indicators(frm) {
	const talabat_status = get_status_indicator(frm.doc.external_status, TALABAT_STATUS_COLORS);
	const erpnext_sync_status = get_status_indicator(frm.doc.erpnext_sync_status, ERPNEXT_SYNC_STATUS_COLORS);
	frm.dashboard.add_indicator(__("Talabat: {0}", [talabat_status.label]), talabat_status.color);
	frm.dashboard.add_indicator(__("ERPNext: {0}", [erpnext_sync_status.label]), erpnext_sync_status.color);
}

function load_item_presentation(frm) {
	frappe.call({
		method: "marketplace_integrations.api.marketplace_order_presentation.get_item_presentation",
		args: { marketplace_order: frm.doc.name },
	}).then((response) => render_item_presentation(frm, response.message));
}

function add_order_actions(frm) {
	const status = frm.doc.external_status;
	if (["RECEIVED", "UPDATE_CART"].includes(status)) {
		frm.add_custom_button(__("Update Cart"), () => update_order(frm, "UPDATE_CART"));
	}
	if (!["CANCELLED", "DELIVERED", "DISPATCHED", "READY_FOR_PICKUP"].includes(status) && frm.doc.transport_type === "VENDOR_DELIVERY") {
		frm.add_custom_button(__("Dispatch"), () => update_order(frm, "DISPATCHED"));
	}
	if (!["CANCELLED", "DELIVERED", "DISPATCHED", "READY_FOR_PICKUP"].includes(status) && frm.doc.transport_type === "LOGISTICS_DELIVERY") {
		frm.add_custom_button(__("Ready for Pickup"), () => update_order(frm, "READY_FOR_PICKUP"));
	}
	if (!["CANCELLED", "DISPATCHED", "READY_FOR_PICKUP", "DELIVERED"].includes(status)) {
		frm.add_custom_button(__("Cancel Order"), () => {
			frappe.prompt(
				[{fieldname: "reason", fieldtype: "Select", label: __("Reason"), options: "CLOSED\nITEM_UNAVAILABLE\nTOO_BUSY", reqd: 1}],
				(values) => update_order(frm, "CANCELLED", values.reason),
				__("Cancel Talabat Order"),
				__("Submit")
			);
		});
	}
}

function update_order(frm, status, cancellation_reason) {
	if (status === "UPDATE_CART") {
		return show_cart_dialog(frm);
	}
	frappe.call({
		method: "marketplace_integrations.api.talabat_orders.update_order",
		args: {marketplace_order: frm.doc.name, status, cancellation_reason},
		freeze: true,
		freeze_message: __("Updating order on Talabat...")
	}).then(() => frm.reload_doc());
}

function show_cart_dialog(frm) {
	const fields = [];
	for (const [index, row] of (frm.doc.items || []).entries()) {
		fields.push({fieldtype: "Section Break", label: row.product_name || row.external_sku || row.external_item_id});
		const status_options = row.external_status === "ADDITION" ? "IN_CART\nNOT_FOUND\nREPLACED\nADDITION" : "IN_CART\nNOT_FOUND\nREPLACED";
		fields.push({fieldname: `item_${index}_status`, fieldtype: "Select", label: __("Status"), options: status_options, default: ["IN_CART", "NOT_FOUND", "REPLACED", "ADDITION"].includes(row.external_status) ? row.external_status : "IN_CART", reqd: 1});
		fields.push({fieldname: `item_${index}_quantity`, fieldtype: "Float", label: row.pricing_type === "KG" ? __("Weight") : __("Quantity"), default: row.pricing_type === "KG" ? row.fulfilled_weight : row.fulfilled_quantity, reqd: 1});
	}
	fields.push({fieldtype: "Section Break", label: __("Add Replacement or Item")});
	fields.push({fieldname: "new_sku", fieldtype: "Data", label: __("New Item SKU")});
	fields.push({fieldname: "new_status", fieldtype: "Select", label: __("New Item Status"), options: "ADDITION\nIN_CART", default: "ADDITION"});
	fields.push({fieldname: "new_replaced_id", fieldtype: "Data", label: __("Replace Item ID"), depends_on: "eval:doc.new_status == 'IN_CART'"});
	fields.push({fieldname: "new_quantity", fieldtype: "Float", label: __("New Quantity")});
	const dialog = new frappe.ui.Dialog({title: __("Update Talabat Cart"), fields, primary_action_label: __("Send to Talabat"), primary_action(values) {
		const items = build_cart_update_items(frm.doc.items || [], values);
		dialog.hide();
		frappe.call({method: "marketplace_integrations.api.talabat_orders.update_order", args: {marketplace_order: frm.doc.name, status: "UPDATE_CART", items: JSON.stringify(items)}, freeze: true, freeze_message: __("Updating cart on Talabat...")}).then(() => frm.reload_doc());
	}});
	dialog.show();
}

function build_cart_update_items(rows, values) {
	const items = rows.map((row, index) => {
		const pricing = {pricing_type: row.pricing_type};
		const item_status = values[`item_${index}_status`];
		if (!["NOT_FOUND", "REPLACED"].includes(item_status)) {
			if (row.pricing_type === "KG") pricing.weight = values[`item_${index}_quantity`];
			else pricing.quantity = values[`item_${index}_quantity`];
		}
		return {
			_id: row.external_item_id,
			sku: row.external_sku,
			status: item_status,
			pricing,
			...(item_status === "IN_CART" && row.replaced_external_item_id
				? {replaced_id: row.replaced_external_item_id}
				: {}),
		};
	});
	if (values.new_sku) {
		items.push({
			sku: values.new_sku,
			status: values.new_status || "ADDITION",
			pricing: {pricing_type: "UNIT", quantity: values.new_quantity || 0},
			...(values.new_status === "IN_CART" && values.new_replaced_id
				? {replaced_id: values.new_replaced_id}
				: {}),
		});
	}
	return items;
}

if (typeof module !== "undefined") {
	module.exports = {
		build_cart_update_items,
		get_status_indicator,
		TALABAT_STATUS_COLORS,
		ERPNEXT_SYNC_STATUS_COLORS,
	};
}

function render_item_presentation(frm, presentation) {
	const wrapper = frm.get_field("talabat_order_presentation").$wrapper;
	wrapper.empty();
	const note = document.createElement("p");
	note.textContent = presentation.price_comparison_available
		? __("ERPNext reference prices are shown for configured comparisons.")
		: __("ERPNext calculates Sales Order prices when you send this order to ERPNext.");
	wrapper.append(note);

	const by_item_id = new Map((presentation.items || []).map((item) => [item.external_item_id, item]));
	const table_wrapper = document.createElement("div");
	table_wrapper.className = "table-responsive";
	const table = document.createElement("table");
	table.className = "table table-bordered";
	table.setAttribute("aria-label", __("Talabat order items"));
	const caption = document.createElement("caption");
	caption.className = "sr-only";
	caption.textContent = __("Talabat order items with fulfillment and live mapping status");
	table.append(caption);

	const headers = [
		__("Item"),
		__("SKU"),
		__("Status"),
		__("Mapping"),
		__("Replacement Item ID"),
		__("Pricing Type"),
		__("Original"),
		__("Fulfilled"),
		__("Original Unit / Total"),
		__("Fulfilled Unit / Total"),
	];
	const header_row = document.createElement("tr");
	for (const header of headers) append_table_cell(header_row, header, "th");
	const thead = document.createElement("thead");
	thead.append(header_row);
	table.append(thead);

	const tbody = document.createElement("tbody");
	for (const row of frm.doc.items || []) {
		const mapping = by_item_id.get(row.external_item_id) || {};
		const item_row = document.createElement("tr");
		append_table_cell(item_row, row.product_name || __("Unnamed item"));
		append_table_cell(item_row, display_value(row.external_sku));
		append_status_cell(item_row, get_status_indicator(row.external_status, TALABAT_STATUS_COLORS));
		append_mapping_cell(item_row, mapping.mapped_item);
		append_table_cell(item_row, display_value(row.replaced_external_item_id));
		append_table_cell(item_row, display_value(row.pricing_type));
		append_table_cell(item_row, fulfillment_value(row, "original"));
		append_table_cell(item_row, fulfillment_value(row, "fulfilled"));
		append_table_cell(item_row, price_value(row.original_unit_price, row.original_total_price));
		append_table_cell(item_row, price_value(row.fulfilled_unit_price, row.fulfilled_total_price));
		tbody.append(item_row);
	}
	table.append(tbody);
	table_wrapper.append(table);
	wrapper.append(table_wrapper);
}

function append_table_cell(table_row, value, tag_name = "td") {
	const cell = document.createElement(tag_name);
	cell.textContent = value;
	table_row.append(cell);
}

function append_status_cell(table_row, status) {
	const cell = document.createElement("td");
	const indicator = document.createElement("span");
	indicator.className = `indicator-pill ${status.color}`;
	indicator.textContent = status.label;
	cell.append(indicator);
	table_row.append(cell);
}

function append_mapping_cell(table_row, mapped_item) {
	const cell = document.createElement("td");
	const indicator = document.createElement("span");
	indicator.className = `indicator-pill ${mapped_item ? "green" : "gray"}`;
	indicator.textContent = mapped_item ? __("Mapped") : __("Unmapped");
	cell.append(indicator);
	if (mapped_item) cell.append(document.createTextNode(` ${mapped_item}`));
	table_row.append(cell);
}

function display_value(value) {
	return value === undefined || value === null || value === "" ? "—" : String(value);
}

function fulfillment_value(row, prefix) {
	const value = row.pricing_type === "KG" ? row[`${prefix}_weight`] : row[`${prefix}_quantity`];
	const unit = row.pricing_type === "KG" ? __("Weight") : __("Quantity");
	const weighted_pieces = row[`${prefix}_weighted_pieces`];
	const pieces = weighted_pieces === undefined || weighted_pieces === null || weighted_pieces === ""
		? ""
		: `; ${__("Weighted pieces")}: ${weighted_pieces}`;
	return `${unit}: ${display_value(value)}${pieces}`;
}

function price_value(unit_price, total_price) {
	return `${display_value(unit_price)} / ${display_value(total_price)}`;
}
