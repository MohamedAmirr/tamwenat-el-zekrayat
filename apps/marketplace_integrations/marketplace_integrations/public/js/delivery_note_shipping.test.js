const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

global.DOMParser = class {
	parseFromString(text) {
		return { body: { textContent: text.replace(/<[^>]*>/g, "").replace(/&lt;/g, "<").replace(/&gt;/g, ">") } };
	}
};

function load({ boot = ["Bosta"], statuses = [], call_fails = false, call_error = null, can_write = true, call_result = null } = {}) {
	const handlers = {};
	const calls = [];
	const dialogs = [];
	const msgprints = [];
	const alerts = [];
	const queue = [...statuses];
	global.frappe = {
		boot: { marketplace_shipping_carriers: boot },
		validated: true,
		utils: { escape_html: (value) => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;") },
		model: { can_write: () => can_write },
		ui: {
			form: { on: (doctype, map) => Object.assign(handlers, map) },
			Dialog: class {
				constructor(config) {
					this.config = config;
					this.fields_dict = { body: { $wrapper: { html: (html) => (this.html = html) } } };
					dialogs.push(this);
				}
				show() {}
				hide() {
					this.hidden = true;
					if (this.config.on_hide) this.config.on_hide();
				}
			},
		},
		msgprint: (opts) => msgprints.push(opts),
		hide_msgprint: () => {},
		show_alert: (opts) => alerts.push(opts),
		call: (opts) => {
			calls.push(opts);
			if (call_error) return Promise.reject(call_error);
			return call_fails ? Promise.reject(new Error("refused")) : Promise.resolve({ message: opts.result || call_result || {} });
		},
		db: {
			get_value: () => Promise.resolve({ message: queue.length > 1 ? queue.shift() : queue[0] || {} }),
		},
	};
	const path = require.resolve("./delivery_note_shipping.js");
	delete require.cache[path];
	const shipping = require(path);
	shipping.poll_ms = 1;
	shipping.max_wait_ms = 30;
	shipping.register_carrier("Bosta", {
		before_send: (frm) => frm.doc.custom_cod_confirmed !== 0,
		refresh: (frm) => (frm.carrier_refreshed = true),
		add_buttons: (frm) => frm.extra_buttons++,
		pick_city: (frm) => (frm.city_picked = (frm.city_picked || 0) + 1),
		locked_fields: ["contact_mobile"],
	});
	return { handlers, calls, dialogs, msgprints, alerts, shipping };
}

function make_form(doc = {}, { dirty = false } = {}) {
	const frm = {
		doc: { name: "MAT-DN-1", transporter_name: "Bosta", docstatus: 0, modified: "2026-10-31 10:00:00", custom_cod_confirmed: 1, ...doc },
		intro: null,
		buttons: [],
		indicators: [],
		reloads: 0,
		extra_buttons: 0,
		df: {},
		values: {},
		is_new: () => false,
		set_df_property(field, property, value) {
			this.df[`${field}.${property}`] = value;
		},
		set_value(field, value) {
			this.values[field] = value;
			this.doc[field] = value;
			return Promise.resolve();
		},
		is_dirty: () => dirty,
		set_intro(text, color) {
			this.intro = { text, color };
		},
		add_custom_button(label, action) {
			this.buttons.push({ label, action });
		},
		reload_doc() {
			this.reloads++;
			return Promise.resolve();
		},
		dashboard: { add_indicator: (text, color) => frm.indicators.push({ text, color }) },
	};
	return frm;
}

test("the section is only driven for a carrier the server lists and a module registered", () => {
	const { shipping } = load();
	assert.equal(shipping.carrier_name(make_form()), "Bosta");
	assert.equal(shipping.carrier_name(make_form({ transporter_name: "Noon" })), null);
	load({ boot: [] });
	assert.equal(global.frappe.marketplace_integrations.shipping.carrier_name(make_form()), null);
});

test("refresh does nothing for a note no listed carrier owns", () => {
	const { handlers } = load();
	const frm = make_form({ transporter_name: "Noon", custom_integration_status: "Created" });
	handlers.refresh(frm);
	assert.equal(frm.intro, null);
	assert.deepEqual(frm.buttons, []);
});

test("the intro follows the integration status and shows the tracking number (lr_no), escaped", () => {
	const { handlers } = load();
	const created = make_form({ custom_integration_status: "Created", lr_no: "<TR1", custom_carrier_delivery_id: "DELIVERY-ID" });
	handlers.refresh(created);
	assert.deepEqual(created.intro, { text: "Bosta shipment created. Tracking number: &lt;TR1", color: "green" });
	const unknown = make_form({ custom_integration_status: "Creation Unknown" });
	handlers.refresh(unknown);
	assert.equal(unknown.intro.color, "orange");
	assert.match(unknown.intro.text, /Check with Bosta/);
	const failed = make_form({ custom_integration_status: "Creation Failed", custom_last_integration_error: "bad <phone> & more" });
	handlers.refresh(failed);
	// stored raw, escaped once where it is shown
	assert.deepEqual(failed.intro, { text: "Bosta did not create the shipment: bad &lt;phone> &amp; more", color: "red" });
	const fresh = make_form({ custom_integration_status: "Not Created" });
	handlers.refresh(fresh);
	assert.equal(fresh.intro, null);
});

test("a submitted note cancelled at the carrier says the next Cancel makes no carrier call", () => {
	const { handlers } = load();
	const submitted = make_form({ custom_integration_status: "Cancelled", docstatus: 1 });
	handlers.refresh(submitted);
	assert.equal(submitted.intro.color, "orange");
	assert.match(submitted.intro.text, /Cancelled at Bosta\. This Delivery Note is still submitted/);
	assert.match(submitted.intro.text, /Bosta will not be asked again/);
	const draft = make_form({ custom_integration_status: "Cancelled", docstatus: 0 });
	handlers.refresh(draft);
	assert.equal(draft.intro.color, "yellow");
});

test("shipping status shows as text in an indicator", () => {
	const { handlers } = load();
	const frm = make_form({ custom_integration_status: "Created", custom_shipping_status: "Delivered" });
	handlers.refresh(frm);
	assert.deepEqual(frm.indicators, [{ text: "Shipping status: Delivered", color: "blue" }]);
});

test("buttons: Check for a draft in doubt, Refresh once created, carrier buttons always", () => {
	const { handlers } = load();
	const unknown = make_form({ custom_integration_status: "Creation Unknown" });
	handlers.refresh(unknown);
	assert.deepEqual(unknown.buttons.map((b) => b.label), ["Check with Bosta"]);
	assert.equal(unknown.extra_buttons, 1);
	const created = make_form({ custom_integration_status: "Created", docstatus: 1 });
	handlers.refresh(created);
	assert.deepEqual(created.buttons.map((b) => b.label), ["Refresh from Bosta"]);
	const fresh = make_form({ custom_integration_status: "Not Created" });
	handlers.refresh(fresh);
	assert.deepEqual(fresh.buttons, []);
});

test("a Creation Failed note has no Check button: Bosta refused it, there is nothing to look up", () => {
	const { handlers } = load();
	const failed = make_form({ custom_integration_status: "Creation Failed" });
	handlers.refresh(failed);
	assert.deepEqual(failed.buttons, []);
});

test("without write permission no button is added", () => {
	const { handlers } = load({ can_write: false });
	const frm = make_form({ custom_integration_status: "Created" });
	handlers.refresh(frm);
	assert.deepEqual(frm.buttons, []);
});

test("the Check button calls the carrier method and reloads the form", async () => {
	const { handlers, calls } = load();
	const frm = make_form({ custom_integration_status: "Creation Unknown" });
	handlers.refresh(frm);
	await frm.buttons[0].action();
	assert.equal(calls[0].method, "marketplace_integrations.api.carrier_shipments.check_shipment");
	assert.deepEqual(calls[0].args, { delivery_note: "MAT-DN-1" });
	assert.equal(frm.reloads, 1);
});

test("the carrier's answer to Check is escaped before it is shown as an alert", async () => {
	const { handlers, alerts, shipping } = load();
	const frm = make_form({ custom_integration_status: "Creation Unknown" });
	handlers.refresh(frm);
	global.frappe.call = () => Promise.resolve({ message: { status: "Creation Unknown", message: "<img src=x> not found" } });
	await frm.buttons[0].action();
	assert.equal(alerts[0].message, "&lt;img src=x> not found");
	assert.ok(shipping);
});

test("the carrier's locked fields are read-only once the shipment started, editable before", () => {
	const { handlers } = load();
	const draft = make_form({ custom_integration_status: "Not Created" });
	handlers.refresh(draft);
	assert.equal(draft.df["contact_mobile.read_only"], 0);
	const created = make_form({ custom_integration_status: "Created" });
	handlers.refresh(created);
	assert.equal(created.df["contact_mobile.read_only"], 1);
});

test("submit of a note already Created continues without a dialog", () => {
	const { handlers, dialogs } = load();
	const frm = make_form({ custom_integration_status: "Created" });
	assert.equal(handlers.before_submit(frm), undefined);
	assert.equal(dialogs.length, 0);
	assert.equal(global.frappe.validated, true);
});

test("submit of a note no carrier owns is left alone", () => {
	const { handlers, calls } = load();
	assert.equal(handlers.before_submit(make_form({ transporter_name: "Noon" })), undefined);
	assert.equal(calls.length, 0);
});

test("submit of an unsaved form asks to save first and does not call the carrier", () => {
	const { handlers, calls, msgprints } = load();
	const frm = make_form({ custom_integration_status: "Not Created" }, { dirty: true });
	assert.equal(handlers.before_submit(frm), undefined);
	assert.equal(global.frappe.validated, false);
	assert.equal(calls.length, 0);
	assert.equal(msgprints.length, 1);
});

test("submit waits for Created, reloads the form, then lets the submit continue", async () => {
	const { handlers, calls, dialogs } = load({
		statuses: [{ custom_integration_status: "Creating" }, { custom_integration_status: "Created" }],
	});
	const frm = make_form({ custom_integration_status: "Not Created" });
	await handlers.before_submit(frm);
	assert.equal(calls[0].method, "marketplace_integrations.api.carrier_shipments.request_shipment");
	assert.deepEqual(calls[0].args, { delivery_note: "MAT-DN-1", modified: "2026-10-31 10:00:00" });
	assert.equal(dialogs[0].config.title, "Sending to Bosta...");
	assert.equal(dialogs[0].hidden, true);
	assert.equal(frm.reloads, 1);
	assert.equal(global.frappe.validated, true);
});

test("submit stops and opens the city picker when the server found no city for the address", async () => {
	const { handlers, calls, dialogs, alerts } = load({ call_result: { status: "Not Created", queued: false, needs_city: true } });
	const frm = make_form({ custom_integration_status: "Not Created" });
	await handlers.before_submit(frm);
	assert.equal(calls.length, 1);
	assert.equal(global.frappe.validated, false);
	assert.equal(frm.city_picked, 1);
	assert.equal(frm.reloads, 0);
	assert.equal(dialogs[0].hidden, true);
	assert.match(alerts[0].message, /Choose the Bosta city, then submit again/);
});

test("submit stops with the carrier error when creation fails", async () => {
	const { handlers, msgprints } = load({
		statuses: [{ custom_integration_status: "Creation Failed", custom_last_integration_error: "bad phone" }],
	});
	const frm = make_form({ custom_integration_status: "Not Created" });
	await handlers.before_submit(frm);
	assert.equal(global.frappe.validated, false);
	assert.equal(frm.reloads, 0);
	assert.match(msgprints[0].message, /bad phone/);
	assert.match(msgprints[0].message, /was not submitted/);
});

test("the stored carrier error is escaped where the failure is shown", async () => {
	const { handlers, msgprints } = load({
		statuses: [{ custom_integration_status: "Creation Failed", custom_last_integration_error: "bad <b>phone</b> & more" }],
	});
	await handlers.before_submit(make_form({ custom_integration_status: "Not Created" }));
	assert.match(msgprints[0].message, /bad &lt;b>phone&lt;\/b> &amp; more/);
	assert.doesNotMatch(msgprints[0].message, /<b>/);
});

test("submit stops and says the outcome is unknown when creation is unknown", async () => {
	const { handlers, msgprints } = load({ statuses: [{ custom_integration_status: "Creation Unknown" }] });
	await handlers.before_submit(make_form({ custom_integration_status: "Not Created" }));
	assert.equal(global.frappe.validated, false);
	assert.match(msgprints[0].message, /not known whether Bosta created/);
	// the dialog offers Check with Bosta right away
	assert.equal(msgprints[0].primary_action.label, "Check with Bosta");
});

test("the Check with Bosta button of the Unknown dialog calls the check method and reloads", async () => {
	const { handlers, msgprints, calls } = load({ statuses: [{ custom_integration_status: "Creation Unknown" }] });
	const frm = make_form({ custom_integration_status: "Not Created" });
	await handlers.before_submit(frm);
	await msgprints[0].primary_action.action();
	assert.equal(calls[calls.length - 1].method, "marketplace_integrations.api.carrier_shipments.check_shipment");
	assert.equal(frm.reloads, 1);
});

test("a failure other than unknown has no Check with Bosta button", async () => {
	const { handlers, msgprints } = load({ statuses: [{ custom_integration_status: "Creation Failed" }] });
	await handlers.before_submit(make_form({ custom_integration_status: "Not Created" }));
	assert.equal(msgprints[0].primary_action, undefined);
});

test("submit gives up after the maximum wait and points to the note", async () => {
	const { handlers, msgprints } = load({ statuses: [{ custom_integration_status: "Creating" }] });
	await handlers.before_submit(make_form({ custom_integration_status: "Not Created" }));
	assert.equal(global.frappe.validated, false);
	assert.match(msgprints[0].message, /taking longer than expected/);
});

test("submit stops when the server refuses the request, showing its parsed and escaped message", async () => {
	const error = { responseJSON: { _server_messages: JSON.stringify([JSON.stringify({ message: "<b>Confirm</b> the &lt;COD&gt;" })]) } };
	const { handlers, dialogs, msgprints, calls } = load({ call_error: error });
	await handlers.before_submit(make_form({ custom_integration_status: "Not Created" }));
	assert.equal(global.frappe.validated, false);
	assert.equal(dialogs[0].hidden, true);
	assert.equal(calls[0].silent, true);
	assert.equal(msgprints[0].message, "Confirm the &lt;COD>");
});

test("a refusal without a readable server message shows a generic escaped text", async () => {
	const { handlers, msgprints } = load({ call_fails: true });
	await handlers.before_submit(make_form({ custom_integration_status: "Not Created" }));
	assert.equal(msgprints[0].message, "The shipment could not be sent.");
});

test("Close, keep sending ends the wait with a quiet alert and keeps the form unsubmitted", async () => {
	const { handlers, dialogs, alerts, msgprints } = load({ statuses: [{ custom_integration_status: "Creating" }] });
	const promise = handlers.before_submit(make_form({ custom_integration_status: "Not Created" }));
	await new Promise((resolve) => setTimeout(resolve, 2));
	assert.equal(dialogs[0].config.primary_action_label, "Close, keep sending");
	dialogs[0].config.primary_action();
	await promise;
	assert.equal(global.frappe.validated, false);
	assert.equal(msgprints.length, 0);
	assert.match(alerts[0].message, /Still sending to Bosta/);
});

test("closing the dialog abandons the wait without submitting", async () => {
	const { handlers, dialogs } = load({ statuses: [{ custom_integration_status: "Creating" }] });
	const promise = handlers.before_submit(make_form({ custom_integration_status: "Not Created" }));
	await new Promise((resolve) => setTimeout(resolve, 2));
	dialogs[0].hide();
	await promise;
	assert.equal(global.frappe.validated, false);
});

test("a carrier's before_send returning false stops the submit before the carrier is called", () => {
	const { handlers, calls } = load();
	const frm = make_form({ custom_integration_status: "Not Created", custom_cod_confirmed: 0 });
	assert.equal(handlers.before_submit(frm), undefined);
	assert.equal(global.frappe.validated, false);
	assert.equal(calls.length, 0);
});

test("the shell runs the carrier's refresh hook on every form refresh", () => {
	const { handlers } = load();
	const frm = make_form({ custom_integration_status: "Not Created" });
	handlers.refresh(frm);
	assert.equal(frm.carrier_refreshed, true);
});
