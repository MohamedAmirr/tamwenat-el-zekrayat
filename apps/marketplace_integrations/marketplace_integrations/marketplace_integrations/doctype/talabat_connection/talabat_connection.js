frappe.ui.form.on("Talabat Connection", {
	refresh(frm) {
		frm.add_custom_button(__("Copy Order Webhook Secret"), () => {
			frm.call("get_order_webhook_secret_for_copy").then((response) => {
				frappe.utils.copy_to_clipboard(response.message.order_webhook_secret);
				frappe.show_alert({ message: __("Order webhook secret copied."), indicator: "green" });
			});
		});

		frm.add_custom_button(__("Copy Order Webhook URL"), () => {
			frappe.utils.copy_to_clipboard(frm.doc.order_webhook_url);
			frappe.show_alert({ message: __("Order webhook URL copied."), indicator: "green" });
		});

		frm.add_custom_button(__("Refresh Order Webhook URL"), () => {
			frm.call("refresh_order_webhook_url").then((response) => {
				frm.set_value("order_webhook_url", response.message.order_webhook_url);
				frappe.show_alert({
					message: __("Order webhook URL refreshed. Update it in Talabat's portal."),
					indicator: "green",
				});
			});
		});
	},
});
