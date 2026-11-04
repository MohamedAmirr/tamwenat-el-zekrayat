const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message) => message;

function load(existing = {}) {
	global.frappe = {
		utils: { escape_html: (value) => String(value).replace(/</g, "&lt;") },
		listview_settings: { "Delivery Note": existing },
	};
	const helper = require.resolve("./carrier_status.js");
	delete require.cache[helper];
	require(helper);
	const path = require.resolve("./delivery_note_list.js");
	delete require.cache[path];
	const module_exports = require(path);
	return { module_exports, settings: global.frappe.listview_settings["Delivery Note"] };
}

test("the carrier status column renders a colored pill for every confirmed color", () => {
	const { settings } = load();
	const format = settings.formatters.custom_shipping_status;
	assert.equal(format("Delivered"), '<span class="indicator-pill green">Delivered</span>');
	assert.equal(format("Shipped"), '<span class="indicator-pill blue">Shipped</span>');
	assert.equal(format("On hold"), '<span class="indicator-pill orange">On hold</span>');
	assert.equal(format("Out of Stock"), '<span class="indicator-pill red">Out of Stock</span>');
	assert.equal(format("Cancelled"), '<span class="indicator-pill grey">Cancelled</span>');
});

test("a note without a carrier status shows nothing extra", () => {
	const { settings } = load();
	for (const value of ["", null, undefined]) assert.equal(settings.formatters.custom_shipping_status(value), "");
});

test("ERPNext's own fields and formatters are kept", () => {
	const kept = () => "kept";
	const get_indicator = () => ["x", "y"];
	const { settings } = load({ add_fields: ["status"], formatters: { other: kept }, get_indicator });
	assert.deepEqual(settings.add_fields, ["status", "custom_shipping_status"]);
	assert.equal(settings.formatters.other, kept);
	assert.equal(settings.get_indicator, get_indicator);
});
