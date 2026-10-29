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
	can_write = true,
	shopify_preview = null,
	shopify_send_result = { ok: true, refreshed: true },
	bosta_creator = true,
	bosta_read = true,
	shipment = null,
	shipment_fails = false,
	defaults_fails = false,
	noon_state = { actions: [], cancelled_on_noon: false, shipments: [] },
	noon_fails = false,
	can_submit_delivery_note = true,
	marketplace_summary = { name: "MO-1", platform: "Shopify", reference: "#SHOP-1", erpnext_sync_status: "Syncing", erpnext_sales_order: "" },
} = {}) {
	const calls = [];
	const edit_calls = [];
	const marketplace_calls = [];
	const bosta_calls = [];
	const noon_calls = [];
	const confirms = [];
	const required = [];
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
			can_write: () => can_write,
			can_submit: (doctype) => can_submit_delivery_note && doctype === "Delivery Note",
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
			if (opts.method.endsWith("get_shopify_order_edit_preview")) {
				edit_calls.push(opts);
				return Promise.resolve({ message: shopify_preview });
			}
			if (opts.method.endsWith("send_shopify_order_edit")) {
				edit_calls.push(opts);
				return Promise.resolve({ message: shopify_send_result });
			}
			if (opts.method.endsWith("get_marketplace_order_summary")) {
				marketplace_calls.push(opts);
				return Promise.resolve({ message: marketplace_summary });
			}
			if (opts.method.includes("api.noon_fulfillment.")) {
				noon_calls.push(opts);
				if (opts.method.endsWith("get_available_actions")) {
					return noon_fails ? Promise.reject(new Error("boom")) : Promise.resolve({ message: noon_state });
				}
				if (opts.method.endsWith("get_shippable_units")) {
					return Promise.resolve({ message: [{ mp_item_nr: "<b>1</b>", partner_sku: "SKU-1" }, { mp_item_nr: "2", partner_sku: "" }] });
				}
				return Promise.resolve({ message: { queued: true } });
			}
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
		confirm: (message, proceed) => confirms.push({ message, proceed }),
		// Like frappe.require: loads the asset (here by requiring the file, which registers itself).
		require: (asset) => {
			required.push(asset);
			if (asset.endsWith("noon_fulfillment.js")) {
				const noon_path = require.resolve("./noon_fulfillment.js");
				delete require.cache[noon_path];
				require(noon_path);
			}
			return Promise.resolve();
		},
	};
	global.format_currency = (value, currency) => `${currency || "EGP"} ${value}`;
	const path = require.resolve("./sales_order.js");
	delete require.cache[path];
	const module_exports = require(path);
	return { calls, edit_calls, marketplace_calls, bosta_calls, noon_calls, confirms, required, alerts, msgprints, dialogs, handlers, module_exports };
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
	const marketplace_tab = { html: "", handler: null, visible: undefined, body_visible: undefined };
	const noon_tab = { html: "", handler: null, visible: undefined, body_visible: undefined };
	const frm = {
		tab,
		marketplace_tab,
		noon_tab,
		toggle_display: (field, visible) => {
			if (field === "custom_marketplace_order_tab") marketplace_tab.visible = visible;
			if (field === "custom_marketplace_order_html") marketplace_tab.body_visible = visible;
			if (field === "custom_bosta_delivery_tab") tab.visible = visible;
			if (field === "custom_bosta_delivery_html") tab.body_visible = visible;
		},
		get_field: (name) => {
			const target =
				name === "custom_marketplace_order_html"
					? marketplace_tab
					: name === "custom_bosta_delivery_html"
					? tab
					: name === "custom_noon_fulfillment_html"
					? noon_tab
					: null;
			if (!target) return null;
			return {
				$wrapper: {
					html(value) { target.html = value; return this; },
					append(value) { target.html += value; return this; },
					off() { return this; },
					on(_event, _selector, handler) { target.handler = handler; return this; },
				},
			};
		},
		doc: {
			name: "SAL-ORD-1",
			custom_marketplace_order: "MO-1",
			docstatus: 1,
			status: "To Deliver and Bill",
			currency: "EGP",
			...doc,
		},
		cscript: {},
		buttons,
		reloads: 0,
		indicators,
		dashboard: { add_indicator: (label, color) => indicators.push({ label, color }) },
		add_custom_button: (label, action, group) => buttons.push({ label, action, group }),
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

test("linked Sales Orders load the marketplace summary and render escaped values", async () => {
	const { handlers, marketplace_calls } = load({
		marketplace_summary: {
			name: "MO-1", platform: "Shopify", reference: "<img src=x onerror=alert(1)>",
			erpnext_sync_status: "Syncing", erpnext_sales_order: "",
			payment_transactions: [{ kind: "SALE", status: "FAILURE", gateway: "<script>bad</script>", amount: "12" }],
			amounts: { order_total: 12, currency: "EGP" },
		},
	});
	const frm = make_frm({ docstatus: 0 });
	await handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(marketplace_calls.length, 1);
	assert.equal(frm.marketplace_tab.visible, true);
	assert.match(frm.marketplace_tab.html, /&lt;img/);
	assert.match(frm.marketplace_tab.html, /&lt;script&gt;bad/);
	assert.doesNotMatch(frm.marketplace_tab.html, /<img|<script>/);
	assert.match(frm.marketplace_tab.html, /Retry Sync/);
});

test("unlinked Sales Orders hide and clear the marketplace tab", async () => {
	const { handlers, marketplace_calls } = load();
	const frm = make_frm({ custom_marketplace_order: "" });
	frm.marketplace_tab.html = "stale";
	await handlers["Sales Order"].refresh(frm);
	assert.equal(frm.marketplace_tab.visible, false);
	assert.equal(frm.marketplace_tab.html, "");
	assert.equal(marketplace_calls.length, 0);
});

test("linked Sales Orders keep the tab visible without exposing an unreadable order", async () => {
	const { handlers } = load({ marketplace_summary: null });
	const frm = make_frm({ docstatus: 0 });
	await handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(frm.marketplace_tab.visible, true);
	assert.match(frm.marketplace_tab.html, /Marketplace Order summary unavailable/);
	assert.doesNotMatch(frm.marketplace_tab.html, /MO-1|#SHOP-1/);
});

test("marketplace sync action uses the existing endpoint and reloads the Sales Order", async () => {
	const { handlers, calls } = load({
		marketplace_summary: { name: "MO-1", platform: "Talabat", erpnext_sync_status: "Needs Action", erpnext_sales_order: "SAL-ORD-1" },
	});
	const frm = make_frm({ docstatus: 0 });
	await handlers["Sales Order"].refresh(frm);
	await settle();
	frm.marketplace_tab.handler({ currentTarget: { getAttribute: () => "sync" } });
	await settle();
	assert.equal(calls.at(-1).method, "marketplace_integrations.api.marketplace_orders.sync_to_erpnext");
	assert.equal(frm.reloads, 1);
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

test("the excluded platform list mirrors the Python constant and holds Noon and Talabat only", () => {
	const { module_exports } = load({ option: NO_PLATFORM });
	// integrations/bosta/test_excluded_platforms.py reads the same list from the source.
	assert.deepEqual(module_exports.BOSTA_EXCLUDED_PLATFORMS, ["Noon", "Talabat"]);
	assert.equal(module_exports.is_bosta_excluded_platform({ custom_marketplace_platform: "Noon" }), true);
	for (const platform of ["Shopify", "", undefined]) {
		assert.equal(module_exports.is_bosta_excluded_platform({ custom_marketplace_platform: platform }), false);
	}
	assert.equal(module_exports.is_bosta_excluded_platform(null), false);
});

test("a Noon or Talabat Sales Order shows an existing Bosta shipment read-only: label and refresh only", async () => {
	for (const platform of ["Noon", "Talabat"]) {
		const { handlers } = load({ option: NO_PLATFORM, shipment: shipment_summary({ bosta_state_code: 10 }) });
		const frm = make_frm({ custom_marketplace_platform: platform });
		handlers["Sales Order"].refresh(frm);
		await settle();
		assert.deepEqual(labels(frm), ["print", "refresh"], platform);
		assert.equal(frm.tab.visible, true, platform);
	}
	// The same shipment on a Shopify order keeps edit and cancel.
	const { handlers } = load({ option: NO_PLATFORM, shipment: shipment_summary({ bosta_state_code: 10 }) });
	const frm = make_frm({ custom_marketplace_platform: "Shopify" });
	handlers["Sales Order"].refresh(frm);
	await settle();
	assert.deepEqual(labels(frm), ["print", "refresh", "edit", "cancel"]);
});

test("a Noon or Talabat Sales Order with no Bosta shipment has no Bosta tab and no Create button", async () => {
	for (const platform of ["Noon", "Talabat"]) {
		const { handlers } = load({ option: NO_PLATFORM, shipment: null });
		const frm = make_frm({ custom_marketplace_platform: platform });
		handlers["Sales Order"].refresh(frm);
		await settle();
		assert.equal(frm.tab.visible, false, platform);
		assert.equal(frm.tab.html, "", platform);
	}
	// Shopify and an order with no platform keep Bosta exactly as before.
	for (const platform of ["Shopify", undefined]) {
		const { handlers } = load({ option: NO_PLATFORM, shipment: null });
		const frm = make_frm({ custom_marketplace_platform: platform });
		handlers["Sales Order"].refresh(frm);
		await settle();
		assert.equal(frm.tab.visible, true, String(platform));
		assert.deepEqual(labels(frm), ["create"], String(platform));
	}
});

test("a Noon Sales Order's existing Bosta shipments offer no handover or retry action", async () => {
	global.format_currency = (value) => `EGP ${value}`;
	const current = detailed({ name: "BOSTA-CURRENT", handover_status: "Awaiting Handover", stock_posting_status: "Not Posted" });
	const multi = { ...current, shipments: [current] };
	const noon = load({ option: NO_PLATFORM, shipment: multi });
	const noon_frm = make_frm({ custom_marketplace_platform: "Noon" });
	noon.handlers["Sales Order"].refresh(noon_frm);
	await settle();
	assert.deepEqual(labels(noon_frm), ["print:BOSTA-CURRENT", "refresh:BOSTA-CURRENT"]);
	const shopify = load({ option: NO_PLATFORM, shipment: multi });
	const shopify_frm = make_frm({ custom_marketplace_platform: "Shopify" });
	shopify.handlers["Sales Order"].refresh(shopify_frm);
	await settle();
	assert.ok(labels(shopify_frm).includes("handover:BOSTA-CURRENT"));
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
	global.format_currency = (value) => `EGP ${value}`;
	const { handlers } = load({ option: NO_PLATFORM, shipment: shipment_summary() });
	const replies = [];
	global.frappe.call = (opts) => {
		if (!opts.method.endsWith("get_sales_order_shipment")) return Promise.resolve({ message: null });
		return new Promise((resolve) => replies.push(resolve));
	};
	const frm = make_frm({});
	handlers["Sales Order"].refresh(frm);
	handlers["Sales Order"].refresh(frm);
	// The Marketplace summary request now runs before Bosta's `frappe.require` continuation.
	// Let both Bosta requests enter flight before resolving either delayed response.
	await settle();
	assert.equal(replies.length, 2);
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
		["cod_note", "lists_error", "retry_lists", "currency", "cod_amount", "receiver_name", "receiver_phone", "city", "address_first_line"]
	);
	assert.equal(dialog.df.currency.hidden, 1);
	assert.equal(dialog.df.cod_amount.fieldtype, "Currency");
	assert.equal(dialog.df.cod_amount.default, 100);
	assert.equal(dialog.df.cod_amount.options, "currency");
	assert.equal(dialog.df.cod_amount.reqd, 1);
	assert.equal(dialog.config.primary_action_label, "Create Shipment");
});

const shopify_preview_result = (extra = {}) => ({
	state: "eligible",
	changes: [{ kind: "set_quantity", label: "<b>Tea</b>", quantity: 3, old_quantity: 2 }],
	reasons: [],
	fingerprint: "abc123",
	old_total: 20,
	new_total: 30,
	...extra,
});

test("the Shopify edit button needs an eligible preview, write permission and an open submitted Shopify order", async () => {
	const cases = [
		["eligible preview", {}, {}, 1, 1],
		["blocked preview", { shopify_preview: shopify_preview_result({ state: "blocked" }) }, {}, 0, 1],
		["not applicable preview", { shopify_preview: { state: "not_applicable", changes: [], reasons: [] } }, {}, 0, 1],
		["no Sales Order write permission", { can_write: false }, {}, 0, 0],
		["no Marketplace Order read permission", { can_read: false }, {}, 0, 0],
		["draft Sales Order", {}, { docstatus: 0 }, 0, 0],
		["closed Sales Order", {}, { status: "Closed" }, 0, 0],
		["Talabat order", {}, { custom_marketplace_platform: "Talabat" }, 0, 0],
	];
	for (const [name, options, doc, buttons, fetches] of cases) {
		const { handlers, edit_calls } = load({ shopify_preview: shopify_preview_result(), ...options });
		const frm = make_frm(doc);
		await handlers["Sales Order"].refresh(frm);
		await settle();
		const labels = frm.buttons.filter((b) => b.label === "Send item changes to Shopify");
		assert.equal(labels.length, buttons, name);
		assert.equal(edit_calls.length, fetches, name);
	}
});

test("a submitted Noon Sales Order shows the item-edit hint; other platforms and drafts do not", async () => {
	const cases = [
		["submitted Noon", { custom_marketplace_platform: "Noon" }, 1],
		["Shopify", { custom_marketplace_platform: "Shopify" }, 0],
		["Talabat", { custom_marketplace_platform: "Talabat" }, 0],
		["draft Noon", { custom_marketplace_platform: "Noon", docstatus: 0 }, 0],
		["cancelled Noon", { custom_marketplace_platform: "Noon", docstatus: 2 }, 0],
	];
	for (const [name, doc, intros] of cases) {
		const { handlers } = load({ shopify_preview: { state: "not_applicable", changes: [], reasons: [] } });
		const frm = make_frm(doc);
		const shown = [];
		frm.set_intro = (message, color) => shown.push({ message, color });
		await handlers["Sales Order"].refresh(frm);
		await settle();
		assert.equal(shown.length, intros, name);
		if (intros) {
			assert.match(shown[0].message, /Noon Fulfillment buttons on this Sales Order/);
			assert.equal(shown[0].color, "orange");
		}
	}
});

test("a preview that arrives after the form moved to another document adds no button", async () => {
	const { handlers } = load({ shopify_preview: shopify_preview_result() });
	const frm = make_frm({});
	const pending = handlers["Sales Order"].refresh(frm);
	frm.doc.name = "SAL-ORD-2";
	await pending;
	await settle();
	assert.equal(frm.buttons.length, 0);
});

test("the Shopify edit dialog escapes labels, shows like-for-like totals and sends the previewed fingerprint", async () => {
	const { handlers, edit_calls, dialogs, alerts } = load({ shopify_preview: shopify_preview_result() });
	const frm = make_frm({ currency: "SAR" });
	await handlers["Sales Order"].refresh(frm);
	await settle();
	frm.buttons[0].action();
	const html = dialogs[0].config.fields[0].options;
	assert.match(html, /&lt;b&gt;Tea&lt;\/b&gt;/);
	assert.doesNotMatch(html, /<b>/);
	assert.match(html, /Items and shipping total, before tax: SAR 20 → SAR 30/);
	dialogs[0].config.primary_action();
	await settle();
	const send = edit_calls.find((c) => c.method.endsWith("send_shopify_order_edit"));
	assert.deepEqual(send.args, { sales_order: "SAL-ORD-1", fingerprint: "abc123" });
	assert.equal(alerts[0].indicator, "green");
	assert.equal(frm.reloads, 1);
});

test("a Shopify edit whose refresh failed shows a distinct warning, not a success", async () => {
	const { handlers, dialogs, alerts } = load({
		shopify_preview: shopify_preview_result(),
		shopify_send_result: { ok: true, refreshed: false },
	});
	const frm = make_frm({});
	await handlers["Sales Order"].refresh(frm);
	await settle();
	frm.buttons[0].action();
	dialogs[0].config.primary_action();
	await settle();
	assert.equal(alerts[0].indicator, "orange");
	assert.match(alerts[0].message, /could not be refreshed/);
});

test("a blocked preview with changes shows a blocked button that lists the escaped reasons", async () => {
	const blocked = shopify_preview_result({
		state: "blocked",
		reasons: ["An item edit was already sent to Shopify; refresh the order to confirm it before sending another.", "<b>x</b>"],
	});
	const { handlers, msgprints } = load({ shopify_preview: blocked });
	const frm = make_frm({});
	await handlers["Sales Order"].refresh(frm);
	await settle();
	assert.deepEqual(frm.buttons.map((b) => b.label), ["Item changes blocked"]);
	frm.buttons[0].action();
	assert.equal(msgprints[0].indicator, "orange");
	assert.match(msgprints[0].message, /already sent to Shopify; refresh the order/);
	assert.match(msgprints[0].message, /&lt;b&gt;x&lt;\/b&gt;/);
	assert.doesNotMatch(msgprints[0].message, /<b>/);
});

test("a blocked preview without changes stays silent", async () => {
	const { handlers, msgprints } = load({
		shopify_preview: shopify_preview_result({ state: "blocked", changes: [], reasons: ["There are no item changes to send to Shopify."] }),
	});
	const frm = make_frm({});
	await handlers["Sales Order"].refresh(frm);
	await settle();
	assert.equal(frm.buttons.length, 0);
	assert.equal(msgprints.length, 0);
});

test("Not now and closing the Shopify edit dialog make no send call", async () => {
	const { handlers, edit_calls, dialogs } = load({ shopify_preview: shopify_preview_result() });
	const frm = make_frm({});
	await handlers["Sales Order"].refresh(frm);
	await settle();
	frm.buttons[0].action();
	dialogs[0].config.secondary_action();
	await settle();
	assert.equal(dialogs[0].hidden, true);
	assert.equal(edit_calls.filter((call) => call.method.endsWith("send_shopify_order_edit")).length, 0);
	assert.equal(frm.reloads || 0, 0);
});

test("a retry locks the COD field and the note names the amount entered earlier when it differs", async () => {
	global.format_currency = (value) => `EGP ${value}`;
	const { module_exports } = load({ option: eligible() });
	const note = module_exports.bosta_cod_note_html({ cod_amount: 500, retry_cod_amount: 300 });
	assert.match(note, /sends the COD entered earlier, EGP 300, not the current outstanding amount, EGP 500/);
	assert.doesNotMatch(
		module_exports.bosta_cod_note_html({ cod_amount: 500, retry_cod_amount: 500 }),
		/entered earlier/
	);

	const { dialog } = await open_bosta_dialog({ cod_amount: 500, retry_cod_amount: 300 });
	assert.equal(dialog.df.cod_amount.default, 300);
	assert.equal(dialog.df.cod_amount.read_only, 1);
	const { dialog: fresh } = await open_bosta_dialog({ cod_amount: 500 });
	assert.equal(fresh.df.cod_amount.read_only, 0);
});

const render_summary = (summary) => {
	const { module_exports } = load();
	return module_exports.marketplace_summary_html(summary);
};

test("mirrored Payment Entries show Draft, Submitted and Cancelled by docstatus", () => {
	global.format_currency = (value, currency) => `${currency} ${value}`;
	const html = render_summary({
		name: "MO-1", platform: "Shopify",
		mirrored_entries: [
			{ name: "PE-0", docstatus: 0 }, { name: "PE-1", docstatus: 1 }, { name: "PE-2", docstatus: 2 },
		],
	});
	assert.match(html, /PE-0 · Draft/);
	assert.match(html, /PE-1 · Submitted/);
	assert.match(html, /PE-2 · Cancelled/);
});

test("Shopify amounts drop zero rows and use the shop currency", () => {
	global.format_currency = (value, currency) => `${currency} ${value}`;
	const html = render_summary({
		name: "MO-1", platform: "Shopify",
		amounts: { currency: "EGP", sub_total: 100, discount: 0, shipping_total: 20, total_taxes: 0, order_total: 120, delivery_fee: 5 },
	});
	assert.match(html, /Subtotal<\/th><td>EGP 100</);
	assert.match(html, /Order Total<\/th><td>EGP 120</);
	assert.doesNotMatch(html, /Discount|Total Taxes|Delivery Fee|Duties Total/);
});

test("Talabat shows its own amounts and the cash to collect even when zero, without a currency", () => {
	delete global.format_currency;
	const html = render_summary({
		name: "MO-2", platform: "Talabat", payment_type: "CASH",
		amounts: { currency: "", sub_total: 50, delivery_fee: 10, service_fee: 0, order_total: 60, collect_at_pickup: 0, shipping_total: 7 },
	});
	assert.match(html, /Delivery Fee/);
	assert.match(html, /Cash Talabat says should be collected at delivery<\/th><td>0</);
	assert.doesNotMatch(html, /Service Fee|Shipping Total|EGP/);
	assert.match(html, /Talabat Payment Type/);
});

test("Noon summary shows identity values and no Amounts section or Shopify payment rows", () => {
	const html = render_summary({
		name: "MO-3", platform: "Noon", reference: "NOON-1", erpnext_sync_status: "Synced",
		payment_transactions: [{ kind: "SALE", status: "SUCCESS" }], amounts: { order_total: 99 },
	});
	assert.match(html, /NOON-1/);
	assert.doesNotMatch(html, /Amounts|Transaction|Order Total/);
});

test("the Kashier example lists all three transactions and the latest sync shows its message, not JSON", () => {
	const html = render_summary({
		name: "MO-4", platform: "Shopify",
		payment_transactions: [
			{ kind: "SALE", status: "ERROR", gateway: "Paymob" },
			{ kind: "SALE", status: "FAILURE", gateway: "Kashier" },
			{ kind: "SALE", status: "SUCCESS", gateway: "Kashier" },
		],
		latest_payment_log: { status: "Invalid", attempted_at: "2026-10-24 10:00:00", message: "Gateway is not mapped" },
	});
	assert.equal((html.match(/<th>Transaction<\/th>/g) || []).length, 3);
	assert.match(html, /SALE · ERROR · Paymob/);
	assert.match(html, /SALE · SUCCESS · Kashier/);
	assert.match(html, /Latest Payment Sync<\/th><td>Invalid · user:2026-10-24 10:00:00 · Gateway is not mapped/);
	assert.doesNotMatch(html, /\{"/);
});

test("the tab shows cancellation, received, last update and last sync values", () => {
	const html = render_summary({
		name: "MO-5", platform: "Shopify", cancelled_at: "2026-10-01 09:00:00", cancellation_reason: "customer",
		received_on: "2026-09-30 08:00:00", last_platform_update_on: "2026-10-02 10:00:00",
		last_erpnext_sync_on: "2026-10-02 11:00:00", last_sync_error: "boom",
	});
	for (const text of ["Cancelled At", "user:2026-10-01 09:00:00", "customer", "Received On", "Last Platform Update On", "Last ERPNext Sync On", "boom"]) {
		assert.ok(html.includes(text), text);
	}
});

// ---- Noon fulfillment on the Sales Order form (moved from the Marketplace Order form) ----

const NOON_ALL_ACTIONS = [
	"create_shipment", "mark_out_of_stock", "cancel_shipment", "cancel_on_noon", "add_awb", "mark_handed_over", "upload_invoice",
	"upload_order_invoice", "retry", "retry_order_invoice", "discard_shipment", "release_stuck_oos_reservation",
];
const NOON_LABELS = {
	create_shipment: "Create Shipment", mark_out_of_stock: "Mark Out of Stock", cancel_shipment: "Cancel Shipment", cancel_on_noon: "Cancel on Noon",
	add_awb: "Add AWB", mark_handed_over: "Mark Handed Over", upload_invoice: "Upload Invoice",
	upload_order_invoice: "Upload Order Invoice", retry: "Retry", retry_order_invoice: "Retry Order Invoice",
	discard_shipment: "Discard Shipment", release_stuck_oos_reservation: "Release Stuck Out of Stock Reservation",
};
const noon_so = (extra = {}) => ({ custom_marketplace_platform: "Noon", custom_marketplace_order: "MO-NOON", ...extra });
const noon_shipments = () => [
	{ integration_shipment_nr: "ORD-1", status: "Created", awbs: [{ courier: "Aramex", awb_nr: "AWB-1" }], delivery_note: "", handed_over: false, can_mark_handed_over: true, unresolved: [] },
	{ integration_shipment_nr: "ORD-2", status: "Error", awbs: [], delivery_note: "", handed_over: false, unresolved: [] },
	{ integration_shipment_nr: "ORD-3", status: "Created", awbs: [], delivery_notes: [{ name: "MAT-DN-1", state: "Draft" }, { name: "MAT-DN-3", state: "Submitted" }], handed_over: true, can_mark_handed_over: true, unresolved: ["SKU-9: not on the Sales Order"] },
	{ integration_shipment_nr: "ORD-4", status: "Created", awbs: [], delivery_note: "MAT-DN-2", handed_over: true, unresolved: [] },
];

async function refresh_noon(options, doc) {
	const context = load(options);
	const frm = make_frm(doc || noon_so());
	await context.handlers["Sales Order"].refresh(frm);
	await settle();
	return { ...context, frm };
}

test("a submitted Noon Sales Order gets the Noon Fulfillment group with one button per available action", async () => {
	const { frm, noon_calls, required } = await refresh_noon({
		noon_state: { actions: NOON_ALL_ACTIONS, cancelled_on_noon: false, shipments: noon_shipments() },
	});
	assert.deepEqual(required.filter((a) => a.endsWith("noon_fulfillment.js")), ["/assets/marketplace_integrations/js/noon_fulfillment.js"]);
	assert.deepEqual(frm.buttons.map((b) => b.label), NOON_ALL_ACTIONS.map((a) => NOON_LABELS[a]));
	assert.ok(frm.buttons.every((b) => b.group === "Noon Fulfillment"));
	assert.equal(noon_calls.length, 1);
	assert.equal(noon_calls[0].method, "marketplace_integrations.api.noon_fulfillment.get_available_actions");
	assert.deepEqual(noon_calls[0].args, { marketplace_order: "MO-NOON" });
});

test("only the actions the server reports are shown, and none when the list is empty", async () => {
	const some = await refresh_noon({ noon_state: { actions: ["retry", "add_awb", "unknown"], shipments: [] } });
	assert.deepEqual(some.frm.buttons.map((b) => b.label), ["Add AWB", "Retry"]);
	const none = await refresh_noon({ noon_state: { actions: [], shipments: [] } });
	assert.deepEqual(none.frm.buttons, []);
});

test("a cancelled-on-Noon order shows the headline and no buttons", async () => {
	const context = load({ noon_state: { actions: [], cancelled_on_noon: true, shipments: [] } });
	const frm = make_frm(noon_so());
	const headlines = [];
	frm.dashboard.set_headline = (message, color) => headlines.push({ message, color });
	await context.handlers["Sales Order"].refresh(frm);
	await settle();
	assert.deepEqual(frm.buttons, []);
	assert.deepEqual(headlines, [{ message: "Cancelled on Noon; no fulfillment actions available", color: "orange" }]);
});

test("the Noon Fulfillment group is hidden for Shopify, Talabat, draft, cancelled, unlinked and unknown-platform orders", async () => {
	const cases = [
		["Shopify", noon_so({ custom_marketplace_platform: "Shopify" })],
		["Talabat", noon_so({ custom_marketplace_platform: "Talabat" })],
		["blank platform", noon_so({ custom_marketplace_platform: "" })],
		["draft", noon_so({ docstatus: 0 })],
		["cancelled", noon_so({ docstatus: 2 })],
		["unlinked", noon_so({ custom_marketplace_order: "" })],
	];
	for (const [name, doc] of cases) {
		const { frm, noon_calls, required } = await refresh_noon(
			{ noon_state: { actions: NOON_ALL_ACTIONS, shipments: noon_shipments() }, shopify_preview: { state: "not_applicable", changes: [], reasons: [] } },
			doc
		);
		assert.deepEqual(frm.buttons, [], name);
		assert.equal(noon_calls.length, 0, name);
		assert.ok(!required.some((a) => a.endsWith("noon_fulfillment.js")), name);
	}
});

test("without write permission on Marketplace Order nothing is shown; without read the call is not made", async () => {
	const no_write = await refresh_noon({ can_write: false, noon_state: { actions: NOON_ALL_ACTIONS, shipments: [] } });
	assert.deepEqual(no_write.frm.buttons, []);
	const no_read = await refresh_noon({ can_read: false, noon_state: { actions: NOON_ALL_ACTIONS, shipments: [] } });
	assert.deepEqual(no_read.frm.buttons, []);
	assert.equal(no_read.noon_calls.length, 0);
});

test("Mark Handed Over is hidden without submit permission on Delivery Note", async () => {
	const { frm } = await refresh_noon({
		can_submit_delivery_note: false,
		noon_state: { actions: ["retry", "mark_handed_over"], shipments: [] },
	});
	assert.deepEqual(frm.buttons.map((b) => b.label), ["Retry"]);
});

test("a failed fulfillment call adds nothing and does not break the refresh", async () => {
	const { frm } = await refresh_noon({ noon_fails: true });
	assert.deepEqual(frm.buttons, []);
});

test("the Noon Fulfillment tab lists a block per shipment with escaped values, an items table and a Delivery Note link", async () => {
	const shipments = [
		{
			integration_shipment_nr: "<img src=x>", status: "Created", awbs: [{ courier: "<b>Ar</b>", awb_nr: "A&1" }],
			delivery_notes: [{ name: "MAT/DN 1", state: "Draft" }], unit_count: 3, shipped_count: 2,
			units: [{ item_name: "<i>Soap</i>", sku: "S&1", qty: 2, status: "INTEGRATION_ITEM_STATUS_SHIPPED", on_note: true }],
			unresolved: ["<script>x</script>: unmapped"],
		},
		{ integration_shipment_nr: "ORD-2", status: "Created", awbs: [], delivery_notes: [], unit_count: 0, shipped_count: 0, units: [], unresolved: [] },
	];
	const { frm } = await refresh_noon({
		noon_state: { actions: [], shipments, sales_invoice: { name: "ACC-SINV-1", state: "Draft" } },
	});
	const html = frm.noon_tab.html;
	assert.match(html, /&lt;img src&#x3D;x&gt;/);
	assert.match(html, /&lt;b&gt;Ar&lt;\/b&gt; A&amp;1/);
	assert.match(html, /<a href="\/app\/delivery-note\/MAT%2FDN%201">MAT\/DN 1<\/a> \(Draft\)/);
	assert.match(html, /<a href="\/app\/sales-invoice\/ACC-SINV-1">ACC-SINV-1<\/a> \(Draft\)/);
	assert.match(html, /2 of 3 units shipped/);
	assert.match(html, /<td>&lt;i&gt;Soap&lt;\/i&gt;<\/td><td>S&amp;1<\/td><td>2<\/td><td>Shipped<\/td><td>Yes<\/td>/);
	assert.match(html, /&lt;script&gt;x&lt;\/script&gt;: unmapped/);
	assert.equal((html.match(/<h5 title=/g) || []).length, 2); // one block per shipment
	assert.doesNotMatch(html, /<img|<script|<i>/);
});

test("the Noon Fulfillment tab shows an empty state, an unavailable state and no invoice name when none is readable", async () => {
	const empty = await refresh_noon({ noon_state: { actions: [], shipments: [], sales_invoice: null } });
	assert.match(empty.frm.noon_tab.html, /No Sales Invoice yet\./);
	assert.match(empty.frm.noon_tab.html, /No shipments yet\./);
	const failed = await refresh_noon({ noon_fails: true });
	assert.match(failed.frm.noon_tab.html, /Noon fulfillment is not available for this order\./);
});

test("the Marketplace Order tab no longer lists Noon shipments and non-Noon orders leave the Noon tab empty", async () => {
	const noon = await refresh_noon({
		marketplace_summary: { name: "MO-NOON", platform: "Noon", reference: "N-1", erpnext_sync_status: "Synced", erpnext_sales_order: "SAL-ORD-1" },
		noon_state: { actions: [], shipments: noon_shipments() },
	});
	assert.doesNotMatch(noon.frm.marketplace_tab.html, /Noon Shipments|<th>Shipment<\/th>/);
	const shopify = await refresh_noon({
		noon_state: { actions: [], shipments: noon_shipments() },
	}, noon_so({ custom_marketplace_platform: "Shopify" }));
	assert.equal(shopify.frm.noon_tab.html, "");
});

test("dialog options come from the shipments and escape shipment numbers", async () => {
	const { frm } = await refresh_noon({ noon_state: { actions: NOON_ALL_ACTIONS, shipments: noon_shipments() } });
	const noon = global.frappe.marketplace_integrations.noon_fulfillment;
	const ctx = noon.build_context(frm, { shipments: [{ integration_shipment_nr: "<i>1</i>", status: "Created", delivery_note: "", can_mark_handed_over: true, unresolved: [] }, ...noon_shipments()] });
	assert.equal(ctx.marketplace_order, "MO-NOON");
	assert.equal(ctx.sales_order, "SAL-ORD-1");
	assert.equal(noon.noon_created_shipment_options(ctx)[0].label, "Shipment &lt;i&gt;1&lt;/i&gt; - Created");
	assert.equal(noon.noon_created_shipment_options(ctx)[1].label, "Shipment 1 - Created");
	assert.deepEqual(noon.noon_created_shipment_options(ctx).map((o) => o.value), ["<i>1</i>", "ORD-1", "ORD-3", "ORD-4"]);
	// Error and Cancel Pending only
	assert.deepEqual(noon.noon_discardable_shipment_options(ctx).map((o) => o.value), ["ORD-2"]);
	// Created and the server says Mark Handed Over has something to do (a draft to submit or uncovered units)
	assert.deepEqual(noon.noon_handover_shipment_options(ctx).map((o) => o.value), ["<i>1</i>", "ORD-1", "ORD-3"]);
});

test("unit checklist uses indices as values, escapes labels and maps back to the real units", () => {
	load();
	const noon = require("./noon_fulfillment.js");
	global.frappe.utils.escape_html = escape_html;
	const units = [{ mp_item_nr: "<b>1</b>", partner_sku: "S\"1" }, { mp_item_nr: "2", partner_sku: undefined }];
	const options = noon.noon_unit_checklist_options(units);
	assert.deepEqual(options.map((o) => o.value), ["0", "1"]);
	assert.match(options[0].label, /&lt;b&gt;1&lt;\/b&gt;/); // raw mp_item_nr only in the tooltip
	assert.match(options[0].label, />S&quot;1</);
	assert.equal(options[1].label, '<span title="2"></span>');
	assert.deepEqual(noon.mp_item_nrs_from_unit_indices(units, ["1", "0", "7"]), ["2", "<b>1</b>"]);
	assert.deepEqual(noon.mp_item_nrs_from_unit_indices(units, undefined), []);
});

test("unit checklist labels: mapped name, unmapped SKU fallback, repeated SKU counter, tooltip and escaping", () => {
	load();
	const noon = require("./noon_fulfillment.js");
	global.frappe.utils.escape_html = escape_html;
	const options = noon.noon_unit_checklist_options([
		{ mp_item_nr: "N1", partner_sku: "SKU-001", item_name: "Hand Soap 500ml" },
		{ mp_item_nr: "N2", partner_sku: "SKU-009" },
		{ mp_item_nr: "N3", partner_sku: "SKU-002", item_name: "Rice <1kg>" },
		{ mp_item_nr: "N4", partner_sku: "SKU-002", item_name: "Rice <1kg>" },
		{ mp_item_nr: "N5", partner_sku: "SKU-002", item_name: "Rice <1kg>" },
	]);
	assert.equal(options[0].label, '<span title="N1">Hand Soap 500ml (SKU-001)</span>');
	assert.equal(options[1].label, '<span title="N2">SKU-009</span>');
	assert.equal(options[2].label, '<span title="N3">Rice &lt;1kg&gt; (SKU-002) (1 of 3)</span>');
	assert.match(options[4].label, /\(SKU-002\) \(3 of 3\)/);
	// the visible text never carries the raw mp_item_nr
	assert.doesNotMatch(options[0].label.replace(/title="[^"]*"/, ""), /N1/);
});

test("noon_handover_alert reports submitted, submit failure, nothing created and skipped units", () => {
	load();
	const { noon_handover_alert } = require("./noon_fulfillment.js");
	assert.deepEqual(noon_handover_alert({ delivery_note: "DN-1", created: true, submitted: true, unresolved: [] }), {
		message: "Delivery Note DN-1 submitted.",
		indicator: "green",
	});
	const failed = noon_handover_alert({ delivery_note: "DN-1", submitted: false, submit_error: "Not enough stock", unresolved: [] });
	assert.equal(failed.indicator, "red");
	assert.match(failed.message, /DN-1 could not be submitted and stays a draft: Not enough stock/);
	const partial = noon_handover_alert({ delivery_note: "DN-1", created: true, submitted: true, unresolved: [{ text: "X: no enabled product mapping", reason: "unmapped" }] });
	assert.equal(partial.indicator, "orange");
	assert.match(partial.message, /Not added: X: no enabled product mapping/);
	assert.doesNotMatch(partial.message, /sync was queued/);
	const none = noon_handover_alert({ delivery_note: null, unresolved: [{ text: "X: not on the Sales Order", reason: "not_on_order" }] });
	assert.equal(none.indicator, "orange");
	assert.match(none.message, /No Delivery Note was created\./);
	assert.match(none.message, /sync was queued/);
	assert.equal(noon_handover_alert(undefined).indicator, "orange");
});

test("noon_handover_alert escapes Noon-sourced unit text and the Delivery Note name", () => {
	load();
	const { noon_handover_alert } = require("./noon_fulfillment.js");
	global.frappe.utils.escape_html = escape_html;
	const alert = noon_handover_alert({
		delivery_note: "DN<1>",
		created: true,
		unresolved: [{ text: "<img src=x onerror=1>: not on the Sales Order", reason: "not_on_order" }],
	});
	assert.doesNotMatch(alert.message, /<img/);
	assert.match(alert.message, /&lt;img src&#x3D;x onerror&#x3D;1&gt;/);
	assert.match(alert.message, /DN&lt;1&gt;/);
});

test("the Noon tab shows Shipment n with a tooltip, Noon AWBs, every Delivery Note and an escaped submit error", () => {
	load();
	const { noon_fulfillment_tab_html } = require("./noon_fulfillment.js");
	global.frappe.utils.escape_html = escape_html;
	const html = noon_fulfillment_tab_html({
		shipments: [
			{
				integration_shipment_nr: "ORD-12-3", status: "Created",
				awbs: [{ courier: "noon", awb_nr: "N123" }, { courier: "Aramex", awb_nr: "A9" }],
				delivery_notes: [{ name: "MAT-DN-1", state: "Submitted" }, { name: "MAT-DN-3", state: "Draft" }],
				delivery_note_error: "<b>no stock</b>", units: [], unresolved: [],
			},
		],
	});
	assert.match(html, /<h5 title="ORD-12-3">Shipment 3 /);
	assert.match(html, /Noon N123, Aramex A9/);
	assert.match(html, /MAT-DN-1<\/a> \(Submitted\), <a href="\/app\/delivery-note\/MAT-DN-3">MAT-DN-3<\/a> \(Draft\)/);
	assert.match(html, /<p class="text-danger">&lt;b&gt;no stock&lt;\/b&gt;<\/p>/);
	assert.doesNotMatch(html, /<b>/);
});

test("a Closed submitted Noon Sales Order keeps the Noon Fulfillment buttons (the server decides)", async () => {
	const { frm } = await refresh_noon(
		{ noon_state: { actions: ["create_shipment", "mark_handed_over"], shipments: [] } },
		noon_so({ status: "Closed" })
	);
	assert.deepEqual(frm.buttons.map((b) => b.label), ["Create Shipment", "Mark Handed Over"]);
});

test("Add AWB dialog: Courier is a one-option Noon Select sending lowercase noon", async () => {
	const { frm, dialogs, noon_calls } = await refresh_noon({
		noon_state: { actions: ["add_awb"], shipments: noon_shipments() },
	});
	const by_label = Object.fromEntries(frm.buttons.map((b) => [b.label, b.action]));
	by_label["Add AWB"]();
	const dialog = dialogs[dialogs.length - 1];
	const courier = dialog.config.fields.find((f) => f.fieldname === "courier");
	assert.equal(courier.fieldtype, "Select");
	assert.deepEqual(courier.options, [{ label: "Noon", value: "noon" }]);
	assert.equal(courier.default, "noon");
	dialog.config.primary_action({ integration_shipment_nr: "ORD-1", courier: "noon", awb_nr: "A1" });
	await settle();
	const call = noon_calls[noon_calls.length - 1];
	assert.equal(call.method, "marketplace_integrations.api.noon_fulfillment.add_awb");
	assert.equal(call.args.courier, "noon");
});

test("a button opens its dialog and calls the endpoint with the linked Marketplace Order", async () => {
	const { frm, dialogs, noon_calls, confirms } = await refresh_noon({
		noon_state: { actions: ["create_shipment", "cancel_shipment", "mark_handed_over", "release_stuck_oos_reservation"], shipments: noon_shipments() },
	});
	const by_label = Object.fromEntries(frm.buttons.map((b) => [b.label, b.action]));

	by_label["Create Shipment"]();
	await settle();
	const create = dialogs[dialogs.length - 1];
	assert.equal(create.config.title, "Create Noon Shipment");
	assert.deepEqual(create.config.fields.map((f) => f.fieldname), ["units", "awb_nr"]);
	create.config.primary_action({ units: ["1"] });
	await settle();
	const call = noon_calls[noon_calls.length - 1];
	assert.equal(call.method, "marketplace_integrations.api.noon_fulfillment.create_shipment");
	assert.deepEqual(call.args, { marketplace_order: "MO-NOON", mp_item_nrs: ["2"] });
	assert.equal(frm.reloads, 1);

	by_label["Mark Handed Over"]();
	const handover = dialogs[dialogs.length - 1];
	assert.equal(handover.config.title, "Mark Noon Shipment Handed Over");
	handover.config.primary_action({ integration_shipment_nr: "ORD-1" });
	await settle();
	const handed = noon_calls[noon_calls.length - 1];
	assert.equal(handed.method, "marketplace_integrations.api.noon_fulfillment.mark_handed_over");
	assert.deepEqual(handed.args, { marketplace_order: "MO-NOON", integration_shipment_nr: "ORD-1" });

	by_label["Cancel Shipment"]();
	dialogs[dialogs.length - 1].config.primary_action({ integration_shipment_nr: "ORD-1" });
	assert.equal(confirms.length, 1);
	assert.match(confirms[0].message, /SAL-ORD-1/);
	confirms[0].proceed();
	await settle();
	assert.equal(noon_calls[noon_calls.length - 1].method, "marketplace_integrations.api.noon_fulfillment.cancel_shipment");

	by_label["Release Stuck Out of Stock Reservation"]();
	assert.equal(confirms.length, 2);
	confirms[1].proceed();
	await settle();
	assert.deepEqual(noon_calls[noon_calls.length - 1].args, { marketplace_order: "MO-NOON" });
});

test("a dialog with nothing to act on tells the user instead of opening", async () => {
	const { frm, dialogs, msgprints } = await refresh_noon({
		noon_state: { actions: ["cancel_shipment", "add_awb"], shipments: [{ integration_shipment_nr: "ORD-2", status: "Error", awbs: [], delivery_note: "", unresolved: [] }] },
	});
	const before = dialogs.length;
	frm.buttons.find((b) => b.label === "Cancel Shipment").action();
	assert.equal(dialogs.length, before);
	assert.equal(msgprints.length, 1);
});
