// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

const FETCH_BUTTON_LABEL = __("Fetch from Talabat");

frappe.ui.form.on("Talabat Product Mapping", {
	refresh(frm) {
		toggle_fetch_button(frm);
	},
	vendor(frm) {
		toggle_fetch_button(frm);
	},
});

function toggle_fetch_button(frm) {
	// vendor doesn't trigger refresh(), so button visibility is re-derived
	// from both events rather than only on load/save/reload.
	frm.remove_custom_button(FETCH_BUTTON_LABEL);
	if (frm.doc.vendor) {
		frm.add_custom_button(FETCH_BUTTON_LABEL, () => fetch_and_pick_sku(frm));
	}
}

function fetch_and_pick_sku(frm) {
	frappe.call({
		method: "talabat_integration.api.catalog.get_vendor_products",
		args: { vendor: frm.doc.vendor },
		freeze: true,
		freeze_message: __("Fetching products from Talabat..."),
	}).then((r) => {
		const result = r.message || {};
		const products = result.products || [];

		if (!products.length) {
			frappe.msgprint(
				__("Talabat has no products on file yet for this vendor. You can still enter the Talabat SKU manually.")
			);
			return;
		}

		if (result.truncated) {
			frappe.show_alert({
				message: __("This vendor has more than 500 products on Talabat - only the first 500 are shown here."),
				indicator: "orange",
			});
		}

		show_product_picker(frm, products);
	}).catch(() => {
		// frappe.call already surfaces the server-side error to the user;
		// don't block manual entry of talabat_sku on a failed fetch.
		frappe.msgprint(
			__("Couldn't fetch Talabat's product list for this vendor. You can still enter the Talabat SKU manually.")
		);
	});
}

function show_product_picker(frm, products) {
	const dialog = new frappe.ui.Dialog({
		title: __("Select a Talabat product"),
		fields: [
			{
				fieldname: "product",
				fieldtype: "Autocomplete",
				label: __("Product"),
				reqd: 1,
				options: products.map((p) => ({
					label: `${p.sku} — ${p.title}`,
					value: p.sku,
				})),
			},
		],
		primary_action_label: __("Select"),
		primary_action: (values) => {
			frm.set_value("talabat_sku", values.product);
			dialog.hide();
		},
	});
	dialog.show();
}
