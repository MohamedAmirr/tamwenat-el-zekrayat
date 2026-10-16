const assert = require("node:assert/strict");
const test = require("node:test");

const escape_html = (value) =>
	String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function load(call_impl) {
	const state = { handlers: null, calls: [], dialogs: [], msgprints: [] };
	global.__ = (message) => message;
	global.frappe = {
		utils: { escape_html },
		msgprint: (message) => state.msgprints.push(message),
		call: (options) => {
			state.calls.push(options);
			return call_impl();
		},
		ui: {
			form: { on: (_doctype, handlers) => (state.handlers = handlers) },
			Dialog: class {
				constructor(options) {
					this.options = options;
					state.dialogs.push(this);
				}
				show() {}
				hide() {
					this.hidden = true;
				}
			},
		},
	};
	const path = require.resolve("./bosta_settings.js");
	delete require.cache[path];
	state.module_exports = require(path);
	return state;
}

function form(doc, dirty = false) {
	const frm = {
		doc,
		buttons: {},
		values: {},
		add_custom_button(label, action) {
			frm.buttons[label] = action;
		},
		is_dirty: () => dirty,
		set_value(field, value) {
			frm.values[field] = value;
		},
	};
	return frm;
}

const LOCATIONS = [
	{ id: "a", name: "Main <b>", address: "1 St", city: "Cairo", is_default: false },
	{ id: "b", name: "Default", address: "", city: "", is_default: true },
];
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

test("Fetch Locations with no saved API key explains and makes no request", () => {
	const state = load(() => Promise.resolve({ message: LOCATIONS }));
	const frm = form({ api_key: "" });
	state.handlers.refresh(frm);
	frm.buttons["Fetch Locations"]();
	assert.equal(state.calls.length, 0);
	assert.match(state.msgprints[0], /API key/);
});

test("Fetch Locations asks to save pending changes first and makes no request", () => {
	const state = load(() => Promise.resolve({ message: LOCATIONS }));
	const frm = form({ api_key: "********" }, true);
	state.handlers.refresh(frm);
	frm.buttons["Fetch Locations"]();
	assert.equal(state.calls.length, 0);
	assert.match(state.msgprints[0], /Save your changes/);
});

test("the list preselects the default only when no location is set, with escaped labels", async () => {
	const state = load(() => Promise.resolve({ message: LOCATIONS }));
	const frm = form({ api_key: "********", business_location_id: "" });
	state.handlers.refresh(frm);
	frm.buttons["Fetch Locations"]();
	await tick();
	assert.equal(state.calls[0].method, "marketplace_integrations.api.bosta_settings.get_pickup_locations");
	const field = state.dialogs[0].options.fields[0];
	assert.equal(field.default, "b");
	assert.equal(field.options[1].label, "Main &lt;b&gt; - 1 St, Cairo");
	assert.equal(field.options[1].value, "a");

	// A set ID that is in the list stays selected; one that is not is never replaced by the default.
	for (const [current, expected] of [
		["a", "a"],
		["gone", ""],
	]) {
		const other = form({ api_key: "********", business_location_id: current });
		state.handlers.refresh(other);
		other.buttons["Fetch Locations"]();
		await tick();
		assert.equal(state.dialogs.at(-1).options.fields[0].default, expected);
	}
});

test("choosing a location sets Business Location ID", async () => {
	const state = load(() => Promise.resolve({ message: LOCATIONS }));
	const frm = form({ api_key: "********" });
	state.handlers.refresh(frm);
	frm.buttons["Fetch Locations"]();
	await tick();
	const dialog = state.dialogs[0];
	dialog.options.primary_action({ location: "a" });
	assert.equal(frm.values.business_location_id, "a");
	assert.ok(dialog.hidden);
});

test("an empty list leaves the saved ID alone", async () => {
	const state = load(() => Promise.resolve({ message: [] }));
	const frm = form({ api_key: "********", business_location_id: "kept" });
	state.handlers.refresh(frm);
	frm.buttons["Fetch Locations"]();
	await tick();
	assert.equal(state.dialogs.length, 0);
	assert.deepEqual(frm.values, {});
	assert.match(state.msgprints[0], /no pickup locations/);
});

test("a Bosta error is shown escaped with Retry and never touches the saved ID", async () => {
	const server = JSON.stringify([JSON.stringify({ message: "Bosta request failed (HTTP 401): &lt;img src=x&gt;" })]);
	let attempts = 0;
	const state = load(() => {
		attempts += 1;
		return Promise.reject({ responseJSON: { _server_messages: server } });
	});
	const frm = form({ api_key: "********", business_location_id: "kept" });
	state.handlers.refresh(frm);
	frm.buttons["Fetch Locations"]();
	await tick();
	const dialog = state.dialogs[0];
	assert.equal(dialog.options.primary_action_label, "Retry");
	assert.equal(
		dialog.options.fields[0].options,
		'<p class="text-danger">Bosta request failed (HTTP 401): &lt;img src=x&gt;</p>'
	);
	dialog.options.primary_action();
	await tick();
	assert.equal(attempts, 2);
	assert.deepEqual(frm.values, {});
});

test("server_error_text falls back when there is no server message", () => {
	const state = load(() => Promise.resolve({}));
	assert.equal(state.module_exports.server_error_text({}), "Could not load the Bosta pickup locations.");
});
