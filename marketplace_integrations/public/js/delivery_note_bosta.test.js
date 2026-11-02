const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

function load({ cities = [{ id: "c1", name: "Cairo", name_ar: "القاهرة" }], suggested = { city_id: "c1" }, responses = {} } = {}) {
	const registered = {};
	const calls = [];
	const dialogs = [];
	const msgprints = [];
	const alerts = [];
	const confirms = [];
	global.frappe = {
		marketplace_integrations: { shipping: { register_carrier: (name, hooks) => (registered[name] = hooks) } },
		ui: {
			Dialog: class {
				constructor(config) {
					this.config = config;
					this.fields_dict = { body: { $wrapper: { html: (html) => (this.html = html) } } };
					this.primary = { disabled: false, prop: (name, value) => (this.primary[name] = value) };
					this.descriptions = {};
					this.field_values = {};
					dialogs.push(this);
				}
				show() {}
				hide() {
					this.hidden = true;
					if (this.config.on_hide) this.config.on_hide();
				}
				get_primary_btn() {
					return this.primary;
				}
				get_field(name) {
					return { set_description: (text) => (this.descriptions[name] = text) };
				}
				get_values() {
					return this.field_values;
				}
			},
		},
		utils: { escape_html: (text) => String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;") },
		confirm: (message, yes) => {
			confirms.push(message);
			return yes();
		},
		msgprint: (opts) => msgprints.push(opts),
		show_alert: (opts) => alerts.push(opts),
		call: (opts) => {
			calls.push(opts);
			const key = Object.keys(responses).find((name) => opts.method.endsWith(name));
			if (key) {
				const answer = typeof responses[key] === "function" ? responses[key](opts) : responses[key];
				return answer instanceof Error ? Promise.reject(answer) : Promise.resolve({ message: answer });
			}
			if (opts.method.endsWith("get_cities")) return Promise.resolve({ message: { cities, suggested } });
			if (opts.method.endsWith("resolve_city")) return Promise.resolve({ message: { code: "EG-01", name: "Cairo" } });
			return Promise.resolve({ message: {} });
		},
	};
	const path = require.resolve("./delivery_note_bosta.js");
	delete require.cache[path];
	const mod = require(path);
	return { mod, registered, calls, dialogs, msgprints, alerts, confirms };
}

function make_form(doc = {}) {
	return {
		doc: { name: "MAT-DN-1", docstatus: 0, ...doc },
		perm: [{ read: 1, write: 1 }],
		buttons: [],
		values: null,
		reloads: 0,
		add_custom_button(label, action) {
			this.buttons.push({ label, action });
		},
		set_value(values) {
			this.values = values;
			return Promise.resolve();
		},
		reload_doc() {
			this.reloads++;
			return Promise.resolve();
		},
	};
}

test("Bosta registers its hooks with the shared shipping shell", () => {
	const { registered } = load();
	assert.deepEqual(Object.keys(registered), ["Bosta"]);
	const mod = require("./delivery_note_bosta.js");
	assert.equal(registered.Bosta.before_send, mod.before_send);
	assert.equal(typeof registered.Bosta.refresh, "function");
	assert.equal(registered.Bosta.pick_city, mod.pick_city);
	assert.equal(registered.Bosta.request_method, undefined);
});

test("the city button shows only while the shipment can still be created", () => {
	const { mod } = load();
	for (const status of ["", "Not Created", "Creation Failed"]) {
		const frm = make_form({ custom_integration_status: status });
		mod.add_buttons(frm);
		assert.deepEqual(frm.buttons.map((b) => b.label), ["Pick Bosta City"], status);
	}
	for (const status of ["Creating", "Creation Unknown"]) {
		const frm = make_form({ custom_integration_status: status, custom_shipping_label: "x" });
		mod.add_buttons(frm);
		assert.deepEqual(frm.buttons, [], status);
	}
});

test("the label button shows for a Created note without a label", () => {
	const { mod } = load();
	const frm = make_form({ custom_integration_status: "Created", docstatus: 1 });
	mod.add_buttons(frm);
	assert.deepEqual(frm.buttons.map((b) => b.label), ["Fetch Bosta Label", "Edit Bosta Shipment"]);
});

test("fetching the label calls the server and reloads", async () => {
	const { mod, calls } = load();
	const frm = make_form({ custom_integration_status: "Created" });
	await mod.fetch_label(frm);
	assert.equal(calls[0].method, "marketplace_integrations.api.bosta_shipments.fetch_label");
	assert.equal(frm.reloads, 1);
});

test("the city dialog offers Bosta's cities, suggests the match and writes code and name", async () => {
	const { mod, dialogs, calls } = load();
	const frm = make_form({});
	await mod.pick_city(frm);
	const field = dialogs[0].config.fields[0];
	assert.deepEqual(field.options, [{ label: "Cairo - القاهرة", value: "c1" }]);
	assert.equal(field.default, "c1");
	await dialogs[0].config.primary_action({ city: "c1" });
	assert.equal(calls[1].method, "marketplace_integrations.api.bosta_shipments.resolve_city");
	assert.deepEqual(frm.values, { custom_bosta_city: "EG-01", custom_bosta_city_name: "Cairo" });
	assert.equal(dialogs[0].hidden, true);
});

test("an empty city list tells the user instead of opening an empty dialog", async () => {
	const { mod, dialogs, msgprints } = load({ cities: [] });
	await mod.pick_city(make_form({}));
	assert.equal(dialogs.length, 0);
	assert.equal(msgprints.length, 1);
});

test("a city without an Arabic name shows its name once", () => {
	const { mod } = load();
	assert.equal(mod.city_label({ name: "Giza", name_ar: "" }), "Giza");
	assert.equal(mod.city_label({ name: "Giza", name_ar: "Giza" }), "Giza");
});

// ---- cancel at Bosta (#410) ------------------------------------------------------------------

const CANCELLED = { outcome: "cancelled", status: "Cancelled", message: "" };

function bosta_form(doc = {}) {
	return make_form({ transporter_name: "Bosta", custom_integration_status: "Created", docstatus: 1, ...doc });
}

test("a note without a live Bosta shipment is cancelled natively without a call", () => {
	const { mod, calls } = load();
	for (const doc of [
		{ custom_integration_status: "Cancelled" },
		{ custom_integration_status: "Creation Failed" },
		{ custom_integration_status: "" },
		{ transporter_name: "Noon" },
	]) {
		assert.equal(mod.before_cancel(bosta_form(doc)), undefined);
	}
	assert.equal(calls.length, 0);
});

test("a confirmed cancel reloads the note and lets the native cancel go on", async () => {
	const { mod, calls } = load({ responses: { cancel_shipment: CANCELLED } });
	const frm = bosta_form();
	global.frappe.validated = true;

	await mod.before_cancel(frm);

	assert.equal(calls[0].method, "marketplace_integrations.api.carrier_shipments.cancel_shipment");
	assert.equal(calls[0].freeze, true);
	assert.equal(calls[0].freeze_message, "Cancelling at Bosta...");
	assert.equal(frm.reloads, 1);
	assert.equal(global.frappe.validated, true);
});

test("a Bosta refusal stops the cancel and shows Bosta's text escaped", async () => {
	const { mod, msgprints } = load({
		responses: { cancel_shipment: { outcome: "refused", status: "Created", message: "<img src=x onerror=1> not cancellable" } },
	});
	const frm = bosta_form();
	global.frappe.validated = true;

	await mod.before_cancel(frm);

	assert.equal(global.frappe.validated, false);
	assert.match(msgprints[0].message, /Bosta refused: &lt;img src=x onerror=1&gt; not cancellable/);
	assert.doesNotMatch(msgprints[0].message, /<img/);
	// the server wrote the note while asking Bosta: the form is loaded again so the next action is not stale
	assert.equal(frm.reloads, 1);
});

test("an unknown result opens a dialog with Retry and Check with Bosta; closing it stops the cancel", async () => {
	const { mod, dialogs } = load({
		responses: { cancel_shipment: { outcome: "unknown", status: "Cancellation Unknown", message: "<b>timeout</b>" } },
	});
	const frm = bosta_form();
	global.frappe.validated = true;
	const finished = mod.before_cancel(frm);
	await new Promise((resolve) => setImmediate(resolve));

	const dialog = dialogs[0];
	assert.equal(dialog.config.primary_action_label, "Retry");
	assert.equal(dialog.config.secondary_action_label, "Check with Bosta");
	assert.match(dialog.html, /Bosta did not confirm the cancellation\. Nothing was cancelled in ERPNext\./);
	assert.match(dialog.html, /&lt;b&gt;timeout&lt;\/b&gt;/);

	dialog.hide();
	await finished;
	assert.equal(global.frappe.validated, false);
	assert.equal(frm.reloads, 1);
});

test("Retry asks Bosta again and continues the cancel when Bosta confirms", async () => {
	let answers = [{ outcome: "unknown", message: "" }, CANCELLED];
	const { mod, dialogs, calls } = load({ responses: { cancel_shipment: () => answers.shift() } });
	const frm = bosta_form();
	global.frappe.validated = true;
	const finished = mod.before_cancel(frm);
	await new Promise((resolve) => setImmediate(resolve));

	dialogs[0].config.primary_action();
	await finished;

	assert.equal(calls.filter((c) => c.method.endsWith("cancel_shipment")).length, 2);
	assert.equal(global.frappe.validated, true);
});

test("Check with Bosta reads the state and continues when the note is Cancelled", async () => {
	const { mod, dialogs, calls } = load({
		responses: { cancel_shipment: { outcome: "unknown", message: "" }, refresh_shipment: {} },
	});
	const frm = bosta_form();
	frm.reload_doc = function () {
		this.reloads++;
		this.doc.custom_integration_status = "Cancelled";
		return Promise.resolve();
	};
	global.frappe.validated = true;
	const finished = mod.before_cancel(frm);
	await new Promise((resolve) => setImmediate(resolve));

	dialogs[0].config.secondary_action();
	await finished;

	assert.ok(calls.some((c) => c.method.endsWith("refresh_shipment")));
	assert.equal(global.frappe.validated, true);
});

test("the draft note offers Cancel Bosta Shipment and asks before cancelling", async () => {
	const { mod, confirms, calls } = load({ responses: { cancel_shipment: CANCELLED } });
	const frm = bosta_form({ docstatus: 0 });
	mod.add_buttons(frm);
	const button = frm.buttons.find((b) => b.label === "Cancel Bosta Shipment");
	assert.ok(button);

	await button.action();

	assert.match(confirms[0], /This cannot be undone/);
	assert.equal(calls[0].method.endsWith("cancel_shipment"), true);
	assert.equal(frm.reloads, 1);
});

test("a submitted note has no Cancel Bosta Shipment button (Cancel does it)", () => {
	const { mod } = load();
	const frm = bosta_form({ docstatus: 1 });
	mod.add_buttons(frm);
	assert.deepEqual(frm.buttons.map((b) => b.label), ["Fetch Bosta Label", "Edit Bosta Shipment"]);
});

// ---- edit (#410) -----------------------------------------------------------------------------

test("edit values are checked before anything is sent", () => {
	const { mod } = load();
	const ok = { phone: "01000000000", city: "c1", first_line: "12 Example Street" };
	assert.equal(mod.edit_error(ok), "");
	assert.equal(mod.edit_error({ ...ok, phone: " " }), "Enter the receiver phone.");
	assert.match(mod.edit_error({ ...ok, first_line: "12" }), /more than 5 characters/);
	assert.match(mod.edit_error({ ...ok, first_line: "x".repeat(241) }), /at most 240/);
	assert.equal(mod.edit_error({ ...ok, city: "" }), "Choose the Bosta city.");
});

test("the edit dialog starts from Bosta's values, locks its button while sending and reloads", async () => {
	const { mod, dialogs, calls, alerts } = load({
		responses: {
			get_edit_values: { phone: "0100", city_id: "c1", first_line: "Old street 1", city_name: "Cairo" },
			edit_shipment: { status: "Created" },
		},
	});
	const frm = bosta_form();

	await mod.edit_shipment(frm);
	const dialog = dialogs[0];
	const fields = Object.fromEntries(dialog.config.fields.map((f) => [f.fieldname, f]));
	assert.equal(fields.phone.default, "0100");
	assert.equal(fields.city.default, "c1");
	assert.equal(fields.first_line.default, "Old street 1");

	const sending = dialog.config.primary_action({ phone: "0111", city: "c1", first_line: "New street 22" });
	assert.equal(dialog.primary.disabled, true);
	await sending;

	const sent = calls.find((c) => c.method.endsWith("edit_shipment"));
	assert.deepEqual(sent.args, { delivery_note: "MAT-DN-1", receiver_phone: "0111", city_id: "c1", address_first_line: "New street 22" });
	assert.equal(frm.reloads, 1);
	assert.equal(dialog.hidden, true);
	assert.equal(alerts.length, 1);
});

test("an invalid edit is refused in the dialog without a call", async () => {
	const { mod, dialogs, calls, msgprints } = load();
	await mod.edit_shipment(bosta_form());
	const before = calls.length;

	dialogs[0].config.primary_action({ phone: "", city: "c1", first_line: "12 Example Street" });

	assert.equal(calls.length, before);
	assert.equal(msgprints.length, 1);
});

test("a refused edit re-enables the button and shows the escaped reason", async () => {
	const failure = new Error("refused");
	const { mod, dialogs, msgprints } = load({ responses: { edit_shipment: failure } });
	await mod.edit_shipment(bosta_form());
	const dialog = dialogs[0];

	await dialog.config.primary_action({ phone: "0111", city: "c1", first_line: "New street 22" });

	assert.equal(dialog.primary.disabled, false);
	assert.equal(msgprints.length, 1);
	assert.equal(msgprints[0].indicator, "red");
});

function cod_form(doc = {}, { dirty = false } = {}) {
	return {
		doc: { name: "MAT-DN-1", transporter_name: "Bosta", docstatus: 0, custom_cod_confirmed: 1, ...doc },
		df: {},
		values: {},
		dashboard: { add_comment: () => {} },
		is_new: () => false,
		is_dirty: () => dirty,
		set_df_property(field, prop, value) {
			this.df[`${field}.${prop}`] = value;
		},
		set_value(field, value) {
			this.values[field] = value;
		},
	};
}

test("the COD amount and its confirmation are read-only once the shipment started, editable before", () => {
	const { mod } = load();
	for (const [status, read_only] of [["Not Created", 0], ["Creating", 1], ["Created", 1]]) {
		const frm = cod_form({ custom_integration_status: status });
		mod.set_cod_state(frm);
		assert.equal(frm.df["custom_cod_amount.read_only"], read_only, status);
		assert.equal(frm.df["custom_cod_confirmed.read_only"], read_only, status);
	}
});

test("changing the COD confirms it while the note is editable, and only then", () => {
	const { mod } = load();
	const draft = cod_form({ custom_integration_status: "Not Created", custom_cod_confirmed: 0 });
	mod.custom_cod_amount(draft);
	assert.equal(draft.values.custom_cod_confirmed, 1);
	const started = cod_form({ custom_integration_status: "Created", custom_cod_confirmed: 0 });
	mod.custom_cod_amount(started);
	assert.deepEqual(started.values, {});
	const other = cod_form({ transporter_name: "Noon", custom_cod_confirmed: 0 });
	mod.custom_cod_amount(other);
	assert.deepEqual(other.values, {});
});

test("a reload or refresh of a Created note writes no field value, so the posted copy equals the stored one", () => {
	const { mod } = load();
	const frm = cod_form({ custom_integration_status: "Created", custom_cod_confirmed: 0, custom_cod_amount: 100 });
	mod.hooks.refresh(frm);
	mod.custom_cod_amount(frm);
	assert.deepEqual(frm.values, {});
	assert.equal(frm.doc.custom_cod_confirmed, 0);
	assert.equal(frm.doc.custom_cod_amount, 100);
});

test("the submit gate asks to confirm an unconfirmed COD and passes a confirmed one", () => {
	const { mod, msgprints } = load();
	assert.equal(mod.before_send(cod_form({ custom_cod_confirmed: 0 })), false);
	assert.match(msgprints[0].message, /Confirm the COD amount/);
	assert.equal(mod.before_send(cod_form({ custom_cod_confirmed: 1 })), true);
});

test("a confirmed COD that differs from the computed one shows a note with both amounts, escaped", async () => {
	const { mod, calls } = load({ responses: { get_cod_hint: { default: 250 } } });
	const comments = [];
	const frm = cod_form({ custom_integration_status: "Not Created", custom_cod_amount: 100, currency: "EGP" });
	frm.dashboard.add_comment = (text, color, permanent) => comments.push({ text, color, permanent });
	await mod.show_cod_hint(frm);
	assert.equal(calls[0].method, "marketplace_integrations.api.bosta_shipments.get_cod_hint");
	assert.equal(comments.length, 1);
	assert.match(comments[0].text, /100 differs .* \(250\)/);
	assert.equal(comments[0].color, "yellow");
});

test("no COD note when the amount matches, the form is unsaved or dirty, or the shipment started", async () => {
	const { mod, calls } = load({ responses: { get_cod_hint: { default: null } } });
	const comments = [];
	const add = (frm) => (frm.dashboard.add_comment = (text) => comments.push(text));
	const same = cod_form({ custom_integration_status: "Not Created" });
	add(same);
	await mod.show_cod_hint(same);
	assert.equal(calls.length, 1);
	assert.equal(comments.length, 0);
	for (const frm of [
		cod_form({ custom_integration_status: "Creating" }),
		cod_form({ custom_integration_status: "Not Created", custom_cod_confirmed: 0 }),
		cod_form({ custom_integration_status: "Not Created" }, { dirty: true }),
	]) {
		add(frm);
		await mod.show_cod_hint(frm);
	}
	assert.equal(calls.length, 1);
});

// ---- Customer Return Pickup -----------------------------------------------------------------

function return_form(doc = {}, { dirty = false } = {}) {
	const frm = make_form({
		is_return: 1,
		custom_return_source: "Customer Return Pickup",
		custom_integration_status: "Not Created",
		...doc,
	});
	frm.is_dirty = () => dirty;
	return frm;
}

test("Request Bosta Return Pickup shows on a draft pickup return that is Not Created or Creation Failed", () => {
	const { mod } = load();
	for (const status of ["", "Not Created", "Creation Failed"]) {
		const frm = return_form({ custom_integration_status: status });
		mod.add_buttons(frm);
		assert.ok(frm.buttons.some((b) => b.label === "Request Bosta Return Pickup"), status);
	}
});

test("Request Bosta Return Pickup is hidden for other returns, started pickups and submitted returns", () => {
	const { mod } = load();
	for (const doc of [
		{ custom_return_source: "" },
		{ custom_return_source: "Returned by Bosta" },
		{ is_return: 0 },
		{ custom_integration_status: "Creating" },
		{ custom_integration_status: "Created" },
		{ custom_integration_status: "Creation Unknown" },
		{ docstatus: 1 },
	]) {
		const frm = return_form(doc);
		mod.add_buttons(frm);
		assert.ok(!frm.buttons.some((b) => b.label === "Request Bosta Return Pickup"), JSON.stringify(doc));
	}
});

test("Request Bosta Return Pickup needs write permission on the note", () => {
	const { mod } = load();
	const frm = return_form();
	frm.perm = [{ read: 1, write: 0 }];
	mod.add_buttons(frm);
	assert.ok(!frm.buttons.some((b) => b.label === "Request Bosta Return Pickup"));
});

test("a Retry that then fails does not reload the form twice per attempt (stop is not wrapped again)", async () => {
	let answers = [{ outcome: "unknown", message: "" }, { outcome: "unknown", message: "" }];
	const { mod, dialogs } = load({ responses: { cancel_shipment: () => answers.shift() } });
	const frm = bosta_form();
	global.frappe.validated = true;
	const finished = mod.before_cancel(frm);
	await new Promise((resolve) => setImmediate(resolve));
	dialogs[0].config.primary_action(); // Retry
	await new Promise((resolve) => setImmediate(resolve));
	dialogs[1].hide(); // closing the second dialog stops the cancel
	await finished;
	assert.equal(global.frappe.validated, false);
	assert.equal(frm.reloads, 1);
});

test("a Check that finds the note already cancelled automatically does not let the native cancel go on", async () => {
	const { mod, dialogs, alerts } = load({
		responses: { cancel_shipment: { outcome: "unknown", message: "" }, refresh_shipment: {} },
	});
	const frm = bosta_form();
	frm.reload_doc = function () {
		this.reloads++;
		this.doc.custom_integration_status = "Cancelled";
		this.doc.docstatus = 2;
		return Promise.resolve();
	};
	global.frappe.validated = true;
	const finished = mod.before_cancel(frm);
	await new Promise((resolve) => setImmediate(resolve));

	dialogs[0].config.secondary_action();
	await finished;

	assert.equal(global.frappe.validated, false);
	assert.equal(alerts[0].indicator, "green");
});

test("a Created pickup return offers neither the label nor Edit Bosta Shipment", () => {
	const { mod } = load();
	const frm = return_form({ custom_integration_status: "Created" });
	mod.add_buttons(frm);
	assert.deepEqual(frm.buttons.map((b) => b.label), ["Cancel Bosta Shipment"]);
});

test("the button runs the shared wait dialog without submitting", async () => {
	const { mod } = load();
	const sent = [];
	global.frappe.marketplace_integrations.shipping.send_and_wait = (frm, carrier, submitting) => {
		sent.push({ carrier, submitting });
		return Promise.resolve();
	};
	await mod.request_pickup(return_form());
	assert.deepEqual(sent, [{ carrier: "Bosta", submitting: false }]);
});

test("an unsaved return asks to save first and sends nothing", async () => {
	const { mod, msgprints } = load();
	const sent = [];
	global.frappe.marketplace_integrations.shipping.send_and_wait = () => sent.push(1);
	await mod.request_pickup(return_form({}, { dirty: true }));
	assert.deepEqual(sent, []);
	assert.equal(msgprints[0].message, "Save the Delivery Note first, then request the pickup.");
});

// The real neutral shell (its lock), not a copy of it.
function use_lock() {
	const path = require.resolve("./delivery_note_shipping.js");
	delete require.cache[path];
	const shell = require(path);
	global.frappe.marketplace_integrations.shipping = shell;
	return shell;
}

test("a double click on Fetch Bosta Label sends one request and the lock is released", async () => {
	const { mod, calls } = load();
	const shell = use_lock();
	const frm = make_form({ custom_integration_status: "Created" });
	const first = mod.fetch_label(frm);
	await mod.fetch_label(frm);
	assert.equal(calls.length, 1);
	await first;
	assert.equal(frm.reloads, 1);
	assert.equal(shell.is_busy(frm), false);
});

test("the lock is released when the request fails", async () => {
	const { mod, calls } = load({ responses: { fetch_label: new Error("down") } });
	const shell = use_lock();
	const frm = make_form({ custom_integration_status: "Created" });
	await assert.rejects(mod.fetch_label(frm));
	assert.equal(shell.is_busy(frm), false);
	await assert.rejects(mod.fetch_label(frm));
	assert.equal(calls.length, 2);
});

test("a cancel while another request is in flight sends nothing and stops the native cancel", async () => {
	const { mod, calls } = load({ responses: { cancel_shipment: { status: "Cancelled", outcome: "cancelled", message: "" } } });
	use_lock();
	const frm = make_form({ custom_integration_status: "Created", transporter_name: "Bosta" });
	const first = mod.before_cancel(frm);
	global.frappe.validated = true;
	await mod.before_cancel(frm);
	assert.equal(global.frappe.validated, false);
	assert.equal(calls.length, 1);
	await first;
});

test("the Edit dialog's Send button comes back when another request holds the note, and sends nothing", async () => {
	const { mod, dialogs, calls, alerts } = load();
	const shell = use_lock();
	const frm = make_form({ custom_integration_status: "Created" });
	await mod.edit_shipment(frm);
	let release;
	const holder = shell.exclusive(frm, () => new Promise((resolve) => (release = resolve)));
	const before = calls.length;
	await dialogs[0].config.primary_action({ phone: "0111", city: "c1", first_line: "New street 22" });
	assert.equal(dialogs[0].primary.disabled, false);
	assert.equal(calls.length, before);
	assert.equal(alerts.length, 1);
	assert.equal(alerts[0].indicator, "orange");
	release();
	await holder;
});

test("Check with Bosta while another request holds the note does nothing and ends the cancel attempt", async () => {
	const { mod, dialogs, calls } = load({ responses: { cancel_shipment: { outcome: "unknown", message: "" } } });
	const shell = use_lock();
	const frm = make_form({ custom_integration_status: "Created", transporter_name: "Bosta" });
	global.frappe.validated = true;
	const pending = mod.before_cancel(frm);
	await new Promise((resolve) => setImmediate(resolve));
	let release;
	const holder = shell.exclusive(frm, () => new Promise((resolve) => (release = resolve)));
	await dialogs[0].config.secondary_action();
	assert.equal(calls.filter((c) => c.method.endsWith("refresh_shipment")).length, 0);
	await pending;
	assert.equal(global.frappe.validated, false);
	release();
	await holder;
});
