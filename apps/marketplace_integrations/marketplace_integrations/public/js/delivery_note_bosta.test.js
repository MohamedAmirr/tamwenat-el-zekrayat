const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

function load({ cities = [{ id: "c1", name: "Cairo", name_ar: "القاهرة" }], suggested = { city_id: "c1" } } = {}) {
	const registered = {};
	const calls = [];
	const dialogs = [];
	const msgprints = [];
	const alerts = [];
	global.frappe = {
		marketplace_integrations: { shipping: { register_carrier: (name, hooks) => (registered[name] = hooks) } },
		ui: {
			Dialog: class {
				constructor(config) {
					this.config = config;
					dialogs.push(this);
				}
				show() {}
				hide() {
					this.hidden = true;
				}
			},
		},
		msgprint: (opts) => msgprints.push(opts),
		show_alert: (opts) => alerts.push(opts),
		call: (opts) => {
			calls.push(opts);
			if (opts.method.endsWith("get_cities")) return Promise.resolve({ message: { cities, suggested } });
			if (opts.method.endsWith("resolve_city")) return Promise.resolve({ message: { code: "EG-01", name: "Cairo" } });
			return Promise.resolve({ message: {} });
		},
	};
	const path = require.resolve("./delivery_note_bosta.js");
	delete require.cache[path];
	const mod = require(path);
	return { mod, registered, calls, dialogs, msgprints, alerts };
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
});

test("the city button shows only while the shipment can still be created", () => {
	const { mod } = load();
	for (const status of ["", "Not Created", "Creation Failed"]) {
		const frm = make_form({ custom_integration_status: status });
		mod.add_buttons(frm);
		assert.deepEqual(frm.buttons.map((b) => b.label), ["Pick Bosta City"], status);
	}
	for (const status of ["Creating", "Creation Unknown", "Created"]) {
		const frm = make_form({ custom_integration_status: status, custom_shipping_label: "x" });
		mod.add_buttons(frm);
		assert.deepEqual(frm.buttons, [], status);
	}
});

test("the label button shows for a Created note without a label", () => {
	const { mod } = load();
	const frm = make_form({ custom_integration_status: "Created", docstatus: 1 });
	mod.add_buttons(frm);
	assert.deepEqual(frm.buttons.map((b) => b.label), ["Fetch Bosta Label"]);
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
