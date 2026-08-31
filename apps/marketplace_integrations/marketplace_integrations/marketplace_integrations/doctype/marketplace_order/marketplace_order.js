frappe.ui.form.on("Marketplace Order", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Refresh from Talabat"), () => {
			frappe.call({
				method: "marketplace_integrations.api.talabat_orders.refresh_order",
				args: { marketplace_order: frm.doc.name },
				freeze: true,
				freeze_message: __("Refreshing from Talabat..."),
			}).then(() => frm.reload_doc());
		});

		load_item_presentation(frm);
	},
});

function load_item_presentation(frm) {
	frappe.call({
		method: "marketplace_integrations.api.marketplace_order_presentation.get_item_presentation",
		args: { marketplace_order: frm.doc.name },
	}).then((response) => render_item_presentation(frm, response.message));
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
