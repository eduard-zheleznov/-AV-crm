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
    return { ok: true, code: "validated" };
  }

  function validateTargetMeasurement(measurement) {
    if (!measurement?.ok) {
      return {
        ok: false,
        code: safeCode(measurement?.code, "target_measurement_unavailable")
      };
    }
    for (const key of ["x", "y", "width", "height"]) {
      if (!Number.isFinite(measurement[key])) {
        return { ok: false, code: "invalid_coordinates" };
      }
    }
    if (
      measurement.x < 0 ||
      measurement.y < 0 ||
      measurement.x > 10000 ||
      measurement.y > 10000 ||
      measurement.width < 2 ||
      measurement.height < 2 ||
      measurement.width > 5000 ||
      measurement.height > 5000
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
    let coordinates = null;
    let result = { ok: false, code: "browser_click_unavailable" };
    try {
      attachPromise = chromeApi.debugger.attach(target, "1.3");
      await withTimeout(attachPromise, timeoutMs, "debugger_attach_timeout");
      attached = true;
      if (typeof options.afterAttach !== "function") {
        throw codedError("attached_focus_unavailable");
      }
      await withTimeout(
        Promise.resolve().then(() => options.afterAttach()),
        timeoutMs,
        "attached_focus_timeout"
      );
      if (typeof options.measureTarget !== "function") {
        throw codedError("target_measurement_unavailable");
      }
      const measurement = await withTimeout(
        Promise.resolve().then(() => options.measureTarget()),
        Math.max(timeoutMs, Number(options.measureTimeoutMs) || 0),
        "target_measurement_timeout"
      );
      const measurementValidation = validateTargetMeasurement(measurement);
      if (!measurementValidation.ok) {
        throw codedError(measurementValidation.code);
      }
      coordinates = {
        x: measurement.x,
        y: measurement.y,
        width: measurement.width,
        height: measurement.height
      };
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
      await withTimeout(
        chromeApi.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
          type: "mouseMoved",
          x: coordinates.x,
          y: coordinates.y
        }),
        timeoutMs,
        "debugger_input_timeout"
      );
      pressed = true;
      await withTimeout(
        chromeApi.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
          type: "mousePressed",
          x: coordinates.x,
          y: coordinates.y,
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
          x: coordinates.x,
          y: coordinates.y,
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
            x: coordinates?.x || 0,
            y: coordinates?.y || 0,
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
    if (typeof error?.code === "string" && error.code) {
      return safeCode(error.code, "browser_click_unavailable");
    }
    const message = String(error?.message || error || "").toLowerCase();
    if (message.includes("timeout")) {
      const timeoutCode = message.match(/[a-z0-9_]+_timeout/)?.[0] || "";
      return safeCode(timeoutCode, "debugger_input_timeout");
    }
    if (message.includes("another debugger") || message.includes("already attached")) {
      return "debugger_busy";
    }
    return "browser_click_unavailable";
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

  const delay = (milliseconds) =>
    new Promise((resolve) => globalObject.setTimeout(resolve, milliseconds));

  const api = { dispatch, validateRequest, validateTargetMeasurement, withTimeout };
  globalObject.AVITO_CRM_TRUSTED_CLICK = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(globalThis);
