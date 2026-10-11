(() => {
    "use strict";

    const money = new Intl.NumberFormat("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    });

    const parseMoney = (value) => {
        const amount = Number.parseFloat(String(value || "0").replace(/,/g, ""));
        return Number.isFinite(amount) ? amount : 0;
    };

    const escapeHtml = (value) => String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");

    const setText = (root, selector, value) => {
        const element = root.querySelector(selector);
        if (element) element.textContent = value;
    };

    const getCookie = (name) => {
        const prefix = `${name}=`;
        const match = document.cookie
            .split(";")
            .map((value) => value.trim())
            .find((value) => value.startsWith(prefix));
        return match ? match.slice(prefix.length) : "";
    };

    const csrfToken = (form) => {
        const input = form && form.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input) return input.value;
        const pageInput = document.querySelector('input[name="csrfmiddlewaretoken"]');
        return pageInput ? pageInput.value : getCookie("csrftoken");
    };

    const showBox = (box, text, variant = "danger") => {
        if (!box) return;
        box.textContent = text;
        box.classList.remove("d-none", "alert-danger", "alert-success");
        box.classList.add(`alert-${variant}`);
    };

    const showHtmlBox = (box, html, variant = "danger") => {
        if (!box) return;
        box.innerHTML = html;
        box.classList.remove("d-none", "alert-danger", "alert-success");
        box.classList.add(`alert-${variant}`);
    };

    const hideBox = (box) => {
        if (!box) return;
        box.textContent = "";
        box.classList.add("d-none");
    };

    const buildErrorMessages = (payload) => {
        const messages = [];
        const errors = payload.errors || {};
        (errors.non_field_errors || []).forEach((message) => messages.push(message));
        Object.entries(errors.fields || {}).forEach(([field, fieldErrors]) => {
            fieldErrors.forEach((message) => messages.push(`${field}: ${message}`));
        });
        if (!messages.length && payload.message) messages.push(payload.message);
        return messages;
    };

    const renderServeForm = (actions, item) => {
        actions.textContent = "";
        if (item.status !== "READY" || !item.served_url) {
            const muted = document.createElement("span");
            muted.className = "text-muted";
            muted.textContent = "-";
            actions.appendChild(muted);
            return;
        }

        const form = document.createElement("form");
        form.method = "post";
        form.action = item.served_url;
        form.className = "d-inline";
        form.dataset.waiterServeForm = "";

        const csrfInput = document.createElement("input");
        csrfInput.type = "hidden";
        csrfInput.name = "csrfmiddlewaretoken";
        csrfInput.value = csrfToken();
        form.appendChild(csrfInput);

        const nextInput = document.createElement("input");
        nextInput.type = "hidden";
        nextInput.name = "next";
        nextInput.value = window.location.pathname + window.location.search;
        form.appendChild(nextInput);

        const button = document.createElement("button");
        button.className = "btn om-btn-primary btn-sm";
        button.type = "submit";
        button.textContent = "Mark Served";
        form.appendChild(button);
        actions.appendChild(form);
    };

    const updateOrderItemRow = (item) => {
        const itemRow = document.querySelector(`[data-order-item-id="${item.id}"]`);
        if (!itemRow) return;
        setText(itemRow, "[data-order-item-status]", item.status_display);
        const actions = itemRow.querySelector("[data-order-item-actions]");
        if (actions) renderServeForm(actions, item);
    };

    const updateStatusRow = (order) => {
        const row = document.querySelector(`[data-order-status-row][data-order-id="${order.id}"]`);
        if (!row) return;
        const orderStatus = row.querySelector("[data-order-status]");
        if (orderStatus) {
            orderStatus.innerHTML = '<span aria-hidden="true"></span>' + escapeHtml(order.status_display);
            orderStatus.classList.toggle("is-available", order.status === "ACTIVE");
            orderStatus.classList.toggle("is-inactive", order.status !== "ACTIVE");
        }
        const kitchenStatus = row.querySelector("[data-kitchen-status]");
        if (kitchenStatus && order.kitchen_status) {
            kitchenStatus.textContent = `${order.kitchen_status.label} (${order.kitchen_status.served_count}/${order.kitchen_status.total_count} served)`;
        }
        (order.items || []).forEach(updateOrderItemRow);
    };

    const updateSessionPanel = (session) => {
        const root = document.querySelector("[data-waiter-session-state]");
        if (!root || !session) return;
        setText(root, "[data-session-status]", session.status_display || "");
        setText(root, "[data-session-bill]", session.bill && session.bill.invoice_number ? session.bill.invoice_number : "Not finalized");
        setText(root, "[data-session-paid]", session.bill ? session.bill.paid_total : "0.00");
        setText(root, "[data-session-balance]", session.bill ? session.bill.balance_due : "0.00");
        setText(root, "[data-session-next-message]", session.next_message || "");

        const addRoundLink = root.querySelector("[data-session-add-round-link]");
        if (addRoundLink) {
            addRoundLink.setAttribute("href", session.add_round_url || "#");
            addRoundLink.classList.toggle("disabled", !session.can_add_round);
            addRoundLink.setAttribute("aria-disabled", session.can_add_round ? "false" : "true");
        }

        const checkoutLink = root.querySelector("[data-session-checkout-link]");
        if (checkoutLink) {
            checkoutLink.setAttribute("href", session.checkout_url || "#");
            checkoutLink.classList.toggle("disabled", !session.can_checkout);
            checkoutLink.setAttribute("aria-disabled", session.can_checkout ? "false" : "true");
        }

        (session.orders || []).forEach(updateStatusRow);
    };

    const updateLegacyOrderPanel = (legacyOrder) => {
        const root = document.querySelector("[data-legacy-order-state]");
        if (!root || !legacyOrder) return;
        setText(root, "[data-legacy-order-message]", legacyOrder.next_message || "");
        setText(root, "[data-legacy-order-paid]", legacyOrder.paid_total || "0.00");
    };

    const initOrderForms = () => {
        const forms = Array.from(document.querySelectorAll("[data-waiter-order-form], [data-waiter-round-form]"));
        forms.forEach((form) => {
            const orderType = form.querySelector('[name="order_type"]');
            const tableField = form.querySelector("[data-table-field]");
            const tableSelect = form.querySelector('[name="table"]');
            const submitButton = form.querySelector("[data-order-submit]");
            const errorBox = form.querySelector("[data-order-form-errors]");
            const successBox = form.querySelector("[data-order-form-success]");
            const tableMessage = form.querySelector("[data-table-availability-message]");
            const lines = Array.from(form.querySelectorAll("[data-menu-line]"));
            let tablePollInFlight = false;
            let tablePollInterval = 10000;
            let tablePollId = null;
            let submitInFlight = false;

            const refreshTableRequirement = () => {
                if (!orderType || !tableField) return;
                const takeaway = orderType.value === "TAKEAWAY";
                tableField.classList.toggle("opacity-50", takeaway);
                if (tableSelect) {
                    tableSelect.disabled = takeaway;
                    if (takeaway) tableSelect.value = "";
                }
            };

            const refreshCart = () => {
                let selectedCount = 0;
                let previewTotal = 0;
                lines.forEach((line) => {
                    const input = line.querySelector("input");
                    const quantity = Math.max(0, Number.parseInt(input && input.value, 10) || 0);
                    const lineTotal = quantity * parseMoney(line.dataset.itemPrice);
                    if (quantity > 0) selectedCount += quantity;
                    previewTotal += lineTotal;
                    setText(line, "[data-line-total]", money.format(lineTotal));
                });
                setText(form, "[data-cart-count]", String(selectedCount));
                setText(form, "[data-cart-total]", money.format(previewTotal));
            };

            const refreshTableOptions = (tables) => {
                if (!tableSelect) return;
                const selectedValue = tableSelect.value;
                const availableTables = (tables || []).filter((table) => table.is_available);
                tableSelect.innerHTML = "";
                const emptyOption = document.createElement("option");
                emptyOption.value = "";
                emptyOption.textContent = "Select an available table";
                tableSelect.appendChild(emptyOption);
                availableTables.forEach((table) => {
                    const option = document.createElement("option");
                    option.value = String(table.id);
                    option.textContent = `Table ${table.table_no}`;
                    tableSelect.appendChild(option);
                });
                if (selectedValue && availableTables.some((table) => String(table.id) === selectedValue)) {
                    tableSelect.value = selectedValue;
                }
                if (tableMessage) {
                    tableMessage.textContent = availableTables.length
                        ? `${availableTables.length} table${availableTables.length === 1 ? "" : "s"} currently available.`
                        : "No tables are currently available for a new dine-in session.";
                }
                refreshTableRequirement();
            };

            const pollTableAvailability = async () => {
                if (!form.dataset.tableAvailabilityUrl || document.hidden || tablePollInFlight || !window.fetch) return;
                tablePollInFlight = true;
                try {
                    const response = await fetch(form.dataset.tableAvailabilityUrl, {
                        headers: {
                            "Accept": "application/json",
                            "X-Requested-With": "XMLHttpRequest",
                        },
                        credentials: "same-origin",
                    });
                    if (!response.ok) {
                        if (response.status === 401 || response.status === 403) {
                            if (tableMessage) tableMessage.textContent = "Sign in again to refresh table availability.";
                            stopTablePolling();
                        }
                        return;
                    }
                    const payload = await response.json();
                    tablePollInterval = payload.poll_interval_ms || tablePollInterval;
                    refreshTableOptions(payload.tables || []);
                } catch (error) {
                    if (tableMessage) tableMessage.textContent = "Unable to refresh table availability right now.";
                } finally {
                    tablePollInFlight = false;
                }
            };

            function startTablePolling() {
                if (tablePollId || !form.dataset.tableAvailabilityUrl) return;
                pollTableAvailability();
                tablePollId = window.setInterval(pollTableAvailability, tablePollInterval);
            }

            function stopTablePolling() {
                if (!tablePollId) return;
                window.clearInterval(tablePollId);
                tablePollId = null;
            }

            lines.forEach((line) => {
                const input = line.querySelector("input");
                if (!input) return;
                input.addEventListener("input", refreshCart);
                input.addEventListener("change", refreshCart);
            });
            if (orderType) orderType.addEventListener("change", refreshTableRequirement);

            form.addEventListener("submit", async (event) => {
                if (!window.fetch || !window.FormData || submitInFlight) return;
                event.preventDefault();
                hideBox(errorBox);
                hideBox(successBox);
                submitInFlight = true;
                if (submitButton) {
                    submitButton.disabled = true;
                    submitButton.setAttribute("aria-busy", "true");
                }
                try {
                    const response = await fetch(form.action || window.location.href, {
                        method: "POST",
                        body: new FormData(form),
                        headers: {
                            "Accept": "application/json",
                            "X-Requested-With": "XMLHttpRequest",
                            "X-CSRFToken": csrfToken(form),
                        },
                        credentials: "same-origin",
                    });
                    const payload = await response.json().catch(() => ({}));
                    if (!response.ok || !payload.ok) {
                        const tokenInput = form.querySelector('[name="submission_key"]');
                        if (payload.new_submission_key && tokenInput) {
                            tokenInput.value = payload.new_submission_key;
                        }
                        const messages = buildErrorMessages(payload);
                        showHtmlBox(
                            errorBox,
                            messages.length
                                ? messages.map((message) => `<div>${escapeHtml(message)}</div>`).join("")
                                : "Unable to save this order. Please review and try again.",
                            "danger"
                        );
                        return;
                    }
                    const order = payload.order || {};
                    const tokenInput = form.querySelector('[name="submission_key"]');
                    if (payload.new_submission_key && tokenInput) {
                        tokenInput.value = payload.new_submission_key;
                    }
                    const detailUrl = order.session_url || order.detail_url || payload.list_url || form.dataset.sessionDetailUrl || form.dataset.successListUrl || "#";
                    const detailLabel = order.session_url || form.dataset.sessionDetailUrl ? "View table session" : "View order";
                    showHtmlBox(
                        successBox,
                        `Order #${escapeHtml(order.id)} saved. <a href="${escapeHtml(detailUrl)}">${escapeHtml(detailLabel)}</a>.`,
                        "success"
                    );
                    form.querySelectorAll('input[type="number"]').forEach((input) => {
                        input.value = "0";
                    });
                    refreshCart();
                    pollTableAvailability();
                    if (payload.session) updateSessionPanel(payload.session);
                } catch (error) {
                    showBox(errorBox, "Network error. Your order was not confirmed; please try again before leaving this page.", "danger");
                } finally {
                    submitInFlight = false;
                    if (submitButton) {
                        submitButton.disabled = false;
                        submitButton.removeAttribute("aria-busy");
                    }
                }
            });

            document.addEventListener("visibilitychange", () => {
                if (document.hidden) stopTablePolling();
                else startTablePolling();
            });

            refreshTableRequirement();
            refreshCart();
            startTablePolling();
        });
    };

    const initServeActions = () => {
        document.addEventListener("submit", async (event) => {
            const form = event.target.closest("[data-waiter-serve-form]");
            if (!form || !window.fetch || form.dataset.requestPending === "true") return;
            event.preventDefault();
            const button = form.querySelector('button[type="submit"]');
            const feedbackRoot = document.querySelector("[data-waiter-session-state]") || document.querySelector("[data-waiter-order-detail]");
            const successBox = feedbackRoot && feedbackRoot.querySelector("[data-session-success]");
            const errorBox = feedbackRoot && feedbackRoot.querySelector("[data-session-error]");
            hideBox(successBox);
            hideBox(errorBox);
            form.dataset.requestPending = "true";
            if (button) {
                button.disabled = true;
                button.setAttribute("aria-busy", "true");
            }
            try {
                const response = await fetch(form.action, {
                    method: "POST",
                    body: new FormData(form),
                    headers: {
                        "Accept": "application/json",
                        "X-Requested-With": "XMLHttpRequest",
                        "X-CSRFToken": csrfToken(form),
                    },
                    credentials: "same-origin",
                });
                const payload = await response.json().catch(() => ({}));
                if (!response.ok || !payload.ok) {
                    showBox(errorBox, payload.message || "Unable to mark this item served.", "danger");
                    return;
                }
                if (payload.order) updateStatusRow(payload.order);
                if (payload.session) updateSessionPanel(payload.session);
                if (payload.legacy_order) updateLegacyOrderPanel(payload.legacy_order);
                showBox(successBox, payload.message || "Item marked served.", "success");
            } catch (error) {
                showBox(errorBox, "Network error. Please verify the item before trying again.", "danger");
            } finally {
                form.dataset.requestPending = "false";
                if (button) {
                    button.disabled = false;
                    button.removeAttribute("aria-busy");
                }
            }
        });
    };

    const initStatusPolling = () => {
        const root = document.querySelector("[data-waiter-status-poll]");
        if (!root || !root.dataset.statusUrl || !window.fetch) return;
        let inFlight = false;
        let intervalId = null;
        let pollInterval = 7000;

        const currentIds = () => Array.from(root.querySelectorAll("[data-order-status-row]"))
            .map((row) => row.dataset.orderId)
            .filter(Boolean);

        const poll = async () => {
            if (document.hidden || inFlight) return;
            const ids = currentIds();
            if (!ids.length) return;
            inFlight = true;
            try {
                const url = new URL(root.dataset.statusUrl, window.location.origin);
                url.searchParams.set("ids", ids.join(","));
                const response = await fetch(url.toString(), {
                    headers: {"Accept": "application/json"},
                    credentials: "same-origin",
                });
                if (!response.ok) return;
                const payload = await response.json();
                pollInterval = payload.poll_interval_ms || pollInterval;
                (payload.orders || []).forEach(updateStatusRow);
            } finally {
                inFlight = false;
            }
        };

        const start = () => {
            if (intervalId) return;
            poll();
            intervalId = window.setInterval(poll, pollInterval);
        };

        const stop = () => {
            if (!intervalId) return;
            window.clearInterval(intervalId);
            intervalId = null;
        };

        document.addEventListener("visibilitychange", () => {
            if (document.hidden) stop();
            else start();
        });
        start();
    };

    initOrderForms();
    initServeActions();
    initStatusPolling();
})();
