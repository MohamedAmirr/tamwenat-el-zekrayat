if (typeof frappe !== "undefined") {
	frappe.listview_settings["Marketplace Product Mapping"] = {
		// #324: the external reference is the subject; the docname (ID) column is hidden.
		hide_name_column: true,
		get_indicator,
		// Not in_list_view, but get_indicator() reads it (#168 review finding 3).
		add_fields: ["inventory_sync_status"],
		onload(listview) {
			show_row_name_on_hover(listview);
			load_mapping_sync(false);
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

// #374: the sync router and summary live in public/js/mapping_sync_actions.js, shared with the Item
// form's Marketplace Listings tab. In the browser it is loaded with frappe.require() from onload (a
// click happens later) and read from frappe.marketplace_integrations.mapping_sync; under node it is
// required directly. (Frappe 16 runs each doctype's list script in its own scope via new Function(),
// so top-level names only need to be unique within this file.)
const MAPPING_SYNC_ASSET = "/assets/marketplace_integrations/js/mapping_sync_actions.js";

function mapping_sync() {
	if (typeof module !== "undefined") return require("../../../public/js/mapping_sync_actions.js");
	return frappe.marketplace_integrations && frappe.marketplace_integrations.mapping_sync;
}

// Resolves true when the helper is available, false when it failed to load. An action that needs the
// helper stops on false; a failure is shown to the user (report) because the click would otherwise do
// nothing. The preload from onload passes report = false so a load failure is reported on the first
// action instead. In Frappe 16 frappe.require() never rejects: a failed script load still resolves
// (assets.js sets script.onerror = resolve and marks the path executed), so the real failure is
// "resolved but mapping_sync() is falsy", and a later call does not refetch: every action then shows
// the reload error until the page is reloaded. The catch below is defensive only, in case a future
// Frappe version rejects.
function load_mapping_sync(report = true) {
	if (mapping_sync()) return Promise.resolve(true);
	return frappe
		.require(MAPPING_SYNC_ASSET)
		.then(() => Boolean(mapping_sync()))
		.catch((error) => {
			console.error("Mapping sync helper failed to load (#374).", error);
			return false;
		})
		.then((loaded) => {
			if (!loaded && report) {
				frappe.msgprint({
					title: __("Error"),
					message: __("Could not load the sync actions. Reload the page and try again."),
					indicator: "red",
				});
			}
			return loaded;
		});
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

	// A failed helper load already showed an error; stop before any call is made.
	return load_mapping_sync()
		.then((loaded) => (loaded ? resolve_and_route(listview, mapping_names, action) : undefined))
		.catch(log_rejected_call);
}

// A rejected frappe.call (get_list, get_value) has already shown Frappe's own error dialog with the
// server message. Swallow the rejection here so the menu callback does not end in "Uncaught (in
// promise)", and only log it; do not show a second message.
function log_rejected_call(error) {
	console.error("Marketplace Product Mapping sync action failed.", error);
}

function resolve_and_route(listview, mapping_names, action) {
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

function route_selected(listview, rows, platform_by_account, action = "push") {
	return mapping_sync().route_selected(listview, rows, platform_by_account, action);
}

function show_sync_summary(items) {
	return mapping_sync().show_sync_summary(items);
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
	return sync_all_run(listview, account).catch(log_rejected_call);
}

function sync_all_run(listview, account) {
	return load_mapping_sync().then((loaded) => {
		if (!loaded) return;
		return frappe
			.call({
				method: "frappe.client.get_value",
				args: { doctype: "Marketplace Account", filters: account, fieldname: ["platform"] },
			})
			.then((r) => {
				const sync = mapping_sync();
				const platform = r.message && r.message.platform;
				// Shopify legs push their job sets here; the status dialogs open once
				// every leg has queued, one after another (never stacked).
				const dialogs = [];
				const legs = sync_all_legs(platform, account, dialogs);
				if (!legs.length) {
					sync.show_sync_summary([
						{ category: "skipped", platform: account, text: __("No sync is available for this platform.") },
					]);
					return;
				}
				return sync.run_legs(legs).then((items) => {
					listview.clear_checked_items();
					listview.refresh();
					// The summary waits for the status dialogs so it never stacks on
					// top of one; with no dialogs it shows immediately.
					return sync.open_status_dialogs_in_sequence(dialogs, () => sync.show_sync_summary(items));
				});
			});
	});
}

function sync_all_legs(platform, account, dialogs) {
	const sync = mapping_sync();
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
							dialogs.push({ jobs: sync.shopify_jobs(response), inventory: false });
							const items = sync.shopify_job_items(response);
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
							dialogs.push({ jobs: sync.shopify_jobs(response), inventory: true });
							return sync.shopify_job_items(response);
						}),
			},
		];
	}
	if (platform === "Noon") {
		return [{ platform: "Noon", run: () => sync.push_selected_to_noon(account) }];
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
		sync_all,
		push_selected,
		route_selected,
		load_mapping_sync,
	};
}
