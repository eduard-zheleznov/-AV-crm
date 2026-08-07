(function initializeTrustedClick(globalObject) {
  "use strict";

  function validateRequest(request, context) {
    if (!request || request.commandId !== context.currentCommandId) {
      return { ok: false, code: "command_mismatch" };
    }
    if (context.currentCommandType !== "reveal_phone") {
      return { ok: false, code: "command_type_mismatch" };
    }
    if (
      !Number.isInteger(context.senderTabId) ||
      context.senderTabId !== context.managedTabId
    ) {
      return { ok: false, code: "tab_mismatch" };
    }
    if (!context.sameListingIdentity(context.actualUrl, context.expectedUrl)) {
      return { ok: false, code: "listing_mismatch" };
    }
    if (request.activation !== "ui_eval") {
      return { ok: false, code: "invalid_activation" };
    }
    return { ok: true, code: "validated" };
  }

  async function dispatchUserGestureClick(chromeApi, request, options = {}) {
    const timeoutMs = Math.max(1000, Number(options.timeoutMs) || 2500);
    const target = { tabId: request.tabId };
    let attached = false;
    let attachPromise = null;
    let result = { ok: false, code: "browser_click_unavailable" };
    try {
      attachPromise = chromeApi.debugger.attach(target, "1.3");
      await withTimeout(attachPromise, timeoutMs, "debugger_attach_timeout");
      attached = true;
      if (typeof options.revalidate !== "function") {
        throw codedError("pre_dispatch_validation_unavailable");
      }
      const liveValidation = await withTimeout(
        Promise.resolve().then(() => options.revalidate()),
        timeoutMs,
        "pre_dispatch_validation_timeout"
      );
      if (!liveValidation?.ok) {
        throw codedError(liveValidation?.code || "pre_dispatch_validation_failed");
      }
      const evaluation = await withTimeout(
        chromeApi.debugger.sendCommand(target, "Runtime.evaluate", {
          expression: mainWorldRevealExpression(request.listingId),
          userGesture: true,
          awaitPromise: false,
          returnByValue: true
        }),
        timeoutMs,
        "ui_gesture_evaluate_timeout"
      );
      if (evaluation?.exceptionDetails) {
        throw codedError("ui_gesture_evaluate_failed");
      }
      const payload = evaluation?.result?.value;
      if (!payload?.ok) {
        throw codedError(payload?.code || "ui_gesture_click_failed");
      }
      result = {
        ok: true,
        code: "ui_gesture_click_dispatched",
        eventTrusted: Boolean(payload.eventTrusted),
        userActivation: Boolean(payload.userActivation),
        targetKind: safeTargetKind(payload.targetKind)
      };
    } catch (error) {
      if (!attached && attachPromise) {
        attachPromise
          .then(() => withTimeout(chromeApi.debugger.detach(target), timeoutMs, "detach_timeout"))
          .catch(() => undefined);
      }
      result = { ok: false, code: errorCode(error) };
    } finally {
      if (attached) {
        const detached = await withTimeout(
          chromeApi.debugger.detach(target),
          timeoutMs,
          "debugger_detach_timeout"
        )
          .then(() => true)
          .catch(() => false);
        if (!detached && result.ok) {
          result = { ...result, code: "ui_gesture_dispatched_detach_unconfirmed" };
        }
      }
    }
    return result;
  }

  function mainWorldRevealExpression(listingId) {
    const safeListingId = /^\d{6,20}$/.test(String(listingId || ""))
      ? String(listingId)
      : "";
    if (!safeListingId) {
      throw codedError("listing_mismatch");
    }
    const serializedListingId = JSON.stringify(safeListingId);
    return `(() => {
      const expectedId = ${serializedListingId};
      const actualId = (location.pathname.match(/_(\\d+)(?:\\/?$)/) || [])[1] || "";
      if (actualId !== expectedId) return {ok:false, code:"listing_mismatch"};
      if (document.readyState !== "complete") return {ok:false, code:"document_not_complete"};
      const revealRe = /(?:показать\\s+(?:номер(?:\\s+телефона)?|телефон)|позвонить)/i;
      const visible = (element) => {
        if (!element || !element.isConnected) return false;
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" &&
          Number(style.opacity || "1") > 0 && rect.width >= 2 && rect.height >= 2 &&
          rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
      };
      let button = null;
      for (const element of document.querySelectorAll('[data-marker*="phone" i], button, a, [role="button"]')) {
        const label = String(element.textContent || "") + " " + String(element.getAttribute("aria-label") || "");
        if (!visible(element) || !revealRe.test(label) || element.disabled ||
            element.getAttribute("aria-disabled") === "true") continue;
        const candidate = element.closest('button, a, [role="button"]') || element;
        if (!visible(candidate) || candidate.disabled || candidate.getAttribute("aria-disabled") === "true") continue;
        const rect = candidate.getBoundingClientRect();
        const x = Math.min(innerWidth - 1, Math.max(0, rect.left + rect.width / 2));
        const y = Math.min(innerHeight - 1, Math.max(0, rect.top + rect.height / 2));
        const top = document.elementFromPoint(x, y);
        if (top && (top === candidate || candidate.contains(top))) {
          button = candidate;
          break;
        }
      }
      if (!button) return {ok:false, code:"click_target_unavailable"};
      button.scrollIntoView({behavior:"auto", block:"center", inline:"center"});
      button.focus({preventScroll:true});
      let eventTrusted = false;
      const observe = (event) => {
        if (event.target === button || button.contains(event.target)) eventTrusted = event.isTrusted === true;
      };
      document.addEventListener("click", observe, true);
      try { button.click(); } finally { document.removeEventListener("click", observe, true); }
      return {
        ok:true,
        code:"ui_gesture_click_dispatched",
        eventTrusted,
        userActivation:Boolean(navigator.userActivation && navigator.userActivation.isActive),
        targetKind:String(button.tagName || "unknown").toLowerCase().slice(0, 20)
      };
    })()`;
  }

  function withTimeout(promise, timeoutMs, code) {
    let timer = null;
    const timeout = new Promise((_resolve, reject) => {
      timer = globalObject.setTimeout(() => reject(codedError(code)), timeoutMs);
    });
    return Promise.race([promise, timeout]).finally(() => globalObject.clearTimeout(timer));
  }

  function errorCode(error) {
    if (typeof error?.code === "string" && error.code) {
      return safeCode(error.code, "browser_click_unavailable");
    }
    const message = String(error?.message || error || "").toLowerCase();
    if (message.includes("another debugger") || message.includes("already attached")) {
      return "debugger_busy";
    }
    return safeCode(message.match(/[a-z0-9_]+/)?.[0], "browser_click_unavailable");
  }

  function codedError(code) {
    const error = new Error(String(code || "browser_click_unavailable"));
    error.code = safeCode(code, "browser_click_unavailable");
    return error;
  }

  function safeCode(value, fallback) {
    const normalized = String(value || "").trim();
    return /^[a-z0-9_]{1,80}$/.test(normalized) ? normalized : fallback;
  }

  function safeTargetKind(value) {
    const normalized = String(value || "unknown").toLowerCase();
    return /^[a-z][a-z0-9-]{0,19}$/.test(normalized) ? normalized : "unknown";
  }

  const api = {
    dispatchUserGestureClick,
    mainWorldRevealExpression,
    validateRequest,
    withTimeout
  };
  globalObject.AVITO_CRM_TRUSTED_CLICK = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(globalThis);
