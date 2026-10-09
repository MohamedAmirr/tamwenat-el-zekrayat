const assert = require("node:assert/strict");
const test = require("node:test");

// Realtime listeners and polling responses are driven by each test directly.
function install_frappe({ status_response }) {
	const listeners = {};
	const off_calls = [];
	global.frappe = {
		provide() {},
		ui: {},
		realtime: {
			on(event, handler) {
				listeners[event] = handler;
			},
			off(event, handler) {
				off_calls.push({ event, handler });
			},
		},
		call() {
			return status_response();
		},
	};
	// The helper attaches itself to the global frappe.ui when it loads, so each
	// test loads it fresh against its own mock.
	const path = require.resolve("./shopify_catalog_sweep_watch.js");
	delete require.cache[path];
	require(path);
	return { listeners, off_calls };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 5));

test("a completed sweep reports its result once and stops listening", async () => {
	const { off_calls } = install_frappe({
		status_response: () =>
			Promise.resolve({ message: { status: "completed", result: { exceptions: [1] } } }),
	});
	let completed;
	frappe.ui.watchShopifyCatalogSweep({
		job_id: "job-1",
		is_current: () => true,
		on_progress() {},
		on_complete: (result) => {
			completed = result;
		},
		on_failed() {},
		on_error() {},
	});
	await tick();

	assert.deepEqual(completed, { exceptions: [1] });
	assert.equal(off_calls.length, 1);
	assert.equal(off_calls[0].event, "marketplace_shopify_catalog_sweep_progress");
});

test("a watch whose sweep is no longer current stops without calling any callback", async () => {
	const { off_calls } = install_frappe({
		status_response: () => Promise.resolve({ message: { status: "running", processed: 3 } }),
	});
	const calls = [];
	frappe.ui.watchShopifyCatalogSweep({
		job_id: "job-2",
		is_current: () => false,
		on_progress: () => calls.push("progress"),
		on_complete: () => calls.push("complete"),
		on_failed: () => calls.push("failed"),
		on_error: () => calls.push("error"),
	});
	await tick();

	assert.deepEqual(calls, []);
	assert.equal(off_calls.length, 1);
});

test("a failed sweep reports the job's error and stops", async () => {
	const { off_calls } = install_frappe({
		status_response: () =>
			Promise.resolve({
				message: { status: "failed", error: "Shopify rejected the query" },
			}),
	});
	let failed;
	frappe.ui.watchShopifyCatalogSweep({
		job_id: "job-3",
		is_current: () => true,
		on_progress() {},
		on_complete() {},
		on_failed: (error) => {
			failed = error;
		},
		on_error() {},
	});
	await tick();

	assert.equal(failed, "Shopify rejected the query");
	assert.equal(off_calls.length, 1);
});
