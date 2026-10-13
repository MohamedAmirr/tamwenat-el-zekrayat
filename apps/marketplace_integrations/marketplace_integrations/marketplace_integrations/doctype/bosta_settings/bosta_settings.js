frappe.ui.form.on("Bosta Settings", {
	refresh(frm) {
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
