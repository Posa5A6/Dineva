(() => {
    "use strict";

    const intro = document.querySelector("[data-dineva-intro]");
    if (!intro) return;

    const State = Object.freeze({
        INITIAL: "initial",
        READY: "ready",
        PLAYING: "playing",
        EXITING: "exiting",
        COMPLETE: "complete",
    });

    const transitions = Object.freeze({
        [State.INITIAL]: [State.READY, State.EXITING, State.COMPLETE],
        [State.READY]: [State.PLAYING, State.EXITING, State.COMPLETE],
        [State.PLAYING]: [State.EXITING, State.COMPLETE],
        [State.EXITING]: [State.COMPLETE],
        [State.COMPLETE]: [],
    });

    const query = new URLSearchParams(window.location.search);
    const introMode = query.get("intro");
    const forceFull = introMode === null || introMode === "full";
    const skip = introMode === "skip";
    const debug = query.get("intro_debug") === "1";
    const reducedMotion = !forceFull && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const startedAt = performance.now();
    let state = State.INITIAL;
    let playTimer;
    let exitTimer;
    let hardTimeout;

    const debugLog = (label) => {
        if (!debug) return;
        console.debug(`[DINEVA INTRO] ${label} ${Math.round(performance.now() - startedAt)}ms`);
    };

    const setState = (next) => {
        if (state === next) return true;
        if (!transitions[state].includes(next)) return false;
        state = next;
        intro.dataset.introState = next;
        window.__DINEVA_INTRO_STATE__ = next;
        return true;
    };

    const lockPage = () => {
        document.documentElement.classList.add("intro-locked");
        document.body.classList.add("intro-locked");
        if (forceFull) document.body.classList.add("intro-force-motion");
    };

    const unlockPage = () => {
        document.documentElement.classList.remove("intro-locked");
        document.body.classList.remove("intro-locked", "intro-force-motion");
    };

    const removeIntentListeners = () => {
        window.removeEventListener("pointerdown", handleIntent);
        window.removeEventListener("wheel", handleIntent);
        window.removeEventListener("touchstart", handleIntent);
        window.removeEventListener("keydown", handleIntent);
    };

    const complete = (reason = "complete") => {
        if (state === State.COMPLETE) return;
        window.clearTimeout(playTimer);
        window.clearTimeout(exitTimer);
        window.clearTimeout(hardTimeout);
        setState(State.COMPLETE);
        intro.dataset.completeReason = reason;
        unlockPage();
        removeIntentListeners();
        document.body.classList.add("intro-complete");
        debugLog(reason === "timeout" ? "TIMEOUT" : "COMPLETE");
    };

    const beginExit = (reason = "timeline") => {
        if (state === State.EXITING || state === State.COMPLETE) return;
        if (!setState(State.EXITING)) {
            complete("error");
            return;
        }
        intro.dataset.exitReason = reason;
        debugLog("EXIT");
        exitTimer = window.setTimeout(() => complete(reason), reducedMotion ? 180 : 650);
    };

    function handleIntent(event) {
        if (event.type === "keydown" && !["Tab", "Enter", " ", "Escape", "ArrowDown", "PageDown"].includes(event.key)) return;
        beginExit(event.type);
    }

    const addIntentListeners = () => {
        window.addEventListener("pointerdown", handleIntent, { passive: true });
        window.addEventListener("wheel", handleIntent, { passive: true });
        window.addEventListener("touchstart", handleIntent, { passive: true });
        window.addEventListener("keydown", handleIntent);
    };

    const criticalAssetsReady = () => {
        const image = intro.querySelector(".intro-material img");
        const imageReady = image?.decode ? image.decode().catch(() => undefined) : Promise.resolve();
        const fontsReady = document.fonts?.ready || Promise.resolve();
        const readinessLimit = new Promise((resolve) => window.setTimeout(resolve, 850));
        return Promise.race([Promise.all([imageReady, fontsReady]), readinessLimit]);
    };

    const play = () => {
        if (state !== State.READY || !setState(State.PLAYING)) return;
        debugLog("PLAY");
        playTimer = window.setTimeout(() => beginExit("timeline"), 3050);
    };

    const initialise = async () => {
        debugLog("INIT");

        if (skip) {
            complete("skip");
            return;
        }

        lockPage();
        addIntentListeners();
        hardTimeout = window.setTimeout(() => complete("timeout"), 7000);

        try {
            await criticalAssetsReady();
            if (state !== State.INITIAL || !setState(State.READY)) return;
            debugLog("READY");

            if (reducedMotion) {
                debugLog("REDUCED MOTION");
                window.setTimeout(() => beginExit("reduced-motion"), 220);
                return;
            }

            requestAnimationFrame(() => requestAnimationFrame(play));
        } catch (_) {
            debugLog("ERROR");
            beginExit("error");
        }
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initialise, { once: true });
    } else {
        initialise();
    }
})();
