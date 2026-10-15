const assert = require("node:assert/strict");
const test = require("node:test");

let handlers;
let dialog;
const alerts = [];

global.__ = (value) => value;
let route;
let docstatus = 1;
global.frappe = {
	db: {
		get_value: () => Promise.resolve({ message: { docstatus } }),
	},
	set_route(...args) {
		route = args;
	},
	ui: {
		Dialog: class {
			constructor(options) {
				this.options = options;
				dialog = this;
			}

			hide() {
				this.hidden = true;
			}

			show() {
				this.shown = true;
			}
		},
		form: {
			on(doctype, registeredHandlers) {
				assert.equal(doctype, "Noon Return");
				handlers = registeredHandlers;
			},
		},
	},
	show_alert(alert) {
		alerts.push(alert);
	},
};

require("./noon_return.js");

function makeForm({ isNew = false, acknowledgedOn = null, canWrite = true, doc = {} } = {}) {
	return {
		doc: { receipt_acknowledged_on: acknowledgedOn, ...doc },
		perm: [{ write: canWrite ? 1 : 0 }],
		is_new: () => isNew,
		add_custom_button(label, action) {
			this.button = { label, action };
			(this.buttons = this.buttons || []).push(label);
			(this.actions = this.actions || {})[label] = action;
		},
		call(method, args) {
			this.callArgs = { method, args };
			return Promise.resolve({ message: { already_acknowledged: false } });
		},
		reload_doc() {
			this.reloaded = true;
		},
	};
}

test("adds the internal acknowledgement action only when the return is writable and unacknowledged", () => {
	for (const options of [
		{ isNew: true },
		{ canWrite: false },
	]) {
		const form = makeForm(options);
		handlers.refresh(form);
		assert.equal(form.button, undefined);
	}
	const acknowledged = makeForm({ acknowledgedOn: "2026-10-14 10:00:00" });
	handlers.refresh(acknowledged);
	assert.ok(!acknowledged.buttons.includes("Acknowledge Return Received"));

	const form = makeForm();
	handlers.refresh(form);
	assert.equal(form.button.label, "Acknowledge Return Received");
});

test("submits the optional internal note and refreshes the return", async () => {
	alerts.length = 0;
	const form = makeForm();
	handlers.refresh(form);
	form.button.action();

	assert.equal(dialog.shown, true);
	await dialog.options.primary_action({ receipt_note: "Received at warehouse" });
	assert.deepEqual(form.callArgs, {
		method: "acknowledge_receipt",
		args: { receipt_note: "Received at warehouse" },
	});
	assert.equal(dialog.hidden, true);
	assert.equal(form.reloaded, true);
	assert.equal(alerts.at(-1).message, "Return receipt acknowledged internally.");
});

test("Record Inspection is offered once acknowledged and calls record_inspection", async () => {
	alerts.length = 0;
	const form = makeForm({ acknowledgedOn: "2026-10-14 10:00:00" });
	handlers.refresh(form);
	assert.deepEqual(form.buttons, ["Record Inspection"]);
	form.button.action();
	await dialog.options.primary_action({ outcome: "Damaged" });
	assert.deepEqual(form.callArgs, { method: "record_inspection", args: { outcome: "Damaged" } });
	assert.equal(alerts.at(-1).message, "Inspection recorded.");
});

test("a recorded outcome hides Record Inspection and a Sales Return can be opened", () => {
	const form = makeForm({
		acknowledgedOn: "2026-10-14 10:00:00",
		doc: { inspection_outcome: "Sellable", sales_return: "MAT-DN-0001" },
	});
	handlers.refresh(form);
	assert.deepEqual(form.buttons, ["Open Sales Return", "Create Credit Note"]);
	form.actions["Open Sales Return"]();
	assert.deepEqual(route, ["Form", "Delivery Note", "MAT-DN-0001"]);
});

test("Create Credit Note confirms, calls the server and Open Credit Note replaces it", async () => {
	alerts.length = 0;
	let confirmed;
	global.frappe.confirm = (_message, action) => {
		confirmed = action;
	};
	const form = makeForm({ acknowledgedOn: "2026-10-14 10:00:00", doc: { inspection_outcome: "Damaged" } });
	handlers.refresh(form);
	assert.deepEqual(form.buttons, ["Create Credit Note"]);
	form.actions["Create Credit Note"]();
	await confirmed();
	assert.equal(form.callArgs.method, "create_credit_note");
	assert.equal(alerts.at(-1).message, "Draft Credit Note created.");

	const linked = makeForm({
		acknowledgedOn: "2026-10-14 10:00:00",
		doc: { inspection_outcome: "Damaged", credit_note: "ACC-SINV-0001" },
	});
	handlers.refresh(linked);
	assert.deepEqual(linked.buttons, ["Open Credit Note"]);
	linked.actions["Open Credit Note"]();
	assert.deepEqual(route, ["Form", "Sales Invoice", "ACC-SINV-0001"]);
});

test("a cancelled Credit Note or Sales Return can be created again", async () => {
	docstatus = 2;
	const form = makeForm({
		acknowledgedOn: "2026-10-14 10:00:00",
		doc: { inspection_outcome: "Sellable", sales_return: "MAT-DN-0002", credit_note: "ACC-SINV-0002" },
	});
	handlers.refresh(form);
	await new Promise((resolve) => setTimeout(resolve, 0));
	docstatus = 1;
	assert.ok(form.buttons.includes("Create Credit Note"));
	assert.ok(form.buttons.includes("Create Sales Return"));

	const live = makeForm({
		acknowledgedOn: "2026-10-14 10:00:00",
		doc: { inspection_outcome: "Sellable", sales_return: "MAT-DN-0003", credit_note: "ACC-SINV-0003" },
	});
	handlers.refresh(live);
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.ok(!live.buttons.includes("Create Credit Note"));
	assert.ok(!live.buttons.includes("Create Sales Return"));
});

test("a late cancelled-document lookup adds no button once the form shows another record", async () => {
	docstatus = 2;
	const form = makeForm({
		acknowledgedOn: "2026-10-14 10:00:00",
		doc: { inspection_outcome: "Sellable", sales_return: "MAT-DN-0004", credit_note: "ACC-SINV-0004" },
	});
	handlers.refresh(form);
	form.doc.credit_note = "ACC-SINV-0005";
	form.doc.sales_return = "MAT-DN-0005";
	await new Promise((resolve) => setTimeout(resolve, 0));
	docstatus = 1;
	assert.ok(!(form.buttons || []).includes("Create Credit Note"));
	assert.ok(!(form.buttons || []).includes("Create Sales Return"));
});
