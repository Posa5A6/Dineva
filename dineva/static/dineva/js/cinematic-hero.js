(() => {
    const hero = document.querySelector("[data-cinematic-hero]");
    if (!hero) return;

    const query = new URLSearchParams(window.location.search);
    const motionPreview = query.get("motion") === "full";
    const reducedMotion = !motionPreview && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const sessionKey = "dinevaQuietChoreographySeenV1";
    let repeatVisit = false;

    document.body.classList.add("cinematic-ready");
    if (motionPreview) document.body.classList.add("motion-preview");

    try {
        repeatVisit = !motionPreview && sessionStorage.getItem(sessionKey) === "true";
        sessionStorage.setItem(sessionKey, "true");
    } catch (_) {
        repeatVisit = false;
    }

    const clearOpeningState = () => {
        document.body.classList.remove("hero-opening", "hero-first", "hero-repeat", "hero-motion-started");
    };

    if (reducedMotion) {
        hero.classList.add("is-resolved");
        hero.dataset.heroState = "resolved";
        hero.dataset.resolveReason = "reduced-motion";
        clearOpeningState();
        return;
    }

    const duration = repeatVisit ? 920 : 5600;
    hero.classList.add(repeatVisit ? "is-repeat-visit" : "is-first-visit");
    document.body.classList.add("hero-opening", repeatVisit ? "hero-repeat" : "hero-first");

    let resolutionTimer;

    const removeListeners = () => {
        window.removeEventListener("wheel", handleInteraction);
        window.removeEventListener("pointerdown", handleInteraction);
        window.removeEventListener("touchstart", handleInteraction);
        window.removeEventListener("keydown", handleInteraction);
    };

    const resolveHero = (reason = "timer") => {
        if (hero.classList.contains("is-resolved")) return;
        window.clearTimeout(resolutionTimer);
        hero.classList.remove("is-running");
        hero.classList.add("is-resolved");
        hero.dataset.heroState = "resolved";
        hero.dataset.resolveReason = reason;
        clearOpeningState();
        removeListeners();
    };

    function handleInteraction(event) {
        if (event.type === "keydown" && !["Tab", "Enter", " ", "ArrowDown", "PageDown"].includes(event.key)) return;
        resolveHero(event.type);
    }

    window.addEventListener("wheel", handleInteraction, { passive: true, once: true });
    window.addEventListener("pointerdown", handleInteraction, { passive: true, once: true });
    window.addEventListener("touchstart", handleInteraction, { passive: true, once: true });
    window.addEventListener("keydown", handleInteraction);

    const image = hero.querySelector(".hero-material img");
    const imageReady = image?.decode ? image.decode().catch(() => undefined) : Promise.resolve();
    const fontsReady = document.fonts?.ready || Promise.resolve();
    const readinessLimit = new Promise((resolve) => window.setTimeout(resolve, repeatVisit ? 420 : 1600));

    Promise.race([Promise.all([imageReady, fontsReady]), readinessLimit]).then(() => {
        if (hero.classList.contains("is-resolved")) return;
        requestAnimationFrame(() => requestAnimationFrame(() => {
            if (hero.classList.contains("is-resolved")) return;
            document.body.classList.add("hero-motion-started");
            hero.classList.add("is-running");
            resolutionTimer = window.setTimeout(() => resolveHero("timer"), duration);
        }));
    });
})();
