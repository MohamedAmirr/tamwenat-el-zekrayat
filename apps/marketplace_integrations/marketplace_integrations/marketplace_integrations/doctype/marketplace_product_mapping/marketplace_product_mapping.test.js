const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, values) =>
	values ? message.replace("{0}", values[0]).replace("{1}", values[1]) : message;

const {
	push_to_talabat,
	push_to_shopify,
	add_platform_push_button,
} = require("./marketplace_product_mapping.js");

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
	assert.deepEqual(added_buttons, ["Push to Shopify"]);
});

test("add_platform_push_button adds only the Talabat button for a Talabat account", async () => {
	let added_buttons = [];
	global.frappe = {
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

test("add_platform_push_button adds no button and warns for an unrecognized platform", async () => {
	let added_buttons = [];
	let alert_args;
	global.frappe = {
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
	assert.ok(alert_args, "expected a show_alert call for the unrecognized platform");
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
