const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, values) =>
	values ? values.reduce((text, value, index) => text.replace(`{${index}}`, value), message) : message;

const sync = require("./mapping_sync_actions.js");

function install_frappe(overrides = {}) {
	global.frappe = Object.assign(
		{
			utils: { escape_html: (text) => String(text).replace(/</g, "&lt;").replace(/>/g, "&gt;") },
			msgprint() {},
			require: () => Promise.resolve(),
		},
		overrides
	);
	return global.frappe;
}

test("the module registers itself on frappe.marketplace_integrations.mapping_sync when loaded in the browser", () => {
	const path = require.resolve("./mapping_sync_actions.js");
	const saved = global.frappe;
	global.frappe = {};
	delete require.cache[path];
	try {
		const loaded = require(path);
		assert.equal(global.frappe.marketplace_integrations.mapping_sync, loaded);
		for (const name of [
			"route_selected",
			"run_legs",
			"show_sync_summary",
			"noon_queue_items",
			"shopify_job_items",
			"open_status_dialogs_in_sequence",
			"server_message_text",
		]) {
			assert.equal(typeof loaded[name], "function", name);
		}
	} finally {
		delete require.cache[path];
		global.frappe = saved;
	}
});

test("the queue calls freeze the screen by default and not when asked not to", async () => {
	const calls = [];
	install_frappe({
		call(options) {
			calls.push(options);
			return Promise.resolve({ message: { queued: 1, jobs: 1 } });
		},
	});

	await sync.push_selected_to_talabat(["MPM-1"]);
	await sync.push_selected_to_talabat(["MPM-1"], { freeze: false });

	assert.equal(calls[0].freeze, true);
	assert.equal(typeof calls[0].freeze_message, "string");
	assert.equal(calls[1].freeze, undefined);
	assert.equal(calls[1].freeze_message, undefined);
	assert.deepEqual(calls[1].args, { mapping_names: ["MPM-1"] });
});

test("Noon price only calls queue_pricing_push and reports queued, manual and already-queued", async () => {
	const replies = [{ queued: true }, { queued: true, switch_off: true }, { queued: false }];
	const calls = [];
	install_frappe({
		call(options) {
			calls.push(options);
			return Promise.resolve({ message: replies.shift() });
		},
	});

	const texts = [];
	for (let i = 0; i < 3; i++) {
		const items = await sync.push_selected_noon_price("NOON-A", ["MPM-1"]);
		texts.push([items[0].category, items[0].text]);
	}

	assert.equal(calls[0].method, "marketplace_integrations.api.noon_pricing.queue_pricing_push");
	assert.deepEqual(calls[0].args, { account: "NOON-A", mapping_names: ["MPM-1"] });
	assert.deepEqual(texts, [
		["queued", "NOON-A: pricing queued."],
		["queued", "NOON-A: pricing queued as a manual run."],
		["skipped", "NOON-A: pricing already queued."],
	]);
});

async function settle() {
	for (let i = 0; i < 6; i++) {
		await new Promise((resolve) => setImmediate(resolve));
	}
}

test("Noon push reports already-queued pricing or stock as skipped, not as queued", async () => {
	install_frappe({
		call() {
			return Promise.resolve({ message: { pricing: { queued: false }, stock: { queued: true } } });
		},
	});

	const items = await sync.push_selected_to_noon("NOON-A", ["MPM-1"]);

	assert.deepEqual(items, [
		{ category: "skipped", platform: "Noon", text: "NOON-A: pricing already queued." },
		{ category: "queued", platform: "Noon", text: "NOON-A: stock queued." },
	]);
});

test("Noon push reports a manual run queued while the account switch is off", async () => {
	install_frappe({
		call() {
			return Promise.resolve({
				message: { pricing: { queued: true, switch_off: true }, stock: { queued: true, switch_off: false } },
			});
		},
	});

	const items = await sync.push_selected_to_noon("NOON-A", ["MPM-1"]);

	assert.deepEqual(items, [
		{ category: "queued", platform: "Noon", text: "NOON-A: pricing queued as a manual run." },
		{ category: "queued", platform: "Noon", text: "NOON-A: stock queued." },
	]);
});

test("Noon stock push reports queued, already-queued and manual runs", async () => {
	const replies = [{ queued: false }, { queued: true, switch_off: true }];
	install_frappe({
		call() {
			return Promise.resolve({ message: replies.shift() });
		},
	});

	assert.deepEqual(await sync.push_selected_noon_stock("NOON-A", ["MPM-1"]), [
		{ category: "skipped", platform: "Noon", text: "NOON-A: stock already queued." },
	]);
	assert.deepEqual(await sync.push_selected_noon_stock("NOON-A", ["MPM-1"]), [
		{ category: "queued", platform: "Noon", text: "NOON-A: stock queued as a manual run." },
	]);
});

test("push_selected_to_talabat calls the Talabat bulk endpoint and returns a queued summary item", async () => {
	let call_options;
	install_frappe({
		call(options) {
			call_options = options;
			return Promise.resolve({ message: { queued: 2, jobs: 1 } });
		},
	});

	const items = await sync.push_selected_to_talabat(["MPM-00001", "MPM-00002"]);

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

	await sync.push_selected_to_shopify(["MPM-00002"]);
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

	const items = await sync.push_selected_to_shopify(["MPM-00002"]);

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

	const items = await sync.push_selected_to_shopify(["MPM-1"]);

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

	const items = await sync.sync_selected_inventory(["MPM-1"]);
	await settle();

	assert.deepEqual(items.map((item) => item.text), ["Queued 1 job(s).", "B: Warehouse missing"]);
	assert.equal(dialog_args.status_method, "inventory.status");
	assert.deepEqual(dialog_args.jobs, [{ account: "A", job_id: "j", mapping_count: 1 }]);
});

test("server_message_text decodes entities once and drops markup", () => {
	const error = {
		responseJSON: { _server_messages: JSON.stringify([JSON.stringify({ message: "A &amp; B <b>bold</b>" })]) },
	};

	assert.equal(sync.server_message_text(error), "A & B bold");
	assert.equal(sync.server_message_text({}), "");
	assert.equal(sync.server_message_text({ responseJSON: { _server_messages: "not json" } }), "");
});
