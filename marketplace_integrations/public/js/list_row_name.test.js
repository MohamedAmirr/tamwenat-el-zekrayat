const assert = require("node:assert/strict");
const test = require("node:test");

const { attach_to_list } = require("./list_row_name.js");

function attach() {
	const calls = [];
	attach_to_list({
		$result: {
			on(events, selector, fn) {
				calls.push({ events, selector, fn });
			},
		},
	});
	return calls;
}

test("one delegated handler is registered on the list result", () => {
	const calls = attach();
	assert.equal(calls.length, 1);
	assert.equal(calls[0].events, "mouseenter focusin");
	assert.equal(calls[0].selector, ".list-subject a[data-name]");
});

test("the tooltip shows the readable title with the docname, set on hover", () => {
	const [{ fn }] = attach();
	const link = { textContent: " Acme Store ", dataset: { name: "MKTACC-00001" }, title: "" };
	fn.call(link);
	assert.equal(link.title, "Acme Store (MKTACC-00001)");
});

test("the tooltip is skipped when the subject already is the docname", () => {
	const [{ fn }] = attach();
	const link = { textContent: "MKTACC-00002", dataset: { name: "MKTACC-00002" }, title: "" };
	fn.call(link);
	assert.equal(link.title, "");
});

test("a list without a result container is ignored", () => {
	assert.doesNotThrow(() => attach_to_list({}));
	assert.doesNotThrow(() => attach_to_list(null));
});
