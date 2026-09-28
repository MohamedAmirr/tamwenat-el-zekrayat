frappe.listview_settings["Marketplace Sync Log"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Retry Selected Errors"), async () => {
			const selected = listview.get_checked_items().filter((row) => row.status === "Error");
			if (!selected.length) {
				frappe.msgprint(__("Select one or more Error rows to retry."));
				return;
			}

			const results = await Promise.allSettled(
				selected.map((row) =>
					frappe.call({
						method: "frappe.client.run_doc_method",
						args: {
							dt: "Marketplace Sync Log",
							dn: row.name,
							method: "resync",
						},
					})
				)
			);
			const failed = results.filter((result) => result.status === "rejected").length;
			frappe.msgprint(
				failed
					? __("Retried {0} order(s); {1} request(s) failed.", [selected.length, failed])
					: __("Retried {0} order(s).", [selected.length])
			);
			listview.refresh();
		});
	},
};
