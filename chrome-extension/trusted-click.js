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
    if (request.activation !== "dom") {
      return { ok: false, code: "invalid_activation" };
    }
    return { ok: true, code: "validated" };
  }

  function withTimeout(promise, timeoutMs, code) {
    let timer = null;
    const timeout = new Promise((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error(code)), timeoutMs);
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
  }

  const api = { validateRequest, withTimeout };
  globalObject.AVITO_CRM_TRUSTED_CLICK = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(globalThis);
