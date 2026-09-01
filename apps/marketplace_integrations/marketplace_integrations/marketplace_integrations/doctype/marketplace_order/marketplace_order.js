if (typeof frappe !== "undefined") {
	frappe.ui.form.on("Marketplace Order", {
		refresh(frm) {
			if (frm.is_new()) return;
			frm.set_df_property("items", "read_only", 1);
			frm.set_df_property("external_status", "read_only", 1);
			frm.set_df_property("transport_type", "read_only", 1);

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
	module.exports = {build_cart_update_items};
}

function render_item_presentation(frm, presentation) {
	const wrapper = frm.get_field("talabat_order_presentation").$wrapper;
	wrapper.empty();
	const note = document.createElement("p");
	note.textContent = presentation.price_comparison_available
		? __("ERPNext reference prices are shown for configured comparisons.")
		: __("ERPNext reference-price comparison is unavailable until a price source is configured.");
	wrapper.append(note);

	const by_item_id = new Map((presentation.items || []).map((item) => [item.external_item_id, item]));
	for (const row of frm.doc.items || []) {
		const mapping = by_item_id.get(row.external_item_id) || {};
		const card = document.createElement("div");
		card.className = "form-section card-section";
		const title = document.createElement("h5");
		title.textContent = `${row.product_name || __("Unnamed item")} (${row.external_sku || __("No SKU")})`;
		card.append(title);
		const details = document.createElement("p");
		const original = row.pricing_type === "KG" ? row.original_weight : row.original_quantity;
		const fulfilled = row.pricing_type === "KG" ? row.fulfilled_weight : row.fulfilled_quantity;
		details.textContent = [
			`${__("Status")}: ${row.external_status || "—"}`,
			`${__("Pricing")}: ${row.pricing_type || "—"}`,
			`${__("Original")}: ${original ?? "—"}`,
			`${__("Fulfilled")}: ${fulfilled ?? "—"}`,
			`${__("Original unit / total")}: ${row.original_unit_price ?? "—"} / ${row.original_total_price ?? "—"}`,
			`${__("Fulfilled unit / total")}: ${row.fulfilled_unit_price ?? "—"} / ${row.fulfilled_total_price ?? "—"}`,
			`${__("Replacement item ID")}: ${row.replaced_external_item_id || "—"}`,
			`${__("Mapping")}: ${mapping.mapped_item ? __("Mapped") : __("Unmapped")}${mapping.mapped_item ? ` (${mapping.mapped_item})` : ""}`,
		].join(" · ");
		card.append(details);
		wrapper.append(card);
	}
}
