// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Item form extension (#374): the "Marketplace Listings" tab. It lists the Marketplace Product
// Mappings of the Item (read-only, rendered into the HTML Custom Field `custom_marketplace_listings_html`,
// nothing is stored on the Item) and offers a manual price and stock sync for them. The mapping
// stays the source of truth; a row opens its mapping form. The sync reuses the platform endpoints the
// mapping list already calls (public/js/mapping_sync_actions.js); this file adds no sync logic.
//
// Wrapped in a function: every doctype_js file shares one scope with ERPNext's own Item script, so
// top-level names here could collide with its.
(function () {
	const LISTINGS_METHOD = "marketplace_integrations.api.item_listings.get_item_listings";
	const MAPPING = "Marketplace Product Mapping";
	const TAB_BREAK_FIELD = "custom_marketplace_listings_tab";
	const HTML_FIELD = "custom_marketplace_listings_html";
	const SYNC_ASSET = "/assets/marketplace_integrations/js/mapping_sync_actions.js";
	// The form stays usable while the (fast) queue calls run, so no screen freeze.
	const NO_FREEZE = { freeze: false };
	const SYNC_PLATFORMS = ["Shopify", "Noon", "Talabat"];
	const STATUS_COLORS = { Error: "red", Queued: "blue", Synced: "green", "Not Synced": "gray" };
	const KINDS = ["both", "price", "stock"];

	function escape(value) {
		return frappe.utils.escape_html(value === null || value === undefined ? "" : value);
	}

	function get_sync() {
		return frappe.marketplace_integrations && frappe.marketplace_integrations.mapping_sync;
	}

	// Resolves to the shared sync module, or null when it cannot load (the table still renders; the
	// sync controls then show an error). In Frappe 16 frappe.require() never rejects: a failed script
	// load still resolves (assets.js sets script.onerror = resolve and marks the path executed), so the
	// real failure is "resolved but the module is missing", and a later call does not refetch. The
	// catch below is defensive only, in case a future Frappe version rejects.
	function ensure_sync_module() {
		const loaded = get_sync();
		if (loaded) return Promise.resolve(loaded);
		return frappe
			.require(SYNC_ASSET)
			.then(() => get_sync() || null)
			.catch((error) => {
				console.error("Mapping sync helper failed to load (#374).", error);
				return null;
			});
	}

	function get_state(frm) {
		if (!frm.__mi_listings || frm.__mi_listings.item !== frm.doc.name) {
			frm.__mi_listings = {
				item: frm.doc.name,
				request: 0,
				status: "idle",
				rows: [],
				error: "",
				expanded: {},
				busy: false,
				busy_key: null,
			};
		}
		return frm.__mi_listings;
	}

	function toggle_tab(frm, visible) {
		if (!frm.toggle_display) return;
		frm.toggle_display(TAB_BREAK_FIELD, visible);
		frm.toggle_display(HTML_FIELD, visible);
	}

	function listings_field(frm) {
		const field = frm.get_field && frm.get_field(HTML_FIELD);
		return field && field.$wrapper ? field : null;
	}

	// ---- eligibility of a listing for a manual sync -------------------------------------------

	// kind: "both" (price and stock), "price" or "stock". Returns {allowed, reason, talabat_inactive}.
	// Reasons are checked in this order; the first one that applies is shown.
	function row_sync_state(row, kind, can_write) {
		if (!SYNC_PLATFORMS.includes(row.platform)) {
			return { allowed: false, reason: __("This platform has no manual sync.") };
		}
		if (!can_write || !row.can_write) {
			return { allowed: false, reason: __("You do not have permission to sync this listing.") };
		}
		if (row.platform === "Talabat" && kind !== "both") {
			return {
				allowed: false,
				reason: __("Talabat updates price and stock together. Use Sync price and stock."),
			};
		}
		if (row.platform !== "Talabat" && !row.enabled) {
			return { allowed: false, reason: __("This listing is disabled.") };
		}
		// A disabled Talabat listing is allowed: it is sent as inactive with stock 0 (confirmed first).
		return { allowed: true, reason: "", talabat_inactive: row.platform === "Talabat" && !row.enabled };
	}

	// ---- rendering ---------------------------------------------------------------------------

	function pill(text, color) {
		return `<span class="indicator-pill ${color} ellipsis"><span>${escape(text)}</span></span>`;
	}

	function status_pill(status) {
		const value = status || "Not Synced";
		return pill(__(value), STATUS_COLORS[value] || "gray");
	}

	// A cell. `title` (the full value) is shown as a tooltip when the text is ellipsized. `style` is a
	// column width from COLS; the cell grows with its content (no fixed height).
	function static_col(inner, style = "", title = "") {
		const tip = title ? ` title="${escape(title)}"` : "";
		return `<div class="col grid-static-col"${style ? ` style="${style}"` : ""}><div class="static-area ellipsis"${tip}>${inner}</div></div>`;
	}

	// Column widths (flex shorthand, logical only). The expand column is narrow, the account and
	// reference columns get the most room, and the sync column keeps its button on one line with
	// overflow visible so the dropdown menu is never clipped.
	const COLS = {
		expand: "flex: 0 0 2.5rem",
		account: "flex: 2.4 1 0; min-width: 0",
		reference: "flex: 2 1 0; min-width: 0",
		enabled: "flex: 1 1 0; min-width: 0",
		uom: "flex: 1 1 0; min-width: 0",
		status: "flex: 1.8 1 0; min-width: 0",
		sync: "flex: 0 0 15rem; overflow: visible; white-space: nowrap",
	};
	const NOWRAP = "white-space: nowrap";
	// Ancestors of the sync dropdown must not clip it.
	const VISIBLE = 'style="overflow: visible"';

	// `columns`: [{labels: [text, ...], style}]. Several labels stack as lines (the two status columns).
	function heading_row(columns) {
		const cells = columns
			.map(({ labels, style }) => {
				const inner = labels
					.map((label) => `<div class="ellipsis" title="${escape(label)}">${escape(label)}</div>`)
					.join("");
				return `<div class="col grid-static-col" style="${style}"><div class="static-area">${inner}</div></div>`;
			})
			.join("");
		return `<div class="grid-heading-row"><div class="grid-row" ${VISIBLE}><div class="data-row row m-0" ${VISIBLE}>${cells}</div></div></div>`;
	}

	function when(value) {
		if (!value) return "-";
		return frappe.datetime && frappe.datetime.comment_when
			? frappe.datetime.comment_when(value)
			: escape(value);
	}

	function detail_line(label, html) {
		return `<div><span class="text-muted">${escape(label)}:</span> ${html}</div>`;
	}

	function error_html(text) {
		return text ? `<span style="white-space: pre-wrap">${escape(text)}</span>` : "-";
	}

	function detail_html(row) {
		const lines = [
			detail_line(__("Last Synced On"), when(row.last_synced_on)),
			detail_line(__("Last Error"), error_html(row.last_error)),
		];
		// Talabat does not write the inventory status, so there is nothing to show for it.
		if (row.platform !== "Talabat") {
			lines.push(
				detail_line(__("Inventory Last Synced On"), when(row.inventory_last_synced_on)),
				detail_line(__("Inventory Last Error"), error_html(row.inventory_last_error))
			);
		}
		return `<div class="mi-listing-detail text-muted small" style="padding-inline: var(--padding-md); padding-block: var(--padding-sm)">${lines.join("")}</div>`;
	}

	function kind_label(kind) {
		if (kind === "price") return __("Price only");
		if (kind === "stock") return __("Stock only");
		return __("Sync price and stock");
	}

	// One split control: the main "Sync price and stock" button and a menu with "Price only" and
	// "Stock only". `states` maps each kind to {allowed, reason}. A disabled item always carries its
	// reason (tooltip and aria-label).
	function sync_control(state, scope, name, states) {
		const busy_here = (kind) => state.busy_key === `${scope}:${name}:${kind}`;
		const item_attrs = (kind) => {
			const attrs = `data-action="sync" data-scope="${scope}" data-name="${escape(name)}" data-kind="${kind}"`;
			const kind_state = states[kind];
			if (state.busy) return `${attrs} disabled aria-disabled="true"`;
			if (!kind_state.allowed) {
				return `${attrs} disabled aria-disabled="true" title="${escape(kind_state.reason)}" aria-label="${escape(kind_state.reason)}"`;
			}
			return attrs;
		};
		const main_label = busy_here("both") ? __("Queuing...") : kind_label("both");
		const main_title = !state.busy && !states.both.allowed ? ` title="${escape(states.both.reason)}"` : "";
		const menu = ["price", "stock"]
			.map((kind) => {
				const kind_state = states[kind];
				const disabled = state.busy || !kind_state.allowed;
				const label = busy_here(kind) ? __("Queuing...") : kind_label(kind);
				const reason = !state.busy && !kind_state.allowed ? kind_state.reason : "";
				return `<a class="dropdown-item${disabled ? " disabled" : ""}" href="#" role="button" data-action="sync" data-scope="${scope}" data-name="${escape(name)}" data-kind="${kind}"${
					disabled ? ' aria-disabled="true"' : ""
				}${reason ? ` title="${escape(reason)}" aria-label="${escape(reason)}"` : ""}>${escape(label)}</a>`;
			})
			.join("");
		return (
			`<div class="btn-group mi-sync" role="group" style="${NOWRAP}">` +
			`<span${main_title}><button type="button" class="btn btn-default btn-xs" style="${NOWRAP}" ${item_attrs("both")}>${escape(main_label)}</button></span>` +
			`<button type="button" class="btn btn-default btn-xs dropdown-toggle" data-toggle="dropdown" aria-haspopup="true" aria-expanded="false" aria-label="${escape(__("More sync options"))}"${
				state.busy ? " disabled" : ""
			}></button>` +
			`<div class="dropdown-menu">${menu}</div>` +
			`</div>`
		);
	}

	function row_states(row, can_write) {
		const states = {};
		KINDS.forEach((kind) => {
			states[kind] = row_sync_state(row, kind, can_write);
		});
		return states;
	}

	function all_states(rows, can_write) {
		const states = {};
		KINDS.forEach((kind) => {
			const per_row = rows.map((row) => row_sync_state(row, kind, can_write));
			const allowed = per_row.some((entry) => entry.allowed);
			states[kind] = { allowed, reason: allowed || !per_row.length ? "" : per_row[0].reason };
		});
		return states;
	}

	function listing_row_html(state, row, ctx) {
		const open = !!state.expanded[row.name];
		const account = row.account_name || row.marketplace_account;
		// A status line carries its own label for assistive tech and as a tooltip, since the two lines
		// are stacked in one cell and look alike.
		const status_line = (label, inner) =>
			`<div role="group" aria-label="${escape(label)}" title="${escape(label)}">${inner}</div>`;
		const status_cell =
			status_line(__("Sync Status"), status_pill(row.sync_status)) +
			status_line(__("Inventory Sync Status"), row.platform === "Talabat" ? "-" : status_pill(row.inventory_sync_status));
		const cells = [
			// Expand control. Its own click toggles the detail; a click anywhere else on the row opens the mapping.
			`<div class="col grid-static-col" style="${COLS.expand}"><button type="button" class="btn btn-link btn-xs" data-action="expand" data-name="${escape(row.name)}" aria-expanded="${open}" aria-label="${escape(open ? __("Hide details") : __("Show details"))}">${open ? "&#9652;" : "&#9662;"}</button></div>`,
			// Account name (ellipsized, full name as tooltip) with the platform badge beside it.
			`<div class="col grid-static-col" style="${COLS.account}"><div class="static-area d-flex align-items-center" style="gap: var(--margin-xs)"><span class="ellipsis" style="min-width: 0" title="${escape(account)}">${escape(account)}</span><span class="badge badge-light" style="flex: none">${escape(row.platform)}</span></div></div>`,
			static_col(`<bdi dir="ltr">${escape(row.external_reference)}</bdi>`, COLS.reference, row.external_reference),
			static_col(row.enabled ? pill(__("Enabled"), "green") : pill(__("Disabled"), "gray"), COLS.enabled),
			static_col(escape(row.sales_uom), COLS.uom),
			`<div class="col grid-static-col" style="${COLS.status}"><div class="static-area">${status_cell}</div></div>`,
		];
		if (ctx.can_write) {
			cells.push(
				`<div class="col grid-static-col mi-sync-cell" style="${COLS.sync}">${sync_control(state, "row", row.name, row_states(row, ctx.can_write))}</div>`
			);
		}
		return (
			`<div class="grid-row mi-listing-row" tabindex="0" data-name="${escape(row.name)}" ${VISIBLE}>` +
			`<div class="data-row row m-0" style="overflow: visible; align-items: center">${cells.join("")}</div>` +
			(open ? detail_html(row) : "") +
			`</div>`
		);
	}

	function table_html(state, ctx) {
		const columns = [
			{ labels: [], style: COLS.expand },
			{ labels: [__("Marketplace Account")], style: COLS.account },
			{ labels: [__("External Reference")], style: COLS.reference },
			{ labels: [__("Enabled")], style: COLS.enabled },
			{ labels: [__("Sales UOM")], style: COLS.uom },
			{ labels: [__("Sync Status"), __("Inventory Sync Status")], style: COLS.status },
		];
		if (ctx.can_write) columns.push({ labels: [], style: COLS.sync });
		const heading = heading_row(columns);
		const rows = state.rows.map((row) => listing_row_html(state, row, ctx)).join("");
		return `<div class="form-grid-container" ${VISIBLE}><div class="form-grid" ${VISIBLE}>${heading}<div class="grid-body" ${VISIBLE}><div class="rows" ${VISIBLE}>${rows}</div></div></div></div>`;
	}

	function toolbar_html(state, ctx) {
		const left = [];
		if (ctx.can_create) {
			left.push(
				`<button type="button" class="btn btn-default btn-sm" data-action="add">${escape(__("Add Listing"))}</button>`
			);
		}
		left.push(
			`<button type="button" class="btn btn-default btn-sm" data-action="view_all">${escape(__("View all"))}</button>`
		);
		const right =
			ctx.can_write && state.rows.length
				? `<div><div class="btn-group-label text-muted small">${escape(__("Sync all listings"))}</div>${sync_control(
						state,
						"all",
						"",
						all_states(state.rows, ctx.can_write)
				  )}</div>`
				: "";
		return `<div class="d-flex flex-wrap justify-content-between align-items-end" style="gap: var(--margin-sm); margin-block-end: var(--margin-md)"><div class="d-flex" style="gap: var(--margin-sm)">${left.join("")}</div>${right}</div>`;
	}

	function render_html(state, ctx) {
		if (state.status === "loading" || state.status === "idle") {
			return `<p class="text-muted">${escape(__("Loading..."))}</p>`;
		}
		if (state.status === "error") {
			return (
				`<div class="text-danger">${escape(state.error)}</div>` +
				`<p><button type="button" class="btn btn-default btn-sm" data-action="retry">${escape(__("Retry"))}</button></p>`
			);
		}
		if (!state.rows.length) {
			const add = ctx.can_create
				? `<p><button type="button" class="btn btn-default btn-sm" data-action="add">${escape(__("Add Listing"))}</button></p>`
				: "";
			return `<p class="text-muted">${escape(__("No marketplace listings for this Item"))}</p>${add}`;
		}
		return `${toolbar_html(state, ctx)}${table_html(state, ctx)}`;
	}

	function get_ctx() {
		return {
			can_create: frappe.model.can_create(MAPPING),
			can_write: frappe.model.can_write(MAPPING),
		};
	}

	function paint(frm) {
		const field = listings_field(frm);
		if (!field) return;
		field.$wrapper.html(render_html(get_state(frm), get_ctx()));
		bind_events(frm, field);
	}

	function bind_events(frm, field) {
		if (!field.$wrapper.off || !field.$wrapper.on) return;
		field.$wrapper
			.off(".mi_listings")
			.on("click.mi_listings", "[data-action]", (event) => {
				event.preventDefault();
				event.stopPropagation();
				const target = event.currentTarget;
				if (target.disabled || target.getAttribute("aria-disabled") === "true") return;
				on_action(frm, target.getAttribute("data-action"), {
					name: target.getAttribute("data-name"),
					scope: target.getAttribute("data-scope"),
					kind: target.getAttribute("data-kind"),
				});
			})
			.on("click.mi_listings", ".mi-listing-row", (event) => {
				// The dropdown toggle and menu sit inside the row; they must not open the mapping.
				if (event.target.closest && event.target.closest("button, a, .dropdown-menu")) return;
				open_row(event.currentTarget.getAttribute("data-name"));
			})
			.on("keydown.mi_listings", ".mi-listing-row", (event) => {
				// Only when the row itself has focus, so Enter on a button inside it keeps its own meaning.
				if (event.key === "Enter" && event.target === event.currentTarget) {
					open_row(event.currentTarget.getAttribute("data-name"));
				}
			});
	}

	function open_row(name) {
		if (name) frappe.set_route("Form", MAPPING, name);
	}

	function on_action(frm, action, data) {
		const state = get_state(frm);
		if (action === "add") return frappe.new_doc(MAPPING, { item: frm.doc.name });
		if (action === "view_all") return frappe.set_route("List", MAPPING, { item: frm.doc.name });
		if (action === "retry") return load_listings(frm);
		if (action === "expand") {
			state.expanded[data.name] = !state.expanded[data.name];
			return paint(frm);
		}
		if (action === "sync") return start_sync(frm, data.scope, data.name, data.kind);
	}

	// ---- loading -----------------------------------------------------------------------------

	// `quiet` keeps the current table on screen while the rows are fetched again (after a sync).
	function load_listings(frm, { quiet = false } = {}) {
		const state = get_state(frm);
		const item = frm.doc.name;
		const request = ++state.request;
		if (!quiet) {
			state.status = "loading";
			paint(frm);
		}
		return ensure_sync_module().then((sync) =>
			frappe.call({ method: LISTINGS_METHOD, type: "GET", args: { item }, silent: true }).then(
				(response) => {
					if (request !== state.request || frm.doc.name !== item) return;
					state.rows = response.message || [];
					state.status = "ready";
					state.error = "";
					paint(frm);
				},
				(error) => {
					if (request !== state.request || frm.doc.name !== item) return;
					state.status = "error";
					state.rows = [];
					state.error =
						(sync && sync.server_message_text(error)) || (error && error.message) || __("The request failed.");
					paint(frm);
				}
			)
		);
	}

	function render_listings_tab(frm) {
		// No tab and no server call for an unsaved Item or without read on the mapping.
		const visible = !frm.is_new() && frappe.model.can_read(MAPPING);
		toggle_tab(frm, visible);
		if (!visible) {
			const field = listings_field(frm);
			if (field) field.$wrapper.html("");
			return Promise.resolve();
		}
		return load_listings(frm);
	}

	// ---- manual sync -------------------------------------------------------------------------

	function confirm_message(scope, entries, kind) {
		const reference = entries[0].row.external_reference;
		const account = entries[0].row.account_name || entries[0].row.marketplace_account;
		let sentence;
		if (scope === "row") {
			const args = [`<bdi dir="ltr">${escape(reference)}</bdi>`, escape(account)];
			sentence =
				kind === "price"
					? __("Send the current price of {0} to {1}?", args)
					: kind === "stock"
					? __("Send the current stock of {0} to {1}?", args)
					: __("Send the current price and stock of {0} to {1}?", args);
		} else {
			const args = [entries.length];
			sentence =
				kind === "price"
					? __("Send the current price of {0} listings of this Item to their marketplaces?", args)
					: kind === "stock"
					? __("Send the current stock of {0} listings of this Item to their marketplaces?", args)
					: __("Send the current price and stock of {0} listings of this Item to their marketplaces?", args);
		}
		const lines = [sentence];
		// Only the automatic push of the kind being sent matters ("both" is either one).
		const push_is_off = (row) =>
			kind === "price" ? row.price_push_off : kind === "stock" ? row.stock_push_off : row.price_push_off || row.stock_push_off;
		const paused = [...new Set(entries.filter((entry) => push_is_off(entry.row)).map((entry) => entry.row.account_name || entry.row.marketplace_account))];
		if (paused.length) {
			lines.push(__("This runs once even though automatic push is off for {0}.", [escape(paused.join(", "))]));
		}
		if (entries.some((entry) => entry.talabat_inactive)) {
			lines.push(__("This sends the listing to Talabat as inactive with stock 0."));
		}
		return lines.join("<br><br>");
	}

	function skipped_items(entries) {
		return entries.map((entry) => ({
			category: "skipped",
			platform: entry.row.platform || entry.row.account_name || entry.row.marketplace_account,
			text: `${entry.row.external_reference}: ${entry.reason}`,
		}));
	}

	// One leg per platform call. Shopify price and stock are two endpoints; their status dialogs are
	// collected in `dialogs` (price first) and opened one after another by the caller.
	function build_legs(sync, rows, kind, dialogs) {
		const talabat_names = [];
		const shopify_names = [];
		const noon_by_account = {};
		rows.forEach((row) => {
			if (row.platform === "Talabat") talabat_names.push(row.name);
			else if (row.platform === "Shopify") shopify_names.push(row.name);
			else if (row.platform === "Noon") {
				(noon_by_account[row.marketplace_account] = noon_by_account[row.marketplace_account] || []).push(row.name);
			}
		});

		const legs = [];
		if (talabat_names.length) {
			// Talabat takes price, stock and active together; no price-only or stock-only push exists.
			legs.push({ platform: "Talabat", run: () => sync.push_selected_to_talabat(talabat_names, NO_FREEZE) });
		}
		if (shopify_names.length) {
			if (kind === "both" || kind === "price") {
				legs.push({
					platform: "Shopify",
					run: () =>
						sync
							.queue_call(
								"marketplace_integrations.api.shopify_catalog_push.queue_catalog_push",
								{ mapping_names: shopify_names },
								__("Queuing Shopify price pushes..."),
								NO_FREEZE
							)
							.then((response) => {
								dialogs.price = { jobs: sync.shopify_jobs(response), inventory: false };
								return sync.shopify_job_items(response);
							}),
				});
			}
			if (kind === "both" || kind === "stock") {
				legs.push({
					platform: "Shopify",
					run: () =>
						sync
							.queue_call(
								"marketplace_integrations.api.shopify_inventory.queue_inventory_sync",
								{ mapping_names: shopify_names },
								__("Queuing Shopify inventory syncs..."),
								NO_FREEZE
							)
							.then((response) => {
								dialogs.stock = { jobs: sync.shopify_jobs(response), inventory: true };
								return sync.shopify_job_items(response);
							}),
				});
			}
		}
		Object.keys(noon_by_account).forEach((account) => {
			const names = noon_by_account[account];
			// One call per account: pricing (queue_pricing_push), stock (queue_stock_push) or both (queue_account_sync).
			const run =
				kind === "price"
					? () => sync.push_selected_noon_price(account, names, NO_FREEZE)
					: kind === "stock"
					? () => sync.push_selected_noon_stock(account, names, NO_FREEZE)
					: () => sync.push_selected_to_noon(account, names, NO_FREEZE);
			legs.push({ platform: "Noon", run });
		});
		return legs;
	}

	function start_sync(frm, scope, name, kind) {
		const state = get_state(frm);
		// A second click while one sync is being confirmed or queued is ignored.
		if (state.busy) return;
		const rows = scope === "row" ? state.rows.filter((row) => row.name === name) : state.rows;
		if (!rows.length) return;
		const sync = get_sync();
		if (!sync) {
			// The helper failed to load, so the click would otherwise do nothing. A later frappe.require()
			// does not refetch (Frappe caches the path), so the user must reload the page.
			frappe.msgprint({
				title: __("Error"),
				message: __("Could not load the sync actions. Reload the page and try again."),
				indicator: "red",
			});
			return;
		}
		const can_write = frappe.model.can_write(MAPPING);

		const eligible = [];
		const excluded = [];
		rows.forEach((row) => {
			const result = row_sync_state(row, kind, can_write);
			if (result.allowed) eligible.push({ row, talabat_inactive: result.talabat_inactive });
			else excluded.push({ row, reason: result.reason });
		});
		if (!eligible.length) {
			sync.show_sync_summary(skipped_items(excluded));
			return;
		}

		state.busy = true;
		frappe.confirm(
			confirm_message(scope, eligible, kind),
			() => run_sync(frm, sync, scope, name, kind, eligible, excluded),
			() => {
				// Closed or answered "No" before any call was made.
				if (!state.busy_key) state.busy = false;
			}
		);
	}

	function run_sync(frm, sync, scope, name, kind, eligible, excluded) {
		const state = get_state(frm);
		state.busy = true;
		state.busy_key = `${scope}:${name || ""}:${kind}`;
		paint(frm);

		const dialogs = {};
		const legs = build_legs(
			sync,
			eligible.map((entry) => entry.row),
			kind,
			dialogs
		);

		return sync
			.run_legs(legs)
			.then((items) => {
				// Only a Talabat call that was queued gets the "confirms later" line, not a failed one.
				const summary_extra = items.some((item) => item.platform === "Talabat" && item.category === "queued")
					? [
							{
								category: "queued",
								platform: "Talabat",
								text: __("Talabat confirms later. Refresh to see the result."),
							},
						]
					: [];
				const summary = [...skipped_items(excluded), ...items, ...summary_extra];
				const entries = [dialogs.price, dialogs.stock].filter(Boolean);
				state.busy = false;
				state.busy_key = null;
				return load_listings(frm, { quiet: true }).then(() =>
					sync.open_status_dialogs_in_sequence(entries, () => {
						// The Shopify dialog closed: the stored statuses have moved on again.
						const refreshed = entries.some((entry) => entry.jobs.length)
							? load_listings(frm, { quiet: true })
							: Promise.resolve();
						sync.show_sync_summary(summary);
						return refreshed;
					})
				);
			})
			.finally(() => {
				if (state.busy) {
					state.busy = false;
					state.busy_key = null;
					paint(frm);
				}
			});
	}

	if (typeof frappe !== "undefined" && frappe.ui && frappe.ui.form) {
		frappe.ui.form.on("Item", {
			refresh(frm) {
				render_listings_tab(frm);
			},
		});
	}

	if (typeof module !== "undefined") {
		module.exports = {
			row_sync_state,
			render_html,
			render_listings_tab,
			load_listings,
			on_action,
			open_row,
			start_sync,
			confirm_message,
			build_legs,
			get_state,
		};
	}
})();
