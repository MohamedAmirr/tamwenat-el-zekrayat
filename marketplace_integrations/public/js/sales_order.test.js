const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

const escape_html = (value) =>
	String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// Loads sales_order.js fresh against a mock frappe. `options` is what
// get_platform_cancel_option returns; `request_result` what request_platform_cancel returns.
function load({
	option,
	request_result = { ok: true, refreshed: true },
	request_fails = false,
	can_read = true,
} = {}) {
	const calls = [];
	const alerts = [];
	const msgprints = [];
	const dialogs = [];
	const handlers = {};
	global.frappe = {
		utils: { escape_html },
		model: { can_read: (doctype) => can_read && doctype === "Marketplace Order" },
		ui: {
			form: {
				on(doctype, hooks) {
					handlers[doctype] = hooks;
				},
			},
			Dialog: class {
				constructor(config) {
					this.config = config;
					this.hidden = false;
					dialogs.push(this);
				}
				show() {
					this.shown = true;
				}
				hide() {
					this.hidden = true;
				}
			},
		},
		call(opts) {
			calls.push(opts);
			if (opts.method.endsWith("get_platform_cancel_option")) {
				return Promise.resolve({ message: option });
			}
			return request_fails
				? Promise.reject(new Error("boom"))
				: Promise.resolve({ message: request_result });
		},
		show_alert: (alert) => alerts.push(alert),
		msgprint: (message) => msgprints.push(message),
	};
	const path = require.resolve("./sales_order.js");
	delete require.cache[path];
	const module_exports = require(path);
	return { calls, alerts, msgprints, dialogs, handlers, module_exports };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 5));

const eligible = (extra = {}) => ({
	state: "eligible",
	platform: "Talabat",
	marketplace_order: "MO-1",
	instruction: "Cancel the order on Talabat so the customer is not left waiting.",
	reasons: [],
	cancellation_reasons: ["CLOSED", "ITEM_UNAVAILABLE", "TOO_BUSY"],
	already_requested: false,
	platform_cancelled: false,
	erp_executed: false,
	...extra,
});

function make_frm(doc, { close_sales_order } = {}) {
	const buttons = [];
	const frm = {
		doc: {
			name: "SAL-ORD-1",
			custom_marketplace_order: "MO-1",
			docstatus: 1,
			status: "To Deliver and Bill",
			...doc,
		},
		cscript: {},
		buttons,
		reloads: 0,
		add_custom_button: (label, action) => buttons.push({ label, action }),
		reload_doc() {
			frm.reloads += 1;
			return Promise.resolve();
		},
	};
	if (close_sales_order) frm.cscript.close_sales_order = close_sales_order;
	return frm;
}

test("registers refresh and after_cancel on the Sales Order form", () => {
	const { handlers } = load();
	assert.equal(typeof handlers["Sales Order"].refresh, "function");
	assert.equal(typeof handlers["Sales Order"].after_cancel, "function");
});

test("the Sales Order form still has close_sales_order in the installed ERPNext", (t) => {
	// Point ERPNEXT_SALES_ORDER_JS at erpnext/selling/doctype/sales_order/sales_order.js
	// (e.g. copied out of the dev container) to notice an ERPNext upgrade removing the
	// method the Close prompt is wrapped around. Skipped by default: the always-on
	// guard is the Python test marketplace_integrations/test_sales_order_js_wiring.py.
	const path = process.env.ERPNEXT_SALES_ORDER_JS;
	if (!path) return t.skip("set ERPNEXT_SALES_ORDER_JS to check the installed ERPNext");
	assert.match(fs.readFileSync(path, "utf8"), /^\s*close_sales_order\(\)\s*\{/m);
});

test("wrapping reports a missing close_sales_order instead of passing silently", () => {
	const { module_exports } = load();
	assert.equal(module_exports.wrap_close_sales_order(make_frm({})), false);
});

test("the Close wrapper sets the flag, calls the original with the cscript and wraps once", () => {
	const { module_exports } = load();
	let received;
	const frm = make_frm(
		{},
		{
			close_sales_order() {
				received = this;
				return "result";
			},
		}
	);
	const original = frm.cscript.close_sales_order;
	assert.equal(module_exports.wrap_close_sales_order(frm), true);
	const wrapper = frm.cscript.close_sales_order;
	assert.notEqual(wrapper, original);
	module_exports.wrap_close_sales_order(frm);
	assert.equal(frm.cscript.close_sales_order, wrapper);
	assert.equal(frm.cscript.close_sales_order(), "result");
	assert.equal(received, frm.cscript);
	assert.equal(frm.__mi_close_requested, "SAL-ORD-1");
});

test("a Close click followed by a Closed refresh consumes the request once", () => {
	const { module_exports } = load();
	const frm = make_frm({ status: "Closed" });
	frm.__mi_close_requested = "SAL-ORD-1";
	assert.equal(module_exports.consume_close_request(frm), true);
	assert.equal(module_exports.consume_close_request(frm), false);
});

test("a refresh that is not Closed clears the request", () => {
	const { module_exports } = load();
	const frm = make_frm({ status: "To Deliver and Bill" });
	frm.__mi_close_requested = "SAL-ORD-1";
	assert.equal(module_exports.consume_close_request(frm), false);
	frm.doc.status = "Closed";
	assert.equal(module_exports.consume_close_request(frm), false);
});

test("opening or reloading an already Closed order never prompts", async () => {
	const { handlers, calls, dialogs, msgprints } = load({ option: eligible() });
	const frm = make_frm({ status: "Closed" }, { close_sales_order() {} });
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(dialogs.length, 0);
	assert.equal(msgprints.length, 0);
	// One fetch, only to decide on the button.
	assert.equal(calls.length, 1);
	assert.deepEqual(
		frm.buttons.map((b) => b.label),
		["Create Bosta Shipment", "Cancel on Talabat"]
	);
});

test("closing via the wrapped method opens the dialog on the next Closed refresh", async () => {
	const { handlers, dialogs } = load({ option: eligible() });
	const frm = make_frm({}, { close_sales_order() {} });
	handlers["Sales Order"].refresh(frm);
	frm.cscript.close_sales_order();
	frm.doc.status = "Closed";
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(dialogs.length, 1);
	assert.equal(dialogs[0].shown, true);
	assert.equal(frm.buttons.length, 2);
});

test("open orders and orders without a marketplace order make no call", async () => {
	const { handlers, calls } = load({ option: eligible() });
	handlers["Sales Order"].refresh(make_frm({}, { close_sales_order() {} }));
	handlers["Sales Order"].refresh(
		make_frm({ status: "Closed", custom_marketplace_order: "" }, { close_sales_order() {} })
	);
	handlers["Sales Order"].refresh(
		make_frm({ status: "Closed", docstatus: 0 }, { close_sales_order() {} })
	);
	handlers["Sales Order"].after_cancel(
		make_frm({ status: "Cancelled", docstatus: 2, custom_marketplace_order: "" })
	);
	await settle();
	assert.equal(calls.length, 0);
});

test("after_cancel prompts for a cancelled order and adds no button itself", async () => {
	const { handlers, dialogs } = load({ option: eligible() });
	const frm = make_frm({ status: "Cancelled", docstatus: 2 });
	handlers["Sales Order"].after_cancel(frm);
	await settle();
	assert.equal(dialogs.length, 1);
	assert.equal(frm.buttons.length, 0);
});

test("not_applicable does nothing", async () => {
	const { handlers, dialogs, msgprints } = load({ option: { state: "not_applicable" } });
	const frm = make_frm({ status: "Cancelled", docstatus: 2 });
	handlers["Sales Order"].after_cancel(frm);
	await settle();
	assert.equal(dialogs.length + msgprints.length + frm.buttons.length, 0);
});

for (const flag of ["already_requested", "platform_cancelled", "erp_executed"]) {
	test(`${flag} shows no dialog and no button`, async () => {
		const { handlers, dialogs, msgprints } = load({
			option: eligible({ [flag]: true }),
		});
		const frm = make_frm({ status: "Cancelled", docstatus: 2 });
		handlers["Sales Order"].after_cancel(frm);
		handlers["Sales Order"].refresh(frm);
		await settle();
		assert.equal(dialogs.length + msgprints.length + frm.buttons.length, 0);
	});
}

test("a blocked option shows an escaped info dialog and never calls the platform", async () => {
	const { handlers, msgprints, calls, dialogs } = load({
		option: eligible({
			state: "blocked",
			instruction: "Handle it <b>in</b> Noon",
			reasons: ["Status <script>x</script>"],
			cancellation_reasons: [],
		}),
	});
	const frm = make_frm({ status: "Cancelled", docstatus: 2 });
	handlers["Sales Order"].after_cancel(frm);
	await settle();
	assert.equal(dialogs.length, 0);
	assert.equal(msgprints.length, 1);
	assert.equal(
		msgprints[0].message,
		"<p>Handle it &lt;b&gt;in&lt;/b&gt; Noon</p><ul><li>Status &lt;script&gt;x&lt;/script&gt;</li></ul>"
	);
	assert.equal(calls.length, 1);
});

test("a blocked order gets no Cancel button", async () => {
	const { handlers } = load({ option: eligible({ state: "blocked", reasons: ["x"] }) });
	const frm = make_frm({ status: "Closed" });
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(frm.buttons.length, 1);
});

test("the dialog requires a reason built from the server list", async () => {
	const { handlers, dialogs } = load({ option: eligible() });
	handlers["Sales Order"].after_cancel(make_frm({ status: "Cancelled", docstatus: 2 }));
	await settle();
	const field = dialogs[0].config.fields.find((f) => f.fieldname === "cancellation_reason");
	assert.equal(field.reqd, 1);
	assert.equal(field.fieldtype, "Select");
	assert.equal(field.options, "CLOSED\nITEM_UNAVAILABLE\nTOO_BUSY");
	assert.equal(dialogs[0].config.primary_action_label, "Cancel on Talabat");
	assert.equal(dialogs[0].config.secondary_action_label, "Not now");
});

test("no reason field when the platform has no reasons", async () => {
	const { handlers, dialogs } = load({ option: eligible({ cancellation_reasons: [] }) });
	handlers["Sales Order"].after_cancel(make_frm({ status: "Cancelled", docstatus: 2 }));
	await settle();
	assert.equal(
		dialogs[0].config.fields.some((f) => f.fieldname === "cancellation_reason"),
		false
	);
});

test("Not now calls nothing; Confirm calls request_platform_cancel then reloads", async () => {
	const { handlers, dialogs, calls, alerts } = load({ option: eligible() });
	const frm = make_frm({ status: "Cancelled", docstatus: 2 });
	handlers["Sales Order"].after_cancel(frm);
	await settle();
	dialogs[0].config.secondary_action.call(dialogs[0]);
	assert.equal(calls.length, 1);
	assert.equal(dialogs[0].hidden, true);

	dialogs[0].config.primary_action.call(dialogs[0], { cancellation_reason: "TOO_BUSY" });
	await settle();
	const request = calls[1];
	assert.ok(request.method.endsWith("request_platform_cancel"));
	assert.deepEqual(request.args, { sales_order: "SAL-ORD-1", cancellation_reason: "TOO_BUSY" });
	assert.equal(request.freeze, true);
	assert.equal(alerts[0].message, "Cancellation requested.");
	assert.equal(frm.reloads, 1);
});

test("result alerts distinguish already requested and not refreshed", () => {
	const { module_exports } = load();
	assert.equal(
		module_exports.result_alert({ already_requested: true }),
		"Cancellation was already requested."
	);
	assert.match(module_exports.result_alert({ refreshed: false }), /next webhook or refresh/);
});

test("a server error does not reload or alert", async () => {
	const { handlers, dialogs, alerts } = load({ option: eligible(), request_fails: true });
	const frm = make_frm({ status: "Cancelled", docstatus: 2 });
	handlers["Sales Order"].after_cancel(frm);
	await settle();
	dialogs[0].config.primary_action.call(dialogs[0], { cancellation_reason: "CLOSED" });
	await settle();
	assert.equal(alerts.length, 0);
	assert.equal(frm.reloads, 0);
});

test("the button opens the same confirmation dialog", async () => {
	const { handlers, dialogs } = load({ option: eligible() });
	const frm = make_frm({ status: "Cancelled", docstatus: 2 });
	handlers["Sales Order"].refresh(frm);
	await settle();
	frm.buttons[0].action();
	assert.equal(dialogs.length, 1);
});

test("a stale Close flag from another order does not prompt an already Closed order", async () => {
	const { handlers, dialogs, msgprints } = load({ option: eligible() });
	const frm = make_frm({ status: "To Deliver and Bill" }, { close_sales_order() {} });
	handlers["Sales Order"].refresh(frm);
	frm.cscript.close_sales_order(); // order A, Close failed: flag stays set
	frm.doc.name = "SAL-ORD-2"; // the form moves to order B, already Closed
	frm.doc.status = "Closed";
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(dialogs.length + msgprints.length, 0);
	assert.equal(frm.__mi_close_requested, null);
});

test("closing the same order prompts exactly once", async () => {
	const { handlers, dialogs } = load({ option: eligible() });
	const frm = make_frm({}, { close_sales_order() {} });
	handlers["Sales Order"].refresh(frm);
	frm.cscript.close_sales_order();
	frm.doc.status = "Closed";
	handlers["Sales Order"].refresh(frm);
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(dialogs.length, 1);
});

test("a response for a different document than the form shows is ignored", async () => {
	const { handlers, dialogs, msgprints } = load({ option: eligible() });
	const frm = make_frm({ status: "Cancelled", docstatus: 2 });
	handlers["Sales Order"].after_cancel(frm);
	frm.doc.name = "SAL-ORD-2";
	await settle();
	assert.equal(dialogs.length + msgprints.length + frm.buttons.length, 0);
});

test("without read permission on Marketplace Order nothing is fetched or shown", async () => {
	const { handlers, calls, dialogs, msgprints } = load({ option: eligible(), can_read: false });
	const closed = make_frm({}, { close_sales_order() {} });
	handlers["Sales Order"].refresh(closed);
	closed.cscript.close_sales_order();
	closed.doc.status = "Closed";
	handlers["Sales Order"].refresh(closed);
	const cancelled = make_frm({ status: "Cancelled", docstatus: 2 });
	handlers["Sales Order"].refresh(cancelled);
	handlers["Sales Order"].after_cancel(cancelled);
	await settle();
	assert.equal(calls.length, 0);
	assert.equal(dialogs.length + msgprints.length, 0);
	assert.equal(closed.buttons.length + cancelled.buttons.length, 1);
});

test("the platform name is escaped in titles and the button label", async () => {
	const { handlers, dialogs } = load({ option: eligible({ platform: "<i>X</i>" }) });
	const frm = make_frm({ status: "Cancelled", docstatus: 2 });
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(frm.buttons[0].label, "Cancel on &lt;i&gt;X&lt;/i&gt;");
	frm.buttons[0].action();
	assert.equal(dialogs[0].config.title, "Cancel on &lt;i&gt;X&lt;/i&gt;");

	const blocked = load({ option: eligible({ platform: "<i>X</i>", state: "blocked" }) });
	blocked.handlers["Sales Order"].after_cancel(make_frm({ status: "Cancelled", docstatus: 2 }));
	await settle();
	assert.equal(blocked.msgprints[0].title, "Cancel on &lt;i&gt;X&lt;/i&gt;");
});
