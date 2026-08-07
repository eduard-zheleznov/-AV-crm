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
    for (const key of ["x", "y", "width", "height"]) {
      if (!Number.isFinite(request[key])) {
        return { ok: false, code: "invalid_coordinates" };
      }
    }
    if (
      request.x < 0 ||
      request.y < 0 ||
      request.x > 10000 ||
      request.y > 10000 ||
      request.width < 2 ||
      request.height < 2 ||
      request.width > 5000 ||
      request.height > 5000
    ) {
      return { ok: false, code: "invalid_coordinates" };
    }
    return { ok: true, code: "validated" };
  }

  async function dispatch(chromeApi, request, options = {}) {
    const timeoutMs = Math.max(500, Number(options.timeoutMs) || 1500);
    const target = { tabId: request.tabId };
    let attached = false;
    let attachPromise = null;
    let pressed = false;
    let result = { ok: false, code: "browser_click_unavailable" };
    try {
      attachPromise = chromeApi.debugger.attach(target, "1.3");
      await withTimeout(attachPromise, timeoutMs, "debugger_attach_timeout");
      attached = true;
      await withTimeout(
        chromeApi.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
          type: "mouseMoved",
          x: request.x,
          y: request.y
        }),
        timeoutMs,
        "debugger_input_timeout"
      );
      pressed = true;
      await withTimeout(
        chromeApi.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
          type: "mousePressed",
          x: request.x,
          y: request.y,
          button: "left",
          buttons: 1,
          clickCount: 1
        }),
        timeoutMs,
        "debugger_input_timeout"
      );
      await delay(80);
      await withTimeout(
        chromeApi.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
          type: "mouseReleased",
          x: request.x,
          y: request.y,
          button: "left",
          buttons: 0,
          clickCount: 1
        }),
        timeoutMs,
        "debugger_input_timeout"
      );
      pressed = false;
      result = { ok: true, code: "browser_click_dispatched" };
    } catch (error) {
      if (attached && pressed) {
        await withTimeout(
          chromeApi.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
            type: "mouseReleased",
            x: request.x,
            y: request.y,
            button: "left",
            buttons: 0,
            clickCount: 1
          }),
          500,
          "debugger_release_timeout"
        ).catch(() => undefined);
        pressed = false;
      }
      if (!attached && attachPromise) {
        // A timed-out attach cannot be cancelled. If Chrome completes it late,
        // compensate immediately so no debugger session is left behind.
        attachPromise
          .then(() =>
            withTimeout(
              chromeApi.debugger.detach(target),
              timeoutMs,
              "debugger_detach_timeout"
            )
          )
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
          // The trusted click has already happened. Do not report failure and
          // trigger a duplicate click; surface the unconfirmed cleanup in code.
          result = { ok: true, code: "browser_click_dispatched_detach_unconfirmed" };
        }
      }
    }
    return result;
  }

  function withTimeout(promise, timeoutMs, code) {
    let timer = null;
    const timeout = new Promise((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error(code)), timeoutMs);
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
  }

  function errorCode(error) {
    const message = String(error?.message || error || "").toLowerCase();
    if (message.includes("timeout")) {
      return message.includes("attach") ? "debugger_attach_timeout" : "debugger_input_timeout";
    }
    if (message.includes("another debugger") || message.includes("already attached")) {
      return "debugger_busy";
    }
    return "browser_click_unavailable";
  }

  const delay = (milliseconds) =>
    new Promise((resolve) => globalObject.setTimeout(resolve, milliseconds));

  const api = { dispatch, validateRequest, withTimeout };
  globalObject.AVITO_CRM_TRUSTED_CLICK = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(globalThis);
