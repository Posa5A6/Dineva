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

(() => {
    "use strict";

    const body = document.body;
    const toggle = document.querySelector(".app-menu-toggle");
    const sidebar = document.querySelector("#appSidebar");
    const backdrop = document.querySelector(".app-sidebar-backdrop");
    if (!toggle || !sidebar) return;

    const desktopQuery = window.matchMedia("(min-width: 961px)");
    const storageKey = "dineva:sidebar-collapsed";

    const setDesktopCollapsed = (collapsed) => {
        body.classList.toggle("app-shell-collapsed", collapsed);
        toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
        toggle.setAttribute("aria-label", collapsed ? "Expand navigation" : "Collapse navigation");
    };

    const setMobileOpen = (open) => {
        body.classList.toggle("app-sidebar-open", open);
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        toggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
    };

    const restoreDesktopState = () => {
        if (!desktopQuery.matches) {
            body.classList.remove("app-shell-collapsed");
            setMobileOpen(false);
            return;
        }
        setDesktopCollapsed(window.localStorage.getItem(storageKey) === "1");
    };

    const setSidebarVisibility = () => {
        const hidden = desktopQuery.matches
            ? body.classList.contains("app-shell-collapsed")
            : !body.classList.contains("app-sidebar-open");
        sidebar.setAttribute("aria-hidden", hidden ? "true" : "false");
    };

    toggle.addEventListener("click", () => {
        if (desktopQuery.matches) {
            const next = !body.classList.contains("app-shell-collapsed");
            window.localStorage.setItem(storageKey, next ? "1" : "0");
            setDesktopCollapsed(next);
        } else {
            setMobileOpen(!body.classList.contains("app-sidebar-open"));
        }
        setSidebarVisibility();
    });

    backdrop?.addEventListener("click", () => {
        setMobileOpen(false);
        setSidebarVisibility();
    });

    sidebar.querySelectorAll("a").forEach((link) => {
        link.addEventListener("click", () => {
            if (!desktopQuery.matches) {
                setMobileOpen(false);
                setSidebarVisibility();
            }
        });
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && body.classList.contains("app-sidebar-open")) {
            setMobileOpen(false);
            setSidebarVisibility();
            toggle.focus({ preventScroll: true });
        }
    });

    desktopQuery.addEventListener("change", () => {
        restoreDesktopState();
        setSidebarVisibility();
    });
    restoreDesktopState();
    setSidebarVisibility();
})();
