const assert = require("node:assert/strict");
const test = require("node:test");

// Same mapping as frappe/public/js/frappe/utils/utils.js escape_html (Frappe 16): ' ` = are encoded too.
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
global.frappe = {
	utils: {
		escape_html: (value) =>
			value == null ? "" : String(value).replace(/[&<>"'`=]/g, (char) => ESCAPE_MAPPING[char] || char),
	},
};

const badge = require("./platform_badge.js");

test("the helper registers itself under frappe.marketplace_integrations", () => {
	assert.equal(global.frappe.marketplace_integrations.platform_badge, badge);
});

test("each known platform has its own built-in indicator color", () => {
	assert.equal(badge.color_of("Noon"), "yellow");
	assert.equal(badge.color_of("Shopify"), "green");
	assert.equal(badge.color_of("Talabat"), "orange");
});

test("an unknown platform is grey, including names that exist on every object", () => {
	for (const platform of ["Bosta", "InstaShop", "noon", "constructor", "toString", "__proto__"]) {
		assert.equal(badge.color_of(platform), "grey", platform);
	}
});

test("the pill is translated and nothing renders for an empty value", () => {
	assert.equal(badge.pill("Noon"), '<span class="indicator-pill yellow">Noon</span>');
	for (const empty of ["", null, undefined]) {
		assert.equal(badge.pill(empty), "");
		assert.equal(badge.formatter(empty, { fieldname: "platform" }), "");
		assert.equal(badge.carrier_pill(empty), "");
		assert.equal(badge.carrier_formatter(empty, { fieldname: "transporter" }), "");
	}
});

test("the formatter output for each platform keeps the click-to-filter link and names the column in a tooltip", () => {
	const cases = [
		["Noon", "yellow"],
		["Shopify", "green"],
		["Talabat", "orange"],
		["Other", "grey"],
	];
	for (const [platform, color] of cases) {
		assert.equal(
			badge.formatter(platform, { fieldname: "custom_marketplace_platform", label: "Marketplace Platform" }),
			`<a class="filterable ellipsis" data-filter="custom_marketplace_platform,&#x3D;,${platform}" ` +
				`title="Marketplace Platform: ${platform}">` +
				`<span class="indicator-pill ${color}">${platform}</span></a>`
		);
	}
	assert.match(badge.formatter("Noon"), /data-filter="platform,&#x3D;,Noon"/);
	assert.match(badge.formatter("Noon"), /title="Noon"/);
});

test("the tooltip uses the translated label and value", () => {
	const previous = global.__;
	global.__ = (message) => ({ Platform: "المنصة", Noon: "نون" })[message] || message;
	try {
		assert.match(badge.formatter("Noon", { fieldname: "platform", label: "Platform" }), /title="المنصة: نون"/);
	} finally {
		global.__ = previous;
	}
});

test("the value is escaped in the pill text, the filter attribute and the tooltip", () => {
	const html = badge.formatter('"><img src=x onerror=alert(1)>', { fieldname: "platform", label: 'La"bel' });
	assert.ok(!html.includes("<img"), html);
	assert.equal((html.match(/</g) || []).length, 4, html); // only the <a>, <span>, </span>, </a> we wrote
	assert.match(html, /&lt;img src&#x3D;x onerror&#x3D;alert\(1\)&gt;/);
	assert.match(html, /data-filter="platform,&#x3D;,&quot;&gt;&lt;img src&#x3D;x onerror&#x3D;alert\(1\)&gt;"/);
	assert.match(html, /title="La&quot;bel: &quot;&gt;&lt;img/);
	assert.match(html, /indicator-pill grey/);
	// quotes, backticks and equals signs cannot break out of an attribute
	const tricky = badge.formatter("a'b`c=d", { fieldname: "platform" });
	assert.match(tricky, /data-filter="platform,&#x3D;,a&#39;b&#x60;c&#x3D;d"/);
});

test("each courier has a color: Noon yellow, Bosta red, inDrive green, everything else grey", () => {
	assert.equal(badge.carrier_color_of("Noon"), "yellow");
	assert.equal(badge.carrier_color_of("Bosta"), "red");
	assert.equal(badge.carrier_color_of("inDrive"), "green");
	assert.equal(badge.carrier_pill("inDrive"), '<span class="indicator-pill green">inDrive</span>');
	for (const carrier of ["Uber", "Own driver", "Aramex", "noon", "indrive", "Shopify", "constructor", "__proto__"]) {
		assert.equal(badge.carrier_color_of(carrier), "grey", carrier);
	}
	assert.equal(badge.carrier_pill("Bosta"), '<span class="indicator-pill red">Bosta</span>');
});

test("the courier formatter filters on the transporter field", () => {
	assert.equal(
		badge.carrier_formatter("Noon", { fieldname: "transporter", label: "Carrier" }),
		'<a class="filterable ellipsis" data-filter="transporter,&#x3D;,Noon" title="Carrier: Noon">' +
			'<span class="indicator-pill yellow">Noon</span></a>'
	);
	assert.match(badge.carrier_formatter("Bosta"), /data-filter="transporter,&#x3D;,Bosta"/);
});
