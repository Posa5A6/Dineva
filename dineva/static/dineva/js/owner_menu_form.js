(() => {
    "use strict";

    const form = document.querySelector("[data-owner-menu-form]");
    if (!form) return;

    const imageInput = form.querySelector('input[type="file"][name="image"]');
    const preview = form.querySelector("[data-owner-menu-image-preview]");
    const submitButton = form.querySelector("[data-owner-menu-submit]");

    if (imageInput && preview) {
        imageInput.addEventListener("change", () => {
            const file = imageInput.files && imageInput.files[0];
            if (!file || !file.type.startsWith("image/")) {
                preview.innerHTML = '<span aria-hidden="true"><i class="bi bi-card-image"></i></span><p>No image selected</p>';
                return;
            }

            const image = document.createElement("img");
            image.alt = "Selected menu item image preview";
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
