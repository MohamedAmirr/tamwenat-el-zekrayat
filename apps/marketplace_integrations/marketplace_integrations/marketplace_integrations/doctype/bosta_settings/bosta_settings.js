const GET_PICKUP_LOCATIONS = "marketplace_integrations.api.bosta_settings.get_pickup_locations";

function decode_entities(text) {
	return text
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&quot;/g, '"')
		.replace(/&#x27;|&#39;/g, "'")
		.replace(/&amp;/g, "&");
}

// The server message is HTML-ish (and already escaped for Bosta text): decode, strip tags, escape again.
function server_error_text(error) {
	let text = "";
	try {
		const messages = JSON.parse((error && error.responseJSON && error.responseJSON._server_messages) || "[]");
		text = messages.map((message) => JSON.parse(message).message).join(" ");
	} catch (e) {
		text = "";
	}
	text = decode_entities(text.replace(/<[^>]*>/g, " ")).trim();
	return text || __("Could not load the Bosta pickup locations.");
}

function location_label(location) {
	const detail = [location.address, location.city].filter(Boolean).join(", ");
	const label = detail ? `${location.name} - ${detail}` : location.name || location.id;
	return frappe.utils.escape_html(label);
}

function fetch_locations(frm) {
	if (!frm.doc.api_key) {
		frappe.msgprint(__("Save a Bosta API key first, then fetch the locations."));
		return;
	}
	if (frm.is_dirty()) {
		frappe.msgprint(__("Save your changes first, then fetch the locations."));
		return;
	}
	frappe
		.call({ method: GET_PICKUP_LOCATIONS, type: "POST", freeze: true, freeze_message: __("Loading Bosta locations..."), silent: true })
		.then((r) => {
			const locations = r.message || [];
			if (!locations.length) {
				frappe.msgprint(__("Bosta returned no pickup locations. The saved location is unchanged."));
				return;
			}
			show_location_dialog(frm, locations);
		})
		.catch((error) => show_location_error(frm, server_error_text(error)));
}

function show_location_error(frm, text) {
	const dialog = new frappe.ui.Dialog({
		title: __("Bosta Locations"),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "error",
				options: `<p class="text-danger">${frappe.utils.escape_html(text)}</p>`,
			},
		],
		primary_action_label: __("Retry"),
		primary_action() {
			dialog.hide();
			fetch_locations(frm);
		},
	});
	dialog.show();
}

function show_location_dialog(frm, locations) {
	const current = frm.doc.business_location_id;
	const selected = locations.find((location) => location.id === current);
	// The default is preselected only when no location is set yet.
	const preselected = selected || (!current && locations.find((location) => location.is_default));
	const dialog = new frappe.ui.Dialog({
		title: __("Select Bosta Business Location"),
		fields: [
			{
				fieldname: "location",
				fieldtype: "Select",
				label: __("Location"),
				reqd: 1,
				// Select renders option labels as HTML, so they are escaped here.
				options: [
					{ label: "", value: "" },
					...locations.map((location) => ({ label: location_label(location), value: location.id })),
				],
				default: preselected ? preselected.id : "",
			},
		],
		primary_action_label: __("Use Location"),
		primary_action(values) {
			frm.set_value("business_location_id", values.location);
			dialog.hide();
		},
	});
	dialog.show();
}

frappe.ui.form.on("Bosta Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Fetch Locations"), () => fetch_locations(frm));

		frm.add_custom_button(__("Copy Webhook URL"), () => {
			if (!frm.doc.webhook_url) {
				frappe.msgprint(__("Save Bosta Settings first to generate the webhook URL."));
				return;
			}
			frappe.utils.copy_to_clipboard(frm.doc.webhook_url);
			frappe.show_alert({ message: __("Webhook URL copied."), indicator: "green" });
		});

		frm.add_custom_button(__("Copy Webhook Secret"), () => {
			frm.call("get_webhook_secret_for_copy").then((response) => {
				if (!response.message.webhook_secret) {
					frappe.msgprint(__("Save Bosta Settings first to generate the webhook secret."));
					return;
				}
				frappe.utils.copy_to_clipboard(response.message.webhook_secret);
				frappe.show_alert({ message: __("Webhook secret copied."), indicator: "green" });
			});
		});

		frm.add_custom_button(__("Refresh Webhook URL"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save your changes first, then refresh the webhook URL."));
				return;
			}
			frm.call("refresh_webhook_url").then((response) => {
				frm.set_value("webhook_url", response.message.webhook_url);
				frappe.show_alert({
					message: __("Webhook URL refreshed. Update it in Bosta Dashboard."),
					indicator: "green",
				});
			});
		});
	},
});

if (typeof module !== "undefined") {
	module.exports = { location_label, server_error_text };
}
