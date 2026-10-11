(() => {
    "use strict";

    const form = document.querySelector("[data-sa-owner-form]");
    if (!form) return;

    const photoInput = form.querySelector('input[type="file"][name="photo"]');
    const photoPreview = form.querySelector("[data-sa-owner-photo-preview]");
    const proofInput = form.querySelector('input[type="file"][name="id_proof_file"]');
    const proofName = form.querySelector("[data-sa-owner-proof-name]");
    const submitButton = form.querySelector("[data-sa-submit]");

    if (photoInput && photoPreview) {
        photoInput.addEventListener("change", () => {
            const file = photoInput.files && photoInput.files[0];
            if (!file || !file.type.startsWith("image/")) {
                photoPreview.innerHTML = '<span aria-hidden="true"><i class="bi bi-camera"></i></span><p>No photo selected</p>';
                return;
            }

            const image = document.createElement("img");
            image.alt = "Selected owner profile photo preview";
            image.src = URL.createObjectURL(file);
            image.addEventListener("load", () => URL.revokeObjectURL(image.src), { once: true });
            photoPreview.replaceChildren(image);
        });
    }

    if (proofInput && proofName) {
        proofInput.addEventListener("change", () => {
            const file = proofInput.files && proofInput.files[0];
            proofName.textContent = file ? file.name : "No document selected";
        });
    }

    if (submitButton) {
        form.addEventListener("submit", () => {
            submitButton.disabled = true;
            submitButton.setAttribute("aria-busy", "true");
        });
    }
})();
