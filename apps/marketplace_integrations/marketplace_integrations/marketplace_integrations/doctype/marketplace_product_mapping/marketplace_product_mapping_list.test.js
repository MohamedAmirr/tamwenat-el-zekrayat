const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, values) =>
	values ? message.replace("{0}", values[0]).replace("{1}", values[1]) : message;

const {
	push_selected,
	route_selected,
	push_selected_to_talabat,
	push_selected_to_shopify,
} = require("./marketplace_product_mapping_list.js");

function make_listview(checked_names) {
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

test("Push selected does nothing without a selection", () => {
	let called = false;
	global.frappe = {
		call() {
			called = true;
		},
	};

	push_selected({ get_checked_items: () => [] });

	assert.equal(called, false);
});

test("Push selected splits a mixed-platform selection into a Talabat push and a Shopify push", async () => {
	const calls = [];
	global.frappe = {
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
		require() {
			return Promise.resolve();
		},
		show_alert() {},
		ui: {
			ShopifyCatalogPushStatus: class {
				constructor(args) {
					this.args = args;
				}
			},
		},
	};
	const listview = make_listview(["MPM-00001", "MPM-00002"]);

	await push_selected(listview);
	// Flush the microtask queue for the chained Promise.all() inside
	// route_selected() - a single setImmediate isn't enough since there are
	// several chained .then() hops between the two get_list calls and the
	// final clear/refresh.
	for (let i = 0; i < 5; i++) {
		await new Promise((resolve) => setImmediate(resolve));
	}

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
	global.frappe = {
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
		require() {
			return Promise.resolve();
		},
		show_alert() {},
		ui: {
			ShopifyCatalogPushStatus: class {
				constructor(args) {
					this.args = args;
				}
			},
		},
	};
	const listview = make_listview(["MPM-00001", "MPM-00002"]);

	await push_selected(listview);
	for (let i = 0; i < 5; i++) {
		await new Promise((resolve) => setImmediate(resolve));
	}

	assert.ok(msgprint_message, "expected a msgprint warning about the unreadable/missing selected row");
});

test("route_selected does nothing but warns when no selected mapping resolves to a known platform", async () => {
	const calls = [];
	let msgprint_message;
	global.frappe = {
		call(options) {
			calls.push(options);
			return Promise.resolve();
		},
		msgprint(message) {
			msgprint_message = message;
		},
	};
	const listview = make_listview([]);

	const result = await route_selected(
		listview,
		[{ name: "MPM-00003", marketplace_account: "MKTACC-UNKNOWN" }],
		{}
	);

	assert.equal(result, undefined);
	assert.equal(calls.length, 0);
	assert.ok(msgprint_message, "expected a msgprint warning about the skipped row");
});

test("route_selected warns about skipped rows but still routes the recognized ones", async () => {
	let msgprint_message;
	global.frappe = {
		call(options) {
			if (options.method === "marketplace_integrations.api.shopify_catalog_push.queue_catalog_push") {
				return Promise.resolve({
					message: { jobs: [{ account: "MKTACC-SHOPIFY", job_id: "job-1", mapping_count: 1 }] },
				});
			}
			return Promise.resolve({ message: { queued: 0, jobs: 0 } });
		},
		msgprint(message) {
			msgprint_message = message;
		},
		require() {
			return Promise.resolve();
		},
		ui: {
			ShopifyCatalogPushStatus: class {
				constructor(args) {
					this.args = args;
				}
			},
		},
	};
	const listview = make_listview(["MPM-00002", "MPM-00003"]);

	await route_selected(
		listview,
		[
			{ name: "MPM-00002", marketplace_account: "MKTACC-SHOPIFY" },
			{ name: "MPM-00003", marketplace_account: "MKTACC-UNKNOWN" },
		],
		{ "MKTACC-SHOPIFY": "Shopify" }
	);

	assert.ok(msgprint_message, "expected a msgprint warning about the skipped row");
	assert.equal(listview.cleared, true);
	assert.equal(listview.refreshed, true);
});

test("route_selected still clears/refreshes the listview when one platform's leg rejects", async () => {
	global.frappe = {
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
		require() {
			return Promise.resolve();
		},
		ui: {
			ShopifyCatalogPushStatus: class {
				constructor(args) {
					this.args = args;
				}
			},
		},
	};
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
});

test("push_selected_to_talabat calls the Talabat bulk endpoint with exactly the given names", async () => {
	let call_options;
	global.frappe = {
		call(options) {
			call_options = options;
			return Promise.resolve({ message: { queued: 2, jobs: 1 } });
		},
		show_alert() {},
	};

	await push_selected_to_talabat(["MPM-00001", "MPM-00002"]);

	assert.equal(
		call_options.method,
		"marketplace_integrations.api.talabat_catalog.queue_mappings_catalog_sync"
	);
	assert.deepEqual(call_options.args, { mapping_names: ["MPM-00001", "MPM-00002"] });
});

test("push_selected_to_shopify calls the Shopify push endpoint and opens the job-status dialog", async () => {
	let call_options;
	let dialog_args;
	global.frappe = {
		call(options) {
			call_options = options;
			return Promise.resolve({
				message: { jobs: [{ account: "MKTACC-SHOPIFY", job_id: "job-1", mapping_count: 1 }] },
			});
		},
		require() {
			return Promise.resolve();
		},
		ui: {
			ShopifyCatalogPushStatus: class {
				constructor(args) {
					dialog_args = args;
				}
			},
		},
	};

	await push_selected_to_shopify(["MPM-00002"]);
	await new Promise((resolve) => setImmediate(resolve));

	assert.equal(call_options.method, "marketplace_integrations.api.shopify_catalog_push.queue_catalog_push");
	assert.deepEqual(call_options.args, { mapping_names: ["MPM-00002"] });
	assert.deepEqual(dialog_args.jobs, [{ account: "MKTACC-SHOPIFY", job_id: "job-1", mapping_count: 1 }]);
});

test("push_selected_to_shopify shows an alert for skipped accounts and does not open the dialog when nothing was queued", async () => {
	let alert_args;
	let dialog_opened = false;
	global.frappe = {
		call() {
			return Promise.resolve({
				message: { jobs: [], skipped_accounts: [{ account: "MKTACC-SHOPIFY", reason: "already running" }] },
			});
		},
		show_alert(args) {
			alert_args = args;
		},
		require() {
			dialog_opened = true;
			return Promise.resolve();
		},
	};

	await push_selected_to_shopify(["MPM-00002"]);

	assert.ok(alert_args, "expected a show_alert call for the skipped account");
	assert.equal(dialog_opened, false);
});
