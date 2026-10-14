const assert = require("node:assert/strict");
const test = require("node:test");

let handlers;
let dialog;
const alerts = [];

global.__ = (value) => value;
global.frappe = {
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

function makeForm({ isNew = false, acknowledgedOn = null, canWrite = true } = {}) {
	return {
		doc: { receipt_acknowledged_on: acknowledgedOn },
		perm: [{ write: canWrite ? 1 : 0 }],
		is_new: () => isNew,
		add_custom_button(label, action) {
			this.button = { label, action };
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
		{ acknowledgedOn: "2026-10-14 10:00:00" },
		{ canWrite: false },
	]) {
		const form = makeForm(options);
		handlers.refresh(form);
		assert.equal(form.button, undefined);
	}

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
