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
	const BOSTA_CREATE_METHOD = "marketplace_integrations.api.bosta_shipments.create_shipment";

	const BOSTA_CREATOR_ROLES = ["Sales Manager", "Stock User", "System Manager"];

	// The form clears custom buttons on every refresh (Form.refresh -> clear_custom_buttons),
	// so no "already added" flag: the button is added again on each refresh.
	function add_bosta_shipment_button(frm) {
		if (
			!frm.doc ||
			frm.doc.docstatus !== 1 ||
			frm.doc.status === "Cancelled" ||
			!(frappe.user && frappe.user.has_role(BOSTA_CREATOR_ROLES))
		)
			return;
		frm.add_custom_button(__("Create Bosta Shipment"), () => {
			frappe.call({ method: BOSTA_DEFAULTS_METHOD, type: "POST", args: { sales_order: frm.doc.name } }).then((r) => {
				const defaults = r.message || {};
				const dialog = new frappe.ui.Dialog({
					title: __("Create Bosta Shipment"),
					fields: [
						{ fieldtype: "HTML", fieldname: "cod_note", options: bosta_cod_note_html(defaults) },
						{ fieldtype: "HTML", fieldname: "address_help", options: `<p>${__("Enter District ID, or both City ID and District Name.")}</p>` },
						{ fieldname: "receiver_name", fieldtype: "Data", label: __("Receiver Name"), reqd: 1, default: defaults.receiver_name },
						{ fieldname: "receiver_phone", fieldtype: "Data", label: __("Receiver Phone"), reqd: 1, default: defaults.receiver_phone },
						{ fieldname: "city", fieldtype: "Data", label: __("City"), reqd: 1 },
						{ fieldname: "district_id", fieldtype: "Data", label: __("District ID") },
						{ fieldname: "city_id", fieldtype: "Data", label: __("City ID") },
						{ fieldname: "district_name", fieldtype: "Data", label: __("District Name") },
						{ fieldname: "address_first_line", fieldtype: "Small Text", label: __("Address First Line"), reqd: 1 },
					],
					primary_action_label: __("Create Shipment"),
					primary_action(values) {
						dialog.hide();
						frappe.call({
							method: BOSTA_CREATE_METHOD,
							type: "POST",
							args: { sales_order: frm.doc.name, ...values, expected_cod: defaults.cod_amount },
							freeze: true,
							freeze_message: __("Creating Bosta shipment..."),
						}).then(() => frm.reload_doc());
					},
				});
				dialog.show();
			});
		});
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
		add_bosta_shipment_button(frm);
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

	if (typeof frappe !== "undefined" && frappe.ui && frappe.ui.form) {
		frappe.ui.form.on("Sales Order", {
			refresh: on_refresh,
			after_cancel: on_after_cancel,
		});
	}

	if (typeof module !== "undefined") {
		module.exports = {
			bosta_cod_note_html,
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
