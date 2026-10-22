// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// #324: Marketplaces lists show a readable title as the subject link and hide the docname (ID)
// column (`hide_name_column: true` in the list settings). This helper keeps the docname reachable
// as the row tooltip, `title (docname)`. It is set lazily on hover/focus through one delegated
// handler, so there is no per-row work on render. textContent only, nothing is injected as HTML.
(function () {
	function attach_to_list(listview) {
		if (!listview || !listview.$result) return;
		listview.$result.on("mouseenter focusin", ".list-subject a[data-name]", function () {
			const text = this.textContent.trim();
			const name = this.dataset.name;
			if (name && text !== name) this.title = `${text} (${name})`;
		});
	}

	if (typeof frappe !== "undefined") {
		frappe.marketplace_integrations = frappe.marketplace_integrations || {};
		frappe.marketplace_integrations.list_row_name = { attach_to_list };
	}

	if (typeof module !== "undefined") {
		module.exports = { attach_to_list };
	}
})();
