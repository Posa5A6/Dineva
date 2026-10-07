(() => {
    "use strict";

    const menu = document.querySelector("#mainNav");
    const toggle = document.querySelector("[data-bs-target='#mainNav']");
    if (!menu || !toggle || !window.bootstrap?.Collapse) return;

    const mobileQuery = window.matchMedia("(max-width: 960px)");
    const collapse = window.bootstrap.Collapse.getOrCreateInstance(menu, { toggle: false });
    const header = menu.closest(".site-header");

    const updateHeaderAppearance = () => {
        header?.classList.toggle("is-scrolled", window.scrollY > 12);
    };

    const setOpenState = (open) => {
        document.documentElement.classList.toggle("nav-menu-open", open);
        document.body.classList.toggle("nav-menu-open", open);
        toggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
    };

    menu.addEventListener("show.bs.collapse", () => setOpenState(true));
    menu.addEventListener("hidden.bs.collapse", () => {
        setOpenState(false);
        if (mobileQuery.matches) toggle.focus({ preventScroll: true });
    });

    menu.querySelectorAll("a").forEach((link) => {
        link.addEventListener("click", () => {
            if (mobileQuery.matches && menu.classList.contains("show")) collapse.hide();
        });
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && mobileQuery.matches && menu.classList.contains("show")) {
            collapse.hide();
        }
    });

    const handleBreakpointChange = (event) => {
        if (!event.matches) {
            collapse.hide();
            setOpenState(false);
        }
    };

    mobileQuery.addEventListener("change", handleBreakpointChange);
    window.addEventListener("scroll", updateHeaderAppearance, { passive: true });
    updateHeaderAppearance();
})();
