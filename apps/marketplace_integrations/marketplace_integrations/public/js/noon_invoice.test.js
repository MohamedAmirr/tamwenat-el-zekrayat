const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

// Node has no DOMParser: a stand-in that returns the text of the markup.
global.DOMParser = class {
	parseFromString(html) {
		return { body: { textContent: html.replace(/<[^>]*>/g, "") } };
	}
};

const escape_html = (value) =>
	String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const API = "marketplace_integrations.api.noon_fulfillment.";

// Loads noon_invoice.js against a mock frappe. `script(opts)` answers frappe.call: a value (resolved as
// {message}) or throw {rejected: error}.
function load(script = () => ({})) {
	const calls = [];
	const dialogs = [];
	const alerts = [];
	const msgprints = [];
	const confirms = [];
	global.frappe = {
		utils: { escape_html },
		datetime: { str_to_user: (value) => `user:${value}` },
		ui: {
			Dialog: class {
				constructor(config) {
					this.config = config;
					this.hidden = false;
					this.html = "";
					this.fields_dict = { body: { $wrapper: { html: (value) => (this.html = value) } } };
					dialogs.push(this);
				}
				show() {}
				hide() {
					this.hidden = true;
				}
			},
		},
		call(opts) {
			calls.push(opts);
			try {
				return Promise.resolve({ message: script(opts) });
			} catch (error) {
				return Promise.reject(error.rejected);
			}
		},
		msgprint: (message) => msgprints.push(message),
		confirm: (message, proceed) => confirms.push({ message, proceed }),
		show_alert: (alert) => alerts.push(alert),
	};
	const path = require.resolve("./noon_invoice.js");
	delete require.cache[path];
	const invoice = require(path);
	invoice.timing.poll_ms = 1;
	invoice.timing.max_wait_ms = 50;
	return { invoice, calls, dialogs, alerts, msgprints, confirms };
}

async function settle() {
	for (let i = 0; i < 8; i++) {
		await new Promise((resolve) => setTimeout(resolve, 3));
	}
}

const values = { invoice_file: "/private/files/loose.pdf", invoice_nr: "INV-1", issued_at: "2026-11-01" };

// The upload call of a Delivery Note, as delivery_note_noon.js sends it: key empty = a new press.
function options(extra = {}) {
	const sent = [];
	const reloads = [];
	return {
		sent,
		reloads,
		doctype: "Delivery Note",
		name: "DN-1",
		title: "Upload Shipment Invoice",
		send: (form_values, key) => {
			sent.push({ values: form_values, key });
			return global.frappe.call({ method: API + "upload_delivery_note_invoice", args: { key } });
		},
		reload: () => reloads.push(1),
		...extra,
	};
}

test("the form asks for the PDF, the invoice number and the issue date, all required", () => {
	const { invoice, dialogs } = load();
	invoice.start(options());
	const fields = dialogs[0].config.fields;
	assert.deepEqual(
		fields.map((field) => [field.fieldname, field.fieldtype, field.reqd]),
		[
			["invoice_file", "Attach", 1],
			["invoice_nr", "Data", 1],
			["issued_at", "Date", 1],
		]
	);
	assert.equal(dialogs[0].config.title, "Upload Shipment Invoice");
});

test("a second upload warns first that the new one replaces the earlier one; the form opens only after Continue", () => {
	const { invoice, dialogs, confirms } = load();
	invoice.start(options({ uploaded_on: "2026-11-01 10:00:00" }));
	assert.equal(dialogs.length, 0);
	assert.equal(confirms.length, 1);
	assert.match(confirms[0].message, /already uploaded on user:2026-11-01 10:00:00/);
	// the date is escaped where shown
	invoice.start(options({ uploaded_on: "<b>" }));
	assert.match(confirms[1].message, /uploaded on user:&lt;b&gt;/);
	assert.match(confirms[0].message, /replaces it at Noon/);
	confirms[0].proceed();
	assert.equal(dialogs.length, 1);
});

test("a first upload shows no warning", () => {
	const { invoice, confirms, dialogs } = load();
	invoice.start(options());
	assert.equal(confirms.length, 0);
	assert.equal(dialogs.length, 1);
});

test("a new press sends an empty key; success shows a small alert and reloads the form", async () => {
	const polls = [{ state: "pending" }, { state: "done" }];
	const { invoice, dialogs, alerts } = load((opts) => {
		if (opts.method === API + "upload_delivery_note_invoice") return { queued: true, idempotency_key: "key-1", file_url: "/private/files/dn.pdf" };
		return polls.shift() || { state: "done" };
	});
	const opts = options();
	invoice.start(opts);
	dialogs[0].config.primary_action(values);
	await settle();
	assert.deepEqual(opts.sent, [{ values, key: "" }]);
	assert.equal(dialogs[1].config.title, "Sending to Noon...");
	assert.equal(dialogs[1].hidden, true);
	assert.equal(alerts[0].indicator, "green");
	assert.equal(opts.reloads.length, 1);
});

test("a failure shows Noon's message escaped with Retry; Retry reuses the key and sends the attached copy", async () => {
	let attempt = 0;
	const { invoice, dialogs, msgprints, calls } = load((opts) => {
		if (opts.method === API + "upload_delivery_note_invoice") {
			attempt += 1;
			return { queued: true, idempotency_key: "key-1", file_url: "/private/files/dn.pdf" };
		}
		return attempt === 1 ? { state: "failed", message: "<script>x</script> bad" } : { state: "done" };
	});
	const opts = options();
	invoice.start(opts);
	dialogs[0].config.primary_action(values);
	await settle();
	assert.equal(opts.reloads.length, 0, "nothing reloads or blocks after a failure");
	assert.match(msgprints[0].message, /&lt;script&gt;x&lt;\/script&gt; bad/);
	assert.doesNotMatch(msgprints[0].message, /<script>/);
	assert.equal(msgprints[0].primary_action.label, "Retry");
	msgprints[0].primary_action.action();
	await settle();
	assert.equal(opts.sent.length, 2);
	assert.equal(opts.sent[1].key, "key-1", "Retry reuses the key of the press");
	assert.equal(opts.sent[1].values.invoice_file, "/private/files/dn.pdf");
	assert.equal(opts.reloads.length, 1);
	assert.equal(calls.filter((call) => call.method === API + "check_invoice_upload").length >= 2, true);
});

test("a new press after a success sends an empty key again (a new idempotency key)", async () => {
	const { invoice, dialogs } = load((opts) =>
		opts.method === API + "upload_delivery_note_invoice" ? { queued: true, idempotency_key: "key-n" } : { state: "done" }
	);
	const opts = options();
	invoice.start(opts);
	dialogs[0].config.primary_action(values);
	await settle();
	invoice.start(opts);
	dialogs[dialogs.length - 1].config.primary_action(values);
	await settle();
	assert.deepEqual(opts.sent.map((press) => press.key), ["", ""]);
});

test("the poll names the document the button was used on", async () => {
	const { invoice, dialogs, calls } = load((opts) =>
		opts.method === API + "upload_delivery_note_invoice" ? { queued: true, idempotency_key: "key-1" } : { state: "done" }
	);
	invoice.start(options());
	dialogs[0].config.primary_action(values);
	await settle();
	const poll = calls.find((call) => call.method === API + "check_invoice_upload");
	assert.deepEqual(poll.args, { document_type: "Delivery Note", document_name: "DN-1", idempotency_key: "key-1" });
});

test("a server refusal is shown escaped and never polls", async () => {
	const messages = JSON.stringify([JSON.stringify({ message: "Not <b>FBPI</b> & refused." })]);
	const { invoice, dialogs, msgprints, calls } = load(() => {
		throw { rejected: { responseJSON: { _server_messages: messages } } };
	});
	invoice.start(options());
	dialogs[0].config.primary_action(values);
	await settle();
	assert.equal(calls.filter((call) => call.method === API + "check_invoice_upload").length, 0);
	assert.equal(msgprints[0].message, "Not FBPI &amp; refused.");
	assert.equal(dialogs[1].hidden, true);
});

test("a deduplicated press says an action is already queued and does not poll", async () => {
	const { invoice, dialogs, alerts, calls } = load(() => ({ queued: false }));
	invoice.start(options());
	dialogs[0].config.primary_action(values);
	await settle();
	assert.equal(calls.filter((call) => call.method === API + "check_invoice_upload").length, 0);
	assert.equal(alerts[0].message, "A Noon fulfillment action for this order is already queued or running.");
});

test("a wait that outlasts the limit tells the user the job keeps running", async () => {
	const { invoice, dialogs, alerts } = load((opts) =>
		opts.method === API + "upload_delivery_note_invoice" ? { queued: true, idempotency_key: "key-1" } : { state: "pending" }
	);
	invoice.start(options());
	dialogs[0].config.primary_action(values);
	await new Promise((resolve) => setTimeout(resolve, 120));
	assert.match(alerts[0].message, /Still sending to Noon/);
});
