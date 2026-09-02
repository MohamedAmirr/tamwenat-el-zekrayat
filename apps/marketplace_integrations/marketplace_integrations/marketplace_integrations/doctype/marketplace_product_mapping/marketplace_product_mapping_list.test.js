const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, values) =>
	values ? message.replace("{0}", values[0]).replace("{1}", values[1]) : message;

const { push_selected_to_talabat } = require("./marketplace_product_mapping_list.js");

test("Push selected to Talabat queues exactly the selected Marketplace Product Mappings", async () => {
	let call_options;
	let refreshed = false;
	let cleared = false;
	global.frappe = {
		call(options) {
			call_options = options;
			return Promise.resolve({ message: { queued: 2, jobs: 1 } });
		},
		show_alert() {},
	};
	const listview = {
		get_checked_items() {
			return ["MPM-00001", "MPM-00002"];
		},
		clear_checked_items() {
			cleared = true;
		},
		refresh() {
			refreshed = true;
		},
	};

	push_selected_to_talabat(listview);
	await new Promise((resolve) => setImmediate(resolve));

	assert.equal(
		call_options.method,
		"marketplace_integrations.api.talabat_catalog.queue_mappings_catalog_sync"
	);
	assert.deepEqual(call_options.args, { mapping_names: ["MPM-00001", "MPM-00002"] });
	assert.equal(cleared, true);
	assert.equal(refreshed, true);
});

test("Push selected to Talabat does nothing without a selection", () => {
	let called = false;
	global.frappe = {
		call() {
			called = true;
		},
	};

	push_selected_to_talabat({ get_checked_items: () => [] });

	assert.equal(called, false);
});
