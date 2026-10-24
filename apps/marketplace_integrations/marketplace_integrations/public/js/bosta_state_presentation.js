// Presentation only. Carrier states and delivery types are persisted by the
// integration service; this module maps them to the Sales Order tab's progress UI.
(function () {
const BOSTA_SEND_STEPS = [
	{ label: "Created", states: [10, 20] },
	{ label: "Picked up", states: [21] },
	{ label: "In transit", states: [24, 30] },
	{ label: "Out for delivery", states: [41] },
	{ label: "Delivered", states: [45] },
];

const BOSTA_PROBLEM_STATES = new Set([47, 48, 49, 100, 101, 102, 103, 105]);
const BOSTA_RETURN_STATES = new Set([46, 60]);

function bosta_state_presentation(delivery_type, state_code, state_name, exception_reason, delivery_note_posted) {
	const code = Number(state_code);
	if (BOSTA_PROBLEM_STATES.has(code)) {
		return {
			kind: "problem",
			label: state_name || "Bosta exception",
			hint: exception_reason || "Review this shipment in Bosta.",
		};
	}
	if (BOSTA_RETURN_STATES.has(code)) {
		return {
			kind: "returned",
			label: state_name || "Returned",
			hint: "Record and submit a return Delivery Note against the linked Delivery Note.",
		};
	}
	if (delivery_type !== "SEND") {
		return { kind: "state", label: state_name || "", hint: "" };
	}
	const current_step = BOSTA_SEND_STEPS.findIndex((step) => step.states.includes(code));
	if (current_step < 0) return { kind: "state", label: state_name || "", hint: "" };
	return {
		kind: "stepper",
		label: state_name || "",
		hint: code === 45 && delivery_note_posted === false ? "Bosta reports this SEND delivery as delivered, but it has no submitted Delivery Note." : "",
		current_step,
		steps: BOSTA_SEND_STEPS,
	};
}

if (typeof frappe !== "undefined") {
	frappe.marketplace_integrations = frappe.marketplace_integrations || {};
	frappe.marketplace_integrations.bosta_state_presentation = bosta_state_presentation;
}

if (typeof module !== "undefined") {
	module.exports = { bosta_state_presentation };
}
})();
