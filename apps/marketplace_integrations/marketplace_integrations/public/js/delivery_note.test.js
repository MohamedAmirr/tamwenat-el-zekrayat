const assert = require("node:assert/strict");
const test = require("node:test");

function load({ can_write = true, responses = {} } = {}) {
	const calls = [];
	const dialogs = [];
	const handlers = {};
	// Minimal moment(a).diff(b, "minutes") stand-in (moment is a Desk global, not installed for node).
	global.moment = (value) => ({
		value,
		diff: (other) => (Date.parse(value.replace(" ", "T")) - Date.parse(other.value.replace(" ", "T"))) / 60000,
	});
	global.__ = (message, args) => (args ? message.replace("{0}", args[0]) : message);
	global.frappe = {
		utils: { escape_html: (value) => String(value).replace(/</g, "&lt;") },
		model: { can_write: () => can_write, can_delete: () => true },
		validated: true,
		ui: {
			form: { on: (doctype, hooks) => (handlers[doctype] = hooks) },
			Dialog: class {
				constructor(config) {
					this.config = config;
					this.title = config.title;
					this.html = "";
					this.hidden = false;
					this.fields_dict = { body: { $wrapper: { html: (value) => (this.html = value) } } };
					dialogs.push(this);
				}
				show() {}
				set_title(value) { this.title = value; }
				get_primary_btn() { return { toggle() {} }; }
				hide() { if (!this.hidden) { this.hidden = true; this.config.on_hide && this.config.on_hide(); } }
			},
		},
		show_alert() {},
		msgprint() {},
		datetime: { now_datetime: () => "2026-10-29 12:00:00" },
		call: (options) => {
			calls.push(options);
			const response = responses[options.method.split(".").pop()];
			return Promise.resolve({ message: response === undefined ? { queued: true } : response });
		},
	};
	const path = require.resolve("./delivery_note.js");
	delete require.cache[path];
	const exported = require(path);
	return { calls, handlers, dialogs, exported };
}

const retry_buttons = (frm) => frm.state.buttons.filter((b) => b.label === "Retry Noon Shipment");

function form(doc) {
	const state = { intro: null, buttons: [], reloaded: 0 };
	return {
		state,
		doc: { docstatus: 0, ...doc },
		is_new: () => false,
		set_intro: (message, color) => (state.intro = { message, color }),
		add_custom_button: (label, action) => state.buttons.push({ label, action }),
		reload_doc: () => (state.reloaded += 1),
	};
}

test("a Delivery Note without a carrier status shows nothing", () => {
	const { handlers } = load();
	const frm = form({});
	handlers["Delivery Note"].refresh(frm);
	assert.equal(frm.state.intro, null);
	assert.deepEqual(retry_buttons(frm), []);
});

test("Pending and Created show the status, the AWB is escaped, no retry button", () => {
	const { handlers } = load();
	const pending = form({ custom_carrier_status: "Pending" });
	handlers["Delivery Note"].refresh(pending);
	assert.deepEqual(pending.state.intro, { message: "Creating the Noon shipment...", color: "blue" });

	const created = form({ custom_carrier_status: "Created", lr_no: "<b>AWB1" });
	handlers["Delivery Note"].refresh(created);
	assert.equal(created.state.intro.color, "green");
	assert.match(created.state.intro.message, /^Noon shipment created\. AWB: &lt;b>AWB1<br>This Delivery Note cannot be cancelled or deleted until its Noon shipment is cancelled: use the Cancel Noon Shipment button/);

	const submitted = form({ docstatus: 1, custom_carrier_status: "Created", lr_no: "A1" });
	handlers["Delivery Note"].refresh(submitted);
	assert.match(submitted.state.intro.message, /use Cancel, which cancels the Noon shipment first\./);
	assert.deepEqual(retry_buttons(created), []);
});

test("Error shows the stored error and a Retry button that calls the server and reloads", async () => {
	const { handlers, calls } = load();
	const frm = form({ name: "DN-1", custom_carrier_status: "Error", custom_carrier_error: "No units" });
	handlers["Delivery Note"].refresh(frm);
	assert.deepEqual(frm.state.intro, { message: "Noon shipment failed: No units", color: "red" });
	assert.equal(retry_buttons(frm).length, 1);

	retry_buttons(frm)[0].action();
	await new Promise((resolve) => setTimeout(resolve, 0));
	const retry = calls.find((c) => c.method.endsWith("retry_delivery_note_shipment"));
	assert.deepEqual(retry.args, { delivery_note: "DN-1" });
	assert.equal(frm.state.reloaded, 1);
});

test("no Retry button without write permission or on a submitted note", () => {
	const denied = load({ can_write: false });
	const frm = form({ custom_carrier_status: "Error" });
	denied.handlers["Delivery Note"].refresh(frm);
	assert.deepEqual(retry_buttons(frm), []);

	const { handlers } = load();
	const submitted = form({ custom_carrier_status: "Error", docstatus: 1 });
	handlers["Delivery Note"].refresh(submitted);
	assert.deepEqual(retry_buttons(submitted), []);
});

test("a Pending note shows Retry only once it is stale", () => {
	const { handlers } = load();
	const fresh = form({ name: "DN-1", custom_carrier_status: "Pending", modified: "2026-10-29 11:50:00" });
	handlers["Delivery Note"].refresh(fresh);
	assert.deepEqual(retry_buttons(fresh), []);

	const stale = form({ name: "DN-1", custom_carrier_status: "Pending", modified: "2026-10-29 11:30:00" });
	handlers["Delivery Note"].refresh(stale);
	assert.equal(retry_buttons(stale).length, 1);
});

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test("Cancel with no live Noon shipment continues without a dialog", async () => {
	const { handlers, dialogs, calls } = load({ responses: { cancel_delivery_note_shipment: { state: "done" } } });
	const frm = form({ name: "DN-1", docstatus: 1, custom_carrier_status: "Created" });
	await handlers["Delivery Note"].before_cancel(frm);
	assert.equal(global.frappe.validated, true);
	assert.equal(dialogs.length, 0);
	assert.equal(calls[0].method, "marketplace_integrations.api.noon_fulfillment.cancel_delivery_note_shipment");
});

test("Cancel waits for the Noon shipment: dialog while cancelling, then continues", async () => {
	const { handlers, dialogs } = load({
		responses: {
			cancel_delivery_note_shipment: { state: "cancelling" },
			get_delivery_note_cancel_state: { state: "done" },
		},
	});
	const frm = form({ name: "DN-1", docstatus: 1, custom_carrier_status: "Created" });
	const pending = handlers["Delivery Note"].before_cancel(frm);
	await settle();
	assert.equal(dialogs.length, 1);
	assert.equal(dialogs[0].title, "Cancelling the Noon shipment...");
	assert.match(dialogs[0].html, /does not stop the Noon cancel once it has started/);
	assert.match(dialogs[0].html, /still not cancelled/);
	await pending;
	assert.equal(global.frappe.validated, true);
	assert.equal(dialogs[0].hidden, true);
});

test("A Noon refusal stops the cancel and shows Noon's escaped message", async () => {
	const { handlers, dialogs } = load({
		responses: {
			cancel_delivery_note_shipment: { state: "error", message: "<b>Noon says no</b>" },
		},
	});
	const frm = form({ name: "DN-1", docstatus: 1, custom_carrier_status: "Created" });
	const pending = handlers["Delivery Note"].before_cancel(frm);
	await settle();
	assert.match(dialogs[0].html, /&lt;b>Noon says no&lt;\/b>/);
	assert.match(dialogs[0].html, /The Delivery Note was not changed\./);
	dialogs[0].config.primary_action(); // Close
	await pending;
	assert.equal(global.frappe.validated, false);
});

test("closing the dialog while cancelling abandons the cancel", async () => {
	const { handlers, dialogs } = load({ responses: { cancel_delivery_note_shipment: { state: "cancelling" } } });
	const frm = form({ name: "DN-1", docstatus: 1, custom_carrier_status: "Created" });
	const pending = handlers["Delivery Note"].before_cancel(frm);
	await settle();
	dialogs[0].hide();
	await pending;
	assert.equal(global.frappe.validated, false);
});

test("a note that is not a Noon note is not intercepted", () => {
	const { handlers, calls } = load();
	const frm = form({ name: "DN-1", docstatus: 1 });
	assert.equal(handlers["Delivery Note"].before_cancel(frm), undefined);
	assert.equal(calls.length, 0);
});

test("a draft Noon note gets Cancel Noon Shipment; a submitted one does not", () => {
	const { handlers } = load();
	const draft = form({ name: "DN-1", custom_carrier_status: "Created" });
	handlers["Delivery Note"].refresh(draft);
	assert.ok(draft.state.buttons.some((b) => b.label === "Cancel Noon Shipment"));
	const submitted = form({ name: "DN-1", docstatus: 1, custom_carrier_status: "Created" });
	handlers["Delivery Note"].refresh(submitted);
	assert.ok(!submitted.state.buttons.some((b) => b.label === "Cancel Noon Shipment"));
});

test("shipment buttons follow the Created shipment's AWB and invoice state", () => {
	const { exported } = load();
	const actions = exported.note_shipment_actions;
	assert.deepEqual(actions(null), []);
	assert.deepEqual(actions({ status: "Error", awbs: [] }), ["discard"]);
	assert.deepEqual(actions({ status: "Cancel Pending", awbs: [] }), []);
	assert.deepEqual(actions({ status: "Created", awbs: [{ status: "Added" }], invoice_status: "Not Uploaded" }), ["add_awb", "upload_invoice"]);
	assert.deepEqual(actions({ status: "Created", awbs: [{ status: "Pending" }], invoice_status: "Pending" }), ["add_awb", "retry"]);
	assert.deepEqual(actions({ status: "Created", awbs: [], invoice_status: "Error" }), ["add_awb", "upload_invoice", "retry"]);
});

test("Created note shows Add AWB / invoice buttons from the server summary", async () => {
	const { handlers } = load({
		responses: { get_delivery_note_shipment: { status: "Created", awbs: [], invoice_status: "Not Uploaded" } },
	});
	const frm = form({ name: "DN-1", custom_carrier_status: "Created" });
	handlers["Delivery Note"].refresh(frm);
	await settle();
	assert.deepEqual(
		frm.state.buttons.map((b) => b.label).filter((l) => l !== "Cancel Noon Shipment"),
		["Add AWB", "Upload Shipment Invoice"]
	);
});

test("an Error note offers Discard Noon Shipment (with a confirmation) only while a failed shipment exists", async () => {
	const { handlers, calls } = load({ responses: { get_delivery_note_shipment: { status: "Error", awbs: [{ status: "Pending" }] } } });
	let confirmed = null;
	global.frappe.confirm = (message, yes) => { confirmed = { message, yes }; };
	const frm = form({ name: "DN-1", custom_carrier_status: "Error" });
	handlers["Delivery Note"].refresh(frm);
	await settle();
	const button = frm.state.buttons.find((b) => b.label === "Discard Noon Shipment");
	assert.ok(button);
	button.action();
	assert.match(confirmed.message, /Nothing is sent to Noon/);
	confirmed.yes();
	await settle();
	assert.equal(calls.at(-1).method, "marketplace_integrations.api.noon_fulfillment.discard_delivery_note_shipment");
	assert.equal(frm.state.reloaded, 1);

	const none = load({ responses: { get_delivery_note_shipment: null } });
	const gone = form({ name: "DN-1", custom_carrier_status: "Error" });
	none.handlers["Delivery Note"].refresh(gone);
	await settle();
	assert.ok(!gone.state.buttons.some((b) => b.label === "Discard Noon Shipment"));
});

test("a live shipment with no cancel running does not poll forever", async () => {
	const { handlers, dialogs, calls } = load({ responses: { cancel_delivery_note_shipment: { state: "live" } } });
	const frm = form({ name: "DN-1", docstatus: 1, custom_carrier_status: "Created" });
	const pending = handlers["Delivery Note"].before_cancel(frm);
	await settle();
	assert.equal(dialogs[0].title, "The Noon shipment was not cancelled");
	assert.ok(!calls.some((c) => c.method.endsWith("get_delivery_note_cancel_state")));
	dialogs[0].config.primary_action();
	await pending;
	assert.equal(global.frappe.validated, false);
});

test("a Cancelled note shows a neutral banner and the Retry button", () => {
	const { handlers } = load();
	const frm = form({ name: "DN-1", custom_carrier_status: "Cancelled" });
	handlers["Delivery Note"].refresh(frm);
	assert.equal(frm.state.intro.color, "yellow");
	assert.match(frm.state.intro.message, /was cancelled/);
	assert.equal(retry_buttons(frm).length, 1);
});
