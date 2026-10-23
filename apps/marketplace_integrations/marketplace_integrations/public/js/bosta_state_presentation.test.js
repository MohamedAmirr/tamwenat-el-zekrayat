const assert = require("node:assert/strict");
const test = require("node:test");

const { bosta_state_presentation } = require("./bosta_state_presentation.js");

test("SEND states map to the five outbound progress steps", () => {
	assert.deepEqual([10, 20, 21, 24, 30, 41, 45].map((state) => bosta_state_presentation("SEND", state, "state").current_step), [0, 0, 1, 2, 2, 3, 4]);
});

test("Bosta problem and return states carry a readable hint", () => {
	assert.equal(bosta_state_presentation("SEND", 103, "Awaiting your action", "Call the recipient").hint, "Call the recipient");
	assert.equal(bosta_state_presentation("SEND", 46, "Returned to business").kind, "returned");
	assert.equal(bosta_state_presentation("SEND", 60, "Returned to stock").kind, "returned");
	assert.match(bosta_state_presentation("SEND", 45, "Delivered", undefined, false).hint, /no submitted Delivery Note/);
	assert.equal(bosta_state_presentation("SEND", 45, "Delivered", undefined, true).hint, "");
	assert.equal(bosta_state_presentation("SEND", 45, "Delivered").hint, "");
});

test("other delivery types keep their documented raw state", () => {
	assert.deepEqual(bosta_state_presentation("EXCHANGE", 41, "Picked up"), { kind: "state", label: "Picked up", hint: "" });
});
