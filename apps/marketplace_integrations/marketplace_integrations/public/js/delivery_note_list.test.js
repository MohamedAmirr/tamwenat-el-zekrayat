const assert = require("node:assert/strict");
const test = require("node:test");

// Same mapping as frappe/public/js/frappe/utils/utils.js escape_html (Frappe 16).
const ESCAPE_MAPPING = {
	"&": "&amp;",
	"<": "&lt;",
	">": "&gt;",
	'"': "&quot;",
	"'": "&#39;",
	"`": "&#x60;",
	"=": "&#x3D;",
};

global.__ = (message) => message;

function load(existing = {}, { with_badge = true } = {}) {
	global.frappe = {
		utils: {
			escape_html: (value) =>
				value == null ? "" : String(value).replace(/[&<>"'`=]/g, (char) => ESCAPE_MAPPING[char] || char),
		},
		listview_settings: { "Delivery Note": existing },
	};
	for (const file of with_badge ? ["./carrier_status.js", "./platform_badge.js"] : ["./carrier_status.js"]) {
		const helper = require.resolve(file);
		delete require.cache[helper];
		require(helper);
	}
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
	assert.deepEqual(settings.add_fields, ["status", "custom_shipping_status", "transporter"]);
	assert.equal(settings.formatters.other, kept);
	assert.equal(settings.get_indicator, get_indicator);
});

const DF = { fieldname: "transporter", label: "Carrier" };
const pill = (color, name) =>
	`<a class="filterable ellipsis" data-filter="transporter,&#x3D;,${name}" title="Carrier: ${name}">` +
	`<span class="indicator-pill ${color}">${name}</span></a>`;

test("the Carrier column is a yellow Noon, red Bosta and grey for any other courier", () => {
	const { settings } = load();
	const format = settings.formatters.transporter;
	assert.equal(format("Noon", DF), pill("yellow", "Noon"));
	assert.equal(format("Bosta", DF), pill("red", "Bosta"));
	assert.equal(format("Manual delivery", DF), pill("grey", "Manual delivery"));
	assert.equal(format("inDrive", DF), pill("grey", "inDrive"));
	assert.equal(format("Uber", DF), pill("grey", "Uber"));
	assert.equal(format("Own driver", DF), pill("grey", "Own driver"));
	assert.equal(format("Aramex", DF), pill("grey", "Aramex"));
});

test("a note without a carrier shows nothing in the Carrier column", () => {
	const { settings } = load();
	for (const value of ["", null, undefined]) assert.equal(settings.formatters.transporter(value, DF), "");
});

test("a hostile carrier name is escaped in the pill, the filter and the tooltip", () => {
	const { settings } = load();
	const html = settings.formatters.transporter('"><img src=x onerror=alert(1)>', DF);
	assert.ok(!html.includes("<img"), html);
	assert.equal((html.match(/</g) || []).length, 4, html);
	assert.match(html, /data-filter="transporter,&#x3D;,&quot;&gt;&lt;img/);
	assert.match(html, /title="Carrier: &quot;&gt;&lt;img/);
});

test("without the shared badge helper the Carrier column falls back to escaped text", () => {
	const { settings } = load({}, { with_badge: false });
	assert.equal(settings.formatters.transporter("<b>", DF), "&lt;b&gt;");
	assert.equal(settings.formatters.transporter("", DF), "");
});
