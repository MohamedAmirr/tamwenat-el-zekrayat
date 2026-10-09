const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

function load_list_script({selected, rejectedIndexes = []}) {
	const calls = [];
	const messages = [];
	let refreshCount = 0;
	let retryHandler;
	const context = {
		frappe: {
			listview_settings: {},
			msgprint: (message) => messages.push(message),
			call: (options) => {
				calls.push(options);
				return rejectedIndexes.includes(calls.length - 1)
					? Promise.reject(new Error("request failed"))
					: Promise.resolve({message: {status: "Success"}});
			},
		},
		__: (message, args) =>
			args ? message.replace(/\{(\d+)\}/g, (_, index) => args[index]) : message,
	};
	vm.runInNewContext(
		fs.readFileSync(`${__dirname}/marketplace_sync_log_list.js`, "utf8"),
		context,
	);
	const listview = {
		page: {add_inner_button: (_label, handler) => (retryHandler = handler)},
		get_checked_items: () => selected,
		refresh: () => refreshCount++,
	};
	context.frappe.listview_settings["Marketplace Sync Log"].onload(listview);
	return {calls, messages, listview, retry: () => retryHandler(), refreshCount: () => refreshCount};
}

test("bulk retry sends resync only for selected Error rows and refreshes the list", async () => {
	const harness = load_list_script({
		selected: [
			{name: "MSL-00001", status: "Error", marketplace_order: "MPO-0001"},
			{name: "MSL-00002", status: "Invalid", marketplace_order: "MPO-0002"},
			{name: "MSL-00003", status: "Error", marketplace_order: "MPO-0003"},
		],
	});

	await harness.retry();

	assert.deepEqual(
		harness.calls.map((call) => call.args.dn),
		["MSL-00001", "MSL-00003"],
	);
	assert.ok(harness.calls.every((call) => call.method === "frappe.client.run_doc_method"));
	assert.ok(harness.calls.every((call) => call.args.method === "resync"));
	assert.deepEqual(harness.messages, ["Retried 2 order(s)."]);
	assert.equal(harness.refreshCount(), 1);
});

test("bulk retry reports rejected requests while still refreshing", async () => {
	const harness = load_list_script({
		selected: [
			{name: "MSL-00004", status: "Error", marketplace_order: "MPO-0004"},
			{name: "MSL-00005", status: "Error", marketplace_order: "MPO-0005"},
		],
		rejectedIndexes: [1],
	});

	await harness.retry();

	assert.deepEqual(harness.messages, ["Retried 2 order(s); 1 request(s) failed."]);
	assert.equal(harness.refreshCount(), 1);
});

test("bulk retry with no selected Error rows shows guidance and makes no requests", async () => {
	const harness = load_list_script({selected: [{name: "MSL-00006", status: "Invalid"}]});

	await harness.retry();

	assert.deepEqual(harness.calls, []);
	assert.deepEqual(harness.messages, ["Select one or more Error rows to retry."]);
	assert.equal(harness.refreshCount(), 0);
});

test("bulk retry excludes Order Import failure rows (no marketplace_order to resync)", async () => {
	const harness = load_list_script({
		selected: [
			{name: "MSL-00007", status: "Error", sync_type: "Order Import", marketplace_order: null},
			{name: "MSL-00008", status: "Error", sync_type: "Sales Order Sync", marketplace_order: "MPO-0001"},
		],
	});

	await harness.retry();

	assert.deepEqual(
		harness.calls.map((call) => call.args.dn),
		["MSL-00008"],
	);
	assert.deepEqual(harness.messages, ["Retried 1 order(s)."]);
	assert.equal(harness.refreshCount(), 1);
});

test("bulk retry with only Order Import failure rows selected shows guidance and makes no requests", async () => {
	const harness = load_list_script({
		selected: [{name: "MSL-00009", status: "Error", sync_type: "Order Import", marketplace_order: null}],
	});

	await harness.retry();

	assert.deepEqual(harness.calls, []);
	assert.deepEqual(harness.messages, ["Select one or more Error rows to retry."]);
	assert.equal(harness.refreshCount(), 0);
});
