const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, values) =>
	values ? message.replace("{0}", values[0]).replace("{1}", values[1]) : message;

const {
	push_to_talabat,
	push_to_shopify,
	sync_shopify_inventory,
	sync_noon_mapping,
	add_platform_push_button,
	show_save_notices,
} = require("./marketplace_product_mapping.js");

function save_notice_frappe({ roles = [], platform = "Talabat" } = {}) {
	const alerts = [];
	global.frappe = {
		user: { has_role: (role) => roles.includes(role) },
		show_alert: (args) => alerts.push(args),
		utils: { escape_html: (text) => text },
		db: { get_value: () => Promise.resolve({ message: { platform } }) },
	};
	return alerts;
}

test("saving as an Item Manager shows one green notice about the automatic re-sync", async () => {
	const alerts = save_notice_frappe({ roles: ["Item Manager"] });

	await show_save_notices({ doc: { marketplace_account: "A", external_reference: "r", external_sku: "" } });

	assert.equal(alerts.length, 1);
	assert.equal(alerts[0].indicator, "green");
	assert.match(alerts[0].message, /re-sync this account's failed orders and push stock/);
});

test("saving without the Item Manager role shows no re-sync notice", async () => {
	const alerts = save_notice_frappe({ roles: ["System Manager"] });

	await show_save_notices({ doc: { marketplace_account: "A", external_reference: "r", external_sku: "" } });

	assert.deepEqual(alerts, []);
});

test("saving a Talabat mapping whose SKU differs from the reference shows an orange warning", async () => {
	const alerts = save_notice_frappe({ platform: "Talabat" });

	await show_save_notices({ doc: { marketplace_account: "A", external_reference: "ref-1", external_sku: "sku-1" } });

	assert.equal(alerts.length, 1);
	assert.equal(alerts[0].indicator, "orange");
	assert.match(alerts[0].message, /sku-1 differs from External Reference ref-1/);
});

test("no SKU warning for a matching or blank SKU, or for another platform", async () => {
	for (const [platform, sku] of [["Talabat", "ref-1"], ["Talabat", " "], ["Noon", "sku-1"], ["Shopify", "sku-1"]]) {
		const alerts = save_notice_frappe({ platform });

		await show_save_notices({ doc: { marketplace_account: "A", external_reference: "ref-1", external_sku: sku } });

		assert.deepEqual(alerts, [], `${platform} / ${sku}`);
	}
});

test("Push to Talabat queues only the current Marketplace Product Mapping", async () => {
	let call_options;
	global.frappe = {
		call(options) {
			call_options = options;
			return Promise.resolve();
		},
		show_alert() {},
	};
	const frm = { doc: { name: "MPM-00001" }, reload_doc() {} };

	push_to_talabat(frm);
	await new Promise((resolve) => setImmediate(resolve));

	assert.equal(
		call_options.method,
		"marketplace_integrations.api.talabat_catalog.queue_mapping_catalog_sync"
	);
	assert.deepEqual(call_options.args, { mapping: "MPM-00001" });
});

test("Push to Shopify queues only the current Marketplace Product Mapping and shows the job-status dialog", async () => {
	let call_options;
	let required_asset;
	let dialog_args;
	global.frappe = {
		call(options) {
			call_options = options;
			return Promise.resolve({
				message: { jobs: [{ account: "Some Account", job_id: "job-1", mapping_count: 1 }] },
			});
		},
		require(asset) {
			required_asset = asset;
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
	const frm = { doc: { name: "MPM-00002" }, reload_doc() {} };

	push_to_shopify(frm);
	await new Promise((resolve) => setImmediate(resolve));
	await new Promise((resolve) => setImmediate(resolve));

	assert.equal(
		call_options.method,
		"marketplace_integrations.api.shopify_catalog_push.queue_catalog_push"
	);
	assert.deepEqual(call_options.args, { mapping_names: ["MPM-00002"] });
	assert.equal(required_asset, "/assets/marketplace_integrations/js/shopify_catalog_push_status.js");
	assert.deepEqual(dialog_args.jobs, [{ account: "Some Account", job_id: "job-1", mapping_count: 1 }]);
});

test("add_platform_push_button adds only the Shopify button for a Shopify account", async () => {
	let get_value_args;
	let added_buttons = [];
	global.frappe = {
		model: { can_write: () => true },
		db: {
			get_value(...args) {
				get_value_args = args;
				return Promise.resolve({ message: { platform: "Shopify" } });
			},
		},
	};
	const frm = {
		doc: { marketplace_account: "MKTACC-00001" },
		add_custom_button(label) {
			added_buttons.push(label);
		},
	};

	await add_platform_push_button(frm);

	assert.deepEqual(get_value_args, ["Marketplace Account", "MKTACC-00001", "platform"]);
	assert.deepEqual(added_buttons, ["Push to Shopify", "Sync Shopify Inventory"]);
});

test("add_platform_push_button adds only the Talabat button for a Talabat account", async () => {
	let added_buttons = [];
	global.frappe = {
		model: { can_write: () => true },
		db: {
			get_value() {
				return Promise.resolve({ message: { platform: "Talabat" } });
			},
		},
	};
	const frm = {
		doc: { marketplace_account: "MKTACC-00002" },
		add_custom_button(label) {
			added_buttons.push(label);
		},
	};

	await add_platform_push_button(frm);

	assert.deepEqual(added_buttons, ["Push to Talabat"]);
});

test("add_platform_push_button adds no button and shows no warning for an unrecognized platform", async () => {
	let added_buttons = [];
	let alert_args;
	global.frappe = {
		model: { can_write: () => true },
		db: {
			get_value() {
				return Promise.resolve({ message: { platform: "Amazon" } });
			},
		},
		show_alert(args) {
			alert_args = args;
		},
	};
	const frm = {
		doc: { marketplace_account: "MKTACC-00003" },
		add_custom_button(label) {
			added_buttons.push(label);
		},
	};

	await add_platform_push_button(frm);

	assert.deepEqual(added_buttons, []);
	assert.equal(alert_args, undefined, "an unrecognized/unreadable platform must not raise a misleading warning on form load");
});

test("add_platform_push_button does nothing, not even the platform lookup, without write permission", async () => {
	let lookup_called = false;
	let added_buttons = [];
	global.frappe = {
		model: { can_write: () => false },
		db: {
			get_value() {
				lookup_called = true;
				return Promise.resolve({ message: { platform: "Shopify" } });
			},
		},
	};
	const frm = {
		doc: { marketplace_account: "MKTACC-00004" },
		add_custom_button(label) {
			added_buttons.push(label);
		},
	};

	await add_platform_push_button(frm);

	assert.equal(lookup_called, false);
	assert.deepEqual(added_buttons, []);
});

test("push_to_shopify shows an alert for a skipped account and does not open the dialog when nothing was queued", async () => {
	let alert_args;
	let dialog_opened = false;
	global.frappe = {
		call() {
			return Promise.resolve({
				message: { jobs: [], skipped_accounts: [{ account: "Some Account", reason: "already running" }] },
			});
		},
		show_alert(args) {
			alert_args = args;
		},
		utils: { escape_html: (text) => text },
		require() {
			dialog_opened = true;
			return Promise.resolve();
		},
	};
	const frm = { doc: { name: "MPM-00002" }, reload_doc() {} };

	push_to_shopify(frm);
	await new Promise((resolve) => setImmediate(resolve));

	assert.ok(alert_args, "expected a show_alert call for the skipped account");
	assert.equal(dialog_opened, false);
});

test("push_to_shopify shows the skipped account's real config-error reason, not a hardcoded 'already running' message", async () => {
	let alert_args;
	const reason = "Marketplace Account Some Account requires an ERPNext Selling Price List.";
	global.frappe = {
		call() {
			return Promise.resolve({
				message: { jobs: [], skipped_accounts: [{ account: "Some Account", reason }] },
			});
		},
		show_alert(args) {
			alert_args = args;
		},
		utils: { escape_html: (text) => text },
		require: () => Promise.resolve(),
	};

	push_to_shopify({ doc: { name: "MPM-00002" }, reload_doc() {} });
	await new Promise((resolve) => setImmediate(resolve));

	assert.equal(alert_args.message, reason);
	assert.ok(!/already running/.test(alert_args.message));
});

test("Sync Shopify Inventory queues only the current mapping and opens the dialog with the inventory options", async () => {
	let call_options;
	let required_asset;
	let dialog_args;
	const inventory_options = { status_method: "inventory.status", labels: {} };
	global.frappe = {
		call(options) {
			call_options = options;
			return Promise.resolve({
				message: { jobs: [{ account: "Some Account", job_id: "job-9", mapping_count: 1 }] },
			});
		},
		require(asset) {
			required_asset = asset;
			return Promise.resolve();
		},
		ui: {
			ShopifyCatalogPushStatus: class {
				static INVENTORY_OPTIONS = inventory_options;
				constructor(args) {
					dialog_args = args;
				}
			},
		},
	};

	sync_shopify_inventory({ doc: { name: "MPM-00007" }, reload_doc() {} });
	await new Promise((resolve) => setImmediate(resolve));
	await new Promise((resolve) => setImmediate(resolve));

	assert.equal(call_options.method, "marketplace_integrations.api.shopify_inventory.queue_inventory_sync");
	assert.deepEqual(call_options.args, { mapping_names: ["MPM-00007"] });
	assert.equal(required_asset, "/assets/marketplace_integrations/js/shopify_catalog_push_status.js");
	assert.deepEqual(dialog_args.jobs, [{ account: "Some Account", job_id: "job-9", mapping_count: 1 }]);
	assert.equal(dialog_args.status_method, "inventory.status");
});

test("Sync Shopify Inventory shows the skipped account's real reason (escaped) and opens no dialog", async () => {
	let alert_args;
	let dialog_opened = false;
	const reason = "Marketplace Account X requires a Shopify Location for inventory sync.";
	global.frappe = {
		call() {
			return Promise.resolve({ message: { jobs: [], skipped_accounts: [{ account: "X", reason }] } });
		},
		show_alert(args) {
			alert_args = args;
		},
		utils: { escape_html: (text) => `esc(${text})` },
		require() {
			dialog_opened = true;
			return Promise.resolve();
		},
	};

	sync_shopify_inventory({ doc: { name: "MPM-00007" }, reload_doc() {} });
	await new Promise((resolve) => setImmediate(resolve));

	assert.equal(alert_args.message, `esc(${reason})`);
	assert.equal(dialog_opened, false);
});

test("Sync Noon Pricing and Stock says a manual run was queued when the account switch is off", async () => {
	let msg;
	global.frappe = {
		call() {
			return Promise.resolve({
				message: { pricing: { queued: true, switch_off: true }, stock: { queued: true, switch_off: true } },
			});
		},
		msgprint(message) {
			msg = message;
		},
	};

	sync_noon_mapping({ doc: { name: "MPM-1", marketplace_account: "NOON-A" } });
	await new Promise((resolve) => setImmediate(resolve));

	assert.match(msg, /queued as a manual run/);
});

test("Sync Noon Pricing and Stock uses the plain queued message when the switch is on", async () => {
	let msg;
	global.frappe = {
		call() {
			return Promise.resolve({ message: { pricing: { queued: true, switch_off: false }, stock: { queued: false } } });
		},
		msgprint(message) {
			msg = message;
		},
	};

	sync_noon_mapping({ doc: { name: "MPM-1", marketplace_account: "NOON-A" } });
	await new Promise((resolve) => setImmediate(resolve));

	assert.doesNotMatch(msg, /manual run/);
	assert.match(msg, /queued/);
});
