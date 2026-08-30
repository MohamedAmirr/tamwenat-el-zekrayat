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

// Talabat SKUs/titles from get_vendor_products() are never server-side
// XSS-sanitized before reaching this dialog (talabat_sku is an opaque
// external identifier with ignore_xss_filter: 1 - see frappe-conventions.md),
// and the Autocomplete control below renders each option's label via
// .html() with no escaping of its own. Escape before interpolating.
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
					label: `${frappe.utils.escape_html(p.sku)} — ${frappe.utils.escape_html(p.title)}`,
					value: p.sku,
				})),
				// Autocomplete isn't in ControlData.set_disp_area()'s escape
				// whitelist (Data/Long Text/Small Text/Text/Password/
				// MultiSelect), so the selected option's *display* value also
				// renders unescaped via .html() - see frappe-conventions.md.
				// `value` (used by frm.set_value below) must stay raw.
				formatter: (value) => frappe.utils.escape_html(value),
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
