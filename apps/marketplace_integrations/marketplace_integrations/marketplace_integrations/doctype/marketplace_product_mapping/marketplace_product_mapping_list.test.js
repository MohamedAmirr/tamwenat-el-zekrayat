const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, values) =>
	values ? message.replace("{0}", values[0]).replace("{1}", values[1]) : message;

const {
	get_indicator,
	show_sync_errors,
	show_inventory_errors,
	show_sync_summary,
	push_selected,
	route_selected,
	push_selected_to_talabat,
	push_selected_to_shopify,
	push_selected_to_noon,
	push_selected_noon_stock,
	sync_selected_inventory,
	sync_all,
} = require("./marketplace_product_mapping_list.js");

// Every test installs its own global frappe mock. utils/msgprint/require/ui
// are always present so the shared summary and job-dialog code paths never
// depend on which test happens to set them.
function install_frappe(overrides = {}) {
	global.frappe = Object.assign(
		{
			utils: { escape_html: (text) => String(text) },
			msgprint() {},
			show_alert() {},
			require: () => Promise.resolve(),
			model: { can_write: () => true },
			ui: {
				ShopifyCatalogPushStatus: class {
					static INVENTORY_OPTIONS = { status_method: "inventory.status" };
					constructor(args) {
						this.args = args;
					}
				},
			},
		},
		overrides
	);
	return global.frappe;
}

function make_listview(checked_names = []) {
	return {
		get_checked_items() {
			return checked_names;
		},
		clear_checked_items() {
			this.cleared = true;
		},
		refresh() {
			this.refreshed = true;
		},
	};
}

async function settle() {
	for (let i = 0; i < 6; i++) {
		await new Promise((resolve) => setImmediate(resolve));
	}
}

test("get_indicator colours each sync status", () => {
	assert.deepEqual(get_indicator({ sync_status: "Error" }), ["Error", "red", "sync_status,=,Error"]);
	assert.deepEqual(get_indicator({ sync_status: "Queued" }), ["Queued", "blue", "sync_status,=,Queued"]);
	assert.deepEqual(get_indicator({ sync_status: "Synced" }), ["Synced", "green", "sync_status,=,Synced"]);
	assert.deepEqual(get_indicator({ sync_status: "Not Synced" }), ["Not Synced", "grey", "sync_status,in,Not Synced,"]);
});

test("get_indicator treats a blank sync status as Not Synced and its filter matches blank values too", () => {
	assert.deepEqual(get_indicator({}), ["Not Synced", "grey", "sync_status,in,Not Synced,"]);
	assert.deepEqual(get_indicator({ sync_status: "Not Synced" }), ["Not Synced", "grey", "sync_status,in,Not Synced,"]);
});

test("get_indicator is red when inventory_sync_status is Error, even if the catalog sync is fine", () => {
	assert.deepEqual(get_indicator({ sync_status: "Synced", inventory_sync_status: "Error" }), [
		"Inventory Error",
		"red",
		"inventory_sync_status,=,Error",
	]);
	assert.deepEqual(get_indicator({ sync_status: "Queued", inventory_sync_status: "Error" })[1], "red");
	assert.deepEqual(get_indicator({ sync_status: "Not Synced", inventory_sync_status: "Error" })[1], "red");
});

test("a catalog Error still wins over an inventory Error", () => {
	assert.deepEqual(get_indicator({ sync_status: "Error", inventory_sync_status: "Error" }), [
		"Error",
		"red",
		"sync_status,=,Error",
	]);
});

test("get_indicator keeps the Synced and Not Synced colours when inventory is fine", () => {
	assert.equal(get_indicator({ sync_status: "Synced", inventory_sync_status: "Synced" })[1], "green");
	assert.equal(get_indicator({ sync_status: "Not Synced", inventory_sync_status: "Not Synced" })[1], "grey");
});

test("the list view reads inventory_sync_status through add_fields", () => {
	const saved_frappe = global.frappe;
	const path = require.resolve("./marketplace_product_mapping_list.js");
	global.frappe = { listview_settings: {}, utils: { escape_html: (text) => String(text) } };
	delete require.cache[path];
	try {
		require(path);
		assert.deepEqual(global.frappe.listview_settings["Marketplace Product Mapping"].add_fields, [
			"inventory_sync_status",
		]);
	} finally {
		delete require.cache[path];
		global.frappe = saved_frappe;
	}
});

test("the Sync Errors quick filter filters on sync_status = Error", () => {
	const added = [];
	show_sync_errors({ filter_area: { add: (filters) => added.push(filters) } });

	assert.deepEqual(added, [[["Marketplace Product Mapping", "sync_status", "=", "Error"]]]);
});

test("the Inventory Errors quick filter filters on inventory_sync_status = Error", () => {
	const added = [];
	show_inventory_errors({ filter_area: { add: (filters) => added.push(filters) } });

	assert.deepEqual(added, [[["Marketplace Product Mapping", "inventory_sync_status", "=", "Error"]]]);
});

test("Push selected does nothing without a selection", () => {
	let called = false;
	install_frappe({
		call() {
			called = true;
		},
	});

	push_selected({ get_checked_items: () => [] });

	assert.equal(called, false);
});

test("Push selected splits a mixed-platform selection into a Talabat push and a Shopify push", async () => {
	const calls = [];
	install_frappe({
		call(options) {
			calls.push(options);
			if (options.method === "frappe.client.get_list" && options.args.doctype === "Marketplace Product Mapping") {
				return Promise.resolve({
					message: [
						{ name: "MPM-00001", marketplace_account: "MKTACC-TALABAT" },
						{ name: "MPM-00002", marketplace_account: "MKTACC-SHOPIFY" },
					],
				});
			}
			if (options.method === "frappe.client.get_list" && options.args.doctype === "Marketplace Account") {
				return Promise.resolve({
					message: [
						{ name: "MKTACC-TALABAT", platform: "Talabat" },
						{ name: "MKTACC-SHOPIFY", platform: "Shopify" },
					],
				});
			}
			if (options.method === "marketplace_integrations.api.talabat_catalog.queue_mappings_catalog_sync") {
				return Promise.resolve({ message: { queued: 1, jobs: 1 } });
			}
			if (options.method === "marketplace_integrations.api.shopify_catalog_push.queue_catalog_push") {
				return Promise.resolve({
					message: { jobs: [{ account: "MKTACC-SHOPIFY", job_id: "job-1", mapping_count: 1 }] },
				});
			}
			throw new Error(`Unexpected frappe.call in test: ${options.method}`);
		},
	});
	const listview = make_listview(["MPM-00001", "MPM-00002"]);

	await push_selected(listview);
	await settle();

	const talabat_call = calls.find(
		(c) => c.method === "marketplace_integrations.api.talabat_catalog.queue_mappings_catalog_sync"
	);
	const shopify_call = calls.find(
		(c) => c.method === "marketplace_integrations.api.shopify_catalog_push.queue_catalog_push"
	);
	assert.deepEqual(talabat_call.args, { mapping_names: ["MPM-00001"] });
	assert.deepEqual(shopify_call.args, { mapping_names: ["MPM-00002"] });
	assert.equal(listview.cleared, true);
	assert.equal(listview.refreshed, true);
});

test("push_selected warns when get_list returns fewer rows than were selected (permission-filtered or deleted)", async () => {
	let msgprint_message;
	install_frappe({
		call(options) {
			if (options.method === "frappe.client.get_list" && options.args.doctype === "Marketplace Product Mapping") {
				// Only one of the two selected names comes back - the other was
				// filtered out by read permissions.
				return Promise.resolve({
					message: [{ name: "MPM-00001", marketplace_account: "MKTACC-SHOPIFY" }],
				});
			}
			if (options.method === "frappe.client.get_list" && options.args.doctype === "Marketplace Account") {
				return Promise.resolve({ message: [{ name: "MKTACC-SHOPIFY", platform: "Shopify" }] });
			}
			if (options.method === "marketplace_integrations.api.shopify_catalog_push.queue_catalog_push") {
				return Promise.resolve({
					message: { jobs: [{ account: "MKTACC-SHOPIFY", job_id: "job-1", mapping_count: 1 }] },
				});
			}
			throw new Error(`Unexpected frappe.call in test: ${options.method}`);
		},
		msgprint(message) {
			msgprint_message = message;
		},
	});
	const listview = make_listview(["MPM-00001", "MPM-00002"]);

	await push_selected(listview);
	await settle();

	assert.ok(msgprint_message, "expected a msgprint warning about the unreadable/missing selected row");
});

test("route_selected does nothing but warns when no selected mapping resolves to a known platform", async () => {
	const calls = [];
	let msgprint_args;
	install_frappe({
		call(options) {
			calls.push(options);
			return Promise.resolve();
		},
		msgprint(args) {
			msgprint_args = args;
		},
	});
	const listview = make_listview([]);

	const result = await route_selected(listview, [{ name: "MPM-00003", marketplace_account: "MKTACC-UNKNOWN" }], {});

	assert.equal(result, undefined);
	assert.equal(calls.length, 0);
	assert.equal(msgprint_args.indicator, "orange");
	assert.match(msgprint_args.message, /1 mapping\(s\) skipped/);
});

test("route_selected warns about skipped rows but still routes the recognized ones", async () => {
	let msgprint_args;
	install_frappe({
		call(options) {
			if (options.method === "marketplace_integrations.api.shopify_catalog_push.queue_catalog_push") {
				return Promise.resolve({
					message: { jobs: [{ account: "MKTACC-SHOPIFY", job_id: "job-1", mapping_count: 1 }] },
				});
			}
			return Promise.resolve({ message: { queued: 0, jobs: 0 } });
		},
		msgprint(args) {
			msgprint_args = args;
		},
	});
	const listview = make_listview(["MPM-00002", "MPM-00003"]);

	await route_selected(
		listview,
		[
			{ name: "MPM-00002", marketplace_account: "MKTACC-SHOPIFY" },
			{ name: "MPM-00003", marketplace_account: "MKTACC-UNKNOWN" },
		],
		{ "MKTACC-SHOPIFY": "Shopify" }
	);

	assert.match(msgprint_args.message, /Queued/);
	assert.match(msgprint_args.message, /Skipped/);
	assert.equal(listview.cleared, true);
	assert.equal(listview.refreshed, true);
});

test("route_selected still clears/refreshes the listview and reports Failed when one platform's leg rejects", async () => {
	let msgprint_args;
	install_frappe({
		call(options) {
			if (options.method === "marketplace_integrations.api.talabat_catalog.queue_mappings_catalog_sync") {
				return Promise.reject(new Error("Talabat push failed"));
			}
			if (options.method === "marketplace_integrations.api.shopify_catalog_push.queue_catalog_push") {
				return Promise.resolve({
					message: { jobs: [{ account: "MKTACC-SHOPIFY", job_id: "job-1", mapping_count: 1 }] },
				});
			}
			throw new Error(`Unexpected frappe.call in test: ${options.method}`);
		},
		msgprint(args) {
			msgprint_args = args;
		},
	});
	const listview = make_listview(["MPM-00001", "MPM-00002"]);

	await route_selected(
		listview,
		[
			{ name: "MPM-00001", marketplace_account: "MKTACC-TALABAT" },
			{ name: "MPM-00002", marketplace_account: "MKTACC-SHOPIFY" },
		],
		{ "MKTACC-TALABAT": "Talabat", "MKTACC-SHOPIFY": "Shopify" }
	);

	assert.equal(listview.cleared, true);
	assert.equal(listview.refreshed, true);
	assert.equal(msgprint_args.indicator, "red");
	assert.match(msgprint_args.message, /Talabat push failed/);
	assert.match(msgprint_args.message, /Queued/);
});

test("route_selected sends each Noon account once with its own mapping names", async () => {
	const calls = [];
	install_frappe({
		call(options) {
			calls.push(options);
			return Promise.resolve({ message: { pricing: { queued: true }, stock: { queued: true } } });
		},
	});
	const listview = make_listview([]);

	await route_selected(
		listview,
		[
			{ name: "MPM-1", marketplace_account: "NOON-A" },
			{ name: "MPM-2", marketplace_account: "NOON-A" },
			{ name: "MPM-3", marketplace_account: "NOON-B" },
		],
		{ "NOON-A": "Noon", "NOON-B": "Noon" }
	);

	const noon_calls = calls.filter((c) => c.method === "marketplace_integrations.api.noon_catalog.queue_account_sync");
	assert.equal(noon_calls.length, 2);
	assert.deepEqual(noon_calls.map((c) => c.args), [
		{ account: "NOON-A", mapping_names: ["MPM-1", "MPM-2"] },
		{ account: "NOON-B", mapping_names: ["MPM-3"] },
	]);
});

test("Noon push reports already-queued pricing or stock as skipped, not as queued", async () => {
	install_frappe({
		call() {
			return Promise.resolve({ message: { pricing: { queued: false }, stock: { queued: true } } });
		},
	});

	const items = await push_selected_to_noon("NOON-A", ["MPM-1"]);

	assert.deepEqual(items, [
		{ category: "skipped", platform: "Noon", text: "NOON-A: pricing already queued." },
		{ category: "queued", platform: "Noon", text: "NOON-A: stock queued." },
	]);
});

test("Noon push reports a paused pricing or stock switch as skipped", async () => {
	install_frappe({
		call() {
			return Promise.resolve({ message: { pricing: { queued: false, paused: true }, stock: { queued: true } } });
		},
	});

	const items = await push_selected_to_noon("NOON-A", ["MPM-1"]);

	assert.deepEqual(items, [
		{ category: "skipped", platform: "Noon", text: "NOON-A: pricing push paused." },
		{ category: "queued", platform: "Noon", text: "NOON-A: stock queued." },
	]);
});

test("the inventory action reads INVENTORY_OPTIONS only after the status-dialog script has loaded", async () => {
	// Fresh page load: the dialog class and INVENTORY_OPTIONS do not exist until
	// frappe.require() resolves and the script defines them.
	let dialog_args;
	install_frappe({
		call() {
			return Promise.resolve({ message: { jobs: [{ account: "A", job_id: "j", mapping_count: 1 }] } });
		},
		ui: {},
		require() {
			global.frappe.ui.ShopifyCatalogPushStatus = class {
				constructor(args) {
					dialog_args = args;
				}
			};
			global.frappe.ui.ShopifyCatalogPushStatus.INVENTORY_OPTIONS = { status_method: "inventory.status" };
			return Promise.resolve();
		},
	});

	await sync_selected_inventory(["MPM-1"]);
	await settle();

	assert.ok(dialog_args, "the status dialog should open once the script has loaded");
	assert.equal(dialog_args.status_method, "inventory.status");
});

test("Sync All for Shopify opens the price dialog first and the inventory dialog only after it finishes", async () => {
	const dialog_args = [];
	install_frappe({
		call(options) {
			if (options.method === "frappe.client.get_value") {
				return Promise.resolve({ message: { platform: "Shopify" } });
			}
			if (options.method === "marketplace_integrations.api.shopify_catalog_push.queue_account_catalog_push") {
				return Promise.resolve({ message: { jobs: [{ account: "S", job_id: "price-1", mapping_count: 1 }], skipped_accounts: [] } });
			}
			return Promise.resolve({ message: { jobs: [{ account: "S", job_id: "inv-1", mapping_count: 1 }], skipped_accounts: [] } });
		},
		ui: {
			ShopifyCatalogPushStatus: class {
				static INVENTORY_OPTIONS = { status_method: "inventory.status" };
				constructor(args) {
					dialog_args.push(args);
				}
			},
		},
	});

	await sync_all(make_listview([]), "SHOP-ACC");
	await settle();

	assert.equal(dialog_args.length, 1, "only the price dialog opens while the price jobs run");
	assert.deepEqual(dialog_args[0].jobs.map((job) => job.job_id), ["price-1"]);

	dialog_args[0].on_done();
	await settle();

	assert.equal(dialog_args.length, 2);
	assert.equal(dialog_args[1].status_method, "inventory.status");
	assert.deepEqual(dialog_args[1].jobs.map((job) => job.job_id), ["inv-1"]);
});

test("Sync All for Shopify opens the inventory dialog when the price dialog is closed early, and shows the summary only after the last dialog", async () => {
	const dialog_args = [];
	const events = [];
	install_frappe({
		call(options) {
			if (options.method === "frappe.client.get_value") {
				return Promise.resolve({ message: { platform: "Shopify" } });
			}
			if (options.method === "marketplace_integrations.api.shopify_catalog_push.queue_account_catalog_push") {
				return Promise.resolve({ message: { jobs: [{ account: "S", job_id: "price-1", mapping_count: 1 }], skipped_accounts: [] } });
			}
			return Promise.resolve({ message: { jobs: [{ account: "S", job_id: "inv-1", mapping_count: 1 }], skipped_accounts: [] } });
		},
		msgprint() {
			events.push("summary");
		},
		ui: {
			ShopifyCatalogPushStatus: class {
				static INVENTORY_OPTIONS = { status_method: "inventory.status" };
				constructor(args) {
					events.push("dialog");
					dialog_args.push(args);
				}
			},
		},
	});

	await sync_all(make_listview([]), "SHOP-ACC");
	await settle();

	assert.equal(dialog_args.length, 1);
	assert.deepEqual(events, ["dialog"], "the summary must not stack on the first status dialog");

	// The user closes the first dialog before its jobs finish.
	dialog_args[0].on_close();
	await settle();
	assert.equal(dialog_args.length, 2, "closing the first dialog early must still open the inventory dialog");
	assert.equal(dialog_args[1].status_method, "inventory.status");

	// A later on_done from the closed first dialog must not open a third one.
	dialog_args[0].on_done();
	await settle();
	assert.equal(dialog_args.length, 2);
	assert.deepEqual(events, ["dialog", "dialog"], "the summary still waits for the last dialog");

	dialog_args[1].on_done();
	await settle();
	assert.deepEqual(events, ["dialog", "dialog", "summary"]);
});

test("Sync All for Shopify says No enabled mappings when the price push queues and skips nothing", async () => {
	let msgprint_args;
	install_frappe({
		call(options) {
			if (options.method === "frappe.client.get_value") {
				return Promise.resolve({ message: { platform: "Shopify" } });
			}
			return Promise.resolve({ message: { jobs: [], skipped_accounts: [] } });
		},
		msgprint(args) {
			msgprint_args = args;
		},
	});

	await sync_all(make_listview([]), "SHOP-ACC");

	assert.match(msgprint_args.message, /Shopify: No enabled mappings\./);
});

test("a failed leg reports the server's own message, with its entities decoded once", async () => {
	let msgprint_args;
	install_frappe({
		call() {
			return Promise.reject({
				responseJSON: {
					_server_messages: JSON.stringify([
						JSON.stringify({ message: "Price &amp; stock limit &lt;b&gt;reached&lt;/b&gt;" }),
					]),
				},
			});
		},
		msgprint(args) {
			msgprint_args = args;
		},
	});

	await route_selected(make_listview([]), [{ name: "MPM-1", marketplace_account: "TAL" }], { TAL: "Talabat" });

	assert.equal(msgprint_args.indicator, "red");
	assert.ok(msgprint_args.message.includes("Price & stock limit reached"), msgprint_args.message);
	assert.doesNotMatch(msgprint_args.message, /request failed/);
});

test("Noon stock push reports queued or already-queued", async () => {
	install_frappe({
		call() {
			return Promise.resolve({ message: { queued: false } });
		},
	});

	const items = await push_selected_noon_stock("NOON-A", ["MPM-1"]);

	assert.deepEqual(items, [{ category: "skipped", platform: "Noon", text: "NOON-A: stock already queued." }]);
});

test("the inventory action routes Shopify to queue_inventory_sync and Noon to queue_stock_push per account", async () => {
	const calls = [];
	install_frappe({
		call(options) {
			calls.push(options);
			if (options.method === "marketplace_integrations.api.shopify_inventory.queue_inventory_sync") {
				return Promise.resolve({ message: { jobs: [{ account: "SHOP", job_id: "j", mapping_count: 1 }] } });
			}
			return Promise.resolve({ message: { queued: true } });
		},
	});
	const listview = make_listview([]);

	await route_selected(
		listview,
		[
			{ name: "MPM-1", marketplace_account: "TALABAT" },
			{ name: "MPM-2", marketplace_account: "SHOP" },
			{ name: "MPM-3", marketplace_account: "NOON-A" },
		],
		{ TALABAT: "Talabat", SHOP: "Shopify", "NOON-A": "Noon" },
		"inventory"
	);

	const shopify_call = calls.find((c) => c.method === "marketplace_integrations.api.shopify_inventory.queue_inventory_sync");
	const noon_call = calls.find((c) => c.method === "marketplace_integrations.api.noon_inventory.queue_stock_push");
	assert.deepEqual(shopify_call.args, { mapping_names: ["MPM-2"] });
	assert.deepEqual(noon_call.args, { account: "NOON-A", mapping_names: ["MPM-3"] });
	assert.equal(calls.some((c) => c.method === "marketplace_integrations.api.talabat_catalog.queue_mappings_catalog_sync"), false);
	assert.equal(listview.refreshed, true);
});

test("the inventory action makes no call when nothing is Shopify or Noon", async () => {
	let called = false;
	install_frappe({
		call() {
			called = true;
			return Promise.resolve({});
		},
	});

	await route_selected(make_listview([]), [{ name: "MPM-1", marketplace_account: "T" }], { T: "Talabat" }, "inventory");

	assert.equal(called, false);
});

test("Sync All for Talabat runs the catalog sync for the account", async () => {
	const calls = [];
	install_frappe({
		call(options) {
			calls.push(options);
			if (options.method === "frappe.client.get_value") {
				return Promise.resolve({ message: { platform: "Talabat" } });
			}
			return Promise.resolve({ message: { queued: true } });
		},
	});
	const listview = make_listview([]);

	await sync_all(listview, "TAL-ACC");

	const sync = calls.find((c) => c.method === "marketplace_integrations.api.talabat_catalog.queue_catalog_sync");
	assert.deepEqual(sync.args, { account: "TAL-ACC" });
	assert.equal(listview.refreshed, true);
});

test("Sync All for Shopify runs both the account price push and the inventory reconciliation", async () => {
	const calls = [];
	install_frappe({
		call(options) {
			calls.push(options);
			if (options.method === "frappe.client.get_value") {
				return Promise.resolve({ message: { platform: "Shopify" } });
			}
			return Promise.resolve({ message: { jobs: [], skipped_accounts: [] } });
		},
	});

	await sync_all(make_listview([]), "SHOP-ACC");

	const methods = calls.map((c) => c.method);
	assert.ok(methods.includes("marketplace_integrations.api.shopify_catalog_push.queue_account_catalog_push"));
	assert.ok(methods.includes("marketplace_integrations.api.shopify_inventory.queue_account_reconciliation"));
	assert.deepEqual(
		calls.find((c) => c.method === "marketplace_integrations.api.shopify_catalog_push.queue_account_catalog_push").args,
		{ account: "SHOP-ACC" }
	);
});

test("Sync All for Noon runs queue_account_sync for the whole account, with no mapping selection", async () => {
	const calls = [];
	install_frappe({
		call(options) {
			calls.push(options);
			if (options.method === "frappe.client.get_value") {
				return Promise.resolve({ message: { platform: "Noon" } });
			}
			return Promise.resolve({ message: { pricing: { queued: true }, stock: { queued: true } } });
		},
	});

	await sync_all(make_listview([]), "NOON-ACC");

	const sync = calls.find((c) => c.method === "marketplace_integrations.api.noon_catalog.queue_account_sync");
	assert.deepEqual(sync.args, { account: "NOON-ACC" });
});

test("show_sync_summary escapes every line before rendering it", () => {
	let msgprint_args;
	install_frappe({
		utils: { escape_html: (text) => String(text).replace(/</g, "&lt;").replace(/>/g, "&gt;") },
		msgprint(args) {
			msgprint_args = args;
		},
	});

	show_sync_summary([{ category: "failed", platform: "Shopify", text: "<script>x</script>" }]);

	assert.equal(msgprint_args.indicator, "red");
	assert.match(msgprint_args.message, /&lt;script&gt;x&lt;\/script&gt;/);
	assert.doesNotMatch(msgprint_args.message, /<script>/);
});

test("push_selected_to_talabat calls the Talabat bulk endpoint and returns a queued summary item", async () => {
	let call_options;
	install_frappe({
		call(options) {
			call_options = options;
			return Promise.resolve({ message: { queued: 2, jobs: 1 } });
		},
	});

	const items = await push_selected_to_talabat(["MPM-00001", "MPM-00002"]);

	assert.equal(call_options.method, "marketplace_integrations.api.talabat_catalog.queue_mappings_catalog_sync");
	assert.deepEqual(call_options.args, { mapping_names: ["MPM-00001", "MPM-00002"] });
	assert.deepEqual(items, [{ category: "queued", platform: "Talabat", text: "Queued 2 mapping(s) in 1 job(s)." }]);
});

test("push_selected_to_shopify calls the Shopify push endpoint and opens the job-status dialog", async () => {
	let call_options;
	let dialog_args;
	install_frappe({
		call(options) {
			call_options = options;
			return Promise.resolve({
				message: { jobs: [{ account: "MKTACC-SHOPIFY", job_id: "job-1", mapping_count: 1 }] },
			});
		},
		ui: {
			ShopifyCatalogPushStatus: class {
				constructor(args) {
					dialog_args = args;
				}
			},
		},
	});

	await push_selected_to_shopify(["MPM-00002"]);
	await settle();

	assert.equal(call_options.method, "marketplace_integrations.api.shopify_catalog_push.queue_catalog_push");
	assert.deepEqual(call_options.args, { mapping_names: ["MPM-00002"] });
	assert.deepEqual(dialog_args.jobs, [{ account: "MKTACC-SHOPIFY", job_id: "job-1", mapping_count: 1 }]);
});

test("push_selected_to_shopify returns skipped items and does not open the dialog when nothing was queued", async () => {
	let dialog_opened = false;
	install_frappe({
		call() {
			return Promise.resolve({
				message: { jobs: [], skipped_accounts: [{ account: "MKTACC-SHOPIFY", reason: "already running" }] },
			});
		},
		require() {
			dialog_opened = true;
			return Promise.resolve();
		},
	});

	const items = await push_selected_to_shopify(["MPM-00002"]);

	assert.deepEqual(items, [{ category: "skipped", platform: "Shopify", text: "MKTACC-SHOPIFY: already running" }]);
	assert.equal(dialog_opened, false);
});

test("push_selected_to_shopify lists each skipped account's real reason", async () => {
	install_frappe({
		call() {
			return Promise.resolve({
				message: {
					jobs: [],
					skipped_accounts: [
						{ account: "ACC-A", reason: "Marketplace Account ACC-A requires an ERPNext Selling Price List." },
						{ account: "ACC-B", reason: "A catalog push is already running for this Marketplace Account." },
					],
				},
			});
		},
	});

	const items = await push_selected_to_shopify(["MPM-1"]);

	assert.deepEqual(
		items.map((item) => item.text),
		[
			"ACC-A: Marketplace Account ACC-A requires an ERPNext Selling Price List.",
			"ACC-B: A catalog push is already running for this Marketplace Account.",
		]
	);
});

test("sync_selected_inventory returns each skipped account's reason and opens the dialog with inventory options", async () => {
	let dialog_args;
	install_frappe({
		call() {
			return Promise.resolve({
				message: {
					jobs: [{ account: "A", job_id: "j", mapping_count: 1 }],
					skipped_accounts: [{ account: "B", reason: "Warehouse missing" }],
				},
			});
		},
		ui: {
			ShopifyCatalogPushStatus: class {
				static INVENTORY_OPTIONS = { status_method: "inventory.status" };
				constructor(args) {
					dialog_args = args;
				}
			},
		},
	});

	const items = await sync_selected_inventory(["MPM-1"]);
	await settle();

	assert.deepEqual(items.map((item) => item.text), ["Queued 1 job(s).", "B: Warehouse missing"]);
	assert.equal(dialog_args.status_method, "inventory.status");
	assert.deepEqual(dialog_args.jobs, [{ account: "A", job_id: "j", mapping_count: 1 }]);
});

test("the mapping list hides the docname column and wires the shared row tooltip helper (#324)", async () => {
	const path = require.resolve("./marketplace_product_mapping_list.js");
	delete require.cache[path];
	install_frappe({ listview_settings: {} });
	const loaded = require("./marketplace_product_mapping_list.js");
	const settings = global.frappe.listview_settings["Marketplace Product Mapping"];
	assert.equal(settings.hide_name_column, true);

	// onload keeps the existing buttons and actions, and attaches the tooltip helper.
	const attached = [];
	global.frappe.marketplace_integrations = { list_row_name: { attach_to_list: (lv) => attached.push(lv) } };
	const buttons = [];
	const actions = [];
	const listview = {
		page: {
			add_inner_button: (label) => buttons.push(label),
			add_actions_menu_item: (label) => actions.push(label),
		},
	};
	settings.onload(listview);
	await settle();
	assert.deepEqual(attached, [listview]);
	assert.deepEqual(buttons, ["Sync Errors", "Inventory Errors"]);
	assert.deepEqual(actions, ["Push selected", "Sync Inventory", "Sync All"]);
	assert.equal(typeof loaded.show_row_name_on_hover, "function");
});
