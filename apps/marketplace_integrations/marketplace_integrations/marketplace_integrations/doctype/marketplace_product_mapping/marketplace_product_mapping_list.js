if (typeof frappe !== "undefined") {
	frappe.listview_settings["Marketplace Product Mapping"] = {
		onload(listview) {
			if (!frappe.model.can_write("Marketplace Product Mapping")) return;
			listview.page.add_actions_menu_item(__("Push selected to Talabat"), () => {
				push_selected_to_talabat(listview);
			});
		},
	};
}

function push_selected_to_talabat(listview) {
	const mapping_names = listview.get_checked_items(true);
	if (!mapping_names.length) return;

	frappe
		.call({
			method: "marketplace_integrations.api.talabat_catalog.queue_mappings_catalog_sync",
			args: { mapping_names },
			freeze: true,
			freeze_message: __("Queuing Talabat catalog updates..."),
		})
		.then((response) => {
			const result = response.message;
			frappe.show_alert({
				message: __("Queued {0} mappings in {1} Talabat jobs.", [
					result.queued,
					result.jobs,
				]),
				indicator: "green",
			});
			listview.clear_checked_items();
			listview.refresh();
		});
}

if (typeof module !== "undefined") {
	module.exports = { push_selected_to_talabat };
}
