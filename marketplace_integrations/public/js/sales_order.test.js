const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

const escape_html = (value) =>
	String(value).replace(/[&<>"'`=]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#x60;", "=": "&#x3D;" })[c]);

// Loads sales_order.js fresh against a mock frappe. `options` is what
// get_platform_cancel_option returns; `request_result` what request_platform_cancel returns.
function load({
	option,
	request_result = { ok: true, refreshed: true },
	request_fails = false,
	can_read = true,
	bosta_creator = true,
	bosta_read = true,
	shipment = null,
	shipment_fails = false,
} = {}) {
	const calls = [];
	const bosta_calls = [];
	const alerts = [];
	const msgprints = [];
	const dialogs = [];
	const handlers = {};
	global.frappe = {
		user: { has_role: () => bosta_creator },
		utils: { escape_html },
		model: {
			can_read: (doctype) =>
				doctype === "Bosta Shipment" ? bosta_read : can_read && doctype === "Marketplace Order",
		},
		ui: {
			form: {
				on(doctype, hooks) {
					// Like Frappe, clear custom buttons before every refresh.
					handlers[doctype] = {
						...hooks,
						refresh(frm) {
							if (frm.buttons) frm.buttons.length = 0;
							if (frm.indicators) frm.indicators.length = 0;
							return hooks.refresh(frm);
						},
					};
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
			if (opts.method.endsWith("get_sales_order_shipment")) {
				bosta_calls.push(opts);
				return shipment_fails
					? Promise.reject(new Error("boom"))
					: Promise.resolve({ message: shipment });
			}
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
	return { calls, bosta_calls, alerts, msgprints, dialogs, handlers, module_exports };
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
	const indicators = [];
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
		indicators,
		dashboard: { add_indicator: (label, color) => indicators.push({ label, color }) },
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
	// Button order is intentionally unspecified: Create is added after an async reply.
	assert.deepEqual(
		frm.buttons.map((b) => b.label).sort(),
		["Cancel on Talabat", "Create Bosta Shipment"]
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

test("the Bosta dialog shows the COD to send, with a note when it is 0 or reduced by payments", () => {
	const { module_exports } = load({ option: eligible() });
	global.format_currency = (value) => `EGP ${value}`;
	const paid = module_exports.bosta_cod_note_html({ cod_amount: 0, order_total: 500, advance_paid: 500 });
	assert.match(paid, /COD to be sent to Bosta: EGP 0/);
	assert.match(paid, /This amount is 0/);
	assert.match(paid, /Order total EGP 500, less payments already received EGP 500/);

	const unpaid = module_exports.bosta_cod_note_html({ cod_amount: 500, order_total: 500, advance_paid: 0 });
	assert.match(unpaid, /EGP 500/);
	assert.doesNotMatch(unpaid, /This amount is 0|less payments/);
});

test("the Bosta button is creator-only and survives a reload refresh", async () => {
	const { handlers } = load({ option: eligible({ state: "not_applicable" }), bosta_creator: false });
	const denied = make_frm({});
	handlers["Sales Order"].refresh(denied);
	await settle();
	assert.ok(!denied.buttons.some((b) => b.label === "Create Bosta Shipment"));

	const { handlers: allowed_handlers } = load({ option: eligible({ state: "not_applicable" }) });
	const frm = make_frm({});
	allowed_handlers["Sales Order"].refresh(frm);
	allowed_handlers["Sales Order"].refresh(frm);
	await settle();
	assert.deepEqual(frm.buttons.map((b) => b.label), ["Create Bosta Shipment"]);
});

// ---- Bosta shipment status, tracking and label on the Sales Order (#246) ----

const shipment_summary = (extra = {}) => ({
	name: "BOSTA-SHP-00001",
	status: "Completed",
	tracking_number: "77873113",
	bosta_state: "Delivered",
	active: true,
	...extra,
});
const NO_PLATFORM = eligible({ state: "not_applicable" });
const labels = (frm) => frm.buttons.map((b) => b.label);

test("an active shipment shows status, tracking and a link, with a label button and no Create", async () => {
	const { handlers, bosta_calls } = load({ option: NO_PLATFORM, shipment: shipment_summary() });
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.deepEqual(bosta_calls.map((c) => c.args), [{ sales_order: "SAL-ORD-1" }]);
	assert.equal(frm.indicators.length, 1);
	assert.equal(frm.indicators[0].color, "green");
	assert.match(frm.indicators[0].label, /<a href="\/app\/bosta-shipment\/BOSTA-SHP-00001">Bosta Shipment: Completed<\/a>/);
	assert.match(frm.indicators[0].label, /Tracking: 77873113 \(Delivered\)/);
	assert.deepEqual(labels(frm), ["Print Bosta A4 Label"]);
});

test("indicator colour follows the shipment status", async () => {
	const expected = {
		"Pending Bosta": "orange",
		"Bosta Created": "blue",
		"Delivery Note Failed": "red",
		Completed: "green",
		"Bosta Failed": "red",
	};
	for (const [status, color] of Object.entries(expected)) {
		const { handlers } = load({ option: NO_PLATFORM, shipment: shipment_summary({ status }) });
		const frm = make_frm({});
		handlers["Sales Order"].refresh(frm);
		await settle();
		assert.equal(frm.indicators[0].color, color, status);
		assert.match(frm.indicators[0].label, new RegExp(`Bosta Shipment: ${status}</a>`));
	}
});

test("a shipment without a tracking number shows the status but no label button", async () => {
	const { handlers } = load({
		option: NO_PLATFORM,
		shipment: shipment_summary({ status: "Pending Bosta", tracking_number: "", bosta_state: "" }),
	});
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(frm.indicators.length, 1);
	assert.doesNotMatch(frm.indicators[0].label, /Tracking/);
	assert.deepEqual(labels(frm), []);
});

function click_label(frm) {
	frm.buttons.find((b) => b.label === "Print Bosta A4 Label").action();
}

// Loads a form with an active shipment, a window stub that records what happens to it, and a
// label call that is answered by `answer` (a function returning a promise).
async function label_setup({ window_blocked = false, answer }) {
	const { handlers, calls, msgprints } = load({ option: NO_PLATFORM, shipment: shipment_summary() });
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	const win = { opener: "set", closed: false, location: { href: "" }, close() { this.closed = true; } };
	const opens = [];
	global.window = {
		open: (...args) => {
			opens.push(args);
			return window_blocked ? null : win;
		},
	};
	const original = global.frappe.call;
	global.frappe.call = (opts) => {
		calls.push(opts);
		return answer(opts);
	};
	return { frm, win, opens, calls, msgprints, restore: () => (global.frappe.call = original) };
}

test("Print label opens the window synchronously, then points it at that shipment's label", async () => {
	let resolve;
	const s = await label_setup({ answer: () => new Promise((r) => (resolve = r)) });
	click_label(s.frm);
	// Before the server answers, a blank window already exists and cannot reach back to us.
	assert.deepEqual(s.opens, [["", "_blank"]]);
	assert.equal(s.win.opener, null);
	assert.equal(s.win.location.href, "");
	resolve({ message: { file_url: "/private/files/bosta-awb.pdf" } });
	await settle();
	const request = s.calls.at(-1);
	assert.equal(request.method, "marketplace_integrations.api.bosta_shipments.print_a4_arabic_label");
	assert.deepEqual(request.args, { shipment: "BOSTA-SHP-00001" });
	assert.equal(s.win.location.href, "/private/files/bosta-awb.pdf");
	assert.equal(s.win.closed, false);
	s.restore();
});

test("Print label closes the blank window when the call fails or returns no file", async () => {
	for (const answer of [() => Promise.reject(new Error("boom")), () => Promise.resolve({ message: {} })]) {
		const s = await label_setup({ answer });
		click_label(s.frm);
		await settle();
		assert.equal(s.win.closed, true);
		assert.equal(s.win.location.href, "");
		s.restore();
	}
});

test("Print label falls back to an escaped link when the browser blocks the window", async () => {
	const s = await label_setup({
		window_blocked: true,
		answer: () => Promise.resolve({ message: { file_url: '/private/files/a"><img src=x>.pdf' } }),
	});
	click_label(s.frm);
	await settle();
	assert.equal(s.msgprints.length, 1);
	assert.doesNotMatch(s.msgprints[0].message, /<img/);
	assert.match(s.msgprints[0].message, /href="\/private\/files\/a&quot;&gt;&lt;img src&#x3D;x&gt;\.pdf"/);
	s.restore();
});

test("a Bosta Failed shipment without tracking keeps Create next to the indicator", async () => {
	const { handlers } = load({
		option: NO_PLATFORM,
		shipment: shipment_summary({ status: "Bosta Failed", tracking_number: "", bosta_state: "" }),
	});
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(frm.indicators.length, 1);
	assert.equal(frm.indicators[0].color, "red");
	assert.deepEqual(labels(frm), ["Create Bosta Shipment"]);

	// A read-only user gets the indicator but no Create.
	const { handlers: h2 } = load({
		option: NO_PLATFORM,
		bosta_creator: false,
		shipment: shipment_summary({ status: "Bosta Failed", tracking_number: "" }),
	});
	const reader = make_frm({});
	h2["Sales Order"].refresh(reader);
	await settle();
	assert.equal(reader.indicators.length, 1);
	assert.deepEqual(labels(reader), []);
});

test("no other active status offers Create", async () => {
	const cases = [
		{ status: "Bosta Failed", tracking_number: "77873113" },
		{ status: "Pending Bosta", tracking_number: "" },
		{ status: "Bosta Created", tracking_number: "77873113" },
		{ status: "Delivery Note Failed", tracking_number: "77873113" },
		{ status: "Completed", tracking_number: "77873113" },
	];
	for (const extra of cases) {
		const { handlers } = load({ option: NO_PLATFORM, shipment: shipment_summary(extra) });
		const frm = make_frm({});
		handlers["Sales Order"].refresh(frm);
		await settle();
		assert.equal(frm.indicators.length, 1, extra.status);
		assert.ok(!labels(frm).includes("Create Bosta Shipment"), `${extra.status} ${extra.tracking_number}`);
	}
});

test("a Canceled latest shipment (or none) shows Create again, with no indicator", async () => {
	for (const shipment of [shipment_summary({ status: "Canceled", active: false }), null]) {
		const { handlers } = load({ option: NO_PLATFORM, shipment });
		const frm = make_frm({});
		handlers["Sales Order"].refresh(frm);
		await settle();
		assert.deepEqual(labels(frm), ["Create Bosta Shipment"]);
		assert.equal(frm.indicators.length, 0);
	}
});

test("a read-only user sees the status and label but never Create", async () => {
	const { handlers } = load({ option: NO_PLATFORM, bosta_creator: false, shipment: shipment_summary() });
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(frm.indicators.length, 1);
	assert.deepEqual(labels(frm), ["Print Bosta A4 Label"]);

	const { handlers: h2 } = load({
		option: NO_PLATFORM,
		bosta_creator: false,
		shipment: shipment_summary({ status: "Canceled", active: false }),
	});
	const canceled = make_frm({});
	h2["Sales Order"].refresh(canceled);
	await settle();
	assert.deepEqual(labels(canceled), []);
});

test("without read permission on Bosta Shipment nothing is requested and no details show", async () => {
	const { handlers, bosta_calls } = load({
		option: NO_PLATFORM,
		bosta_read: false,
		bosta_creator: false,
		shipment: shipment_summary(),
	});
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(bosta_calls.length, 0);
	assert.equal(frm.indicators.length, 0);
	assert.deepEqual(labels(frm), []);
});

test("the server returning nothing (no permission) shows no details either", async () => {
	const { handlers } = load({ option: NO_PLATFORM, bosta_creator: false, shipment: null });
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(frm.indicators.length, 0);
	assert.deepEqual(labels(frm), []);
});

test("remote text in the shipment is escaped in the indicator", async () => {
	const { handlers } = load({
		option: NO_PLATFORM,
		shipment: shipment_summary({
			name: 'BOSTA-SHP-"1',
			tracking_number: "<img src=x onerror=alert(1)>",
			bosta_state: "<script>x</script>",
			status: "Completed",
		}),
	});
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	const html = frm.indicators[0].label;
	assert.doesNotMatch(html, /<img|<script/);
	assert.match(html, /&lt;img src&#x3D;x onerror&#x3D;alert\(1\)&gt;/);
	assert.match(html, /&lt;script&gt;/);
	assert.match(html, /href="\/app\/bosta-shipment\/BOSTA-SHP-%221"/);
});

test("a reload refresh re-adds the indicator and buttons once, not twice", async () => {
	const { handlers } = load({ option: NO_PLATFORM, shipment: shipment_summary() });
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	handlers["Sales Order"].refresh(frm); // like frm.reload_doc(): everything is cleared first
	await settle();
	assert.equal(frm.indicators.length, 1);
	assert.deepEqual(labels(frm), ["Print Bosta A4 Label"]);
});

test("a slow reply that lands after a newer refresh is dropped", async () => {
	const { handlers } = load({ option: NO_PLATFORM, shipment: shipment_summary() });
	const replies = [];
	global.frappe.call = (opts) => {
		if (!opts.method.endsWith("get_sales_order_shipment")) return Promise.resolve({ message: null });
		return new Promise((resolve) => replies.push(resolve));
	};
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	handlers["Sales Order"].refresh(frm);
	replies.forEach((resolve) => resolve({ message: shipment_summary() }));
	await settle();
	assert.equal(frm.indicators.length, 1);
	assert.deepEqual(labels(frm), ["Print Bosta A4 Label"]);
});

test("a failed shipment read leaves Create available to creators and shows no indicator", async () => {
	const { handlers } = load({ option: NO_PLATFORM, shipment_fails: true });
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(frm.indicators.length, 0);
	assert.deepEqual(labels(frm), ["Create Bosta Shipment"]);
});

test("Draft and Cancelled-status orders get no Create button; drafts are not asked about shipments", async () => {
	const { handlers, bosta_calls } = load({ option: NO_PLATFORM });
	const draft = make_frm({ docstatus: 0 });
	handlers["Sales Order"].refresh(draft);
	await settle();
	assert.equal(bosta_calls.length, 0);
	assert.deepEqual(labels(draft), []);

	const cancelled = make_frm({ status: "Cancelled", docstatus: 2 });
	handlers["Sales Order"].refresh(cancelled);
	await settle();
	assert.deepEqual(labels(cancelled), []);
});

// ---- Bosta create dialog: city / district dropdowns and address prefill (#245) ----

const CITIES = [
	{ id: "c-cairo", name: "Cairo", name_ar: "القاهرة" },
	{ id: "c-alex", name: "Alexandria", name_ar: "" },
];
const DISTRICTS = {
	"c-cairo": [
		{ id: "d-1", name: "Maadi", name_ar: "المعادي", zone: "Z1" },
		{ id: "d-2", name: "Maadi", name_ar: "المعادي", zone: "Z2" },
	],
	"c-alex": [{ id: "d-9", name: "Smouha", name_ar: "", zone: "Z" }],
};

// Opens the Bosta dialog against scripted server answers. `server` maps a method suffix to
// a function(args) returning the answer or throwing to simulate a failing call.
async function open_bosta_dialog(defaults, server = {}) {
	const { module_exports } = load({ option: eligible({ state: "not_applicable" }) });
	const calls = [];
	const dialogs = [];
	const answers = {
		get_cities: () => CITIES,
		get_districts: (args) => DISTRICTS[args.city_id] || [],
		create_shipment: () => ({}),
		...server,
	};
	global.format_currency = (value) => `EGP ${value}`;
	global.frappe.call = (opts) => {
		calls.push(opts);
		const name = opts.method.split(".").pop();
		try {
			return Promise.resolve(answers[name](opts.args)).then((message) => ({ message }));
		} catch (error) {
			return Promise.reject(error);
		}
	};
	global.frappe.ui.Dialog = class {
		constructor(config) {
			this.config = config;
			this.values = {};
			this.df = {};
			this.data = {};
			this.primary = { disabled: false };
			this.fields_dict = {};
			config.fields.forEach((df) => {
				this.df[df.fieldname] = { ...df };
				this.values[df.fieldname] = df.default || "";
				this.fields_dict[df.fieldname] = { set_data: (data) => (this.data[df.fieldname] = data) };
			});
			dialogs.push(this);
		}
		show() {}
		hide() {
			this.hidden = true;
		}
		get_primary_btn() {
			return { prop: (name, value) => (this.primary[name] = value) };
		}
		set_df_property(field, prop, value) {
			this.df[field][prop] = value;
		}
		get_value(field) {
			return this.values[field];
		}
		set_value(field, value) {
			const changed = this.values[field] !== value;
			this.values[field] = value;
			return changed && this.df[field].change ? Promise.resolve(this.df[field].change()) : Promise.resolve();
		}
	};
	const frm = make_frm({});
	await module_exports.show_bosta_dialog(frm, defaults);
	await settle();
	return { dialog: dialogs[0], calls, frm, module_exports };
}

test("the dialog lists Bosta cities by name (Arabic too) with the Bosta ID as the value", async () => {
	const { dialog, calls } = await open_bosta_dialog({});
	assert.deepEqual(dialog.data.city, [
		{ value: "c-cairo", label: "Cairo - القاهرة" },
		{ value: "c-alex", label: "Alexandria" },
	]);
	assert.equal(dialog.df.city.fieldtype, "Autocomplete");
	assert.equal(calls.filter((c) => c.method.endsWith("get_cities")).length, 1);
	assert.equal(dialog.primary.disabled, false);
	// The old free-text ID fields are gone.
	["district_id", "city_id", "district_name"].forEach((name) => assert.equal(dialog.df[name], undefined));
});

test("choosing a city loads only its districts and changing the city clears the district", async () => {
	const { dialog, calls } = await open_bosta_dialog({});
	await dialog.set_value("city", "c-cairo");
	await settle();
	assert.deepEqual(
		dialog.data.district.map((d) => d.value),
		["d-1", "d-2"]
	);
	// Identical names are told apart by zone.
	assert.deepEqual(
		dialog.data.district.map((d) => d.label),
		["Maadi - المعادي (Z1)", "Maadi - المعادي (Z2)"]
	);
	await dialog.set_value("district", "d-1");
	await dialog.set_value("city", "c-alex");
	await settle();
	assert.equal(dialog.values.district, "");
	assert.deepEqual(dialog.data.district, [{ value: "d-9", label: "Smouha" }]);
	assert.deepEqual(
		calls.filter((c) => c.method.endsWith("get_districts")).map((c) => c.args.city_id),
		["c-cairo", "c-alex"]
	);
});

test("a late district answer for a previous city is ignored", async () => {
	let release;
	const slow = new Promise((resolve) => (release = resolve));
	const { dialog } = await open_bosta_dialog(
		{},
		{
			get_districts: (args) => (args.city_id === "c-cairo" ? slow : DISTRICTS[args.city_id]),
		}
	);
	// The scripted server returns a promise as "message" for the slow city.
	await dialog.set_value("city", "c-cairo");
	await dialog.set_value("city", "c-alex");
	await settle();
	release([{ id: "late", name: "Late" }]);
	await settle();
	assert.deepEqual(dialog.data.district, [{ value: "d-9", label: "Smouha" }]);
});

test("remote names cannot inject markup, and an ampersand stays readable", async () => {
	const { module_exports } = load({ option: eligible() });
	const options = module_exports.location_options([
		{ id: "x", name: "<img src=x onerror=1>", name_ar: "" },
		{ id: "bosta-id-7", name: "Agmey & Amreya", name_ar: "" },
	]);
	assert.ok(!/[<>]/.test(options[0].label));
	assert.equal(options[1].label, "Agmey & Amreya");
	assert.equal(options[1].value, "bosta-id-7"); // the stored value stays the Bosta ID
});

test("creating sends the district ID, the city NAME and the shown COD", async () => {
	const { dialog, calls, frm } = await open_bosta_dialog({ cod_amount: 300 });
	await dialog.set_value("city", "c-cairo");
	await settle();
	Object.assign(dialog.values, {
		receiver_name: "Ahmed",
		receiver_phone: "0100",
		district: "d-1",
		address_first_line: "12 Example Street",
	});
	dialog.config.primary_action(dialog.values);
	await settle();
	const create = calls.find((c) => c.method.endsWith("create_shipment"));
	assert.deepEqual(create.args, {
		sales_order: "SAL-ORD-1",
		receiver_name: "Ahmed",
		receiver_phone: "0100",
		city: "Cairo",
		district_id: "d-1",
		address_first_line: "12 Example Street",
		expected_cod: 300,
	});
	assert.equal(frm.reloads, 1);
});

test("when Bosta cannot be reached the dialog shows an error, blocks submit, and Retry recovers", async () => {
	let healthy = false;
	const { dialog, calls } = await open_bosta_dialog(
		{},
		{
			get_cities: () => {
				if (!healthy) throw new Error("down");
				return CITIES;
			},
		}
	);
	assert.equal(dialog.primary.disabled, true);
	assert.equal(dialog.df.lists_error.hidden, 0);
	assert.match(dialog.df.lists_error.options, /Could not load the Bosta cities/);
	assert.equal(dialog.df.retry_lists.hidden, 0);
	assert.deepEqual(dialog.data.city, undefined); // no fallback to typing IDs

	healthy = true;
	await dialog.df.retry_lists.click();
	assert.equal(dialog.primary.disabled, false);
	assert.equal(dialog.df.lists_error.hidden, 1);
	assert.equal(calls.filter((c) => c.method.endsWith("get_cities")).length, 2);
});

test("a district load failure also blocks submit until retried", async () => {
	let healthy = false;
	const { dialog } = await open_bosta_dialog(
		{},
		{
			get_districts: (args) => {
				if (!healthy) throw new Error("down");
				return DISTRICTS[args.city_id];
			},
		}
	);
	await dialog.set_value("city", "c-cairo");
	await settle();
	assert.equal(dialog.primary.disabled, true);
	assert.match(dialog.df.lists_error.options, /Bosta districts/);
	healthy = true;
	await dialog.df.retry_lists.click();
	await settle();
	assert.equal(dialog.primary.disabled, false);
});

test("Refresh Bosta Lists asks the server to refresh and keeps the chosen city", async () => {
	const { dialog, calls } = await open_bosta_dialog({});
	await dialog.set_value("city", "c-cairo");
	await settle();
	assert.equal(dialog.config.secondary_action_label, "Refresh Bosta Lists");
	await dialog.config.secondary_action();
	await settle();
	const refreshes = calls.filter((c) => c.args && c.args.refresh === 1).map((c) => c.method.split(".").pop());
	assert.deepEqual(refreshes, ["get_cities", "get_districts"]);
	assert.equal(dialog.values.city, "c-cairo");
});

test("a Bosta city matching the shipping address city is preselected; the district never is", async () => {
	const { dialog } = await open_bosta_dialog({
		address_first_line: "12 Example Street, Floor 3",
		address_city: "  cAiRo ",
	});
	assert.equal(dialog.values.city, "c-cairo");
	assert.equal(dialog.values.district, "");
	assert.equal(dialog.df.address_first_line.default, "12 Example Street, Floor 3");
	assert.equal(dialog.data.district.length, 2); // listed for the choice, none chosen
});

test("a non-matching, partial or empty source city leaves the City dropdown empty", async () => {
	for (const address_city of ["Giza", "Cai", "", undefined]) {
		const { dialog } = await open_bosta_dialog({ address_city });
		assert.equal(dialog.values.city, "", `city ${address_city}`);
		assert.equal(dialog.values.district, "");
	}
});

test("without any address source the address fields stay blank and editable", async () => {
	const { dialog } = await open_bosta_dialog({ receiver_name: "Ahmed", receiver_phone: "0100" });
	assert.equal(dialog.values.address_first_line, "");
	assert.equal(dialog.df.address_first_line.read_only, undefined);
	assert.equal(dialog.values.receiver_name, "Ahmed");
});
