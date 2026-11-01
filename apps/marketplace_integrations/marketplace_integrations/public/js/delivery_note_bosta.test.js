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

test("Bosta registers its server methods with the shared shipping shell", () => {
	const { registered } = load();
	assert.deepEqual(Object.keys(registered), ["Bosta"]);
	assert.equal(registered.Bosta.request_method, "marketplace_integrations.api.bosta_shipments.request_shipment");
	assert.equal(registered.Bosta.check_method, "marketplace_integrations.api.bosta_shipments.check_with_bosta");
	assert.equal(registered.Bosta.refresh_method, "marketplace_integrations.api.bosta_shipments.refresh_from_bosta");
	assert.equal(registered.Bosta.pick_city, require("./delivery_note_bosta.js").pick_city);
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
	const { mod, calls } = load({ responses: { cancel_at_bosta: CANCELLED } });
	const frm = bosta_form();
	global.frappe.validated = true;

	await mod.before_cancel(frm);

	assert.equal(calls[0].method, "marketplace_integrations.api.bosta_shipments.cancel_at_bosta");
	assert.equal(calls[0].freeze, true);
	assert.equal(calls[0].freeze_message, "Cancelling at Bosta...");
	assert.equal(frm.reloads, 1);
	assert.equal(global.frappe.validated, true);
});

test("a Bosta refusal stops the cancel and shows Bosta's text escaped", async () => {
	const { mod, msgprints } = load({
		responses: { cancel_at_bosta: { outcome: "refused", status: "Created", message: "<img src=x onerror=1> not cancellable" } },
	});
	const frm = bosta_form();
	global.frappe.validated = true;

	await mod.before_cancel(frm);

	assert.equal(global.frappe.validated, false);
	assert.match(msgprints[0].message, /Bosta refused: &lt;img src=x onerror=1&gt; not cancellable/);
	assert.doesNotMatch(msgprints[0].message, /<img/);
	assert.equal(frm.reloads, 0);
});

test("an unknown result opens a dialog with Retry and Check with Bosta; closing it stops the cancel", async () => {
	const { mod, dialogs } = load({
		responses: { cancel_at_bosta: { outcome: "unknown", status: "Cancellation Unknown", message: "<b>timeout</b>" } },
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
});

test("Retry asks Bosta again and continues the cancel when Bosta confirms", async () => {
	let answers = [{ outcome: "unknown", message: "" }, CANCELLED];
	const { mod, dialogs, calls } = load({ responses: { cancel_at_bosta: () => answers.shift() } });
	const frm = bosta_form();
	global.frappe.validated = true;
	const finished = mod.before_cancel(frm);
	await new Promise((resolve) => setImmediate(resolve));

	dialogs[0].config.primary_action();
	await finished;

	assert.equal(calls.filter((c) => c.method.endsWith("cancel_at_bosta")).length, 2);
	assert.equal(global.frappe.validated, true);
});

test("Check with Bosta reads the state and continues when the note is Cancelled", async () => {
	const { mod, dialogs, calls } = load({
		responses: { cancel_at_bosta: { outcome: "unknown", message: "" }, refresh_from_bosta: {} },
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

	assert.ok(calls.some((c) => c.method.endsWith("refresh_from_bosta")));
	assert.equal(global.frappe.validated, true);
});

test("the draft note offers Cancel Bosta Shipment and asks before cancelling", async () => {
	const { mod, confirms, calls } = load({ responses: { cancel_at_bosta: CANCELLED } });
	const frm = bosta_form({ docstatus: 0 });
	mod.add_buttons(frm);
	const button = frm.buttons.find((b) => b.label === "Cancel Bosta Shipment");
	assert.ok(button);

	await button.action();

	assert.match(confirms[0], /This cannot be undone/);
	assert.equal(calls[0].method.endsWith("cancel_at_bosta"), true);
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
