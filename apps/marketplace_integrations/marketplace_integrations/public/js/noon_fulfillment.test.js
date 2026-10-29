const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

// Loads noon_fulfillment.js fresh against a mock frappe. Only the Create Noon Shipment dialog
// is exercised here (#385): its AWB field and the create_shipment arguments it sends.
function load() {
	const calls = [];
	const dialogs = [];
	const alerts = [];
	global.frappe = {
		utils: { escape_html: (value) => String(value) },
		ui: {
			Dialog: class {
				constructor(config) {
					this.config = config;
					dialogs.push(this);
				}
				show() {}
				hide() {}
			},
		},
		call(opts) {
			calls.push(opts);
			if (opts.method.endsWith("get_shippable_units")) {
				return Promise.resolve({ message: [{ mp_item_nr: "1", partner_sku: "SKU-1" }] });
			}
			return Promise.resolve({ message: { queued: true } });
		},
		msgprint() {},
		show_alert: (alert) => alerts.push(alert),
	};
	const path = require.resolve("./noon_fulfillment.js");
	delete require.cache[path];
	const module_exports = require(path);
	return { calls, dialogs, alerts, module_exports };
}

async function settle() {
	for (let i = 0; i < 5; i++) {
		await new Promise((resolve) => setImmediate(resolve));
	}
}

async function open_create_dialog() {
	const state = load();
	state.module_exports.show_noon_create_shipment_dialog({
		marketplace_order: "MO-NOON",
		sales_order: "SO-1",
		shipments: [],
		reload() {},
	});
	await settle();
	const dialog = state.dialogs[state.dialogs.length - 1];
	return { ...state, dialog };
}

async function submit(dialog, values) {
	dialog.config.primary_action(values);
	await settle();
}

test("the Create Noon Shipment dialog has an optional AWB Nr Data field after the units", async () => {
	const { dialog } = await open_create_dialog();
	assert.equal(dialog.config.title, "Create Noon Shipment");
	assert.deepEqual(
		dialog.config.fields.map((field) => field.fieldname),
		["units", "awb_nr"]
	);
	const awb = dialog.config.fields.find((field) => field.fieldname === "awb_nr");
	assert.equal(awb.fieldtype, "Data");
	assert.equal(awb.label, "AWB Nr");
	assert.ok(!awb.reqd, "the AWB field is optional");
	assert.match(awb.description, /Leave empty to use a Noon AWB/);
});

test("a filled AWB is trimmed and sent with create_shipment", async () => {
	const { dialog, calls } = await open_create_dialog();
	await submit(dialog, { units: ["0"], awb_nr: "  NOON-STK-1  " });
	const call = calls[calls.length - 1];
	assert.equal(call.method, "marketplace_integrations.api.noon_fulfillment.create_shipment");
	assert.deepEqual(call.args, {
		marketplace_order: "MO-NOON",
		mp_item_nrs: ["1"],
		awb_nr: "NOON-STK-1",
	});
});

test("an empty or spaces-only AWB is not sent, so the worker uses a Noon AWB as before", async () => {
	for (const awb_nr of [undefined, "", "   "]) {
		const { dialog, calls } = await open_create_dialog();
		await submit(dialog, { units: ["0"], awb_nr });
		const call = calls[calls.length - 1];
		assert.equal(call.method, "marketplace_integrations.api.noon_fulfillment.create_shipment");
		assert.deepEqual(call.args, { marketplace_order: "MO-NOON", mp_item_nrs: ["1"] });
		assert.equal(Object.prototype.hasOwnProperty.call(call.args, "awb_nr"), false, `awb_nr=${JSON.stringify(awb_nr)}`);
	}
});

// ---- Cancel on Noon (#376) ----

const PREVIEW = {
	created: [{ integration_shipment_nr: "ORD-1", items: [{ label: "Tea <b>x</b> (SKU-1)", qty: 2 }] }],
	skipped: [{ integration_shipment_nr: "ORD-2", items: [] }],
	blockers: [],
};

function load_cancel(preview, result) {
	const state = load();
	const previous = global.frappe.call;
	global.frappe.call = (opts) => {
		if (opts.method.endsWith("get_cancel_on_noon_preview")) {
			state.calls.push(opts);
			return Promise.resolve({ message: preview });
		}
		if (opts.method.endsWith("cancel_created_shipments")) {
			state.calls.push(opts);
			return Promise.resolve({ message: result });
		}
		return previous(opts);
	};
	state.msgprints = [];
	global.frappe.msgprint = (m) => state.msgprints.push(m);
	return state;
}

test("the Cancel on Noon dialog names the Created shipments and sends nothing until confirmed", async () => {
	const state = load_cancel(PREVIEW, { queued: ["ORD-1"], skipped: [], failed: [] });
	const reloads = [];
	state.module_exports.show_noon_cancel_on_noon_dialog({ marketplace_order: "MO-NOON", reload: () => reloads.push(1) });
	await settle();
	assert.deepEqual(state.calls.map((c) => c.method.split(".").pop()), ["get_cancel_on_noon_preview"]);
	const dialog = state.dialogs[state.dialogs.length - 1];
	const html = dialog.config.fields[0].options;
	assert.match(html, /Shipment 1/);
	assert.match(html, /Tea <b>x<\/b> \(SKU-1\) x 2/); // escape_html is the identity in this mock
	assert.match(html, /Delivery Note is live: Shipment 2/);
	assert.match(html, /not in a shipment are not changed/);
	assert.match(html, /completes only after Noon confirms/);

	dialog.config.secondary_action(); // "Not now": nothing is sent
	assert.equal(state.calls.length, 1);
	assert.equal(reloads.length, 0);

	dialog.config.primary_action();
	await settle();
	const call = state.calls[state.calls.length - 1];
	assert.equal(call.method, "marketplace_integrations.api.noon_fulfillment.cancel_created_shipments");
	assert.deepEqual(call.args, { marketplace_order: "MO-NOON" });
	assert.equal(reloads.length, 1);
	assert.equal(state.alerts[0].indicator, "blue");
});

test("with no cancellable Created shipment the dialog only tells the user", async () => {
	const state = load_cancel({ created: [], skipped: [], blockers: [] }, {});
	state.module_exports.show_noon_cancel_on_noon_dialog({ marketplace_order: "MO-NOON", reload() {} });
	await settle();
	assert.equal(state.dialogs.length, 0);
	assert.equal(state.msgprints.length, 1);
});

test("the Cancel on Noon alert reports failures to queue as a warning", () => {
	const { module_exports } = load();
	const alert = module_exports.noon_cancel_on_noon_alert({
		queued: ["ORD-1"],
		skipped: [],
		failed: [{ integration_shipment_nr: "ORD-3", error: "not cancellable" }],
	});
	assert.equal(alert.indicator, "orange");
	assert.match(alert.message, /Queued 1 shipment/);
	assert.match(alert.message, /ORD-3: not cancellable/);
});

test("Cancel on Noon is one of the fulfillment buttons", () => {
	const { module_exports } = load();
	const buttons = module_exports.noon_fulfillment_buttons({});
	assert.ok(buttons.some((b) => b.action === "cancel_on_noon" && b.label === "Cancel on Noon"));
});
