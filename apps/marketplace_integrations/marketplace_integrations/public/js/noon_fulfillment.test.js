const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

// Loads noon_fulfillment.js fresh against a mock frappe. After #415 the Sales Order keeps only Mark Out of
// Stock, the order invoice and the stuck-reservation recovery; the shipment dialogs are gone.
function load() {
	const calls = [];
	const dialogs = [];
	const alerts = [];
	global.frappe = {
		utils: { escape_html: (value) => String(value) },
		model: { can_write: () => true },
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
			return Promise.resolve({ message: { queued: false } });
		},
		msgprint() {},
		confirm: (_message, proceed) => proceed(),
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

const ctx = () => ({ marketplace_order: "MO-NOON", sales_order: "SO-1", reload() {} });

test("only the order-level buttons exist", () => {
	const { module_exports } = load();
	assert.deepEqual(
		module_exports.noon_fulfillment_buttons(ctx()).map((button) => button.action),
		["mark_out_of_stock", "upload_order_invoice", "retry_order_invoice", "release_stuck_oos_reservation"]
	);
});

test("the Mark Out of Stock dialog lists the shippable units and sends the chosen mp_item_nrs", async () => {
	const { module_exports, dialogs, calls } = load();
	module_exports.show_noon_mark_out_of_stock_dialog(ctx());
	await settle();
	const dialog = dialogs[dialogs.length - 1];
	assert.equal(dialog.config.title, "Mark Units Out of Stock");
	assert.deepEqual(
		dialog.config.fields.map((field) => field.fieldname),
		["units"]
	);
	dialog.config.primary_action({ units: ["0"] });
	await settle();
	const call = calls[calls.length - 1];
	assert.equal(call.method, "marketplace_integrations.api.noon_fulfillment.mark_out_of_stock");
	assert.deepEqual(call.args, { marketplace_order: "MO-NOON", mp_item_nrs: ["1"] });
});

test("a queued answer that was deduplicated says the action is already queued for the order", async () => {
	const { module_exports, dialogs, alerts } = load();
	module_exports.show_noon_upload_order_invoice_dialog(ctx());
	dialogs[dialogs.length - 1].config.primary_action({
		invoice_file: "/private/files/i.pdf",
		invoice_nr: "INV-1",
		issued_at: "2026-11-01",
	});
	await settle();
	assert.equal(alerts[0].message, "A Noon fulfillment action for this order is already queued or running.");
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
