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
	defaults_fails = false,
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
		datetime: { str_to_user: (value) => `user:${value}` },
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
			if (opts.method.endsWith("get_create_defaults")) {
				return defaults_fails ? Promise.reject(new Error("ineligible")) : Promise.resolve({ message: {} });
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
	const tab = { html: "", handler: null, visible: undefined, body_visible: undefined };
	const frm = {
		tab,
		toggle_display: (field, visible) => {
			if (field === "custom_bosta_delivery_tab") tab.visible = visible;
			if (field === "custom_bosta_delivery_html") tab.body_visible = visible;
		},
		get_field: (name) =>
			name === "custom_bosta_delivery_html"
				? {
						$wrapper: {
							html(value) {
								tab.html = value;
								return this;
							},
							off() {
								return this;
							},
							on(_event, _selector, handler) {
								tab.handler = handler;
								return this;
							},
						},
				  }
				: null,
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
	assert.deepEqual(frm.buttons.map((b) => b.label), ["Cancel on Talabat"]);
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
	assert.equal(frm.buttons.length, 1);
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
	assert.equal(frm.buttons.length, 0);
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
	assert.equal(closed.buttons.length + cancelled.buttons.length, 0);
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
	assert.deepEqual(labels(denied), []);

	const { handlers: allowed_handlers } = load({ option: eligible({ state: "not_applicable" }) });
	const frm = make_frm({});
	allowed_handlers["Sales Order"].refresh(frm);
	allowed_handlers["Sales Order"].refresh(frm);
	await settle();
	assert.deepEqual(labels(frm), ["create"]);
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
// The Bosta tab's buttons, as their data-bosta-action names.
const labels = (frm) => [...frm.tab.html.matchAll(/data-bosta-action="([^"]+)"/g)].map((m) => m[1]);
const click = (frm, action) =>
	frm.tab.handler({ currentTarget: { getAttribute: () => action } });

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
	assert.deepEqual(labels(frm), ["print", "refresh", "cancel"]);
	assert.equal(frm.tab.visible, true);
	assert.equal(frm.tab.body_visible, true);
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
	click(frm, "print");
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
	assert.deepEqual(labels(frm), ["create"]);

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
		assert.ok(!labels(frm).includes("create"), `${extra.status} ${extra.tracking_number}`);
	}
});

test("a Canceled latest shipment (or none) shows Create again, with no indicator", async () => {
	for (const shipment of [shipment_summary({ status: "Canceled", active: false }), null]) {
		const { handlers } = load({ option: NO_PLATFORM, shipment });
		const frm = make_frm({});
		handlers["Sales Order"].refresh(frm);
		await settle();
		assert.deepEqual(labels(frm), ["create"]);
		assert.equal(frm.indicators.length, 0);
	}
});

test("a read-only user sees the status and label but never Create", async () => {
	const { handlers } = load({ option: NO_PLATFORM, bosta_creator: false, shipment: shipment_summary() });
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(frm.indicators.length, 1);
	assert.deepEqual(labels(frm), ["print"]);

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

// ---- Bosta Delivery tab (#271) ----

const detailed = (extra = {}) =>
	shipment_summary({
		last_update: "2026-10-16 10:30:00.000000",
		receiver_name: "Mona",
		receiver_phone: "0100",
		city: "Cairo",
		city_id: "c-cairo",
		address_first_line: "12 Example Street",
		cod_amount: 250,
		delivery_note: "MAT-DN-1",
		delivery_note_status: "To Bill",
		...extra,
	});

async function tab_for(shipment_extra, options = {}) {
	global.format_currency = (value) => `EGP ${value}`;
	const { handlers, calls } = load({ option: NO_PLATFORM, shipment: detailed(shipment_extra), ...options });
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	return { frm, calls, html: frm.tab.html };
}

test("the tab shows every listed value and no district", async () => {
	const { html } = await tab_for();
	for (const expected of [
		"Completed",
		"77873113",
		"Delivered",
		"user:2026-10-16 10:30:00.000000",
		"EGP 250",
		"Mona - 0100",
		"12 Example Street, Cairo",
		'href="/app/delivery-note/MAT-DN-1"',
		"MAT-DN-1</a> (To Bill)",
	]) {
		assert.ok(html.includes(expected), expected);
	}
	assert.doesNotMatch(html, /District/);
});

test("multiple Bosta shipments are shown as collapsible history with separate handover and stock states", async () => {
	global.format_currency = (value) => `EGP ${value}`;
	const previous = detailed({ name: "BOSTA-OLD", tracking_number: "TRK-OLD", status: "Canceled", active: false, handover_status: "Review Required", stock_posting_status: "Review Required" });
	const current = detailed({ name: "BOSTA-CURRENT", tracking_number: "TRK-CURRENT", handover_status: "Awaiting Handover", stock_posting_status: "Not Posted", allocations: [{ item_code: "ITEM-1", qty: 2, uom: "Nos", warehouse: "Main - TEZ", stock_qty: 2 }] });
	const { handlers, module_exports } = load({ option: NO_PLATFORM, shipment: { ...current, shipments: [current, previous] } });
	assert.doesNotThrow(() => module_exports.bosta_tab_html({ ...current, shipments: [current, previous] }, { create: true }));
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.match(frm.tab.html, /Bosta shipments: 2/);
	assert.match(frm.tab.html, /BOSTA-CURRENT/);
	assert.match(frm.tab.html, /BOSTA-OLD/);
	assert.match(frm.tab.html, /Awaiting Handover/);
	assert.match(frm.tab.html, /Not Posted/);
	assert.match(frm.tab.html, /Main - TEZ/);
	assert.deepEqual(labels(frm), ["print:BOSTA-CURRENT", "refresh:BOSTA-CURRENT", "cancel:BOSTA-CURRENT", "handover:BOSTA-CURRENT"]);
});

test("a Delivery Note Failed shipment shows its escaped error in red above the table", async () => {
	const { html } = await tab_for({ status: "Delivery Note Failed", delivery_note_error: "<b>no stock" });
	assert.match(html, /^<p class="text-danger">&lt;b&gt;no stock<\/p><table/);
	const ok = await tab_for({ status: "Bosta Created", delivery_note_error: "old" });
	assert.doesNotMatch(ok.html, /text-danger/);
});

test("a zero COD is shown as 0, and the Delivery Note status is only shown when given", async () => {
	const { html } = await tab_for({ cod_amount: 0, delivery_note_status: "" });
	assert.match(html, /COD Sent<\/th><td>EGP 0<\/td>/);
	assert.doesNotMatch(html, /\(To Bill\)/);
	assert.match(html, /MAT-DN-1<\/a><\/td>/);
});

test("a Bosta delivery fee is shown in EGP only when stored", async () => {
	const { html } = await tab_for({ shipment_fee: 87 });
	assert.match(html, /Bosta Delivery Fee<\/th><td>EGP 87<\/td>/);
	const without_fee = await tab_for({ shipment_fee: null });
	assert.doesNotMatch(without_fee.html, /Bosta Delivery Fee/);
});

test("stored values render as text in the tab", async () => {
	const evil = "<img src=x onerror=alert(1)>";
	const { html } = await tab_for({
		receiver_name: evil,
		receiver_phone: evil,
		city: evil,
		city_id: evil,
		address_first_line: evil,
		tracking_number: evil,
		bosta_state: evil,
		delivery_note: evil,
		delivery_note_status: evil,
	});
	assert.doesNotMatch(html, /<img/);
	assert.match(html, /&lt;img src&#x3D;x onerror&#x3D;alert\(1\)&gt;/);
});

test("with no shipment the tab says so and offers Create to creators only", async () => {
	const { handlers } = load({ option: NO_PLATFORM, shipment: null });
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.match(frm.tab.html, /No Bosta shipment/);
	assert.deepEqual(labels(frm), ["create"]);
	assert.equal(frm.tab.visible, true);
	assert.equal(frm.tab.body_visible, true);

	const { handlers: h2 } = load({ option: NO_PLATFORM, shipment: null, bosta_creator: false });
	const reader = make_frm({});
	h2["Sales Order"].refresh(reader);
	await settle();
	assert.equal(reader.tab.visible, false);
	assert.equal(reader.tab.body_visible, false);
	assert.deepEqual(labels(reader), []);
});

test("an ineligible order without a shipment hides the Bosta tab", async () => {
	const { handlers } = load({ option: NO_PLATFORM, shipment: null, defaults_fails: true });
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(frm.tab.visible, false);
	assert.equal(frm.tab.body_visible, false);
	assert.deepEqual(labels(frm), []);
});

test("without read permission the tab stays empty and nothing is requested", async () => {
	const { handlers, bosta_calls } = load({ option: NO_PLATFORM, bosta_read: false, shipment: detailed() });
	const frm = make_frm({});
	frm.tab.html = "stale";
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(bosta_calls.length, 0);
	assert.equal(frm.tab.html, "");
});

test("a failed read says so instead of 'No Bosta shipment'", async () => {
	const { handlers } = load({ option: NO_PLATFORM, shipment_fails: true });
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.match(frm.tab.html, /Could not load the Bosta shipment/);
	assert.doesNotMatch(frm.tab.html, /No Bosta shipment/);
	assert.deepEqual(labels(frm), ["create"]);
});

test("Refresh from Bosta is creator-only, needs a tracking number and calls the server then reloads", async () => {
	const { frm } = await tab_for();
	assert.deepEqual(labels(frm), ["print", "refresh", "cancel"]);
	const refreshes = [];
	global.frappe.call = (opts) => {
		refreshes.push(opts);
		return Promise.resolve({ message: { result: "applied" } });
	};
	global.frappe.show_alert = () => {};
	click(frm, "refresh");
	await settle();
	assert.equal(refreshes.length, 1);
	assert.equal(refreshes[0].method, "marketplace_integrations.api.bosta_shipments.refresh_shipment");
	assert.deepEqual(refreshes[0].args, { shipment: "BOSTA-SHP-00001" });
	assert.equal(frm.reloads, 1);

	const pending = await tab_for({ status: "Pending Bosta", tracking_number: "" });
	assert.deepEqual(labels(pending.frm), []);
	const reader = await tab_for({}, { bosta_creator: false });
	assert.deepEqual(labels(reader.frm), ["print"]);
});

test("a Bosta error on refresh does not reload the form", async () => {
	const { frm } = await tab_for();
	global.frappe.call = () => Promise.reject(new Error("Bosta request failed"));
	click(frm, "refresh");
	await settle();
	assert.equal(frm.reloads, 0);
});

// ---- Cancel Bosta Shipment (#272) ----

function with_confirm() {
	const confirms = [];
	global.frappe.confirm = (message, on_yes) => confirms.push({ message, on_yes });
	return confirms;
}

test("Cancel is hidden for non-creators, Canceled, no tracking number and Delivered", async () => {
	const cases = [
		[{}, { bosta_creator: false }, false],
		[{ status: "Canceled", active: false }, {}, false],
		[{ status: "Pending Bosta", tracking_number: "" }, {}, false],
		[{ bosta_state_code: 45 }, {}, false],
		[{ bosta_state_code: 24 }, {}, true],
		[{ bosta_state_code: 0 }, {}, true],
		[{ bosta_state_code: 47 }, {}, true],
	];
	for (const [extra, options, shown] of cases) {
		const { frm } = await tab_for(extra, options);
		assert.equal(labels(frm).includes("cancel"), shown, JSON.stringify([extra, options]));
	}
});

test("Cancel sends nothing until the confirm is accepted, and the confirm text is escaped", async () => {
	const { frm } = await tab_for({ tracking_number: "<b>T</b>", delivery_note: "<i>DN</i>" });
	const confirms = with_confirm();
	const sent = [];
	global.frappe.call = (opts) => {
		sent.push(opts);
		return Promise.resolve({ message: { canceled: true, delivery_note_error: "" } });
	};
	global.frappe.show_alert = () => {};
	click(frm, "cancel");
	assert.equal(sent.length, 0);
	assert.equal(confirms.length, 1);
	assert.doesNotMatch(confirms[0].message, /<b>|<i>/);
	assert.match(confirms[0].message, /&lt;b&gt;T&lt;\/b&gt;/);
	assert.equal(frm.reloads, 0);

	confirms[0].on_yes();
	await settle();
	assert.equal(sent.length, 1);
	assert.equal(sent[0].method, "marketplace_integrations.api.bosta_shipments.cancel_shipment");
	assert.deepEqual(sent[0].args, { shipment: "BOSTA-SHP-00001" });
	assert.equal(frm.reloads, 1);
});

test("a Delivery Note failure after Bosta accepted the cancel is explained and the form reloads", async () => {
	const { frm } = await tab_for();
	const confirms = with_confirm();
	const printed = [];
	global.frappe.msgprint = (m) => printed.push(m);
	global.frappe.call = () => Promise.resolve({ message: { canceled: false, delivery_note_error: "x" } });
	click(frm, "cancel");
	confirms[0].on_yes();
	await settle();
	assert.equal(printed.length, 1);
	assert.match(printed[0].message, /Bosta termination was confirmed/);
	assert.match(printed[0].message, /No posted stock was reversed/);
	assert.equal(frm.reloads, 1);
});

test("a server error on cancel does not reload the form", async () => {
	const { frm } = await tab_for();
	const confirms = with_confirm();
	global.frappe.call = () => Promise.reject(new Error("Bosta request failed"));
	click(frm, "cancel");
	confirms[0].on_yes();
	await settle();
	assert.equal(frm.reloads, 0);
});

test("Create in the tab fetches the defaults and opens the dialog", async () => {
	const { handlers } = load({ option: NO_PLATFORM, shipment: null });
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	const seen = [];
	global.frappe.call = (opts) => {
		seen.push(opts.method.split(".").pop());
		return new Promise(() => {});
	};
	click(frm, "create");
	await settle();
	assert.equal(seen[0], "get_create_defaults");
});

test("a reload refresh re-adds the indicator and buttons once, not twice", async () => {
	const { handlers } = load({ option: NO_PLATFORM, shipment: shipment_summary() });
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	handlers["Sales Order"].refresh(frm); // like frm.reload_doc(): everything is cleared first
	await settle();
	assert.equal(frm.indicators.length, 1);
	assert.deepEqual(labels(frm), ["print", "refresh", "cancel"]);
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
	assert.deepEqual(labels(frm), ["print", "refresh", "cancel"]);
});

test("a failed shipment read leaves Create available to creators and shows no indicator", async () => {
	const { handlers } = load({ option: NO_PLATFORM, shipment_fails: true });
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(frm.indicators.length, 0);
	assert.deepEqual(labels(frm), ["create"]);
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

// ---- Bosta create dialog: city picker and address prefill (#245, #318) ----

const CITIES = [
	{ id: "c-cairo", name: "Cairo", name_ar: "القاهرة" },
	{ id: "c-alex", name: "Alexandria", name_ar: "" },
];

// Opens the Bosta dialog against scripted server answers. `server` maps a method suffix to
// a function(args) returning the answer or throwing to simulate a failing call.
async function open_bosta_dialog(defaults, server = {}, open = (m, frm) => m.show_bosta_dialog(frm, defaults)) {
	const { module_exports } = load({ option: eligible({ state: "not_applicable" }) });
	const calls = [];
	const dialogs = [];
	const answers = {
		get_cities: () => CITIES,
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
	await open(module_exports, frm);
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
	// No district field exists: Bosta resolves the district from the city and the address (#318).
	["district", "district_id", "district_name", "city_id"].forEach((name) => assert.equal(dialog.df[name], undefined));
});

test("choosing a city only sets the city: no district picker and no other server call", async () => {
	const { dialog, calls } = await open_bosta_dialog({});
	await dialog.set_value("city", "c-cairo");
	await settle();
	assert.equal(dialog.values.city, "c-cairo");
	assert.equal(dialog.data.district, undefined);
	assert.deepEqual(
		calls.map((c) => c.method.split(".").pop()),
		["get_cities"]
	);
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

test("creating sends the city ID, the receiver, the address and the entered COD, and no district", async () => {
	const { dialog, calls, frm } = await open_bosta_dialog({ cod_amount: 300 });
	await dialog.set_value("city", "c-cairo");
	await settle();
	Object.assign(dialog.values, {
		cod_amount: 125.75,
		receiver_name: "Ahmed",
		receiver_phone: "0100",
		address_first_line: "12 Example Street",
	});
	dialog.config.primary_action(dialog.values);
	await settle();
	const create = calls.find((c) => c.method.endsWith("create_shipment"));
	assert.deepEqual(create.args, {
		sales_order: "SAL-ORD-1",
		receiver_name: "Ahmed",
		receiver_phone: "0100",
		city_id: "c-cairo",
		address_first_line: "12 Example Street",
		expected_cod: 300,
		cod_amount: 125.75,
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

test("Refresh Bosta Lists asks the server to refresh the cities and keeps the chosen city", async () => {
	const { dialog, calls } = await open_bosta_dialog({});
	await dialog.set_value("city", "c-cairo");
	await settle();
	assert.equal(dialog.config.secondary_action_label, "Refresh Bosta Lists");
	await dialog.config.secondary_action();
	await settle();
	const refreshes = calls.filter((c) => c.args && c.args.refresh === 1).map((c) => c.method.split(".").pop());
	assert.deepEqual(refreshes, ["get_cities"]);
	assert.equal(dialog.values.city, "c-cairo");
});

test("a Bosta city matching the shipping address city is preselected", async () => {
	const { dialog } = await open_bosta_dialog({
		address_first_line: "12 Example Street, Floor 3",
		address_city: "  cAiRo ",
	});
	assert.equal(dialog.values.city, "c-cairo");
	assert.equal(dialog.df.address_first_line.default, "12 Example Street, Floor 3");
});

test("a non-matching, partial or empty source city leaves the City dropdown empty", async () => {
	for (const address_city of ["Giza", "Cai", "", undefined]) {
		const { dialog } = await open_bosta_dialog({ address_city });
		assert.equal(dialog.values.city, "", `city ${address_city}`);
	}
});

test("a resolved city ID opens the dialog with that city selected", async () => {
	const { dialog } = await open_bosta_dialog({ city_id: "c-alex", city_name: "Alexandria", address_city: "Cairo" });
	assert.equal(dialog.values.city, "c-alex");
});

test("a city ID the Bosta list no longer has is not preselected", async () => {
	const { dialog } = await open_bosta_dialog({ city_id: "c-gone", city_name: "Gone", address_city: "" });
	assert.equal(dialog.values.city, "");
});

test("without any address source the address fields stay blank and editable", async () => {
	const { dialog } = await open_bosta_dialog({ receiver_name: "Ahmed", receiver_phone: "0100" });
	assert.equal(dialog.values.address_first_line, "");
	assert.equal(dialog.df.address_first_line.read_only, undefined);
	assert.equal(dialog.values.receiver_name, "Ahmed");
});

// ---- Edit Bosta Shipment (#273) ----

const editable = (extra = {}) =>
	detailed({ bosta_state_code: 24, city: "Cairo", city_id: "c-cairo", receiver_phone: "0100", ...extra });

const open_edit = (shipment, server) =>
	open_bosta_dialog({}, server, (m, frm) => m.show_bosta_edit_dialog(frm, shipment));

test("Edit is offered only to creators, for an active shipment in a documented editable state", async () => {
	for (const code of [10, 11, 20, 21, 22, 24, 30, 41, 47]) {
		const { frm } = await tab_for({ bosta_state_code: code });
		assert.ok(labels(frm).includes("edit"), `state ${code}`);
	}
	for (const code of [0, 40, 45, 46, 48, 49, 60, 100, undefined]) {
		const { frm } = await tab_for({ bosta_state_code: code });
		assert.ok(!labels(frm).includes("edit"), `state ${code}`);
	}
	const reader = await tab_for({ bosta_state_code: 24 }, { bosta_creator: false });
	assert.ok(!labels(reader.frm).includes("edit"));
	const canceled = await tab_for({ bosta_state_code: 24, status: "Canceled", active: false });
	assert.ok(!labels(canceled.frm).includes("edit"));
	const untracked = await tab_for({ bosta_state_code: 24, tracking_number: "", status: "Pending Bosta" });
	assert.ok(!labels(untracked.frm).includes("edit"));
});

test("the Edit button opens the edit dialog", async () => {
	const { frm } = await tab_for({ bosta_state_code: 24 });
	const dialogs = [];
	global.frappe.ui.Dialog = class {
		constructor(config) {
			this.config = config;
			dialogs.push(this);
		}
		show() {}
		get_primary_btn() {
			return { prop() {} };
		}
		set_df_property() {}
		get_value() {}
		set_value() {}
	};
	global.frappe.call = () => new Promise(() => {});
	click(frm, "edit");
	assert.equal(dialogs[0].config.title, "Edit Bosta Shipment");
});

test("the edit dialog has only the receiver phone, the city picker and the address, prefilled from the shipment", async () => {
	const { dialog } = await open_edit(editable());
	assert.deepEqual(
		dialog.config.fields.map((f) => f.fieldname),
		["edit_note", "lists_error", "retry_lists", "receiver_phone", "city", "address_first_line"]
	);
	assert.equal(dialog.values.receiver_phone, "0100");
	assert.equal(dialog.values.address_first_line, "12 Example Street");
	assert.match(dialog.config.fields[0].options, /updates Bosta and the Bosta Shipment only/);
	assert.match(dialog.config.fields[0].options, /Sales Order address and Shopify are not changed/);
});

test("the edit dialog preselects the shipment's city by its Bosta ID and does not guess a stale one", async () => {
	const { dialog } = await open_edit(editable({ city: "Alexandria", city_id: "c-alex" }));
	assert.equal(dialog.values.city, "c-alex");
	// A stored city ID that Bosta no longer lists, with a name that matches nothing, is not guessed.
	const stale = await open_edit(editable({ city: "Giza", city_id: "c-gone" }));
	assert.equal(stale.dialog.values.city, "");
});

test("the edit dialog sends nothing until the confirm is accepted, then sends only phone, city ID and address", async () => {
	const { dialog, calls, frm } = await open_edit(editable(), { edit_shipment: () => ({}) });
	const confirms = with_confirm();
	global.frappe.show_alert = () => {};
	const before = calls.length;
	dialog.config.primary_action({
		receiver_phone: "0111",
		city: "c-alex",
		address_first_line: "7 New Street",
	});
	assert.equal(calls.length, before);
	assert.equal(confirms.length, 1);
	assert.match(confirms[0].message, /Sales Order address and Shopify are not changed/);
	assert.equal(dialog.hidden, undefined);

	confirms[0].on_yes();
	await settle();
	const request = calls.at(-1);
	assert.equal(request.method, "marketplace_integrations.api.bosta_shipments.edit_shipment");
	assert.deepEqual(request.args, {
		shipment: "BOSTA-SHP-00001",
		receiver_phone: "0111",
		city_id: "c-alex",
		address_first_line: "7 New Street",
	});
	assert.equal(dialog.hidden, true);
	assert.equal(frm.reloads, 1);
});

test("a server error on edit does not reload the form", async () => {
	const { dialog, frm } = await open_edit(editable(), {
		edit_shipment: () => {
			throw new Error("Bosta refused");
		},
	});
	const confirms = with_confirm();
	dialog.config.primary_action({ receiver_phone: "0111", city: "c-cairo", address_first_line: "7 New Street" });
	confirms[0].on_yes();
	await settle();
	assert.equal(frm.reloads, 0);
});

test("the confirm text escapes the tracking number and the dialog never renders stored text as HTML", async () => {
	const evil = "<img src=x onerror=alert(1)>";
	const shipment = editable({ tracking_number: evil, receiver_phone: evil, address_first_line: evil });
	const { dialog } = await open_edit(shipment);
	const confirms = with_confirm();
	dialog.config.primary_action({ receiver_phone: "0111", city: "c-cairo", address_first_line: "7 New Street" });
	assert.doesNotMatch(confirms[0].message, /<img/);
	assert.match(confirms[0].message, /&lt;img/);
	// Stored text is only ever a field default, never an HTML option.
	assert.doesNotMatch(dialog.config.fields[0].options, /<img/);
});

test("the create dialog keeps the receiver name, the COD note and no district field", async () => {
	const { dialog } = await open_bosta_dialog({ cod_amount: 100, receiver_name: "Mona", receiver_phone: "010" });
	assert.deepEqual(
		dialog.config.fields.map((f) => f.fieldname),
		["cod_note", "lists_error", "retry_lists", "receiver_name", "receiver_phone", "city", "address_first_line"]
	);
	assert.equal(dialog.config.primary_action_label, "Create Shipment");
});
