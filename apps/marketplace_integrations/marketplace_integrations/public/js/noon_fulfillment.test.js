const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

// Node has no DOMParser: a stand-in that returns the text of the markup.
global.DOMParser = class {
	parseFromString(html) {
		return { body: { textContent: html.replace(/<[^>]*>/g, "") } };
	}
};

const escape_html = (value) =>
	String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// Loads noon_fulfillment.js fresh against a mock frappe. After #415/#416 the Sales Order keeps the order
// invoice and the stuck-reservation recovery as buttons; Mark as Out of Stock is the item row form button.
// `script(opts)` answers frappe.call: return a value (resolved as {message}) or throw {rejected: error}.
function load(script = () => ({ queued: false })) {
	const calls = [];
	const dialogs = [];
	const alerts = [];
	const msgprints = [];
	const confirms = [];
	global.frappe = {
		utils: { escape_html },
		model: { can_write: () => true },
		ui: {
			Dialog: class {
				constructor(config) {
					this.config = config;
					this.hidden = false;
					this.html = "";
					this.fields_dict = { body: { $wrapper: { html: (value) => (this.html = value) } } };
					dialogs.push(this);
				}
				show() {}
				hide() {
					this.hidden = true;
				}
			},
		},
		call(opts) {
			calls.push(opts);
			try {
				return Promise.resolve({ message: script(opts) });
			} catch (error) {
				return Promise.reject(error.rejected);
			}
		},
		msgprint: (message) => msgprints.push(message),
		confirm: (message, proceed) => confirms.push({ message, proceed }),
		show_alert: (alert) => alerts.push(alert),
	};
	// the shared invoice dialog is its own doctype_js file (hooks.py), loaded with the form script
	const invoice_path = require.resolve("./noon_invoice.js");
	delete require.cache[invoice_path];
	require(invoice_path);
	const path = require.resolve("./noon_fulfillment.js");
	delete require.cache[path];
	const module_exports = require(path);
	module_exports.timing.poll_ms = 1;
	module_exports.timing.max_wait_ms = 50;
	return { calls, dialogs, alerts, msgprints, confirms, module_exports };
}

async function settle() {
	for (let i = 0; i < 8; i++) {
		await new Promise((resolve) => setTimeout(resolve, 3));
	}
}

const ctx = () => ({ marketplace_order: "MO-NOON", sales_order: "SO-1", reload() {} });
const make_frm = () => ({
	doc: { name: "SO-1", docstatus: 1 },
	reloaded: 0,
	reload_doc() {
		this.reloaded += 1;
	},
});
const API = "marketplace_integrations.api.noon_fulfillment.";
const by_method = (calls, name) => calls.filter((call) => call.method === API + name);

// A script: the press is queued, the poll answers each state of `states` in turn (the last one repeats).
function press_script(states) {
	let polls = 0;
	return (opts) => {
		if (opts.method === API + "mark_item_out_of_stock") return { queued: true, request_id: "req-1" };
		if (opts.method === API + "check_mark_out_of_stock") return states[Math.min(polls++, states.length - 1)];
		return {};
	};
}

test("only the order-level buttons exist (Mark as Out of Stock is the row button)", () => {
	const { module_exports } = load();
	assert.deepEqual(
		module_exports.noon_fulfillment_buttons(ctx()).map((button) => button.action),
		["upload_order_invoice", "retry_order_invoice", "release_stuck_oos_reservation"]
	);
	for (const name of ["noon_unit_checklist_options", "mp_item_nrs_from_unit_indices", "show_noon_mark_out_of_stock_dialog"]) {
		assert.equal(module_exports[name], undefined, name);
	}
});

test("the row button shows only for rows the server offered and asks for confirmation first", () => {
	const { module_exports, confirms, calls } = load();
	const shown = [];
	const frm = {
		...make_frm(),
		__mi_noon_oos_rows: ["row-1"],
		fields_dict: {
			items: {
				grid: {
					grid_rows_by_docname: {
						"row-1": { toggle_display: (field, show) => shown.push(["row-1", field, show]) },
						"row-2": { toggle_display: (field, show) => shown.push(["row-2", field, show]) },
					},
				},
			},
		},
	};
	module_exports.on_noon_row_render(frm, "Sales Order Item", "row-1");
	module_exports.on_noon_row_render(frm, "Sales Order Item", "row-2");
	assert.deepEqual(shown, [
		["row-1", "custom_noon_mark_out_of_stock", true],
		["row-2", "custom_noon_mark_out_of_stock", false],
	]);
	module_exports.on_noon_row_button(frm, "Sales Order Item", "row-2");
	assert.equal(confirms.length, 0);
	module_exports.on_noon_row_button(frm, "Sales Order Item", "row-1");
	assert.equal(confirms.length, 1);
	assert.match(confirms[0].message, /whole order: every row of this Item is removed/);
	assert.match(confirms[0].message, /final at Noon/);
	assert.match(confirms[0].message, /cancellation fees/);
	assert.equal(calls.length, 0, "nothing is sent before the user confirms");
});

test("success: waits with Sending to Noon..., polls, then a small alert and the form reloads", async () => {
	const { module_exports, calls, dialogs, alerts } = load(press_script([{ state: "pending" }, { state: "done" }]));
	const frm = make_frm();
	module_exports.send_noon_out_of_stock(frm, "row-1");
	assert.equal(dialogs[0].config.title, "Sending to Noon...");
	await settle();
	assert.deepEqual(by_method(calls, "mark_item_out_of_stock")[0].args, { sales_order: "SO-1", sales_order_item: "row-1" });
	assert.deepEqual(by_method(calls, "check_mark_out_of_stock")[0].args, { sales_order: "SO-1", request_id: "req-1" });
	assert.equal(by_method(calls, "check_mark_out_of_stock").length, 2);
	assert.equal(dialogs[0].hidden, true);
	assert.equal(alerts[0].indicator, "green");
	assert.equal(frm.reloaded, 1);
});

test("a server refusal (the row is in a Delivery Note) shows Noon-free text escaped and never polls", async () => {
	const messages = JSON.stringify([JSON.stringify({ message: "Item <b>A&B</b> is in Delivery Note <b>DN-1</b>." })]);
	const { module_exports, calls, msgprints, dialogs } = load((opts) => {
		throw { rejected: { responseJSON: { _server_messages: messages } } };
	});
	module_exports.send_noon_out_of_stock(make_frm(), "row-1");
	await settle();
	assert.equal(by_method(calls, "check_mark_out_of_stock").length, 0);
	assert.equal(dialogs[0].hidden, true);
	assert.equal(msgprints[0].message, "Item A&amp;B is in Delivery Note DN-1.");
});

test("a definite failure shows Noon's message escaped with Retry; the row stays; Retry sends again", async () => {
	const { module_exports, calls, msgprints } = load(
		press_script([{ state: "failed", message: "<script>x</script> no stock" }])
	);
	const frm = make_frm();
	module_exports.send_noon_out_of_stock(frm, "row-1");
	await settle();
	assert.equal(frm.reloaded, 0);
	assert.match(msgprints[0].message, /&lt;script&gt;x&lt;\/script&gt; no stock/);
	assert.doesNotMatch(msgprints[0].message, /<script>/);
	assert.equal(msgprints[0].primary_action.label, "Retry");
	msgprints[0].primary_action.action();
	await settle();
	assert.equal(by_method(calls, "mark_item_out_of_stock").length, 2);
});

test("an unknown result offers Check Noon, which presses again (the server asks Noon first)", async () => {
	const { module_exports, calls, msgprints } = load(press_script([{ state: "unknown", message: "timeout" }]));
	module_exports.send_noon_out_of_stock(make_frm(), "row-1");
	await settle();
	assert.match(msgprints[0].message, /not known whether Noon marked the item Out of Stock/);
	assert.equal(msgprints[0].primary_action.label, "Check Noon");
	msgprints[0].primary_action.action();
	await settle();
	assert.equal(by_method(calls, "mark_item_out_of_stock").length, 2);
});

test("Noon confirmed but the local step failed: says so and Retry only repeats the update", async () => {
	const { module_exports, msgprints } = load(press_script([{ state: "local_failed", message: "Row refused" }]));
	module_exports.send_noon_out_of_stock(make_frm(), "row-1");
	await settle();
	assert.match(msgprints[0].message, /Noon marked the item Out of Stock, but the Sales Order was not updated/);
	assert.match(msgprints[0].message, /Row refused/);
	assert.equal(msgprints[0].primary_action.label, "Retry");
});

test("a press that was deduplicated says an action is already running and does not poll", async () => {
	const { module_exports, calls, alerts } = load(() => ({ queued: false }));
	module_exports.send_noon_out_of_stock(make_frm(), "row-1");
	await settle();
	assert.equal(by_method(calls, "check_mark_out_of_stock").length, 0);
	assert.equal(alerts[0].message, "A Noon fulfillment action for this order is already queued or running.");
});

test("a wait that outlasts the limit tells the user the job keeps running", async () => {
	const { module_exports, alerts } = load(press_script([{ state: "pending" }]));
	module_exports.send_noon_out_of_stock(make_frm(), "row-1");
	await new Promise((resolve) => setTimeout(resolve, 120));
	assert.match(alerts[0].message, /Still sending to Noon/);
});

test("the order invoice: a queued answer that was deduplicated says the action is already queued", async () => {
	const { module_exports, dialogs, alerts, calls } = load(() => ({ queued: false }));
	module_exports.show_noon_upload_order_invoice_dialog(ctx());
	dialogs[dialogs.length - 1].config.primary_action({
		invoice_file: "/private/files/i.pdf",
		invoice_nr: "INV-1",
		issued_at: "2026-11-01",
	});
	await settle();
	assert.equal(alerts[0].message, "A Noon fulfillment action for this order is already queued or running.");
	assert.deepEqual(by_method(calls, "upload_order_invoice")[0].args, {
		sales_order: "SO-1",
		file_url: "/private/files/i.pdf",
		invoice_nr: "INV-1",
		issued_at: "2026-11-01",
	});
});

test("Retry Order Invoice passes only the Sales Order and waits for the same press", async () => {
	const { module_exports, calls, dialogs } = load((opts) => {
		if (opts.method === API + "retry_order_invoice") return { queued: true, idempotency_key: "key-1" };
		if (opts.method === API + "check_invoice_upload") return { state: "done" };
		return {};
	});
	module_exports.run_noon_retry_order_invoice(ctx());
	await settle();
	assert.deepEqual(by_method(calls, "retry_order_invoice")[0].args, { sales_order: "SO-1" });
	assert.deepEqual(by_method(calls, "check_invoice_upload")[0].args, {
		document_type: "Sales Order",
		document_name: "SO-1",
		idempotency_key: "key-1",
	});
	assert.equal(dialogs[0].hidden, true);
});

test("the Sales Order lists the offered rows with write on the Sales Order only", async () => {
	const { module_exports, calls } = load(() => ["row-1"]);
	const frm = make_frm();
	assert.deepEqual(await module_exports.add_noon_out_of_stock_rows(frm), ["row-1"]);
	assert.equal(calls[0].method, API + "get_out_of_stock_rows");
	assert.deepEqual(frm.__mi_noon_oos_rows, ["row-1"]);
	global.frappe.model.can_write = () => false;
	assert.deepEqual(await module_exports.add_noon_out_of_stock_rows(frm), []);
	assert.equal(calls.length, 1);
});

test("the retired shipment helpers are not exported", () => {
	const { module_exports } = load();
	for (const name of [
		"noon_shipments_html",
		"noon_handover_alert",
		"show_noon_create_shipment_dialog",
		"show_noon_cancel_shipment_dialog",
		"show_noon_mark_handed_over_dialog",
	]) {
		assert.equal(module_exports[name], undefined, name);
	}
});
