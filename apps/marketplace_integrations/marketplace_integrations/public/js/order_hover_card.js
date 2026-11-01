// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Order details hover card for list views (#325), built on frappe.ui.hover_card.
//
// `create({ method, arg, render })` returns a controller bound to one whitelisted endpoint:
//   - `attach_to_list(listview)` gives every list row a hover/focus card and a small "Details"
//     button that opens the same content in a dialog (hover_card ignores touch). The card hangs
//     off the subject link only (never the whole row), start-aligned, so it can sit over the
//     other columns but never over the row checkboxes, which are left of the link. Rows removed
//     by a refresh or re-render have their card destroyed, and a route change closes open cards.
//     The card instance is read from the trigger's jQuery data once, at attach time, and kept in a
//     WeakMap keyed by row: Frappe removes rows with jQuery `.remove()`, which wipes that data
//     before the MutationObserver callback runs, so cleanup never reads jQuery data.
//   - keyboard: the link opens the card on focus (Tab order: link, ..., Details button). Arrow-key
//     row navigation focuses `.list-row-container`, which is not a trigger, so scrolling through
//     rows with the arrows fires no request. Positive tabindex on Frappe's rows (tabindex=1)
//     means Tab visits all rows first; this cannot be changed from here.
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

		if (data.bosta && data.bosta.summary && typeof data.bosta.summary === "object") {
			const lines = Object.entries(data.bosta.summary).map(
				([state, count]) => `${escape(__(state))}: <bdi dir="ltr">${escape(count)}</bdi>`
			);
			if (lines.length) html += section("Bosta", lines.join("<br>"));
		}

		html += section(__("Total"), escape(money(data.total, data.currency)));
		return html;
	}

	function make_element(html) {
		const el = document.createElement("div");
		el.innerHTML = html;
		return el;
	}

	// The subject link: the only element that triggers the card.
	function row_link(row) {
		return row.querySelector(".list-subject a[data-name]");
	}

	function row_docname(row) {
		const holder = row_link(row) || row.querySelector(".list-row-checkbox[data-name]");
		return holder ? holder.getAttribute("data-name") : "";
	}

	// frappe.ui.hover_card stores its instance in jQuery data on the trigger. That data is wiped
	// by jQuery's `.remove()` (Frappe removes list rows that way) before a MutationObserver
	// callback runs, so it is read once at attach time and kept by `create` (see `handles`).
	function hover_card_of(trigger) {
		return typeof $ === "function" ? $(trigger).data("es-hover-card") : null;
	}

	function create({ method, arg = "name", render = render_sales_order_card }) {
		const cache = new Map();
		// Hover card instance per attached row, kept here so cleanup never needs jQuery data.
		const handles = new WeakMap();
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
			const link = row_link(row);
			if (link && frappe.ui && frappe.ui.hover_card) {
				frappe.ui.hover_card(link, {
					side: "bottom",
					align: "start",
					content: (instance) => {
						card = instance;
						const el = make_element("");
						fill(el, name, () => instance.onreposition && instance.onreposition());
						return el;
					},
				});
				const instance = hover_card_of(link);
				if (instance) handles.set(row, instance);
			}
			details.addEventListener("click", (event) => {
				event.preventDefault();
				event.stopPropagation();
				if (card && card.close) card.close();
				show_details(name);
			});
			return link;
		}

		// Closes (or, when the row is gone, destroys) the card hanging off a row's subject link.
		function release(row, destroy) {
			const card = handles.get(row);
			if (!card) return;
			if (destroy) {
				handles.delete(row);
				if (card.destroy) card.destroy();
				else if (card.close) card.close();
			} else if (card.close) card.close();
		}

		function rows_in(node) {
			if (!node || node.nodeType !== 1) return [];
			const rows = node.matches && node.matches(".list-row-container") ? [node] : [];
			return rows.concat(Array.from(node.querySelectorAll ? node.querySelectorAll(".list-row-container") : []));
		}

		// List rows are rebuilt on every refresh and when scrolling, so new rows are picked up
		// from a MutationObserver; a row already handled is skipped.
		function attach_to_list(listview) {
			const container = listview && listview.$result && listview.$result[0];
			if (!container || container.__mi_order_hover_card) return;
			container.__mi_order_hover_card = true;
			const scan = (mutations) => {
				// A removed row cannot fire pointerleave/blur, so its open card would stay on screen.
				(Array.isArray(mutations) ? mutations : []).forEach((mutation) =>
					Array.from(mutation.removedNodes || []).forEach((node) =>
						rows_in(node).forEach((row) => release(row, true))
					)
				);
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
			// Leaving the list (route change) keeps the rows in the DOM, hidden: close what is open.
			if (typeof $ === "function") {
				const on_page_change = () => {
					if (!container.isConnected) {
						$(document).off("page-change", on_page_change);
						return;
					}
					container.querySelectorAll(".list-row-container").forEach((row) => release(row, false));
				};
				$(document).on("page-change", on_page_change);
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
		module.exports = { create, render_sales_order_card, server_message, row_docname, row_link };
	}
})();
