(() => {
    "use strict";

    const scrollRegions = [...document.querySelectorAll("[data-sa-table-scroll]")];
    if (!scrollRegions.length) return;

    const updateScrollState = (region) => {
        const maxScroll = region.scrollWidth - region.clientWidth;
        const isScrollable = maxScroll > 1;
        region.classList.toggle("is-scrollable", isScrollable);
        region.classList.toggle("is-scroll-start", !isScrollable || region.scrollLeft <= 1);
        region.classList.toggle("is-scroll-end", !isScrollable || region.scrollLeft >= maxScroll - 1);
    };

    const updateAllScrollStates = () => {
        scrollRegions.forEach(updateScrollState);
    };

    scrollRegions.forEach((region) => {
        updateScrollState(region);
        region.addEventListener("scroll", () => updateScrollState(region), { passive: true });
    });

    window.addEventListener("resize", updateAllScrollStates);
    document.querySelectorAll('[data-bs-toggle="tab"]').forEach((tabTrigger) => {
        tabTrigger.addEventListener("shown.bs.tab", updateAllScrollStates);
    });
})();
