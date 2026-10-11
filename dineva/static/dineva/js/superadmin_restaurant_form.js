(() => {
    "use strict";

    const form = document.querySelector("[data-sa-restaurant-form]");
    if (!form) return;

    const logoInput = form.querySelector('input[type="file"][name="logo"]');
    const preview = form.querySelector("[data-sa-logo-preview]");
    const submitButton = form.querySelector("[data-sa-submit]");

    if (logoInput && preview) {
        logoInput.addEventListener("change", () => {
            const file = logoInput.files && logoInput.files[0];
            if (!file || !file.type.startsWith("image/")) {
                preview.innerHTML = '<span aria-hidden="true"><i class="bi bi-image"></i></span><p>No logo selected</p>';
                return;
            }

            const image = document.createElement("img");
            image.alt = "Selected restaurant logo preview";
            image.src = URL.createObjectURL(file);
            image.addEventListener("load", () => URL.revokeObjectURL(image.src), { once: true });
            preview.replaceChildren(image);
        });
    }

    if (submitButton) {
        form.addEventListener("submit", () => {
            submitButton.disabled = true;
            submitButton.setAttribute("aria-busy", "true");
        });
    }
})();
