// Shared router and summary for the manual Marketplace Product Mapping sync actions (#374).
// Moved out of marketplace_product_mapping_list.js so the Item form's Marketplace Listings tab
// (item.js) and the mapping list send the same selections to the same platform endpoints.
// Loaded with frappe.require() and exposed as frappe.marketplace_integrations.mapping_sync.
// Every platform call here is an existing whitelisted endpoint; this file adds no sync logic.
(function () {
	// Every leg resolves to an array of summary items {category, platform, text}
	// where category is "queued", "skipped" or "failed". A leg that rejects becomes
	// a "failed" item instead of aborting the others (Promise.allSettled).
	function run_legs(legs) {
		return Promise.allSettled(legs.map((leg) => Promise.resolve().then(leg.run))).then((results) =>
			results.flatMap((result, index) =>
				result.status === "fulfilled"
					? result.value
					: [{ category: "failed", platform: legs[index].platform, text: error_text(result.reason) }]
			)
		);
	}

	// Frappe stores the server's message in responseJSON._server_messages: a JSON
	// array of JSON strings, each carrying a `message`. Surface that instead of the
	// generic fallback. show_sync_summary() escapes the result again, so decode the
	// entities here and drop any markup first.
	const HTML_ENTITIES = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" };

	function server_message_text(error) {
		const raw = error && error.responseJSON && error.responseJSON._server_messages;
		if (!raw) return "";
		try {
			return JSON.parse(raw)
				.map((entry) => JSON.parse(entry).message || "")
				.map((message) =>
					String(message)
						.replace(/&(amp|lt|gt|quot|#39);/g, (entity) => HTML_ENTITIES[entity])
						.replace(/<[^>]*>/g, "")
						.trim()
				)
				.filter(Boolean)
				.join(" ");
		} catch (parse_error) {
			return "";
		}
	}

	function error_text(error) {
		return server_message_text(error) || (error && error.message) || __("The request failed.");
	}

	// `options.freeze` false keeps the screen usable while the (fast) queue call runs.
	function queue_call(method, args, freeze_message, options = {}) {
		const freeze = options.freeze !== false;
		return frappe.call(freeze ? { method, args, freeze: true, freeze_message } : { method, args });
	}

	// Decision 6: a mixed-platform selection is split and routed to each platform's own push.
	// `listview` only needs clear_checked_items() and refresh() (the Item tab passes a stand-in).
	//
	// #107: `action` is "push" (default - the catalog/price push) or "inventory"
	// (Shopify and Noon stock sync; Talabat mappings are reported as skipped, since
	// inventory sync is not offered for Talabat).
	function route_selected(listview, rows, platform_by_account, action = "push") {
		const talabat_names = [];
		const shopify_names = [];
		const noon_names_by_account = {};
		const skipped_items = [];
		let unknown_count = 0;
		rows.forEach((row) => {
			const platform = platform_by_account[row.marketplace_account];
			if (platform === "Shopify") {
				shopify_names.push(row.name);
			} else if (platform === "Talabat") {
				talabat_names.push(row.name);
			} else if (platform === "Noon") {
				if (!noon_names_by_account[row.marketplace_account]) {
					noon_names_by_account[row.marketplace_account] = [];
				}
				noon_names_by_account[row.marketplace_account].push(row.name);
			} else {
				unknown_count += 1;
			}
		});

		const legs = [];
		if (action === "inventory") {
			if (talabat_names.length) {
				skipped_items.push({
					category: "skipped",
					platform: "Talabat",
					text: __("{0} mapping(s) skipped: inventory sync does not apply to Talabat.", [talabat_names.length]),
				});
			}
			if (shopify_names.length) {
				legs.push({ platform: "Shopify", run: () => sync_selected_inventory(shopify_names) });
			}
			Object.keys(noon_names_by_account).forEach((account) => {
				const names = noon_names_by_account[account];
				legs.push({ platform: "Noon", run: () => push_selected_noon_stock(account, names) });
			});
		} else {
			if (talabat_names.length) {
				legs.push({ platform: "Talabat", run: () => push_selected_to_talabat(talabat_names) });
			}
			if (shopify_names.length) {
				legs.push({ platform: "Shopify", run: () => push_selected_to_shopify(shopify_names) });
			}
			Object.keys(noon_names_by_account).forEach((account) => {
				const names = noon_names_by_account[account];
				legs.push({ platform: "Noon", run: () => push_selected_to_noon(account, names) });
			});
		}
		if (unknown_count) {
			skipped_items.push({
				category: "skipped",
				platform: __("Unknown"),
				text: __("{0} mapping(s) skipped: their Marketplace Account's platform does not support this action.", [
					unknown_count,
				]),
			});
		}

		if (!legs.length) {
			show_sync_summary(skipped_items);
			return;
		}

		// Promise.allSettled (via run_legs): a rejection on one platform's leg must
		// not suppress the checked-item clear/refresh or the other legs' results.
		return run_legs(legs).then((items) => {
			listview.clear_checked_items();
			listview.refresh();
			show_sync_summary([...skipped_items, ...items]);
		});
	}

	// One summary for every platform touched by the action (#168): queued, skipped
	// and failed, grouped. Every platform here queues a background job, so "queued"
	// is the success state - none of these legs completes synchronously.
	function show_sync_summary(items) {
		if (!items.length) return;
		const sections = [
			["queued", __("Queued")],
			["skipped", __("Skipped")],
			["failed", __("Failed")],
		]
			.map(([category, label]) => {
				const lines = items.filter((item) => item.category === category);
				if (!lines.length) return "";
				const body = lines
					.map(
						(item) =>
							`<div>${frappe.utils.escape_html(item.platform)}: ${frappe.utils.escape_html(item.text)}</div>`
					)
					.join("");
				return `<p><b>${label}</b></p>${body}`;
			})
			.join("");
		const indicator = items.some((item) => item.category === "failed")
			? "red"
			: items.some((item) => item.category === "skipped")
			? "orange"
			: "green";
		frappe.msgprint({ title: __("Marketplace Sync Summary"), indicator, message: sections });
	}

	function push_selected_to_talabat(mapping_names, options) {
		return queue_call(
			"marketplace_integrations.api.talabat_catalog.queue_mappings_catalog_sync",
			{ mapping_names },
			__("Queuing Talabat catalog updates..."),
			options
		).then((response) => {
			const result = response.message;
			return [
				{
					category: "queued",
					platform: "Talabat",
					text: __("Queued {0} mapping(s) in {1} job(s).", [result.queued, result.jobs]),
				},
			];
		});
	}

	function push_selected_to_shopify(mapping_names, options) {
		return queue_call(
			"marketplace_integrations.api.shopify_catalog_push.queue_catalog_push",
			{ mapping_names },
			__("Queuing Shopify price pushes..."),
			options
		).then((response) => open_shopify_jobs(response));
	}

	function shopify_jobs(response) {
		return (response.message && response.message.jobs) || [];
	}

	// Summary items for a Shopify job response: each queued job, and each account
	// the server skipped with its own reason.
	function shopify_job_items(response) {
		const jobs = shopify_jobs(response);
		const skipped_accounts = (response.message && response.message.skipped_accounts) || [];
		const items = [];
		if (jobs.length) {
			items.push({ category: "queued", platform: "Shopify", text: __("Queued {0} job(s).", [jobs.length]) });
		}
		skipped_accounts.forEach((skipped) => {
			// Each account's own server-provided reason (already running OR a config error).
			items.push({ category: "skipped", platform: "Shopify", text: `${skipped.account}: ${skipped.reason}` });
		});
		return items;
	}

	const SHOPIFY_STATUS_DIALOG_ASSET = "/assets/marketplace_integrations/js/shopify_catalog_push_status.js";

	// Opens one job-status dialog per entry ({jobs, inventory}), one after another:
	// the next opens when the previous one finishes (on_done, every job terminal) OR
	// is closed early (on_close), so two status dialogs never stack and closing the
	// first never skips the second. Entries without jobs are skipped. `on_complete`
	// runs once, after the last dialog finished/closed (immediately if none opened).
	// The inventory options are read inside frappe.require()'s callback - the
	// dialog script that defines INVENTORY_OPTIONS is not loaded until it resolves.
	function open_status_dialogs_in_sequence(entries, on_complete) {
		const [next, ...rest] = entries.filter((entry) => entry.jobs.length);
		if (!next) {
			if (on_complete) on_complete();
			return Promise.resolve();
		}
		return frappe.require(SHOPIFY_STATUS_DIALOG_ASSET).then(() => {
			const dialog_options = next.inventory ? frappe.ui.ShopifyCatalogPushStatus.INVENTORY_OPTIONS : {};
			let advanced = false;
			const advance = () => {
				if (advanced) return;
				advanced = true;
				open_status_dialogs_in_sequence(rest, on_complete);
			};
			new frappe.ui.ShopifyCatalogPushStatus({
				jobs: next.jobs,
				...dialog_options,
				on_done: advance,
				on_close: advance,
			});
		});
	}

	// Opens the shared job-status dialog for queued Shopify jobs and returns the
	// summary items. Shared by the price push and the inventory actions.
	function open_shopify_jobs(response, { inventory = false } = {}) {
		const items = shopify_job_items(response);
		return open_status_dialogs_in_sequence([{ jobs: shopify_jobs(response), inventory }]).then(() => items);
	}

	function push_selected_to_noon(account, mapping_names, options) {
		return queue_call(
			"marketplace_integrations.api.noon_catalog.queue_account_sync",
			mapping_names ? { account, mapping_names } : { account },
			__("Queuing Noon updates..."),
			options
		).then((response) => noon_queue_items(response.message || {}, account));
	}

	function push_selected_noon_stock(account, mapping_names, options) {
		return queue_call(
			"marketplace_integrations.api.noon_inventory.queue_stock_push",
			mapping_names ? { account, mapping_names } : { account },
			__("Queuing Noon stock updates..."),
			options
		).then((response) => {
			const queued = !!(response.message && response.message.queued);
			const manual_override = queued && !!response.message.switch_off;
			return [
				queued
					? {
							category: "queued",
							platform: "Noon",
							text: `${account}: ${manual_override ? __("stock queued as a manual run.") : __("stock queued.")}`,
					  }
					: { category: "skipped", platform: "Noon", text: `${account}: ${__("stock already queued.")}` },
			];
		});
	}

	// #374: price only for Noon, through the existing noon_pricing endpoint (the Item tab's "Price only").
	function push_selected_noon_price(account, mapping_names, options) {
		return queue_call(
			"marketplace_integrations.api.noon_pricing.queue_pricing_push",
			mapping_names ? { account, mapping_names } : { account },
			__("Queuing Noon price updates..."),
			options
		).then((response) => {
			const queued = !!(response.message && response.message.queued);
			const manual_override = queued && !!response.message.switch_off;
			return [
				queued
					? {
							category: "queued",
							platform: "Noon",
							text: `${account}: ${manual_override ? __("pricing queued as a manual run.") : __("pricing queued.")}`,
					  }
					: { category: "skipped", platform: "Noon", text: `${account}: ${__("pricing already queued.")}` },
			];
		});
	}

	// queue_account_sync returns {pricing: {queued, switch_off}, stock: {...}}; queued=false
	// means that update was already queued (a duplicate enqueue was rejected).
	// switch_off: the account's automatic push switch is off and this manual click ran anyway.
	function noon_queue_items(message, account) {
		return ["pricing", "stock"].map((kind) => {
			const queued = !!(message[kind] && message[kind].queued);
			const label = kind === "pricing" ? __("pricing") : __("stock");
			if (queued && message[kind].switch_off) {
				return { category: "queued", platform: "Noon", text: `${account}: ${label} ${__("queued as a manual run.")}` };
			}
			return queued
				? { category: "queued", platform: "Noon", text: `${account}: ${label} ${__("queued.")}` }
				: { category: "skipped", platform: "Noon", text: `${account}: ${label} ${__("already queued.")}` };
		});
	}

	function sync_selected_inventory(mapping_names, options) {
		return queue_call(
			"marketplace_integrations.api.shopify_inventory.queue_inventory_sync",
			{ mapping_names },
			__("Queuing Shopify inventory syncs..."),
			options
		).then((response) => open_shopify_jobs(response, { inventory: true }));
	}

	const api = {
		run_legs,
		server_message_text,
		error_text,
		queue_call,
		route_selected,
		show_sync_summary,
		push_selected_to_talabat,
		push_selected_to_shopify,
		push_selected_to_noon,
		push_selected_noon_stock,
		push_selected_noon_price,
		sync_selected_inventory,
		shopify_jobs,
		shopify_job_items,
		open_status_dialogs_in_sequence,
		open_shopify_jobs,
		noon_queue_items,
	};

	if (typeof frappe !== "undefined") {
		frappe.marketplace_integrations = frappe.marketplace_integrations || {};
		frappe.marketplace_integrations.mapping_sync = api;
	}
	if (typeof module !== "undefined") {
		module.exports = api;
	}
})();
