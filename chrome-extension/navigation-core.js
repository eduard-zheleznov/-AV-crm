(function initializeNavigationCore(globalObject) {
  "use strict";

  function isExpectedSurface(actualUrl, expectedUrl, mode, runtime) {
    if (runtime.isManualSurface(actualUrl)) {
      return true;
    }
    if (mode === "health") {
      return Boolean(runtime.avitoUrl(actualUrl)) && !runtime.listingId(actualUrl);
    }
    return runtime.sameListingIdentity(actualUrl, expectedUrl);
  }

  function surfaceClass(value, runtime) {
    const raw = String(value || "");
    if (!raw) {
      return "empty";
    }
    if (raw === "about:blank") {
      return "about:blank";
    }
    const avito = runtime.avitoUrl(raw);
    if (avito) {
      if (runtime.isManualSurface(raw)) {
        return "avito:manual";
      }
      if (runtime.listingId(raw)) {
        return "avito:listing";
      }
      return avito.pathname === "/" ? "avito:root" : "avito:path";
    }
    try {
      const parsed = new URL(raw);
      const scheme = parsed.protocol.replace(/:$/, "").toLowerCase();
      return ["http", "https", "chrome", "chrome-extension"].includes(scheme)
        ? `${scheme}:other`
        : "other";
    } catch (_error) {
      return "invalid";
    }
  }

  function contentGate(tab, expectedUrl, mode, runtime) {
    if (!tab || !Number.isInteger(tab.id)) {
      return { ok: false, code: "tab_unavailable" };
    }
    if (String(tab.pendingUrl || "")) {
      return { ok: false, code: "navigation_pending" };
    }
    if (tab.discarded || tab.status === "unloaded") {
      return { ok: false, code: "document_unavailable" };
    }
    if (!isExpectedSurface(String(tab.url || ""), expectedUrl, mode, runtime)) {
      return { ok: false, code: "unexpected_surface" };
    }
    return { ok: true, code: "committed_expected_surface" };
  }

  function shouldInject(context, runtime) {
    const gate = contentGate(
      context.tab,
      context.expectedUrl,
      context.mode,
      runtime
    );
    if (!gate.ok) {
      return gate;
    }
    if (!context.missingReceiver) {
      return { ok: false, code: "receiver_error_not_missing" };
    }
    if (context.attempted) {
      return { ok: false, code: "injection_already_attempted" };
    }
    if (Number(context.committedForMs) < Number(context.graceMs)) {
      return { ok: false, code: "content_script_grace" };
    }
    return { ok: true, code: "inject_content_files" };
  }

  function renderedProbeToken(probe) {
    if (!probe || !probe.rendered) {
      return "";
    }
    const bodyLength = Math.max(0, Number(probe.bodyLength) || 0);
    const bodyBucket = Math.floor(bodyLength / 100);
    return [
      String(probe.readyState || "unknown"),
      bodyBucket,
      Math.max(0, Number(probe.visibleHeadings) || 0),
      Boolean(probe.hasPhone),
      Boolean(probe.hasPhoneButton),
      String(probe.contentVersion || "")
    ].join(":");
  }

  function advanceRenderedStability(previous, probe, nowMs, options = {}) {
    const token = renderedProbeToken(probe);
    const now = Number(nowMs) || 0;
    const minSamples = Math.max(2, Number(options.minSamples) || 4);
    const minStableMs = Math.max(500, Number(options.minStableMs) || 1500);
    const same = Boolean(previous && token && previous.token === token);
    const state = {
      token,
      samples: same ? previous.samples + 1 : token ? 1 : 0,
      sinceMs: same ? previous.sinceMs : now
    };
    state.stableForMs = state.samples ? Math.max(0, now - state.sinceMs) : 0;
    state.ready = Boolean(
      token && state.samples >= minSamples && state.stableForMs >= minStableMs
    );
    return state;
  }

  async function injectContentFiles(chromeApi, tabId, expectedUrl, mode, runtime) {
    let tab = null;
    try {
      tab = await chromeApi.tabs.get(tabId);
    } catch (error) {
      return { attempted: false, status: "tab_lookup_failed", error };
    }
    const gate = contentGate(tab, expectedUrl, mode, runtime);
    if (!gate.ok) {
      return { attempted: false, status: `skipped_${gate.code}` };
    }
    try {
      await chromeApi.scripting.executeScript({
        target: { tabId, frameIds: [0] },
        files: ["runtime-core.js", "content.js"]
      });
      return { attempted: true, status: "injected" };
    } catch (error) {
      return { attempted: true, status: "execute_failed", error };
    }
  }

  const api = {
    advanceRenderedStability,
    contentGate,
    injectContentFiles,
    isExpectedSurface,
    renderedProbeToken,
    shouldInject,
    surfaceClass
  };
  globalObject.AVITO_CRM_NAVIGATION_CORE = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(globalThis);
