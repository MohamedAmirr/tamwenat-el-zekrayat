const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

function load({ responses = {} } = {}) {
	const registered = {};
	const calls = [];
	const dialogs = [];
	const msgprints = [];
	const alerts = [];
	const confirms = [];
	global.frappe = {
		validated: true,
		datetime: { str_to_user: (value) => `user:${value}` },
		marketplace_integrations: { shipping: { register_carrier: (name, hooks) => (registered[name] = hooks) } },
		ui: {
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
			return Promise.resolve({ message: {} });
		},
	};
	const path = require.resolve("./delivery_note_noon.js");
	delete require.cache[path];
	const mod = require(path);
	return { mod, registered, calls, dialogs, msgprints, alerts, confirms };
}

function make_form(doc = {}) {
	return {
		doc: { name: "MAT-DN-1", docstatus: 0, transporter_name: "Noon", ...doc },
		perm: [{ read: 1, write: 1 }],
		buttons: [],
		props: {},
		values: {},
		reloads: 0,
		add_custom_button(label, action) {
			this.buttons.push({ label, action });
		},
		set_df_property(field, property, value) {
			this.props[`${field}.${property}`] = value;
		},
		set_value(field, value) {
			this.values[field] = value;
			this.doc[field] = value;
			return Promise.resolve();
		},
		reload_doc() {
			this.reloads++;
			return Promise.resolve();
		},
	};
}

// The real neutral shell (its lock), not a copy of it.
function use_real_shell() {
	const path = require.resolve("./delivery_note_shipping.js");
	delete require.cache[path];
	const shell = require(path);
	global.frappe.marketplace_integrations.shipping = shell;
	return shell;
}

test("Noon registers its hooks with the shared shipping shell", () => {
	const { registered } = load();
	assert.deepEqual(Object.keys(registered), ["Noon"]);
	const mod = require("./delivery_note_noon.js");
	assert.equal(registered.Noon.before_send, mod.before_send);
	assert.equal(registered.Noon.refresh, mod.refresh);
	assert.equal(registered.Noon.add_buttons, mod.add_buttons);
	assert.equal(registered.Noon.pick_city, undefined);
});

test("the carrier is always locked; an editable note without an AWB choice has both fields open", () => {
	const { mod } = load();
	const frm = make_form({ custom_integration_status: "Not Created" });
	mod.set_awb_state(frm);
	assert.equal(frm.props["transporter.read_only"], 1);
	assert.equal(frm.props["custom_noon_generate_awb.read_only"], 0);
	assert.equal(frm.props["lr_no.read_only"], 0);
});

test("a ticked checkbox makes the AWB read-only", () => {
	const { mod } = load();
	const frm = make_form({ custom_integration_status: "", custom_noon_generate_awb: 1 });
	mod.set_awb_state(frm);
	assert.equal(frm.props["lr_no.read_only"], 1);
	assert.equal(frm.props["custom_noon_generate_awb.read_only"], 0);
});

test("a generated AWB locks the checkbox and the AWB even on an editable note", () => {
	const { mod } = load();
	const frm = make_form({ custom_integration_status: "Creation Failed", custom_noon_generate_awb: 1, lr_no: "AWB1" });
	assert.equal(mod.awb_generated(frm), true);
	mod.set_awb_state(frm);
	assert.equal(frm.props["lr_no.read_only"], 1);
	assert.equal(frm.props["custom_noon_generate_awb.read_only"], 1);
});

test("once the shipment started both fields lock", () => {
	const { mod } = load();
	for (const status of ["Creating", "Creation Unknown", "Created", "Cancelled"]) {
		const frm = make_form({ custom_integration_status: status, lr_no: "TYPED" });
		mod.set_awb_state(frm);
		assert.equal(frm.props["lr_no.read_only"], 1, status);
		assert.equal(frm.props["custom_noon_generate_awb.read_only"], 1, status);
	}
	const submitted = make_form({ docstatus: 1, custom_integration_status: "Created" });
	mod.set_awb_state(submitted);
	assert.equal(submitted.props["lr_no.read_only"], 1);
});

test("ticking the checkbox clears a typed AWB; an untouched or generated note is left alone", () => {
	const { mod } = load();
	const typed = make_form({ custom_noon_generate_awb: 1, lr_no: "TYPED" });
	mod.custom_noon_generate_awb(typed);
	assert.equal(typed.values.lr_no, "");
	assert.equal(typed.props["lr_no.read_only"], 1);

	const generated = make_form({ custom_integration_status: "Creation Failed", custom_noon_generate_awb: 1, lr_no: "AWB1" });
	mod.refresh(generated);
	mod.custom_noon_generate_awb(generated);
	assert.equal(generated.values.lr_no, undefined);

	// typing an AWB, then ticking the box (the live checkbox and AWB alone do not make it "generated")
	const ticked_after_typing = make_form({ custom_integration_status: "Not Created", lr_no: "TYPED" });
	mod.refresh(ticked_after_typing);
	ticked_after_typing.doc.custom_noon_generate_awb = 1;
	mod.custom_noon_generate_awb(ticked_after_typing);
	assert.equal(ticked_after_typing.values.lr_no, "");
	assert.equal(ticked_after_typing.props["lr_no.read_only"], 1);

	const other = make_form({ transporter_name: "Bosta", custom_noon_generate_awb: 1, lr_no: "X" });
	mod.custom_noon_generate_awb(other);
	assert.deepEqual(other.values, {});
});

test("Submit needs a typed AWB or the checkbox", () => {
	const { mod, msgprints } = load();
	assert.equal(mod.before_send(make_form({})), false);
	assert.equal(msgprints.length, 1);
	assert.equal(mod.before_send(make_form({ lr_no: "TYPED" })), true);
	assert.equal(mod.before_send(make_form({ custom_noon_generate_awb: 1 })), true);
	assert.equal(msgprints.length, 1);
});

test("the cancel button shows on a draft note with a live shipment only", () => {
	const { mod } = load();
	for (const status of ["Created", "Cancellation Pending", "Cancellation Unknown"]) {
		const frm = make_form({ custom_integration_status: status });
		mod.add_buttons(frm);
		assert.deepEqual(frm.buttons.map((b) => b.label), ["Cancel Noon Shipment"], status);
	}
	for (const status of ["", "Not Created", "Creating", "Creation Unknown", "Creation Failed", "Cancelled"]) {
		const frm = make_form({ custom_integration_status: status });
		mod.add_buttons(frm);
		assert.deepEqual(frm.buttons, [], status);
	}
	// a submitted note has no cancel button here (the native Cancel runs before_cancel); it offers the invoice
	const submitted = make_form({ docstatus: 1, custom_integration_status: "Created" });
	mod.add_buttons(submitted);
	assert.deepEqual(submitted.buttons.map((b) => b.label), ["Upload Shipment Invoice"]);
});

test("Upload Shipment Invoice shows only on a submitted note whose shipment is Created", () => {
	const { mod } = load();
	for (const [doc, shown] of [
		[{ docstatus: 1, custom_integration_status: "Created" }, true],
		[{ docstatus: 1, custom_integration_status: "Cancelled" }, false],
		[{ docstatus: 1, custom_integration_status: "Cancellation Unknown" }, false],
		[{ docstatus: 0, custom_integration_status: "Created" }, false],
		[{ docstatus: 1, custom_integration_status: "" }, false],
	]) {
		const frm = make_form(doc);
		mod.add_buttons(frm);
		assert.equal(frm.buttons.some((b) => b.label === "Upload Shipment Invoice"), shown, JSON.stringify(doc));
	}
});

test("the shipment invoice uses the shared dialog with the note, the upload time and the right endpoint", async () => {
	const { mod, calls } = load({ responses: { upload_delivery_note_invoice: { queued: true } } });
	let started = null;
	global.frappe.marketplace_integrations.noon_invoice = { start: (opts) => (started = opts) };
	const frm = make_form({ docstatus: 1, custom_integration_status: "Created", custom_noon_invoice_uploaded_on: "2026-11-01 10:00:00" });
	mod.upload_shipment_invoice(frm);
	assert.equal(started.doctype, "Delivery Note");
	assert.equal(started.name, "MAT-DN-1");
	assert.equal(started.uploaded_on, "2026-11-01 10:00:00");
	await started.send({ invoice_file: "/private/files/i.pdf", invoice_nr: "INV-1", issued_at: "2026-11-01" }, "");
	await started.send({ invoice_file: "/private/files/i.pdf", invoice_nr: "INV-1", issued_at: "2026-11-01" }, "key-1");
	assert.deepEqual(calls[0].args, {
		delivery_note: "MAT-DN-1",
		file_url: "/private/files/i.pdf",
		invoice_nr: "INV-1",
		issued_at: "2026-11-01",
		idempotency_key: null,
	});
	assert.equal(calls[1].args.idempotency_key, "key-1");
	assert.match(calls[0].method, /noon_fulfillment\.upload_delivery_note_invoice$/);
});

test("the form shows when the invoice was uploaded, escaped", () => {
	const { mod } = load();
	const indicators = [];
	const frm = make_form({ docstatus: 1, custom_noon_invoice_uploaded_on: "2026-11-01 <b>" });
	frm.dashboard = { add_indicator: (text, color) => indicators.push({ text, color }) };
	mod.refresh(frm);
	// shown in the user's date format (the fake formatter prefixes "user:"), then escaped
	assert.equal(indicators[0].text, "Invoice uploaded on user:2026-11-01 &lt;b&gt;");
});

test("Upload Shipment Invoice is hidden without write permission on the note", () => {
	const { mod } = load();
	const frm = make_form({ docstatus: 1, custom_integration_status: "Created" });
	frm.perm = [{ read: 1, write: 0 }];
	mod.add_buttons(frm);
	assert.deepEqual(frm.buttons, []);
});

test("before_cancel ignores a note that is not a created Noon shipment", async () => {
	const { mod, calls } = load();
	assert.equal(mod.before_cancel(make_form({ transporter_name: "Bosta", custom_integration_status: "Created" })), undefined);
	assert.equal(mod.before_cancel(make_form({ custom_integration_status: "Cancelled", docstatus: 1 })), undefined);
	assert.equal(mod.before_cancel(make_form({ custom_integration_status: "" })), undefined);
	assert.deepEqual(calls, []);
});

test("before_cancel lets the native cancel go on once Noon cancelled, and reloads the form", async () => {
	const { mod, calls } = load({ responses: { cancel_shipment: { status: "Cancelled", outcome: "cancelled", message: "" } } });
	const frm = make_form({ docstatus: 1, custom_integration_status: "Created" });
	await mod.before_cancel(frm);
	assert.equal(frappe.validated, true);
	assert.equal(frm.reloads, 1);
	assert.equal(calls.length, 1);
	assert.ok(calls[0].method.endsWith("carrier_shipments.cancel_shipment"));
});

test("a Noon refusal stops the cancel and shows Noon's text escaped", async () => {
	const { mod, msgprints } = load({
		responses: { cancel_shipment: { status: "Created", outcome: "refused", message: "Not <b>allowed</b>" } },
	});
	const frm = make_form({ docstatus: 1, custom_integration_status: "Created" });
	await mod.before_cancel(frm);
	assert.equal(frappe.validated, false);
	// the server wrote the note while asking Noon: the form is loaded again so the next action is not stale
	assert.equal(frm.reloads, 1);
	assert.match(msgprints[0].message, /Noon refused: Not &lt;b&gt;allowed&lt;\/b&gt;/);
	assert.match(msgprints[0].message, /Nothing was cancelled in ERPNext/);
});

test("an unknown result keeps the note and offers Retry and Check with Noon", async () => {
	const { mod, dialogs } = load({
		responses: { cancel_shipment: { status: "Cancellation Unknown", outcome: "unknown", message: "timeout" } },
	});
	const frm = make_form({ docstatus: 1, custom_integration_status: "Created" });
	const pending = mod.before_cancel(frm);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(dialogs.length, 1);
	assert.equal(dialogs[0].config.primary_action_label, "Retry");
	assert.equal(dialogs[0].config.secondary_action_label, "Check with Noon");
	assert.match(dialogs[0].html, /timeout/);
	// closing without choosing ends the attempt: the cancel does not go on
	dialogs[0].hide();
	await pending;
	assert.equal(frappe.validated, false);
});

test("Retry asks the server again", async () => {
	let answers = [
		{ status: "Cancellation Unknown", outcome: "unknown", message: "timeout" },
		{ status: "Cancelled", outcome: "cancelled", message: "" },
	];
	const { mod, dialogs, calls } = load({ responses: { cancel_shipment: () => answers.shift() } });
	const frm = make_form({ docstatus: 1, custom_integration_status: "Created" });
	const pending = mod.before_cancel(frm);
	await new Promise((resolve) => setImmediate(resolve));
	await dialogs[0].config.primary_action();
	await pending;
	assert.equal(calls.length, 2);
	assert.equal(frappe.validated, true);
});

test("a Check that finds the note already cancelled automatically does not let the native cancel go on", async () => {
	const { mod, dialogs, alerts } = load({
		responses: { cancel_shipment: { outcome: "unknown", message: "" }, refresh_shipment: {} },
	});
	const frm = make_form({ docstatus: 1, custom_integration_status: "Created" });
	frm.reload_doc = function () {
		this.reloads++;
		this.doc.custom_integration_status = "Cancelled";
		this.doc.docstatus = 2;
		return Promise.resolve();
	};
	const pending = mod.before_cancel(frm);
	await new Promise((resolve) => setImmediate(resolve));
	dialogs[0].config.secondary_action();
	await pending;
	assert.equal(frappe.validated, false);
	assert.equal(alerts[0].indicator, "green");
});

test("the submitted-note cancel asks once, warning that Noon also cancels the items", async () => {
	const { mod, confirms, calls } = load({ responses: { cancel_shipment: { status: "Cancelled", outcome: "cancelled", message: "" } } });
	const frm = make_form({ docstatus: 1, custom_integration_status: "Created" });
	await mod.before_cancel(frm);
	assert.equal(confirms.length, 1);
	assert.match(confirms[0], /also cancels its items at Noon/);
	assert.match(confirms[0], /Sales Order lines may be cancelled or reduced/);
	assert.match(confirms[0], /may not be shippable again/);
	assert.equal(calls.length, 1);
	assert.equal(frappe.validated, true);
});

test("declining the warning sends nothing and stops the native cancel", async () => {
	const { mod, calls } = load();
	frappe.confirm = (message, yes, no) => no();
	const frm = make_form({ docstatus: 1, custom_integration_status: "Created" });
	await mod.before_cancel(frm);
	assert.equal(frappe.validated, false);
	assert.deepEqual(calls, []);
});

test("the draft Cancel Noon Shipment button confirms, cancels and reloads", async () => {
	const { mod, confirms, alerts } = load({ responses: { cancel_shipment: { status: "Cancelled", outcome: "cancelled", message: "" } } });
	const frm = make_form({ custom_integration_status: "Created" });
	await mod.cancel_shipment(frm);
	assert.equal(confirms.length, 1);
	assert.match(confirms[0], /also cancels its items at Noon/);
	assert.equal(frm.reloads, 1);
	assert.equal(alerts[0].indicator, "green");
});

test("a second cancel while one is in flight sends nothing and stops; the lock is released afterwards", async () => {
	const { mod, calls, alerts } = load({ responses: { cancel_shipment: { status: "Cancelled", outcome: "cancelled", message: "" } } });
	const shell = use_real_shell();
	const frm = make_form({ custom_integration_status: "Created" });
	const first = mod.before_cancel(frm);
	frappe.validated = true;
	await mod.before_cancel(frm);
	assert.equal(frappe.validated, false);
	assert.equal(calls.length, 1);
	// the click was not silent
	assert.equal(alerts.length, 1);
	assert.equal(alerts[0].indicator, "orange");
	await first;
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(shell.is_busy(frm), false);
});

test("Check with Noon while another request runs does nothing and ends the cancel attempt", async () => {
	const { mod, dialogs, calls } = load({ responses: { cancel_shipment: { outcome: "unknown", message: "" } } });
	const shell = use_real_shell();
	const frm = make_form({ custom_integration_status: "Created" });
	const pending = mod.before_cancel(frm);
	await new Promise((resolve) => setImmediate(resolve));
	let release;
	const holder = shell.exclusive(frm, () => new Promise((resolve) => (release = resolve)));
	await dialogs[0].config.secondary_action();
	assert.equal(calls.filter((c) => c.method.endsWith("refresh_shipment")).length, 0);
	await pending;
	assert.equal(frappe.validated, false);
	release();
	await holder;
});
