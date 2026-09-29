const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message) => message;
let handlers;
const alerts = [];
global.frappe = {
	ui: { form: { on(_doctype, events) { handlers = events; } } },
	show_alert(alert) { alerts.push(alert); },
};

require("./noon_connection.js");

test("Test Connection is offered only after the document is saved", () => {
	const buttons = [];
	handlers.refresh({ is_new: () => true, add_custom_button: (label) => buttons.push(label) });
	assert.deepEqual(buttons, []);
	handlers.refresh({ is_new: () => false, add_custom_button: (label) => buttons.push(label) });
	assert.deepEqual(buttons, ["Test Connection"]);
});

test("Test Connection calls the server method and shows only a success indicator", async () => {
	alerts.length = 0;
	let button;
	let method;
	const frm = {
		is_new: () => false,
		add_custom_button(_label, action) { button = action; },
		call(name) {
			method = name;
			return Promise.resolve({ message: { connected: true } });
		},
	};
	handlers.refresh(frm);
	button();
	await Promise.resolve();
	assert.equal(method, "test_connection");
	assert.deepEqual(alerts, [{ message: "Noon connection succeeded.", indicator: "green" }]);
});
