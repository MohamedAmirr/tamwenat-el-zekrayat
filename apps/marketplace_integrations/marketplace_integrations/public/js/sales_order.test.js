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
	defaults_fails = false,
	noon_state = { actions: [], cancelled_on_noon: false, shipments: [] },
	noon_fails = false,
	can_create_delivery_note = true,
	marketplace_summary = { name: "MO-1", platform: "Shopify", reference: "#SHOP-1", erpnext_sync_status: "Syncing", erpnext_sales_order: "" },
} = {}) {
	const calls = [];
	const edit_calls = [];
	const marketplace_calls = [];
	const noon_calls = [];
	const confirms = [];
	const required = [];
	const alerts = [];
	const msgprints = [];
	const dialogs = [];
	const handlers = {};
	global.frappe = {
		user: { has_role: () => false },
		utils: { escape_html },
		datetime: { str_to_user: (value) => `user:${value}` },
		model: {
			can_read: (doctype) =>
				can_read && doctype === "Marketplace Order",
			can_write: () => can_write,
			can_create: (doctype) => can_create_delivery_note && doctype === "Delivery Note",
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
	return { calls, edit_calls, marketplace_calls, noon_calls, confirms, required, alerts, msgprints, dialogs, handlers, module_exports };
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
	const frm = {
		tab,
		marketplace_tab,
		toggle_display: (field, visible) => {
			if (field === "custom_marketplace_order_tab") marketplace_tab.visible = visible;
			if (field === "custom_marketplace_order_html") marketplace_tab.body_visible = visible;
		},
		get_field: (name) => {
			const target = name === "custom_marketplace_order_html" ? marketplace_tab : null;
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
			assert.match(shown[0].message, /Mark Out of Stock on this Sales Order, or cancel and amend the Delivery Note/);
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

// #415: shipments, AWBs and handover live on the Delivery Note; the Sales Order keeps Mark Out of Stock,
// the order invoice and the stuck-reservation recovery.
const NOON_ALL_ACTIONS = [
	"mark_out_of_stock", "upload_order_invoice", "retry_order_invoice", "release_stuck_oos_reservation",
];
const NOON_LABELS = {
	mark_out_of_stock: "Mark Out of Stock", upload_order_invoice: "Upload Order Invoice",
	retry_order_invoice: "Retry Order Invoice", release_stuck_oos_reservation: "Release Stuck Out of Stock Reservation",
};
const noon_so = (extra = {}) => ({ custom_marketplace_platform: "Noon", custom_marketplace_order: "MO-NOON", ...extra });

async function refresh_noon(options, doc) {
	const context = load(options);
	const frm = make_frm(doc || noon_so());
	await context.handlers["Sales Order"].refresh(frm);
	await settle();
	return { ...context, frm };
}

test("a submitted Noon Sales Order gets the Noon Fulfillment group with one button per available action", async () => {
	const { frm, noon_calls, required } = await refresh_noon({
		noon_state: { actions: NOON_ALL_ACTIONS, cancelled_on_noon: false },
	});
	assert.deepEqual(required.filter((a) => a.endsWith("noon_fulfillment.js")), ["/assets/marketplace_integrations/js/noon_fulfillment.js"]);
	assert.deepEqual(frm.buttons.map((b) => b.label), NOON_ALL_ACTIONS.map((a) => NOON_LABELS[a]));
	assert.ok(frm.buttons.every((b) => b.group === "Noon Fulfillment"));
	assert.equal(noon_calls.length, 1);
	assert.equal(noon_calls[0].method, "marketplace_integrations.api.noon_fulfillment.get_available_actions");
	assert.deepEqual(noon_calls[0].args, { marketplace_order: "MO-NOON" });
});

test("only the actions the server reports are shown, and none when the list is empty", async () => {
	const some = await refresh_noon({ noon_state: { actions: ["retry_order_invoice", "mark_out_of_stock", "unknown"] } });
	assert.deepEqual(some.frm.buttons.map((b) => b.label), ["Mark Out of Stock", "Retry Order Invoice"]);
	const none = await refresh_noon({ noon_state: { actions: [] } });
	assert.deepEqual(none.frm.buttons, []);
});

test("a cancelled-on-Noon order shows the headline and no buttons", async () => {
	const context = load({ noon_state: { actions: [], cancelled_on_noon: true } });
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
			{ noon_state: { actions: NOON_ALL_ACTIONS }, shopify_preview: { state: "not_applicable", changes: [], reasons: [] } },
			doc
		);
		assert.deepEqual(frm.buttons, [], name);
		assert.equal(noon_calls.length, 0, name);
		assert.ok(!required.some((a) => a.endsWith("noon_fulfillment.js")), name);
	}
});

test("without write permission on Marketplace Order nothing is shown; without read the call is not made", async () => {
	const no_write = await refresh_noon({ can_write: false, noon_state: { actions: NOON_ALL_ACTIONS } });
	assert.deepEqual(no_write.frm.buttons, []);
	const no_read = await refresh_noon({ can_read: false, noon_state: { actions: NOON_ALL_ACTIONS } });
	assert.deepEqual(no_read.frm.buttons, []);
	assert.equal(no_read.noon_calls.length, 0);
});

test("a failed fulfillment call adds nothing and does not break the refresh", async () => {
	const { frm } = await refresh_noon({ noon_fails: true });
	assert.deepEqual(frm.buttons, []);
});

test("the Marketplace Order tab no longer lists Noon shipments (they are on the Delivery Note)", async () => {
	const { frm } = await refresh_noon({
		marketplace_summary: { name: "MO-NOON", platform: "Noon", reference: "N-1", erpnext_sync_status: "Synced", erpnext_sales_order: "SAL-ORD-1" },
		noon_state: { actions: [], shipments: [{ integration_shipment_nr: "ORD-1", status: "Created" }] },
	});
	assert.doesNotMatch(frm.marketplace_tab.html, /Noon Shipments|ORD-1/);
});

test("the dialog context carries the linked Marketplace Order and Sales Order", async () => {
	const { frm } = await refresh_noon({ noon_state: { actions: NOON_ALL_ACTIONS } });
	const noon = global.frappe.marketplace_integrations.noon_fulfillment;
	const ctx = noon.build_context(frm);
	assert.equal(ctx.marketplace_order, "MO-NOON");
	assert.equal(ctx.sales_order, "SAL-ORD-1");
	assert.equal(typeof ctx.reload, "function");
});

test("the retired shipment helpers are gone from the Noon asset", () => {
	load();
	const noon = require("./noon_fulfillment.js");
	for (const name of ["noon_shipments_html", "noon_handover_alert", "noon_created_shipment_options", "noon_discardable_shipment_options", "noon_handover_shipment_options", "show_noon_create_shipment_dialog", "show_noon_cancel_shipment_dialog", "show_noon_mark_handed_over_dialog"]) {
		assert.equal(noon[name], undefined, name);
	}
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

test("a Closed submitted Noon Sales Order keeps the Noon Fulfillment buttons (the server decides)", async () => {
	const { frm } = await refresh_noon(
		{ noon_state: { actions: ["mark_out_of_stock", "retry_order_invoice"] } },
		noon_so({ status: "Closed" })
	);
	assert.deepEqual(frm.buttons.map((b) => b.label), ["Mark Out of Stock", "Retry Order Invoice"]);
});

test("the Mark Out of Stock and recovery buttons call the endpoints with the linked Marketplace Order", async () => {
	const { frm, dialogs, noon_calls, confirms } = await refresh_noon({
		noon_state: { actions: ["mark_out_of_stock", "release_stuck_oos_reservation"] },
	});
	const by_label = Object.fromEntries(frm.buttons.map((b) => [b.label, b.action]));

	by_label["Mark Out of Stock"]();
	await settle();
	const dialog = dialogs[dialogs.length - 1];
	dialog.config.primary_action({ units: ["1"] });
	assert.equal(confirms.length, 1);
	assert.match(confirms[0].message, /SAL-ORD-1/);
	confirms[0].proceed();
	await settle();
	const call = noon_calls[noon_calls.length - 1];
	assert.equal(call.method, "marketplace_integrations.api.noon_fulfillment.mark_out_of_stock");
	assert.deepEqual(call.args, { marketplace_order: "MO-NOON", mp_item_nrs: ["2"] });

	by_label["Release Stuck Out of Stock Reservation"]();
	assert.equal(confirms.length, 2);
	confirms[1].proceed();
	await settle();
	assert.deepEqual(noon_calls[noon_calls.length - 1].args, { marketplace_order: "MO-NOON" });
});

test("the order invoice dialog sends the file, number and date", async () => {
	const { frm, dialogs, noon_calls } = await refresh_noon({ noon_state: { actions: ["upload_order_invoice"] } });
	frm.buttons.find((b) => b.label === "Upload Order Invoice").action();
	dialogs[dialogs.length - 1].config.primary_action({ invoice_file: "/private/files/i.pdf", invoice_nr: "INV-1", issued_at: "2026-11-01" });
	await settle();
	const call = noon_calls[noon_calls.length - 1];
	assert.equal(call.method, "marketplace_integrations.api.noon_fulfillment.upload_order_invoice");
	assert.deepEqual(call.args, { marketplace_order: "MO-NOON", file_url: "/private/files/i.pdf", invoice_nr: "INV-1", issued_at: "2026-11-01" });
});
