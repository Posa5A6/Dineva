(() => {
    "use strict";

    document.querySelectorAll("[data-sa-confirm]").forEach((form) => {
        form.addEventListener("submit", (event) => {
            const message = form.getAttribute("data-sa-confirm");
            if (message && !window.confirm(message)) {
                event.preventDefault();
            }
        });
    });
})();
