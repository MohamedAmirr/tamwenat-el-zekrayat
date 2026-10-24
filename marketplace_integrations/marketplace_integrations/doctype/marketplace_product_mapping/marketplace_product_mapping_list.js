if (typeof frappe !== "undefined") {
	frappe.listview_settings["Marketplace Product Mapping"] = {
		// #324: the external reference is the subject; the docname (ID) column is hidden.
		hide_name_column: true,
		get_indicator,
		// Not in_list_view, but get_indicator() reads it (#168 review finding 3).
		add_fields: ["inventory_sync_status"],
		onload(listview) {
			show_row_name_on_hover(listview);
			listview.page.add_inner_button(__("Sync Errors"), () => show_sync_errors(listview));
			listview.page.add_inner_button(__("Inventory Errors"), () => show_inventory_errors(listview));
			if (!frappe.model.can_write("Marketplace Product Mapping")) return;
			listview.page.add_actions_menu_item(__("Push selected"), () => {
				push_selected(listview);
			});
			listview.page.add_actions_menu_item(__("Sync Inventory"), () => {
				push_selected(listview, "inventory");
			});
			listview.page.add_actions_menu_item(__("Sync All"), () => {
				show_sync_all_dialog(listview);
			});
		},
	};
}

// #324: loads the shared row tooltip helper (public/js/list_row_name.js). The tooltip is a
// convenience: if the helper cannot load, the list works the same without it. (No top-level const:
// list scripts share one global scope, so a repeated const name would break the other lists.)
function show_row_name_on_hover(listview) {
	return frappe
		.require("/assets/marketplace_integrations/js/list_row_name.js")
		.then(() => {
			const helper = frappe.marketplace_integrations && frappe.marketplace_integrations.list_row_name;
			if (helper) helper.attach_to_list(listview);
		})
		.catch((error) => console.error("List row name helper failed to load (#324).", error));
}

// #168: list-view status colours, so a merchant can scan a mapping list for
// problems without opening each document. Unknown/blank statuses fall back to
// grey (the same as "Not Synced").
const SYNC_STATUS_COLORS = {
	Error: "red",
	Queued: "blue",
	Synced: "green",
	"Not Synced": "grey",
};

// "in" with a trailing empty value so blank/NULL sync_status matches as Not Synced
// (the same empty-string-for-NULL form the repo's backfill patches use).
const NOT_SYNCED_FILTER = "sync_status,in,Not Synced,";

// An Error on either the catalog or the inventory sync turns the row red, so a
// merchant can spot a failing inventory push without opening the mapping.
function get_indicator(doc) {
	if (doc.sync_status === "Error") return [__("Error"), "red", "sync_status,=,Error"];
	if (doc.inventory_sync_status === "Error") {
		return [__("Inventory Error"), "red", "inventory_sync_status,=,Error"];
	}
	const status = doc.sync_status || "Not Synced";
	if (status === "Not Synced") return [__(status), SYNC_STATUS_COLORS[status], NOT_SYNCED_FILTER];
	return [__(status), SYNC_STATUS_COLORS[status] || "grey", `sync_status,=,${status}`];
}

// Same quick-filter pattern as marketplace_order_list.js's show_refunded_orders:
// filter_area.add() takes [doctype, fieldname, condition, value] tuples.
function show_sync_errors(listview) {
	listview.filter_area.add([["Marketplace Product Mapping", "sync_status", "=", "Error"]]);
}

function show_inventory_errors(listview) {
	listview.filter_area.add([["Marketplace Product Mapping", "inventory_sync_status", "=", "Error"]]);
}

// Decision 6: a mixed-platform selection (some Talabat mappings, some
// Shopify mappings) is split and routed to each platform's own push
// automatically, rather than rejected outright. Uses only the standard
// whitelisted frappe.client.get_list - no new server-side endpoint is needed
// just to resolve which platform each selected mapping's account is on.
//
// #107: `action` is "push" (default - the catalog/price push above) or
// "inventory" (Shopify and Noon stock sync; Talabat mappings are reported as
// skipped, since inventory sync is not offered for Talabat).
function push_selected(listview, action = "push") {
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
					return route_selected(listview, rows, platform_by_account, action);
				});
		});
}

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
			return [
				{
					category: "queued",
					platform: "Talabat",
					text: __("Queued {0} mapping(s) in {1} job(s).", [result.queued, result.jobs]),
				},
			];
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
		.then((response) => open_shopify_jobs(response));
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

function push_selected_to_noon(account, mapping_names) {
	return frappe
		.call({
			method: "marketplace_integrations.api.noon_catalog.queue_account_sync",
			args: mapping_names ? { account, mapping_names } : { account },
			freeze: true,
			freeze_message: __("Queuing Noon updates..."),
		})
		.then((response) => noon_queue_items(response.message || {}, account));
}

function push_selected_noon_stock(account, mapping_names) {
	return frappe
		.call({
			method: "marketplace_integrations.api.noon_inventory.queue_stock_push",
			args: mapping_names ? { account, mapping_names } : { account },
			freeze: true,
			freeze_message: __("Queuing Noon stock updates..."),
		})
		.then((response) => {
			const queued = !!(response.message && response.message.queued);
			return [
				queued
					? { category: "queued", platform: "Noon", text: `${account}: ${__("stock queued.")}` }
					: { category: "skipped", platform: "Noon", text: `${account}: ${__("stock already queued.")}` },
			];
		});
}

// queue_account_sync returns {pricing: {queued}, stock: {queued}}; queued=false
// means that update was already queued (a duplicate enqueue was rejected).
function noon_queue_items(message, account) {
	return ["pricing", "stock"].map((kind) => {
		const queued = !!(message[kind] && message[kind].queued);
		const label = kind === "pricing" ? __("pricing") : __("stock");
		if (message[kind] && message[kind].paused) {
			return { category: "skipped", platform: "Noon", text: `${account}: ${label} ${__("push paused.")}` };
		}
		return queued
			? { category: "queued", platform: "Noon", text: `${account}: ${label} ${__("queued.")}` }
			: { category: "skipped", platform: "Noon", text: `${account}: ${label} ${__("already queued.")}` };
	});
}

function sync_selected_inventory(mapping_names) {
	return frappe
		.call({
			method: "marketplace_integrations.api.shopify_inventory.queue_inventory_sync",
			args: { mapping_names },
			freeze: true,
			freeze_message: __("Queuing Shopify inventory syncs..."),
		})
		.then((response) => open_shopify_jobs(response, { inventory: true }));
}

// #168 "Sync All": one account, every platform-appropriate leg. Talabat runs its
// catalog sync; Shopify runs the price push plus inventory reconciliation; Noon
// runs its pricing and stock sync. Each leg reuses the endpoint the per-mapping
// actions already use, so no parallel sync logic is introduced.
function show_sync_all_dialog(listview) {
	const dialog = new frappe.ui.Dialog({
		title: __("Sync All for Marketplace Account"),
		fields: [
			{
				fieldname: "account",
				fieldtype: "Link",
				label: __("Marketplace Account"),
				options: "Marketplace Account",
				get_query: () => ({ filters: { enabled: 1 } }),
				reqd: 1,
			},
		],
		primary_action_label: __("Sync All"),
		primary_action(values) {
			dialog.hide();
			sync_all(listview, values.account);
		},
	});
	dialog.show();
}

function sync_all(listview, account) {
	return frappe
		.call({
			method: "frappe.client.get_value",
			args: { doctype: "Marketplace Account", filters: account, fieldname: ["platform"] },
		})
		.then((r) => {
			const platform = r.message && r.message.platform;
			// Shopify legs push their job sets here; the status dialogs open once
			// every leg has queued, one after another (never stacked).
			const dialogs = [];
			const legs = sync_all_legs(platform, account, dialogs);
			if (!legs.length) {
				show_sync_summary([{ category: "skipped", platform: account, text: __("No sync is available for this platform.") }]);
				return;
			}
			return run_legs(legs).then((items) => {
				listview.clear_checked_items();
				listview.refresh();
				// The summary waits for the status dialogs so it never stacks on
				// top of one; with no dialogs it shows immediately.
				return open_status_dialogs_in_sequence(dialogs, () => show_sync_summary(items));
			});
		});
}

function sync_all_legs(platform, account, dialogs) {
	if (platform === "Talabat") {
		return [
			{
				platform: "Talabat",
				run: () =>
					frappe
						.call({
							method: "marketplace_integrations.api.talabat_catalog.queue_catalog_sync",
							args: { account },
							freeze: true,
							freeze_message: __("Queuing Talabat catalog sync..."),
						})
						.then(() => [{ category: "queued", platform: "Talabat", text: __("Catalog sync queued.") }]),
			},
		];
	}
	if (platform === "Shopify") {
		return [
			{
				platform: "Shopify",
				run: () =>
					frappe
						.call({
							method: "marketplace_integrations.api.shopify_catalog_push.queue_account_catalog_push",
							args: { account },
							freeze: true,
							freeze_message: __("Queuing Shopify price pushes..."),
						})
						.then((response) => {
							dialogs.push({ jobs: shopify_jobs(response), inventory: false });
							const items = shopify_job_items(response);
							// Nothing queued and nothing skipped: the account has no enabled mappings.
							if (!items.length) {
								items.push({ category: "skipped", platform: "Shopify", text: __("No enabled mappings.") });
							}
							return items;
						}),
			},
			{
				platform: "Shopify",
				run: () =>
					frappe
						.call({
							method: "marketplace_integrations.api.shopify_inventory.queue_account_reconciliation",
							args: { account },
							freeze: true,
							freeze_message: __("Queuing Shopify inventory reconciliation..."),
						})
						.then((response) => {
							dialogs.push({ jobs: shopify_jobs(response), inventory: true });
							return shopify_job_items(response);
						}),
			},
		];
	}
	if (platform === "Noon") {
		return [{ platform: "Noon", run: () => push_selected_to_noon(account) }];
	}
	return [];
}

if (typeof module !== "undefined") {
	module.exports = {
		get_indicator,
		show_row_name_on_hover,
		show_sync_errors,
		show_inventory_errors,
		show_sync_summary,
		sync_selected_inventory,
		sync_all,
		push_selected,
		route_selected,
		push_selected_to_talabat,
		push_selected_to_shopify,
		push_selected_to_noon,
		push_selected_noon_stock,
	};
}
