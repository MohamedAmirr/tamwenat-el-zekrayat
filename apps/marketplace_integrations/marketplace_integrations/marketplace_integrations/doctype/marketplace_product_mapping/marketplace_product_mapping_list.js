if (typeof frappe !== "undefined") {
	frappe.listview_settings["Marketplace Product Mapping"] = {
		onload(listview) {
			if (!frappe.model.can_write("Marketplace Product Mapping")) return;
			listview.page.add_actions_menu_item(__("Push selected"), () => {
				push_selected(listview);
			});
		},
	};
}

// Decision 6: a mixed-platform selection (some Talabat mappings, some
// Shopify mappings) is split and routed to each platform's own push
// automatically, rather than rejected outright. Uses only the standard
// whitelisted frappe.client.get_list - no new server-side endpoint is needed
// just to resolve which platform each selected mapping's account is on.
function push_selected(listview) {
	const mapping_names = listview.get_checked_items(true);
	if (!mapping_names.length) return;

	return frappe
		.call({
			method: "frappe.client.get_list",
			args: {
				doctype: "Marketplace Product Mapping",
				filters: { name: ["in", mapping_names] },
				fields: ["name", "marketplace_account"],
				limit_page_length: 0,
			},
		})
		.then((r) => {
			const rows = r.message || [];
			// frappe.client.get_list applies read permissions - a row the user
			// selected but cannot read simply isn't returned. Without this
			// check those rows would silently vanish from the push with no
			// message at all (review finding #2).
			if (rows.length < mapping_names.length) {
				frappe.msgprint(
					__(
						"{0} selected Marketplace Product Mapping(s) could not be included: you may not have permission to read them, or they may have been deleted.",
						[mapping_names.length - rows.length]
					)
				);
			}
			if (!rows.length) return;
			const account_names = [...new Set(rows.map((row) => row.marketplace_account))];
			return frappe
				.call({
					method: "frappe.client.get_list",
					args: {
						doctype: "Marketplace Account",
						filters: { name: ["in", account_names] },
						fields: ["name", "platform"],
						limit_page_length: 0,
					},
				})
				.then((account_response) => {
					const platform_by_account = {};
					(account_response.message || []).forEach((account) => {
						platform_by_account[account.name] = account.platform;
					});
					return route_selected(listview, rows, platform_by_account);
				});
		});
}

function route_selected(listview, rows, platform_by_account) {
	const talabat_names = [];
	const shopify_names = [];
	let unknown_count = 0;
	rows.forEach((row) => {
		const platform = platform_by_account[row.marketplace_account];
		if (platform === "Shopify") {
			shopify_names.push(row.name);
		} else if (platform === "Talabat") {
			talabat_names.push(row.name);
		} else {
			unknown_count += 1;
		}
	});

	if (unknown_count) {
		frappe.msgprint(
			__(
				"Skipped {0} selected Marketplace Product Mapping(s): their Marketplace Account's platform does not support a catalog push.",
				[unknown_count]
			)
		);
	}

	const calls = [];
	if (talabat_names.length) calls.push(push_selected_to_talabat(talabat_names));
	if (shopify_names.length) calls.push(push_selected_to_shopify(shopify_names));
	if (!calls.length) return;

	// Promise.allSettled (not Promise.all): a rejection on one platform's leg
	// must not suppress the checked-item clear/refresh for the other
	// platform's leg that DID succeed.
	return Promise.allSettled(calls).then(() => {
		listview.clear_checked_items();
		listview.refresh();
	});
}

function push_selected_to_talabat(mapping_names) {
	return frappe
		.call({
			method: "marketplace_integrations.api.talabat_catalog.queue_mappings_catalog_sync",
			args: { mapping_names },
			freeze: true,
			freeze_message: __("Queuing Talabat catalog updates..."),
		})
		.then((response) => {
			const result = response.message;
			frappe.show_alert({
				message: __("Queued {0} mappings in {1} Talabat jobs.", [result.queued, result.jobs]),
				indicator: "green",
			});
		});
}

function push_selected_to_shopify(mapping_names) {
	return frappe
		.call({
			method: "marketplace_integrations.api.shopify_catalog_push.queue_catalog_push",
			args: { mapping_names },
			freeze: true,
			freeze_message: __("Queuing Shopify price pushes..."),
		})
		.then((response) => {
			const jobs = (response.message && response.message.jobs) || [];
			const skipped_accounts = (response.message && response.message.skipped_accounts) || [];
			if (skipped_accounts.length) {
				// Show each account's own server-provided reason (already
				// running OR a config error), not an assumed one.
				const lines = skipped_accounts.map(
					(skipped) =>
						`${frappe.utils.escape_html(skipped.account)}: ${frappe.utils.escape_html(skipped.reason)}`
				);
				frappe.msgprint({
					title: __("Marketplace Accounts skipped"),
					message: lines.join("<br>"),
					indicator: "orange",
				});
			}
			if (!jobs.length) return;
			return frappe
				.require("/assets/marketplace_integrations/js/shopify_catalog_push_status.js")
				.then(() => {
					new frappe.ui.ShopifyCatalogPushStatus({ jobs });
				});
		});
}

if (typeof module !== "undefined") {
	module.exports = {
		push_selected,
		route_selected,
		push_selected_to_talabat,
		push_selected_to_shopify,
	};
}
