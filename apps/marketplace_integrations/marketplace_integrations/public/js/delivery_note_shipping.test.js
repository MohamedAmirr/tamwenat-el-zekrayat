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
	assert.match(submitted.intro.text, /Cancelled at Bosta\. Press Cancel to cancel this Delivery Note here too/);
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

test("Refresh of an already Cancelled note says there is nothing else to refresh and reloads", async () => {
	const { handlers, calls, alerts } = load({ call_result: { shipping_status: "Terminated" } });
	const frm = make_form({ custom_integration_status: "Cancelled", docstatus: 1 });
	handlers.refresh(frm);
	const refresh = frm.buttons.find((b) => b.label === "Refresh from Bosta");
	await refresh.action();
	assert.equal(calls[0].method, "marketplace_integrations.api.carrier_shipments.refresh_shipment");
	assert.deepEqual(alerts, [
		{ message: "The carrier shows this shipment as cancelled. Nothing else to refresh.", indicator: "green" },
	]);
	assert.equal(frm.reloads, 1);
});

test("Refresh of a Cancelled note whose answer is the stored status (not asked) shows no alert", async () => {
	const { handlers, alerts } = load({ call_result: { shipping_status: "Cancelled" } });
	const frm = make_form({ custom_integration_status: "Cancelled", docstatus: 1 });
	handlers.refresh(frm);
	await frm.buttons.find((b) => b.label === "Refresh from Bosta").action();
	assert.deepEqual(alerts, []);
	assert.equal(frm.reloads, 1);
});

test("Refresh of a Created note shows no cancelled alert", async () => {
	const { handlers, alerts } = load({ call_result: { shipping_status: "Terminated" } });
	const frm = make_form({ custom_integration_status: "Created", docstatus: 1 });
	handlers.refresh(frm);
	await frm.buttons.find((b) => b.label === "Refresh from Bosta").action();
	assert.deepEqual(alerts, []);
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

test("unlocking restores the field's own read-only setting instead of forcing 0", () => {
	const { handlers } = load();
	global.frappe.meta = { get_docfield: (doctype, fieldname) => ({ read_only: fieldname === "contact_mobile" ? 1 : 0 }) };
	const draft = make_form({ custom_integration_status: "Not Created" });
	handlers.refresh(draft);
	assert.equal(draft.df["contact_mobile.read_only"], 1);
	const created = make_form({ custom_integration_status: "Created" });
	handlers.refresh(created);
	assert.equal(created.df["contact_mobile.read_only"], 1);
	global.frappe.meta = { get_docfield: () => ({ read_only: 0 }) };
	handlers.refresh(draft);
	assert.equal(draft.df["contact_mobile.read_only"], 0);
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

test("a return is a carrier note only when marked Customer Return Pickup, and its submit never waits", () => {
	const { shipping, handlers, calls, dialogs } = load();
	const plain = make_form({ is_return: 1, custom_integration_status: "Not Created" });
	assert.equal(shipping.carrier_name(plain), null);
	assert.equal(shipping.carrier_name(make_form({ is_return: 1, custom_return_source: "Returned by Bosta" })), null);
	const pickup = make_form({ is_return: 1, custom_return_source: "Customer Return Pickup", custom_integration_status: "Not Created" });
	assert.equal(shipping.carrier_name(pickup), "Bosta");
	assert.equal(handlers.before_submit(pickup), undefined);
	assert.equal(calls.length, 0);
	assert.equal(dialogs.length, 0);
	assert.equal(global.frappe.validated, true);
});

test("a pickup request through the button waits for Created and does not talk about submitting", async () => {
	const { shipping, calls, dialogs } = load({
		statuses: [{ custom_integration_status: "Creating" }, { custom_integration_status: "Created" }],
	});
	const frm = make_form({ custom_integration_status: "Not Created" });
	await shipping.send_and_wait(frm, "Bosta", false);
	assert.equal(calls[0].method, "marketplace_integrations.api.carrier_shipments.request_shipment");
	assert.match(dialogs[0].html, /Sending the return pickup request to Bosta/);
	assert.doesNotMatch(dialogs[0].html, /submitted/);
	assert.equal(frm.reloads, 1);
});

test("a failed pickup request does not say the Delivery Note was not submitted", () => {
	const { shipping } = load();
	for (const status of ["Creation Unknown", "Creation Failed", "Creating"]) {
		assert.doesNotMatch(shipping.failure_html("Bosta", status, "x", false), /not submitted/, status);
	}
});

test("submit of an unsaved form asks to save first and does not call the carrier", () => {
	const { handlers, calls, msgprints } = load();
	const frm = make_form({ custom_integration_status: "Not Created" }, { dirty: true });
	assert.equal(handlers.before_submit(frm), undefined);
	assert.equal(global.frappe.validated, false);
	assert.equal(calls.length, 0);
	assert.equal(msgprints.length, 1);
});

test("submit waits for the job to submit the note, reloads, and never lets Frappe save again", async () => {
	const { handlers, calls, dialogs } = load({
		statuses: [
			{ custom_integration_status: "Creating", docstatus: 0 },
			{ custom_integration_status: "Created", docstatus: 0 },
			{ custom_integration_status: "Created", docstatus: 1 },
		],
	});
	const frm = make_form({ custom_integration_status: "Not Created" });
	await handlers.before_submit(frm);
	assert.equal(calls[0].method, "marketplace_integrations.api.carrier_shipments.request_shipment");
	assert.deepEqual(calls[0].args, { delivery_note: "MAT-DN-1", modified: "2026-10-31 10:00:00" });
	assert.equal(dialogs[0].config.title, "Sending to Bosta...");
	assert.equal(dialogs[0].hidden, true);
	assert.equal(frm.reloads, 1);
	assert.equal(global.frappe.validated, false);
});

test("Created but still a draft with an error: the wait stops, shows the escaped reason and reloads", async () => {
	const { handlers, msgprints } = load({
		statuses: [
			{
				custom_integration_status: "Created",
				docstatus: 0,
				custom_last_integration_error: "Shipment created at Bosta; the Delivery Note could not be submitted: <b>negative</b> stock",
			},
		],
	});
	const frm = make_form({ custom_integration_status: "Not Created" });
	await handlers.before_submit(frm);
	assert.equal(global.frappe.validated, false);
	assert.equal(frm.reloads, 1);
	assert.match(msgprints[0].message, /could not be submitted: &lt;b>negative&lt;\/b> stock/);
	assert.match(msgprints[0].message, /The shipment exists at Bosta/);
	assert.equal(msgprints[0].primary_action, undefined);
});

test("Created but still a draft without an error keeps waiting for the job, then points to the note", async () => {
	const { handlers, msgprints } = load({ statuses: [{ custom_integration_status: "Created", docstatus: 0 }] });
	const frm = make_form({ custom_integration_status: "Not Created" });
	await handlers.before_submit(frm);
	assert.equal(global.frappe.validated, false);
	assert.equal(frm.reloads, 0);
	assert.match(msgprints[0].message, /moves on automatically once the shipment is created/);
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
	assert.match(alerts[0].message, /Choose the Bosta city, then create the shipment again/);
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
	assert.match(msgprints[0].message, /The Delivery Note was not changed/);
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

test("after Close, keep sending the lock is free and Submit again starts a new request", async () => {
	const { handlers, dialogs, calls, shipping } = load({ statuses: [{ custom_integration_status: "Creating" }] });
	const frm = make_form({ custom_integration_status: "Not Created" });
	const first = handlers.before_submit(frm);
	await new Promise((resolve) => setTimeout(resolve, 2));
	dialogs[0].config.primary_action();
	await first;
	assert.equal(global.frappe.validated, false);
	assert.equal(shipping.is_busy(frm), false);
	global.frappe.validated = true;
	const second = handlers.before_submit(frm);
	await new Promise((resolve) => setTimeout(resolve, 2));
	assert.equal(calls.length, 2);
	assert.equal(dialogs.length, 2);
	dialogs[1].config.primary_action();
	await second;
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

function hold_calls() {
	const held = [];
	global.frappe.call = (opts) => {
		held.push(opts);
		return new Promise((resolve, reject) => (opts.release = { resolve, reject }));
	};
	return held;
}

test("a double click on Check sends one request; the button works again once it is over", async () => {
	const { handlers } = load();
	const frm = make_form({ custom_integration_status: "Creation Unknown" });
	handlers.refresh(frm);
	const held = hold_calls();
	const first = frm.buttons[0].action();
	await frm.buttons[0].action();
	assert.equal(held.length, 1);
	held[0].release.resolve({ message: {} });
	await first;
	assert.equal(frm.reloads, 1);
	const again = frm.buttons[0].action();
	assert.equal(held.length, 2);
	held[1].release.resolve({ message: {} });
	await again;
});

test("a failed request releases the button", async () => {
	const { handlers } = load();
	const frm = make_form({ custom_integration_status: "Created" });
	handlers.refresh(frm);
	const held = hold_calls();
	const first = frm.buttons[0].action();
	held[0].release.reject(new Error("boom"));
	await assert.rejects(first);
	const again = frm.buttons[0].action();
	assert.equal(held.length, 2);
	held[1].release.resolve({ message: {} });
	await again;
});

test("Refresh and Check share one lock per note, other notes are not blocked", async () => {
	const { shipping, alerts } = load();
	const a = make_form({});
	const b = make_form({ name: "MAT-DN-2" });
	let ran = 0;
	let release;
	const first = shipping.exclusive(a, () => new Promise((resolve) => (release = resolve)));
	assert.equal(shipping.is_busy(a), true);
	let busy_called = 0;
	await shipping.exclusive(a, () => ran++, () => busy_called++);
	await shipping.exclusive(b, () => ran++);
	assert.deepEqual([ran, busy_called], [1, 1]);
	// the ignored click says why, without naming a carrier
	assert.equal(alerts.length, 1);
	assert.equal(alerts[0].message, "A shipping request is still running.");
	assert.equal(alerts[0].indicator, "orange");
	release();
	await first;
	assert.equal(shipping.is_busy(a), false);
});

test("a second submit while a request is in flight does not start another and stops the submit", async () => {
	const { handlers, calls } = load({ statuses: [{ custom_integration_status: "Created", docstatus: 1 }] });
	const frm = make_form({ custom_integration_status: "Not Created" });
	const first = handlers.before_submit(frm);
	frappe.validated = true;
	await handlers.before_submit(frm);
	assert.equal(frappe.validated, false);
	assert.equal(calls.length, 1);
	await first;
	assert.equal(global.frappe.marketplace_integrations.shipping.is_busy(frm), false);
});

test("before_workflow_action lets Create Shipment through and remembers the note for the wait dialog", async () => {
	const { handlers, calls, dialogs } = load({
		statuses: [{ custom_integration_status: "Created", docstatus: 0 }],
		call_result: { status: "Creating" },
	});
	const frm = make_form({ custom_integration_status: "Not Created" });
	frm.selected_workflow_action = "Create Shipment";
	assert.equal(handlers.before_workflow_action(frm), undefined);
	assert.equal(frm.__create_shipment_for, "MAT-DN-1");
	frm.selected_workflow_action = null;
	await handlers.after_workflow_action(frm);
	assert.equal(dialogs.length > 0, true);
	// the Workflow already queued the request: the form must not ask for it again
	assert.equal(calls.length, 0);
	assert.equal(frm.__create_shipment_for, null);
});

test("before_workflow_action stops Create Shipment when the carrier pre-check refuses", () => {
	let unfrozen = 0;
	const { handlers } = load();
	global.frappe.dom = { unfreeze: () => unfrozen++ };
	const frm = make_form({ custom_cod_confirmed: 0 });
	frm.selected_workflow_action = "Create Shipment";
	const result = handlers.before_workflow_action(frm);
	assert.equal(result instanceof Promise, true);
	assert.equal(unfrozen, 1);
	assert.equal(frm.selected_workflow_action, null);
	assert.equal(frm.__create_shipment_for, null);
});

test("other workflow actions are not touched and open no wait dialog", async () => {
	const { handlers, dialogs } = load();
	const frm = make_form();
	frm.selected_workflow_action = "Confirm Delivery";
	assert.equal(handlers.before_workflow_action(frm), undefined);
	await handlers.after_workflow_action(frm);
	assert.equal(dialogs.length, 0);
});
