const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, values) =>
	values ? values.reduce((text, value, index) => text.replace(`{${index}}`, value), message) : message;

// Chainable stand-in for jQuery: every property/call returns another
// chainable, so the render code runs without a DOM.
function chain() {
	return new Proxy(function () {}, {
		get: (_target, prop) => (prop === "then" ? undefined : chain()),
		apply: () => chain(),
	});
}

let dialogs = [];
class FakeDialog {
	constructor(options) {
		this.options = options;
		this.fields_dict = { status_html: { $wrapper: chain() } };
		dialogs.push(this);
	}
	show() {}
}

global.$ = () => chain();
global.frappe = {
	utils: { escape_html: (text) => text },
	ui: { Dialog: FakeDialog },
};
require("./shopify_catalog_push_status.js");
const Status = global.frappe.ui.ShopifyCatalogPushStatus;

const tick = () => new Promise((resolve) => setTimeout(resolve, 5));

function install_call(handler) {
	const calls = [];
	global.frappe.call = (options) => {
		calls.push(options);
		return handler(options, calls.length);
	};
	return calls;
}

test("defaults to the price-push status method and title", async () => {
	dialogs = [];
	const calls = install_call(() => Promise.resolve({ message: { status: "completed" } }));

	new Status({ jobs: [{ account: "A", job_id: "j1" }], poll_interval: 1 });
	await tick();

	assert.equal(dialogs[0].options.title, "Pushing to Shopify");
	assert.equal(calls[0].method, "marketplace_integrations.api.shopify_catalog_push.get_catalog_push_status");
});

test("inventory options change the title and poll the inventory status method", async () => {
	dialogs = [];
	const calls = install_call(() => Promise.resolve({ message: { status: "completed" } }));

	new Status({ jobs: [{ account: "A", job_id: "j1" }], poll_interval: 1, ...Status.INVENTORY_OPTIONS });
	await tick();

	assert.equal(dialogs[0].options.title, "Syncing Shopify inventory");
	assert.equal(calls[0].method, "marketplace_integrations.api.shopify_inventory.get_inventory_sync_status");
	assert.deepEqual(calls[0].args, { job_id: "j1" });
});

test("polling stops for a job once it is terminal, and on_done fires once", async () => {
	dialogs = [];
	let on_done_count = 0;
	const calls = install_call((_options, count) =>
		Promise.resolve({ message: { status: count < 3 ? "running" : "completed" } })
	);

	new Status({ jobs: [{ account: "A", job_id: "j1" }], poll_interval: 1, on_done: () => on_done_count++ });
	await tick();
	await tick();

	assert.equal(calls.length, 3);
	assert.equal(on_done_count, 1);
});

test("a finished job is not polled again while another job is still running", async () => {
	dialogs = [];
	const calls = install_call((options) =>
		Promise.resolve({ message: { status: options.args.job_id === "fast" ? "completed" : "running" } })
	);

	const dialog = new Status({
		jobs: [
			{ account: "A", job_id: "fast" },
			{ account: "B", job_id: "slow" },
		],
		poll_interval: 1,
	});
	await tick();
	await tick();
	dialog.dialog.onhide();

	assert.equal(calls.filter((call) => call.args.job_id === "fast").length, 1);
	assert.ok(calls.filter((call) => call.args.job_id === "slow").length > 1);
});

test("closing the dialog stops polling", async () => {
	dialogs = [];
	const calls = install_call(() => Promise.resolve({ message: { status: "running" } }));

	const dialog = new Status({ jobs: [{ account: "A", job_id: "j1" }], poll_interval: 1 });
	await tick();
	dialog.dialog.onhide();
	const count_at_close = calls.length;
	await tick();
	await tick();

	assert.equal(calls.length, count_at_close);
});

test("transient poll errors are retried; three consecutive errors fail the job with the inventory wording", async () => {
	dialogs = [];
	let on_done_count = 0;
	const calls = install_call((_options, count) =>
		count === 1
			? Promise.reject(new Error("blip"))
			: count === 2
				? Promise.resolve({ message: { status: "running" } })
				: Promise.reject(new Error("down"))
	);

	const dialog = new Status({
		jobs: [{ account: "A", job_id: "j1" }],
		poll_interval: 1,
		on_done: () => on_done_count++,
		...Status.INVENTORY_OPTIONS,
	});
	for (let i = 0; i < 8; i++) await tick();

	// error (retried), success, then 3 consecutive errors -> failed.
	assert.equal(calls.length, 5);
	assert.equal(dialog.job_states.j1.status, "failed");
	assert.equal(dialog.job_states.j1.error, "Could not retrieve the inventory sync status.");
	assert.equal(on_done_count, 1);
});

test("the results table is drawn in its own container, so the totals line is not wiped", async () => {
	dialogs = [];
	install_call(() =>
		Promise.resolve({
			message: {
				status: "completed",
				result: { results: [{ mapping_name: "M1", ok: true }], total: 1, succeeded: 1, failed: 0, skipped_mappings: 0 },
			},
		})
	);
	const status_spy = chain();
	let status_empties = 0;
	const wrapper = new Proxy(status_spy, {
		get: (target, prop) => (prop === "empty" ? () => (status_empties++, wrapper) : target[prop]),
	});
	class SpyDialog extends FakeDialog {
		constructor(options) {
			super(options);
			this.fields_dict = { status_html: { $wrapper: wrapper } };
		}
	}
	global.frappe.ui.Dialog = SpyDialog;
	try {
		const status = new Status({ jobs: [{ account: "A", job_id: "j1" }], poll_interval: 1 });
		await tick();
		// One empty() per render() (the initial one and the one after the poll): never a third from the table.
		assert.equal(status_empties, 2);
		assert.ok(status.finished);
	} finally {
		global.frappe.ui.Dialog = FakeDialog;
	}
});
