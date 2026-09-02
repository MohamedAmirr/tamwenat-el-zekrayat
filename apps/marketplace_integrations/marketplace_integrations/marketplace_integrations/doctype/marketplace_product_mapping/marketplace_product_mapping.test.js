const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message) => message;

const { push_to_talabat } = require("./marketplace_product_mapping.js");

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
