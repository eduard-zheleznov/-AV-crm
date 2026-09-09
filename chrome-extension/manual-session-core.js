(function initializeManualSessionCore(globalObject) {
  "use strict";

  async function recoverHealthProbe(prepared, handlers) {
    let current = prepared;
    let manualWaits = 0;

    while (current?.classification?.status === "manual_required") {
      manualWaits += 1;
      await handlers.focus(current.tab.id);
      const result = await handlers.wait(current.tab.id);
      if (!result || result.status !== "manual_cleared") {
        return { completed: false, result, prepared: current, manualWaits };
      }
      current = await handlers.recheck();
    }

    return { completed: true, result: null, prepared: current, manualWaits };
  }

  const api = { recoverHealthProbe };
  globalObject.AVITO_CRM_MANUAL_SESSION_CORE = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(globalThis);
