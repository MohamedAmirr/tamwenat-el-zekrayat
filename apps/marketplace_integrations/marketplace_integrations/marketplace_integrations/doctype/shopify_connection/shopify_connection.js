frappe.ui.form.on("Shopify Connection", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		frm.add_custom_button(__("Sync Webhook Subscriptions"), () => {
			frappe.confirm(
				__(
					"This will delete any of the 7 tracked webhook topics' subscriptions found " +
						"registered at a URL other than this site's own callback URL, then re-create " +
						"whatever is missing at this site's callback URL. If another environment " +
						"(e.g. a different dev tunnel, or another site) shares this same Shopify app, " +
						"its subscriptions can be deleted by this action. Continue?"
				),
				() => {
					frm.call("sync_webhook_subscriptions").then((response) => {
						frm.reload_doc();
						const result = response.message || {};
						if (result.status === "Complete") {
							frappe.show_alert({
								message: __("All Shopify webhook subscriptions are registered."),
								indicator: "green",
							});
						} else if (result.errors && result.errors.length) {
							frappe.show_alert({
								message: __("Shopify webhook subscription sync had errors: {0}", [
									frappe.utils.escape_html(result.errors.join("; ")),
								]),
								indicator: "red",
							});
						} else {
							frappe.show_alert({
								message: __(
									"Shopify webhook subscriptions are incomplete - see Missing Topics."
								),
								indicator: "orange",
							});
						}
					});
				}
			);
		});
	},
});
