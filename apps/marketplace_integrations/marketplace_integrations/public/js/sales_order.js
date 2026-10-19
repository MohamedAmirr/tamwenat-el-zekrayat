// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Sales Order form extension (#183): after the merchant Closes or Cancels a
// marketplace Sales Order in ERPNext, offer to cancel the order on the platform
// too. The ERPNext action always runs first and is never changed; nothing reaches
// the platform without an explicit confirmation. Platform wording, eligibility and
// cancellation reasons all come from get_platform_cancel_option - nothing here
// names a platform.
//
// Wrapped in a function: every doctype_js file shares one scope with ERPNext's own
// Sales Order script, so top-level names here could collide with its.
(function () {
	const OPTION_METHOD =
		"marketplace_integrations.api.marketplace_outbound.get_platform_cancel_option";
	const REQUEST_METHOD =
		"marketplace_integrations.api.marketplace_outbound.request_platform_cancel";
	const BOSTA_DEFAULTS_METHOD = "marketplace_integrations.api.bosta_shipments.get_create_defaults";
	const BOSTA_CITIES_METHOD = "marketplace_integrations.api.bosta_shipments.get_cities";
	const BOSTA_DISTRICTS_METHOD = "marketplace_integrations.api.bosta_shipments.get_districts";
	const BOSTA_CREATE_METHOD = "marketplace_integrations.api.bosta_shipments.create_shipment";

	const BOSTA_CREATOR_ROLES = ["Sales Manager", "Stock User", "System Manager"];

	const BOSTA_SHIPMENT_METHOD = "marketplace_integrations.api.bosta_shipments.get_sales_order_shipment";
	const BOSTA_LABEL_METHOD = "marketplace_integrations.api.bosta_shipments.print_a4_arabic_label";
	const BOSTA_REFRESH_METHOD = "marketplace_integrations.api.bosta_shipments.refresh_shipment";
	const BOSTA_EDIT_METHOD = "marketplace_integrations.api.bosta_shipments.edit_shipment";
	// Bosta's documented states in which a delivery may be edited (OpenAPI, "Business update delivery").
	const BOSTA_EDITABLE_STATES = [10, 11, 20, 21, 22, 24, 30, 41, 47];
	const BOSTA_CANCEL_METHOD = "marketplace_integrations.api.bosta_shipments.cancel_shipment";
	const BOSTA_DELIVERED_STATE = 45;
	const BOSTA_TAB_FIELD = "custom_bosta_delivery_html";

	// Colours for the Bosta Shipment doctype's own statuses; the status text is the doctype's value.
	const BOSTA_STATUS_COLORS = {
		"Pending Bosta": "orange",
		"Bosta Created": "blue",
		"Delivery Note Failed": "red",
		Completed: "green",
		"Bosta Failed": "red",
		Canceled: "gray",
	};

	function can_create_bosta_shipment(frm) {
		return Boolean(
			frm.doc &&
				frm.doc.docstatus === 1 &&
				frm.doc.status !== "Cancelled" &&
				frappe.user &&
				frappe.user.has_role(BOSTA_CREATOR_ROLES)
		);
	}

	function start_create_bosta_shipment(frm) {
		frappe.call({ method: BOSTA_DEFAULTS_METHOD, type: "POST", args: { sales_order: frm.doc.name } }).then((r) => {
			show_bosta_dialog(frm, r.message || {});
		});
	}

	// Status, tracking number and Bosta state are stored/remote text: escaped before they
	// reach the dashboard (which renders raw HTML).
	function bosta_indicator_html(shipment) {
		const escape = frappe.utils.escape_html;
		const href = `/app/bosta-shipment/${encodeURIComponent(shipment.name)}`;
		let html = `<a href="${escape(href)}">${escape(__("Bosta Shipment"))}: ${escape(__(shipment.status))}</a>`;
		if (shipment.tracking_number) {
			html += ` &middot; ${escape(__("Tracking"))}: ${escape(shipment.tracking_number)}`;
		}
		if (shipment.bosta_state) html += ` (${escape(shipment.bosta_state)})`;
		return html;
	}

	// The window is opened inside the click (a popup blocker only allows that) and pointed at
	// the file when the server answers. window.open with "noopener" returns null, so the
	// opener is cut by hand instead. If the browser blocks the window, the URL is offered as
	// a link; if the call fails or returns no file, the blank window is closed.
	function print_bosta_label(shipment) {
		const label_window = window.open("", "_blank");
		if (label_window) label_window.opener = null;
		const close = () => {
			if (label_window) label_window.close();
		};
		return frappe
			.call({
				method: BOSTA_LABEL_METHOD,
				type: "POST",
				args: { shipment: shipment.name },
				freeze: true,
				freeze_message: __("Preparing Bosta label..."),
			})
			.then((r) => {
				const label = r.message || {};
				if (!label.file_url) return close();
				if (label_window) {
					label_window.location.href = label.file_url;
					return;
				}
				const escape = frappe.utils.escape_html;
				frappe.msgprint({
					title: __("Bosta A4 Label"),
					message: `<a href="${escape(label.file_url)}" target="_blank" rel="noopener">${escape(
						__("Open the label")
					)}</a>`,
					indicator: "green",
				});
			})
			.catch(close);
	}

	// Refresh from Bosta (#271): the server reads the delivery and applies its state. A Bosta
	// error is thrown by the server (already escaped), shown by Frappe, and nothing is stored.
	function refresh_bosta_shipment(frm, shipment) {
		return frappe
			.call({
				method: BOSTA_REFRESH_METHOD,
				type: "POST",
				args: { shipment: shipment.name },
				freeze: true,
				freeze_message: __("Reading the delivery from Bosta..."),
			})
			.then(() => {
				frappe.show_alert({ message: __("Bosta state refreshed."), indicator: "green" });
				return frm.reload_doc();
			})
			.catch(() => {});
	}

	// Cancel Bosta Shipment (#272). Nothing is sent before the confirm is accepted; the confirm
	// text is HTML, so the tracking number and Delivery Note name are escaped into it.
	function cancel_bosta_shipment(frm, shipment) {
		const escape = frappe.utils.escape_html;
		const message = escape(
			__("Cancel Bosta delivery {0}? Bosta will terminate it and Delivery Note {1} will be cancelled. This cannot be undone.", [
				shipment.tracking_number,
				shipment.delivery_note || "-",
			])
		);
		frappe.confirm(message, () => {
			frappe
				.call({
					method: BOSTA_CANCEL_METHOD,
					type: "POST",
					args: { shipment: shipment.name },
					freeze: true,
					freeze_message: __("Cancelling the Bosta delivery..."),
				})
				.then((r) => {
					const result = r.message || {};
					if (result.canceled) {
						frappe.show_alert({ message: __("Bosta delivery cancelled."), indicator: "green" });
					} else {
						frappe.msgprint({
							title: __("Bosta Cancel"),
							message: escape(
								__("Bosta terminated the delivery, but ERPNext could not cancel the Delivery Note. The shipment stays active and is listed in Needs Attention; the cancellation is retried automatically.")
							),
							indicator: "orange",
						});
					}
					return frm.reload_doc();
				})
				// A server error already showed Frappe's message; nothing changed.
				.catch(() => {});
		});
	}

	function bosta_row(label, value_html) {
		const escape = frappe.utils.escape_html;
		return `<tr><th style="width: 30%">${escape(label)}</th><td>${value_html}</td></tr>`;
	}

	function bosta_button(action, label) {
		const escape = frappe.utils.escape_html;
		return `<button class="btn btn-default btn-sm" style="margin-inline-end: 8px" data-bosta-action="${escape(action)}">${escape(label)}</button>`;
	}

	// The Bosta Delivery tab. Every stored or remote value is escaped. `options` is
	// { create, failed }: create = the user is a creator and may start a shipment; failed =
	// the shipment read failed (so "No Bosta shipment" would be a guess).
	function bosta_tab_html(shipment, options) {
		const escape = frappe.utils.escape_html;
		const { create, failed } = options || {};
		const buttons = [];
		let html = "";
		if (!shipment) {
			html += `<p class="text-muted">${escape(
				failed ? __("Could not load the Bosta shipment.") : __("No Bosta shipment")
			)}</p>`;
			if (create) buttons.push(bosta_button("create", __("Create Bosta Shipment")));
		} else {
			const text = (value) => (value ? escape(value) : "");
			const address = [shipment.address_first_line, shipment.city].filter(Boolean).join(", ");
			const district = shipment.district_id
				? `${escape(__("District ID"))}: ${escape(shipment.district_id)}`
				: "";
			let delivery_note = "";
			if (shipment.delivery_note) {
				const href = `/app/delivery-note/${encodeURIComponent(shipment.delivery_note)}`;
				delivery_note = `<a href="${escape(href)}">${escape(shipment.delivery_note)}</a>`;
				if (shipment.delivery_note_status) delivery_note += ` (${escape(__(shipment.delivery_note_status))})`;
			}
			const cod = escape(format_currency(shipment.cod_amount || 0));
			const last_update = shipment.last_update ? escape(frappe.datetime.str_to_user(shipment.last_update)) : "";
			html += `<table class="table table-bordered"><tbody>`;
			html += bosta_row(__("Status"), text(__(shipment.status)));
			html += bosta_row(__("Tracking Number"), text(shipment.tracking_number));
			html += bosta_row(__("Bosta State"), text(shipment.bosta_state));
			html += bosta_row(__("Last Bosta Update"), last_update);
			html += bosta_row(__("COD Sent"), cod);
			html += bosta_row(__("Receiver"), [text(shipment.receiver_name), text(shipment.receiver_phone)].filter(Boolean).join(" - "));
			html += bosta_row(__("Address"), [text(address), district].filter(Boolean).join("<br>"));
			html += bosta_row(__("Delivery Note"), delivery_note);
			html += `</tbody></table>`;
			if (shipment.active && shipment.tracking_number) {
				buttons.push(bosta_button("print", __("Print A4 Label")));
				if (create) buttons.push(bosta_button("refresh", __("Refresh from Bosta")));
				if (create && BOSTA_EDITABLE_STATES.includes(shipment.bosta_state_code)) {
					buttons.push(bosta_button("edit", __("Edit Bosta Shipment")));
				}
				if (create && shipment.bosta_state_code !== BOSTA_DELIVERED_STATE) {
					buttons.push(bosta_button("cancel", __("Cancel Bosta Shipment")));
				}
			}
			// A Bosta Failed shipment without a tracking number keeps Create: that retry
			// (search Bosta, then send) is what the server resumes on the same row.
			if (create && (!shipment.active || (shipment.status === "Bosta Failed" && !shipment.tracking_number))) {
				buttons.push(bosta_button("create", __("Create Bosta Shipment")));
			}
		}
		return html + (buttons.length ? `<p>${buttons.join("")}</p>` : "");
	}

	// Draws the tab and wires its buttons (delegated, so a redraw never stacks handlers).
	function render_bosta_tab(frm, shipment, options) {
		const field = frm.get_field && frm.get_field(BOSTA_TAB_FIELD);
		if (!field || !field.$wrapper) return;
		field.$wrapper.html(bosta_tab_html(shipment, options));
		field.$wrapper.off("click.mi_bosta").on("click.mi_bosta", "[data-bosta-action]", (event) => {
			const action = event.currentTarget.getAttribute("data-bosta-action");
			if (action === "create") start_create_bosta_shipment(frm);
			else if (action === "print") print_bosta_label(shipment);
			else if (action === "refresh") refresh_bosta_shipment(frm, shipment);
			else if (action === "cancel") cancel_bosta_shipment(frm, shipment);
			else if (action === "edit") show_bosta_edit_dialog(frm, shipment);
		});
	}

	// The form clears custom buttons and the dashboard indicators on every refresh
	// (Form.refresh), so nothing here is remembered: it is all added again each time. The
	// current shipment is read from the server; a reply that arrives after a newer refresh
	// (or for another document) is dropped, so the indicator is never added twice. A user
	// who cannot read Bosta Shipment never asks and sees nothing in the tab. If the read
	// fails, Create stays available to creators in the tab (the server still allows one
	// active shipment per order).
	function add_bosta_shipment_ux(frm) {
		if (!frm.doc || !frm.doc.name || frm.doc.docstatus < 1) return Promise.resolve();
		const request = (frm.__mi_bosta_request = (frm.__mi_bosta_request || 0) + 1);
		const sales_order = frm.doc.name;
		const create = can_create_bosta_shipment(frm);
		if (!(frappe.model && frappe.model.can_read("Bosta Shipment"))) {
			const field = frm.get_field && frm.get_field(BOSTA_TAB_FIELD);
			if (field && field.$wrapper) field.$wrapper.html("");
			return Promise.resolve();
		}
		let failed = false;
		return frappe
			.call({ method: BOSTA_SHIPMENT_METHOD, type: "POST", args: { sales_order }, silent: true })
			.then(
				(r) => r.message || null,
				() => {
					failed = true;
					return null;
				}
			)
			.then((shipment) => {
				if (request !== frm.__mi_bosta_request || frm.doc.name !== sales_order) return;
				if (shipment && shipment.active && frm.dashboard) {
					frm.dashboard.add_indicator(
						bosta_indicator_html(shipment),
						BOSTA_STATUS_COLORS[shipment.status] || "gray"
					);
				}
				render_bosta_tab(frm, shipment, { create, failed });
			});
	}

	// Bosta city / district names are remote text. Frappe renders an Autocomplete label as
	// dropdown HTML and also uses it as the input text, so escaping "&" would show "&amp;"
	// after picking. Only "<" and ">" are swapped for look-alikes: a label can then never
	// open a tag, and "&" stays readable.
	function safe_text(text) {
		return String(text).replace(/</g, "\u2039").replace(/>/g, "\u203a");
	}
	function location_label(item, suffix) {
		const label = [item.name, item.name_ar].filter(Boolean).map(safe_text).join(" - ");
		return suffix ? `${label} (${safe_text(suffix)})` : label;
	}

	// Autocomplete matches the typed text back to an option by label, so two districts with
	// the same label would collide: those get their zone appended.
	function location_options(items) {
		const counts = {};
		items.forEach((item) => {
			const label = location_label(item);
			counts[label] = (counts[label] || 0) + 1;
		});
		return items.map((item) => ({
			value: item.id,
			label: location_label(item, counts[location_label(item)] > 1 ? item.zone : ""),
		}));
	}

	function match_city(cities, source_city) {
		const wanted = String(source_city || "").trim().toLowerCase();
		if (!wanted) return null;
		return cities.find((city) => city.name.trim().toLowerCase() === wanted) || null;
	}

	function show_bosta_dialog(frm, defaults) {
		return open_create_dialog({
			defaults,
			submit(dialog, values, city) {
				dialog.hide();
				frappe.call({
					method: BOSTA_CREATE_METHOD,
					type: "POST",
					args: {
						sales_order: frm.doc.name,
						receiver_name: values.receiver_name,
						receiver_phone: values.receiver_phone,
						city: city.name,
						district_id: values.district,
						address_first_line: values.address_first_line,
						expected_cod: defaults.cod_amount,
					},
					freeze: true,
					freeze_message: __("Creating Bosta shipment..."),
				}).then(() => frm.reload_doc());
			},
		});
	}

	// The Create Bosta Shipment dialog, shared with the Sales Order list's bulk create
	// (#305, sales_order_list.js). `submit(dialog, values, city)` runs once a city and district
	// are chosen. Bulk also passes `progress` ("Order 2 of 5"), `on_skip` (adds a Skip button)
	// and `on_hide` (the dialog was closed without submit or skip).
	function open_create_dialog({ defaults, submit, progress, on_skip, on_hide }) {
		const intro_fields = [];
		if (progress) {
			intro_fields.push({
				fieldtype: "HTML",
				fieldname: "progress_note",
				options: `<p><b>${frappe.utils.escape_html(progress)}</b></p>`,
			});
		}
		intro_fields.push({ fieldtype: "HTML", fieldname: "cod_note", options: bosta_cod_note_html(defaults) });
		return open_bosta_location_dialog({
			title: __("Create Bosta Shipment"),
			primary_action_label: __("Create Shipment"),
			defaults,
			intro_fields,
			receiver_fields: [
				{ fieldname: "receiver_name", fieldtype: "Data", label: __("Receiver Name"), reqd: 1, default: defaults.receiver_name },
				{ fieldname: "receiver_phone", fieldtype: "Data", label: __("Receiver Phone"), reqd: 1, default: defaults.receiver_phone },
			],
			submit,
			on_skip,
			on_hide,
		});
	}

	// Edit Bosta Shipment (#273): the same city / district pickers as the create dialog, with
	// only the receiver phone and address. The values shown are stored text, so they only
	// ever go into field defaults (never HTML). Nothing is sent until the confirm is accepted.
	function show_bosta_edit_dialog(frm, shipment) {
		const defaults = {
			address_city: shipment.city,
			district_id: shipment.district_id,
			address_first_line: shipment.address_first_line,
		};
		const note = __(
			"This updates Bosta and the Bosta Shipment only. The Sales Order address and Shopify are not changed; update them by hand if needed."
		);
		return open_bosta_location_dialog({
			title: __("Edit Bosta Shipment"),
			primary_action_label: __("Update Shipment"),
			defaults,
			intro_fields: [
				{ fieldtype: "HTML", fieldname: "edit_note", options: `<p>${frappe.utils.escape_html(note)}</p>` },
			],
			receiver_fields: [
				{ fieldname: "receiver_phone", fieldtype: "Data", label: __("Receiver Phone"), reqd: 1, default: shipment.receiver_phone },
			],
			submit(dialog, values, city) {
				const message = frappe.utils.escape_html(
					__("Update Bosta delivery {0}? {1}", [shipment.tracking_number, note])
				);
				frappe.confirm(message, () => {
					dialog.hide();
					frappe
						.call({
							method: BOSTA_EDIT_METHOD,
							type: "POST",
							args: {
								shipment: shipment.name,
								receiver_phone: values.receiver_phone,
								city: city.name,
								district_id: values.district,
								address_first_line: values.address_first_line,
							},
							freeze: true,
							freeze_message: __("Updating the Bosta delivery..."),
						})
						.then(() => {
							frappe.show_alert({ message: __("Bosta delivery updated."), indicator: "green" });
							return frm.reload_doc();
						})
						// A server error already showed Frappe's message; nothing changed.
						.catch(() => {});
				});
			},
		});
	}

	// The dialog shell shared by Create and Edit: leading fields, receiver fields, then the
	// Bosta city / district pickers and the address first line. `submit(dialog, values, city)`
	// runs once a city and district are chosen.
	function open_bosta_location_dialog({ title, primary_action_label, defaults, intro_fields, receiver_fields, submit, on_skip, on_hide }) {
		const state = { cities: [], error: false, loading_districts: 0 };
		const dialog = new frappe.ui.Dialog({
			title,
			fields: [
				...intro_fields,
				{ fieldtype: "HTML", fieldname: "lists_error", options: "", hidden: 1 },
				{ fieldtype: "Button", fieldname: "retry_lists", label: __("Retry"), hidden: 1, click: () => load_cities(false) },
				...receiver_fields,
				// Autocomplete: searchable (hundreds of districts), shows the name label and stores the Bosta ID.
				{ fieldname: "city", fieldtype: "Autocomplete", label: __("City"), reqd: 1, max_items: 1000, options: [], change: on_city_change },
				{ fieldname: "district", fieldtype: "Autocomplete", label: __("District"), reqd: 1, max_items: 1000, options: [] },
				{ fieldname: "address_first_line", fieldtype: "Small Text", label: __("Address First Line"), reqd: 1, default: defaults.address_first_line },
			],
			primary_action_label,
			primary_action(values) {
				const city = state.cities.find((c) => c.id === values.city);
				if (!city || !values.district) return;
				submit(dialog, values, city);
			},
			secondary_action_label: __("Refresh Bosta Lists"),
			secondary_action() {
				load_cities(true);
			},
		});

		if (on_skip && dialog.add_custom_action) {
			dialog.add_custom_action(__("Skip"), () => on_skip(dialog));
		}
		if (on_hide) dialog.onhide = on_hide;

		function set_ready(ready, error_text) {
			state.error = !ready && Boolean(error_text);
			dialog.get_primary_btn().prop("disabled", !ready);
			dialog.set_df_property("lists_error", "hidden", state.error ? 0 : 1);
			dialog.set_df_property("retry_lists", "hidden", state.error ? 0 : 1);
			if (state.error) {
				dialog.set_df_property(
					"lists_error",
					"options",
					`<p class="text-danger">${frappe.utils.escape_html(error_text)}</p>`
				);
			}
		}

		function fail(what) {
			set_ready(false, __("Could not load the Bosta {0}. Check Bosta Settings and try again.", [what]));
		}

		function on_city_change() {
			// Any city change (typed, picked or programmatic) invalidates the district.
			dialog.set_value("district", "");
			dialog.fields_dict.district.set_data([]);
			load_districts(false);
		}

		function load_districts(refresh) {
			const city_id = dialog.get_value("city");
			const request = ++state.loading_districts;
			if (!city_id || !state.cities.some((c) => c.id === city_id)) {
				if (!state.error) set_ready(true);
				return Promise.resolve();
			}
			set_ready(false);
			return frappe
				.call({ method: BOSTA_DISTRICTS_METHOD, type: "POST", silent: true, args: { city_id, refresh: refresh ? 1 : 0 } })
				.then((r) => {
					// A newer city choice has superseded this answer.
					if (request !== state.loading_districts) return;
					dialog.fields_dict.district.set_data(location_options(r.message || []));
					set_ready(true);
					// Edit starts from the shipment's current district when it is in this city.
					const current = defaults.district_id;
					if (current && !dialog.get_value("district") && (r.message || []).some((d) => d.id === current)) {
						return dialog.set_value("district", current);
					}
				})
				.catch(() => {
					if (request === state.loading_districts) fail(__("districts"));
				});
		}

		function load_cities(refresh) {
			set_ready(false);
			return frappe
				.call({ method: BOSTA_CITIES_METHOD, type: "POST", silent: true, args: { refresh: refresh ? 1 : 0 } })
				.then((r) => {
					state.cities = r.message || [];
					dialog.fields_dict.city.set_data(location_options(state.cities));
					const selected = dialog.get_value("city");
					if (selected && state.cities.some((c) => c.id === selected)) {
						// Refreshing keeps the chosen city; reload its districts too.
						return load_districts(refresh);
					}
					dialog.set_value("city", "");
					dialog.fields_dict.district.set_data([]);
					dialog.set_value("district", "");
					// The district matched from the address names its own city.
					const match =
						state.cities.find((c) => c.id === defaults.city_id) ||
						match_city(state.cities, defaults.address_city);
					set_ready(true);
					if (match) return dialog.set_value("city", match.id);
				})
				.catch(() => fail(__("cities")));
		}

		dialog.show();
		return load_cities(false);
	}

	// The COD that will be sent is the outstanding amount (total minus payments). Values are
	// numbers from the server; formatted and escaped before they reach the HTML field.
	function bosta_cod_note_html(defaults) {
		const escape = frappe.utils.escape_html;
		const format = (value) => escape(format_currency(value || 0));
		let html = `<p><b>${escape(__("COD to be sent to Bosta"))}: ${format(defaults.cod_amount)}</b></p>`;
		if (!defaults.cod_amount) {
			html += `<p>${escape(__("This amount is 0. Bosta may reject a delivery without COD."))}</p>`;
		}
		html += `<p>${escape(
			__(
				"COD is the Sales Order total minus advance payments recorded on the Sales Order. Payments made against a Sales Invoice are not seen, and a partly delivered order still uses the whole-order amount."
			)
		)}</p>`;
		if (defaults.advance_paid) {
			html += `<p>${escape(
				__("Order total {0}, less payments already received {1}.", [
					format_currency(defaults.order_total || 0),
					format_currency(defaults.advance_paid || 0),
				])
			)}</p>`;
		}
		return html;
	}

	// Only submitted (1) or cancelled (2) Sales Orders linked to a Marketplace Order.
	function is_marketplace_sales_order(doc) {
		return Boolean(doc && doc.custom_marketplace_order && doc.docstatus >= 1);
	}

	function is_closed_or_cancelled(doc) {
		return Boolean(doc) && (doc.status === "Closed" || doc.status === "Cancelled");
	}

	// Already requested, already cancelled on the platform, or delivered/billed:
	// there is nothing to ask the merchant, so no dialog at all.
	function needs_no_dialog(option) {
		return Boolean(
			option.already_requested || option.platform_cancelled || option.erp_executed
		);
	}

	function can_offer_cancel(option) {
		return Boolean(option) && option.state === "eligible" && !needs_no_dialog(option);
	}

	// ERPNext has no hook for Close, so the click is remembered on the form and acted
	// on at the next refresh that shows Closed. The flag holds the name of the Sales
	// Order that was Closed. Called on every refresh: returns true only when the refresh
	// is of that same document and shows Closed, and clears the flag either way, so
	// opening or reloading an already Closed order (or another one) never prompts. A
	// failed Close leaves the flag set until the next refresh.
	function consume_close_request(frm) {
		const requested = frm.__mi_close_requested;
		frm.__mi_close_requested = null;
		return Boolean(requested) && requested === frm.doc.name && frm.doc.status === "Closed";
	}

	// Wrap frm.cscript.close_sales_order once per form (an own-property wrapper; the
	// original stays on ERPNext's controller). Returns false when ERPNext no longer has
	// the method, so the caller can report it instead of silently losing the Close prompt.
	function wrap_close_sales_order(frm) {
		if (frm.__mi_close_wrapped) return true;
		const original = frm.cscript && frm.cscript.close_sales_order;
		if (typeof original !== "function") return false;
		frm.cscript.close_sales_order = function (...args) {
			frm.__mi_close_requested = frm.doc.name;
			return original.apply(this, args);
		};
		frm.__mi_close_wrapped = true;
		return true;
	}

	function reason_field(option) {
		const reasons = option.cancellation_reasons || [];
		if (!reasons.length) return null;
		return {
			fieldname: "cancellation_reason",
			fieldtype: "Select",
			label: __("Cancellation Reason"),
			options: reasons.join("\n"),
			reqd: 1,
		};
	}

	// instruction and reasons can contain external platform text, so they are escaped.
	function blocked_message(option) {
		const escape = frappe.utils.escape_html;
		const reasons = (option.reasons || [])
			.map((reason) => `<li>${escape(reason)}</li>`)
			.join("");
		return (
			(option.instruction ? `<p>${escape(option.instruction)}</p>` : "") +
			(reasons ? `<ul>${reasons}</ul>` : "")
		);
	}

	function result_alert(result) {
		if (result.already_requested) {
			return __("Cancellation was already requested.");
		}
		if (result.refreshed === false) {
			return __(
				"Cancellation requested. The Marketplace Order will update on the next webhook or refresh."
			);
		}
		return __("Cancellation requested.");
	}

	function request_cancel(frm, values) {
		return (
			frappe
				.call({
					method: REQUEST_METHOD,
					type: "POST",
					args: {
						sales_order: frm.doc.name,
						cancellation_reason: (values && values.cancellation_reason) || "",
					},
					freeze: true,
					freeze_message: __("Cancelling on the platform..."),
				})
				.then((r) => {
					frappe.show_alert({
						message: result_alert(r.message || {}),
						indicator: "green",
					});
					return frm.reload_doc();
				})
				// A server error already showed Frappe's standard message; the ERPNext
				// Close/Cancel that came before is untouched.
				.catch(() => {})
		);
	}

	function show_cancel_dialog(frm, option) {
		const platform = option.platform;
		const escaped_platform = frappe.utils.escape_html(platform);
		const fields = [
			{
				fieldtype: "HTML",
				fieldname: "warning",
				options: `<p>${frappe.utils.escape_html(
					__("Also cancel this order on {0}? This cannot be undone.", [platform])
				)}</p>`,
			},
		];
		const reason = reason_field(option);
		if (reason) fields.push(reason);
		const dialog = new frappe.ui.Dialog({
			title: __("Cancel on {0}", [escaped_platform]),
			fields,
			primary_action_label: __("Cancel on {0}", [escaped_platform]),
			primary_action(values) {
				dialog.hide();
				request_cancel(frm, values);
			},
			secondary_action_label: __("Not now"),
			secondary_action() {
				dialog.hide();
			},
		});
		dialog.show();
	}

	function show_blocked_dialog(option) {
		frappe.msgprint({
			title: __("Cancel on {0}", [frappe.utils.escape_html(option.platform)]),
			message: blocked_message(option),
			indicator: "orange",
		});
	}

	function add_cancel_button(frm, option) {
		frm.add_custom_button(
			__("Cancel on {0}", [frappe.utils.escape_html(option.platform)]),
			() => show_cancel_dialog(frm, option)
		);
	}

	// One option fetch. `prompt` is true right after a Close/Cancel; `button` says
	// whether the fetch may add the "Cancel on <platform>" button (after_cancel does
	// not: the refresh that follows it clears custom buttons and adds its own).
	function load_platform_option(frm, { prompt, button }) {
		// Only System Manager can read Marketplace Order, and the option endpoint checks
		// read on it. For anyone else that check fails server-side and Frappe itself shows
		// its "Not permitted" popup and re-routes, which a .catch cannot suppress, so the
		// call is not made at all.
		if (!frappe.model.can_read("Marketplace Order")) return Promise.resolve();
		const sales_order = frm.doc.name;
		return (
			frappe
				.call({ method: OPTION_METHOD, type: "POST", args: { sales_order } })
				.then((r) => {
					const option = r.message;
					// The form may have moved on to another document while the call ran.
					if (
						!option ||
						option.state === "not_applicable" ||
						frm.doc.name !== sales_order
					)
						return;
					if (button && can_offer_cancel(option) && is_closed_or_cancelled(frm.doc)) {
						add_cancel_button(frm, option);
					}
					if (!prompt || needs_no_dialog(option)) return;
					if (option.state === "eligible") show_cancel_dialog(frm, option);
					else if (option.state === "blocked") show_blocked_dialog(option);
				})
				// A failed fetch only loses the prompt/button; Frappe has already shown its
				// own error for a server failure, and the ERPNext action is unaffected.
				.catch(() => {})
		);
	}

	function on_refresh(frm) {
		add_bosta_shipment_ux(frm);
		if (!wrap_close_sales_order(frm)) {
			console.error(
				"ERPNext no longer defines close_sales_order on the Sales Order form (#183)."
			);
		}
		const closed_by_click = consume_close_request(frm);
		if (!is_marketplace_sales_order(frm.doc)) return;
		if (closed_by_click || is_closed_or_cancelled(frm.doc)) {
			load_platform_option(frm, { prompt: closed_by_click, button: true });
		}
	}

	function on_after_cancel(frm) {
		if (!is_marketplace_sales_order(frm.doc)) return;
		load_platform_option(frm, { prompt: true, button: false });
	}

	// The Sales Order list loads this file (frappe.require) for the dialog; the flag keeps the
	// form handlers from being registered twice when both the list and the form load it.
	if (typeof frappe !== "undefined") {
		frappe.marketplace_integrations = frappe.marketplace_integrations || {};
		frappe.marketplace_integrations.open_bosta_create_dialog = open_create_dialog;
		if (frappe.ui && frappe.ui.form && !frappe.ui.form.__mi_sales_order_hooks) {
			frappe.ui.form.__mi_sales_order_hooks = true;
			frappe.ui.form.on("Sales Order", {
				refresh: on_refresh,
				after_cancel: on_after_cancel,
			});
		}
	}

	if (typeof module !== "undefined") {
		module.exports = {
			add_bosta_shipment_ux,
			bosta_cod_note_html,
			bosta_indicator_html,
			bosta_tab_html,
			location_options,
			match_city,
			open_create_dialog,
			show_bosta_dialog,
			show_bosta_edit_dialog,
			blocked_message,
			can_offer_cancel,
			consume_close_request,
			is_closed_or_cancelled,
			is_marketplace_sales_order,
			load_platform_option,
			needs_no_dialog,
			on_after_cancel,
			on_refresh,
			reason_field,
			result_alert,
			show_cancel_dialog,
			wrap_close_sales_order,
		};
	}
})();
