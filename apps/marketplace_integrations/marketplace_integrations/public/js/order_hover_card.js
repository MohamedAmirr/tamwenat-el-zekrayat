// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Order details hover card for list views (#325), built on frappe.ui.hover_card.
//
// `create({ method, arg, render })` returns a controller bound to one whitelisted endpoint:
//   - `attach_to_list(listview)` gives every list row a hover/focus card and a small "Details"
//     button that opens the same content in a dialog (hover_card ignores touch).
//   - the data is fetched on first use only and cached per document, so one request per order
//     per page load (a failed request is not cached, so the next hover retries).
//   - a response is rendered only if its card or dialog is still the latest one opened.
// The card is a convenience: everything it shows is also on the form (never the only place).
// Used by the Sales Order list; the Marketplace Order list can reuse it with its own endpoint
// and `render`.
(function () {
	const SKELETON_BARS = [60, 90, 75, 85];

	function escape(value) {
		return frappe.utils.escape_html(value === null || value === undefined ? "" : String(value));
	}

	// Server texts are HTML-ish: strip tags, then escape again at render time.
	function strip_tags(text) {
		return String(text).replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
	}

	// The server's own message from a rejected frappe.call(): `_server_messages` is a JSON array of
	// JSON strings, each with a `message` (parse twice). Falls back to a generic sentence.
	function server_message(error) {
		const fallback = __("Could not load the order details.");
		try {
			const raw = error && error.responseJSON && error.responseJSON._server_messages;
			if (raw) {
				const texts = JSON.parse(raw)
					.map((entry) => strip_tags((typeof entry === "string" ? JSON.parse(entry) : entry).message || ""))
					.filter(Boolean);
				if (texts.length) return texts.join(" ");
			}
		} catch (e) {
			// fall through to the generic text
		}
		return (error && error.message && strip_tags(error.message)) || fallback;
	}

	function skeleton_html() {
		const bars = SKELETON_BARS.map(
			(width) =>
				`<div style="height: 10px; width: ${width}%; margin-bottom: 10px; border-radius: var(--border-radius); background: var(--control-bg);"></div>`
		).join("");
		return `<div role="status" aria-label="${escape(__("Loading..."))}">${bars}</div>`;
	}

	function error_html(error) {
		return `<div class="text-danger" role="alert">${escape(server_message(error))}</div>`;
	}

	function section(label, body_html) {
		return `<div style="margin-bottom: var(--margin-md);"><div class="text-muted small">${escape(
			label
		)}</div><div>${body_html}</div></div>`;
	}

	// Identifiers and phone numbers keep their direction inside Arabic text.
	function ltr(value) {
		return `<bdi dir="ltr">${escape(value)}</bdi>`;
	}

	function money(value, currency) {
		if (typeof format_currency === "function") return format_currency(value, currency);
		return String(value);
	}

	function render_marketplace(part) {
		const lines = [];
		if (part.platform) lines.push(`${escape(__("Platform"))}: ${escape(part.platform)}`);
		if (part.reference) lines.push(`${escape(__("Platform order number"))}: ${ltr(part.reference)}`);
		if (part.payment_status) lines.push(`${escape(__("Payment status"))}: ${escape(part.payment_status)}`);
		if (part.last_sync && part.last_sync.status) {
			lines.push(`${escape(__("Last sync"))}: ${escape(__(part.last_sync.status))}`);
		}
		let html = section(__("Marketplace"), lines.join("<br>"));
		if (part.last_sync_error) {
			html += section(__("Last sync error"), `<span class="text-danger">${escape(part.last_sync_error)}</span>`);
		}
		return html;
	}

	// The Sales Order card: customer, items, marketplace and Bosta details, totals.
	function render_sales_order_card(data) {
		let html = `<div style="margin-bottom: var(--margin-md);"><b>${escape(data.sales_order)}</b>`;
		if (data.status) html += ` <span class="text-muted small">${escape(__(data.status))}</span>`;
		html += "</div>";

		const customer = [escape(data.customer)];
		if (data.phone) customer.push(ltr(data.phone));
		html += section(__("Customer"), customer.join("<br>"));

		const items = (data.items || []).map((row) => `${escape(row.item_name)} x ${escape(row.qty)}`);
		if (data.more_items) items.push(`<span class="text-muted">${escape(__("More items on the order."))}</span>`);
		if (items.length) html += section(__("Items"), items.join("<br>"));

		if (data.marketplace) html += render_marketplace(data.marketplace);

		if (data.bosta) {
			const lines = [`${escape(__("Delivery status"))}: ${escape(data.bosta.status)}`];
			if (data.bosta.tracking_number) {
				lines.push(`${escape(__("Tracking number"))}: ${ltr(data.bosta.tracking_number)}`);
			}
			html += section("Bosta", lines.join("<br>"));
		}

		html += section(__("Total"), escape(money(data.total, data.currency)));
		return html;
	}

	function make_element(html) {
		const el = document.createElement("div");
		el.innerHTML = html;
		return el;
	}

	function row_docname(row) {
		const holder = row.querySelector(".list-row-checkbox[data-name]") || row.querySelector("a[data-name]");
		return holder ? holder.getAttribute("data-name") : "";
	}

	function create({ method, arg = "name", render = render_sales_order_card }) {
		const cache = new Map();
		let latest = 0;

		function load(name) {
			if (!cache.has(name)) {
				const request = frappe
					.call({ method, args: { [arg]: name }, silent: true })
					.then((r) => r.message)
					.catch((error) => {
						cache.delete(name);
						throw error;
					});
				cache.set(name, request);
			}
			return cache.get(name);
		}

		// Fills `el` with the skeleton, then the card or the error, unless a newer card or
		// dialog was opened in the meantime (the response then belongs to a stale surface).
		function fill(el, name, after_render) {
			const token = ++latest;
			el.innerHTML = skeleton_html();
			return load(name).then(
				(data) => {
					if (token !== latest) return;
					el.innerHTML = render(data);
					if (after_render) after_render();
				},
				(error) => {
					if (token !== latest) return;
					el.innerHTML = error_html(error);
					if (after_render) after_render();
				}
			);
		}

		// Same content as the card, for touch and keyboard users.
		function show_details(name) {
			const dialog = new frappe.ui.Dialog({
				title: __("Order details"),
				fields: [{ fieldtype: "HTML", fieldname: "details" }],
			});
			const el = make_element("");
			dialog.fields_dict.details.$wrapper.append(el);
			dialog.show();
			return fill(el, name);
		}

		function attach(row, name) {
			const details = row.ownerDocument.createElement("button");
			details.type = "button";
			details.className = "btn btn-xs btn-default";
			details.style.minHeight = "24px";
			details.style.minWidth = "24px";
			details.textContent = __("Details");
			details.setAttribute("aria-label", __("Details of {0}", [name]));
			const holder = row.querySelector(".level-right") || row;
			holder.appendChild(details);

			let card = null;
			if (frappe.ui && frappe.ui.hover_card) {
				frappe.ui.hover_card(row, {
					side: "bottom",
					align: "start",
					content: (instance) => {
						card = instance;
						const el = make_element("");
						fill(el, name, () => instance.onreposition && instance.onreposition());
						return el;
					},
				});
			}
			details.addEventListener("click", (event) => {
				event.preventDefault();
				event.stopPropagation();
				if (card && card.close) card.close();
				show_details(name);
			});
		}

		// List rows are rebuilt on every refresh and when scrolling, so new rows are picked up
		// from a MutationObserver; a row already handled is skipped.
		function attach_to_list(listview) {
			const container = listview && listview.$result && listview.$result[0];
			if (!container || container.__mi_order_hover_card) return;
			container.__mi_order_hover_card = true;
			const scan = () => {
				container.querySelectorAll(".list-row-container").forEach((row) => {
					if (row.__mi_order_hover_card) return;
					const name = row_docname(row);
					if (!name) return;
					row.__mi_order_hover_card = true;
					attach(row, name);
				});
			};
			scan();
			if (typeof MutationObserver !== "undefined") {
				new MutationObserver(scan).observe(container, { childList: true, subtree: true });
			}
			return scan;
		}

		return { attach_to_list, attach, show_details, load, fill };
	}

	if (typeof frappe !== "undefined") {
		frappe.marketplace_integrations = frappe.marketplace_integrations || {};
		frappe.marketplace_integrations.order_hover_card = { create, render_sales_order_card };
	}

	if (typeof module !== "undefined") {
		module.exports = { create, render_sales_order_card, server_message, row_docname };
	}
})();
