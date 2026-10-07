(function initializeCommandLifecycle(globalObject) {
  "use strict";

  function isTerminalCommandStatus(status) {
    return status === "cancelled" || status === "inactive";
  }

  // A content-script reply can remain pending forever after document replacement.
  // Poll the deadline independently; a genuine manual check suspends it.
  function waitForReply(reply, options) {
    const timeoutMs = options.timeoutMs;
    const isManual = options.isManual || (() => false);
    return new Promise((resolve, reject) => {
      let deadline = Date.now() + timeoutMs;
      let wasManual = false;
      const timer = setInterval(() => {
        const manual = Boolean(isManual());
        if (manual || wasManual) deadline = Date.now() + timeoutMs;
        wasManual = manual;
        if (!manual && Date.now() >= deadline) {
          clearInterval(timer);
          const error = new Error("Страница Avito не ответила расширению вовремя");
          error.code = "content_reply_timeout";
          reject(error);
        }
      }, options.tickMs || 250);
      Promise.resolve(reply).then(
        (value) => { clearInterval(timer); resolve(value); },
        (error) => { clearInterval(timer); reject(error); }
      );
    });
  }

  const api = { isTerminalCommandStatus, waitForReply };
  globalObject.AVITO_CRM_COMMAND_LIFECYCLE = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
