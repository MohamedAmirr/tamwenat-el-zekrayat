const MAX_NOON_DESTINATIONS = 5;

function show_webhook_result(frm, result) {
	result = result || {};
	frm.set_value("webhook_status", result.status || "");
	frm.set_value("other_noon_destinations", result.other_destinations || 0);
	frm.set_value("last_webhook_event_received_on", result.last_event_received_on || null);
	if (result.error) {
		frappe.msgprint(frappe.utils.escape_html(result.error));
		return;
	}
	frappe.show_alert({
		message: __("Webhook status: {0}", [frappe.utils.escape_html(result.status || "")]),
		indicator: result.status === "Active" ? "green" : "orange",
	});
	frm.reload_doc();
}

function call_webhook_method(frm, method, args) {
	return frm.call(method, args).then((response) => show_webhook_result(frm, response.message));
}

function add_webhook_destination_buttons(frm) {
	const doc = frm.doc || {};
	const group = __("Noon Webhook");

	if (doc.webhook_status === "Not registered") {
		frm.add_custom_button(
			__("Register Webhook in Noon"),
			() => {
				if ((doc.other_noon_destinations || 0) >= MAX_NOON_DESTINATIONS) {
					frappe.msgprint(
						__("Noon allows {0} destinations and this project already has that many.", [
							MAX_NOON_DESTINATIONS,
						])
					);
					return;
				}
				frappe.confirm(
					__("Create a Noon destination for {0} that sends the {1} header (the key is not shown) and subscribe it to FBPI::ORDER_SYNC?", [
						frappe.utils.escape_html(doc.webhook_url || ""),
						"X-Api-Key",
					]),
					() => call_webhook_method(frm, "register_webhook")
				);
			},
			group
		);
	}
	if (doc.webhook_status === "Registered, not subscribed") {
		frm.add_custom_button(
			__("Subscribe to Order Sync"),
			() => call_webhook_method(frm, "subscribe_webhook"),
			group
		);
	}
	if (doc.noon_destination_code) {
		frm.add_custom_button(
			__("Update Noon Destination"),
			() => {
				frappe.prompt(
					[
						{
							fieldname: "send_key",
							fieldtype: "Check",
							label: __("Also send the current webhook key"),
						},
					],
					(values) => call_webhook_method(frm, "update_webhook_destination", { send_key: values.send_key ? 1 : 0 }),
					__("Send the current webhook URL to Noon"),
					__("Update")
				);
			},
			group
		);
		if (doc.webhook_status === "Active") {
			frm.add_custom_button(
				__("Deactivate Destination"),
				() => call_webhook_method(frm, "set_webhook_active", { active: 0 }),
				group
			);
		}
		if (doc.webhook_status === "Inactive") {
			frm.add_custom_button(
				__("Activate Destination"),
				() => call_webhook_method(frm, "set_webhook_active", { active: 1 }),
				group
			);
		}
	}
}

frappe.ui.form.on("Noon Connection", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Copy Webhook URL"), () => {
			if (!frm.doc.webhook_url) {
				frappe.msgprint(__("Save the connection first to generate the webhook URL."));
				return;
			}
			frappe.utils.copy_to_clipboard(frm.doc.webhook_url);
			frappe.show_alert({ message: __("Webhook URL copied."), indicator: "green" });
		});

		frm.add_custom_button(__("Copy Webhook Key"), () => {
			frm.call("get_webhook_secret_for_copy").then((response) => {
				if (!response.message.webhook_secret) {
					frappe.msgprint(__("Save the connection first to generate the webhook key."));
					return;
				}
				frappe.utils.copy_to_clipboard(response.message.webhook_secret);
				frappe.show_alert({ message: __("Webhook key copied."), indicator: "green" });
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
					message: __("Webhook URL refreshed. Update it in Noon's Partner Center."),
					indicator: "green",
				});
			});
		});

		frm.add_custom_button(__("Refresh Webhook Status"), () => {
			frm.call("refresh_webhook_status").then((response) => show_webhook_result(frm, response.message));
		});

		add_webhook_destination_buttons(frm);

		frm.add_custom_button(__("Test Connection"), () => {
			frm.call("test_connection").then((response) => {
				if (response.message && response.message.connected) {
					frappe.show_alert({
						message: __("Noon connection succeeded."),
						indicator: "green",
					});
				}
			});
		});
	},
});

frappe.ui.form.on("Noon Destination", {
	toggle_active(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const activate = !row.is_active;
		frappe.confirm(
			activate
				? __("Activate destination {0}?", [frappe.utils.escape_html(row.destination_name || row.destination_code)])
				: __("Deactivate destination {0}? Noon stops sending events to it.", [
						frappe.utils.escape_html(row.destination_name || row.destination_code),
				  ]),
			() => call_webhook_method(frm, "set_destination_active", { destination_code: row.destination_code, active: activate ? 1 : 0 })
		);
	},
});
