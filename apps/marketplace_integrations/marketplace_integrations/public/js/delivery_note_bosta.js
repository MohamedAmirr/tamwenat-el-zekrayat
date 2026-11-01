// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Bosta part of the Delivery Note Shipping section (epic #413). delivery_note_shipping.js owns the
// shared behaviour; this file registers Bosta's server methods with it and adds the two Bosta-only
// pieces: choosing the Bosta city (Bosta is sent a city code) and fetching the A4 Arabic label.
//
// Wrapped in a function: doctype_js files share one scope with ERPNext's own Delivery Note script.
(function () {
	const API = "marketplace_integrations.api.bosta_shipments.";
	const EDITABLE = ["", "Not Created", "Creation Failed"];

	function city_label(city) {
		return city.name_ar && city.name_ar !== city.name ? `${city.name} - ${city.name_ar}` : city.name;
	}

	// Opens the city dialog; the chosen city's code and name are written to the (unsaved) form.
	function pick_city(frm) {
		return frappe.call({ method: API + "get_cities", type: "POST", args: { delivery_note: frm.doc.name }, freeze: true }).then((r) => {
			const data = r.message || {};
			const cities = Array.isArray(data.cities) ? data.cities : [];
			if (!cities.length) {
				frappe.msgprint({ message: __("Bosta's cities could not be loaded. Try again in a minute."), indicator: "orange" });
				return;
			}
			const suggested = (data.suggested && data.suggested.city_id) || "";
			const dialog = new frappe.ui.Dialog({
				title: __("Pick Bosta City"),
				fields: [
					{
						fieldtype: "Autocomplete",
						fieldname: "city",
						label: __("Bosta City"),
						reqd: 1,
						options: cities.map((city) => ({ label: city_label(city), value: city.id })),
						default: suggested,
						description: suggested ? __("Suggested from the delivery address.") : "",
					},
				],
				primary_action_label: __("Use this city"),
				primary_action: (values) => {
					dialog.hide();
					return frappe
						.call({ method: API + "resolve_city", type: "POST", args: { city_id: values.city }, freeze: true })
						.then((res) => {
							const city = res.message || {};
							if (!city.code) return;
							return frm
								.set_value({ custom_bosta_city: city.code, custom_bosta_city_name: city.name })
								.then(() => frappe.show_alert({ message: __("City set. Save the Delivery Note."), indicator: "green" }));
						});
				},
			});
			dialog.show();
		});
	}

	function fetch_label(frm) {
		return frappe
			.call({ method: API + "fetch_label", type: "POST", args: { delivery_note: frm.doc.name }, freeze: true, freeze_message: __("Fetching the label...") })
			.then(() => frm.reload_doc());
	}

	function add_buttons(frm) {
		const status = frm.doc.custom_integration_status || "";
		if (frm.doc.docstatus === 0 && EDITABLE.includes(status)) {
			frm.add_custom_button(__("Pick Bosta City"), () => pick_city(frm));
		}
		if (status === "Created" && !frm.doc.custom_shipping_label) {
			frm.add_custom_button(__("Fetch Bosta Label"), () => fetch_label(frm));
		}
	}

	const hooks = {
		request_method: API + "request_shipment",
		check_method: API + "check_with_bosta",
		refresh_method: API + "refresh_from_bosta",
		add_buttons,
	};

	if (typeof frappe !== "undefined" && frappe.marketplace_integrations && frappe.marketplace_integrations.shipping) {
		frappe.marketplace_integrations.shipping.register_carrier("Bosta", hooks);
	}

	if (typeof module !== "undefined") {
		module.exports = { hooks, pick_city, fetch_label, add_buttons, city_label };
	}
})();
