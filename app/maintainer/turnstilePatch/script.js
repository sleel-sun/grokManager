(() => {
    if (window.__grokManagerTurnstilePatchApplied) {
        return;
    }
    window.__grokManagerTurnstilePatchApplied = true;

    function getRandomInt(min, max) {
        return Math.floor(Math.random() * (max - min + 1)) + min;
    }

    const screenX = getRandomInt(800, 1200);
    const screenY = getRandomInt(400, 600);

    try {
        Object.defineProperty(MouseEvent.prototype, "screenX", { value: screenX });
        Object.defineProperty(MouseEvent.prototype, "screenY", { value: screenY });
    } catch (error) {
        // Some pages may already have a non-configurable patch installed.
    }

    const state = window.__grokManagerTurnstile = window.__grokManagerTurnstile || {
        callbacks: {},
        renders: [],
    };

    function compact(value) {
        return String(value || "").trim();
    }

    function recordRender(container, options, widgetId) {
        if (!options || typeof options !== "object") {
            return;
        }
        const callbackId = `cb_${Date.now()}_${Math.random().toString(36).slice(2)}`;
        if (typeof options.callback === "function") {
            state.callbacks[callbackId] = options.callback;
        }
        const record = {
            action: compact(options.action),
            cData: compact(options.cData || options.cdata || options.data),
            callbackId,
            chlPageData: compact(options.chlPageData || options.chlPageData2 || options.pagedata || options.pageData),
            sitekey: compact(options.sitekey || options.siteKey || options.websiteKey),
            url: compact(location.href),
            widgetId: compact(widgetId),
        };
        if (!record.sitekey) {
            return;
        }
        state.renders.push(record);
        state.last = record;
        state.lastCallbackId = callbackId;
    }

    function wrapTurnstile(turnstile) {
        if (!turnstile || turnstile.__grokManagerWrapped) {
            return turnstile;
        }
        const originalRender = turnstile.render;
        if (typeof originalRender !== "function") {
            return turnstile;
        }
        Object.defineProperty(turnstile, "__grokManagerWrapped", {
            value: true,
            configurable: true,
        });
        turnstile.render = function patchedRender(container, options) {
            const widgetId = originalRender.apply(this, arguments);
            try {
                recordRender(container, options, widgetId);
            } catch (error) {
                state.lastError = String(error && error.message || error || "");
            }
            return widgetId;
        };
        return turnstile;
    }

    let storedTurnstile = window.turnstile;
    if (storedTurnstile) {
        storedTurnstile = wrapTurnstile(storedTurnstile);
    }

    try {
        Object.defineProperty(window, "turnstile", {
            configurable: true,
            get() {
                return storedTurnstile;
            },
            set(value) {
                storedTurnstile = wrapTurnstile(value);
            },
        });
    } catch (error) {
        if (window.turnstile) {
            wrapTurnstile(window.turnstile);
        }
    }
})();
