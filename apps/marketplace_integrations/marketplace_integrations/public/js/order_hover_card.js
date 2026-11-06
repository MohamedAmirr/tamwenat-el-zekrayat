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
	const THUMB = 40;
	const LIST_MAX_HEIGHT = 320;
	const SHOPIFY_THUMB_WIDTH = 96;
	const MAX_IMAGE_URL_LENGTH = 2000;

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

	// Same rules as the server (api/order_hover_card.py safe_image_url): an http(s) URL with a host
	// and no userinfo, or an ERPNext /files/ or /private/files/ path (a space and an apostrophe are
	// allowed in file paths only, Frappe keeps them in file names); never "..", double quotes,
	// backslash, angle brackets, backticks, control characters or more than 2000 characters.
	function safe_image(url) {
		const value = typeof url === "string" ? url.trim() : "";
		if (!value || value.length > MAX_IMAGE_URL_LENGTH || value.includes("..")) return "";
		if (/[\\"<>`]/.test(value) || Array.from(value).some((ch) => ch.charCodeAt(0) < 32)) return "";
		if (value.startsWith("/files/") || value.startsWith("/private/files/")) return value;
		if (/[ ']/.test(value)) return "";
		// The URL parser reads "https:///a.png" as host "a.png"; the server rejects it, so does this.
		if (!/^https?:\/\/[^/]/i.test(value)) return "";
		try {
			const parsed = new URL(value);
			const ok = (parsed.protocol === "http:" || parsed.protocol === "https:") && parsed.host && !parsed.username && !parsed.password;
			return ok ? value : "";
		} catch (e) {
			return "";
		}
	}

	// Shopify's CDN resizes on request: ask for a small picture for the 40 px thumbnail (2x for
	// sharp screens). Other hosts and file paths stay as they are; an existing width is kept.
	function thumbnail_url(url) {
		if (!/^https?:\/\/cdn\.shopify\.com(\/|\?|#|$)/i.test(url)) return url;
		let parsed;
		try {
			parsed = new URL(url);
		} catch (e) {
			return url;
		}
		if (parsed.searchParams.has("width")) return url;
		const hash_at = url.indexOf("#");
		const base = hash_at === -1 ? url : url.slice(0, hash_at);
		const hash = hash_at === -1 ? "" : url.slice(hash_at);
		return `${base}${base.includes("?") ? "&" : "?"}width=${SHOPIFY_THUMB_WIDTH}${hash}`;
	}

	// One line per item: fixed-size thumbnail (neutral box when there is no image; the box also
	// stays when the image fails, see wire_images), name, item code, quantity. The list scrolls.
	function render_item(row) {
		const safe = safe_image(row.image);
		const src = safe && thumbnail_url(safe);
		const img = src
			? `<img class="mi-hc-img" src="${escape(src)}" alt="" width="${THUMB}" height="${THUMB}" loading="lazy" decoding="async" referrerpolicy="no-referrer" style="display: block; width: 100%; height: 100%; object-fit: cover;">`
			: "";
		const box = `<span style="flex: none; display: block; width: ${THUMB}px; height: ${THUMB}px; overflow: hidden; border-radius: var(--border-radius); background: var(--control-bg);">${img}</span>`;
		const code = row.item_code ? `<div class="text-muted small">${ltr(row.item_code)}</div>` : "";
		return (
			`<div class="mi-hc-item" style="display: flex; align-items: center; gap: var(--margin-sm); margin-bottom: var(--margin-sm);">` +
			box +
			`<span style="flex: 1 1 auto; min-width: 0; overflow-wrap: anywhere;"><div>${escape(row.item_name)}</div>${code}</span>` +
			`<span style="flex: none;"><bdi dir="ltr">x ${escape(row.qty)}</bdi></span>` +
			`</div>`
		);
	}

	function render_items(items) {
		const rows = Array.isArray(items) ? items : [];
		if (!rows.length) return "";
		const label = __("Items ({0})", [rows.length]);
		const list = `<div class="mi-hc-items" tabindex="0" role="region" aria-label="${escape(
			label
		)}" style="max-height: ${LIST_MAX_HEIGHT}px; overflow-y: auto; overscroll-behavior: contain;">${rows.map(render_item).join("")}</div>`;
		return section(label, list);
	}

	// Failed images (404, blocked, offline) hide themselves and leave the neutral box. No inline
	// handler: the error event does not bubble, so each image gets its own listener.
	function wire_images(root) {
		if (!root || typeof root.querySelectorAll !== "function") return;
		root.querySelectorAll("img.mi-hc-img").forEach((img) => {
			img.addEventListener("error", () => {
				img.style.display = "none";
			});
		});
	}

	// Plain-text address lines from the server, each escaped here; mixed Arabic/English lines keep
	// their own direction (bdi dir=auto) and long words wrap instead of widening the card.
	function render_address(lines) {
		const rows = (Array.isArray(lines) ? lines : []).filter((line) => String(line || "").trim());
		if (!rows.length) return "";
		const body = rows.map((line) => `<bdi dir="auto">${escape(line)}</bdi>`).join("<br>");
		return section(__("Delivery Address"), `<div style="overflow-wrap: anywhere;">${body}</div>`);
	}

	// The Sales Order card: customer, items, marketplace and Bosta details, totals.
	function render_sales_order_card(data) {
		let html = `<div style="margin-bottom: var(--margin-md);"><b>${escape(data.sales_order)}</b>`;
		if (data.status) html += ` <span class="text-muted small">${escape(__(data.status))}</span>`;
		html += "</div>";

		const customer = [escape(data.customer)];
		if (data.phone) customer.push(ltr(data.phone));
		html += section(__("Customer"), customer.join("<br>"));

		html += render_address(data.address);

		html += render_items(data.items);

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
					wire_images(el);
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
		module.exports = { create, render_sales_order_card, server_message, row_docname, row_link, safe_image, thumbnail_url, wire_images };
	}
})();
