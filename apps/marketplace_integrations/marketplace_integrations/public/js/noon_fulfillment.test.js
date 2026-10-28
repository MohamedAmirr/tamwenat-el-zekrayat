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
