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
