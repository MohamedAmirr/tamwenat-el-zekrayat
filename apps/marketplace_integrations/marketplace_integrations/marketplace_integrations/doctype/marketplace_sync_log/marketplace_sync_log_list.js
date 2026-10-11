frappe.listview_settings["Marketplace Sync Log"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Retry Selected Errors"), async () => {
			// Order Import failure rows have no `marketplace_order` yet (there's no
			// Marketplace Order to resync) - `resync()` would blow up on them, so
			// only Error rows that already have a Marketplace Order are retryable
			// here. Those rows need to be re-imported instead, which is a
			// different action.
			const selected = listview
				.get_checked_items()
				.filter((row) => row.status === "Error" && row.marketplace_order);
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
			// resync() returns {ok: false} for an expected blocker (still not
			// syncable): the request succeeded but the order is still stuck.
			const blocked = results.filter(
				(result) => result.status === "fulfilled" && result.value?.message?.ok === false
			).length;
			const recovered = selected.length - failed - blocked;
			frappe.msgprint(
				failed
					? __("Retried {0} order(s); {1} request(s) failed.", [selected.length, failed])
					: blocked
						? __("Retried {0} order(s): {1} recovered, {2} still blocked.", [
								selected.length,
								recovered,
								blocked,
							])
						: __("Retried {0} order(s).", [selected.length])
			);
			listview.refresh();
		});
	},
};
