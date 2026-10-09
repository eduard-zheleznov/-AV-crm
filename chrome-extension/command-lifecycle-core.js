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

  // Chrome APIs can leave their promise pending after window/renderer failure.
  // Bound infrastructure calls, but NOT sendMessage: a real CAPTCHA reply is
  // deliberately allowed to wait for the operator by waitForReply above.
  function boundedBrowserApi(chromeApi, timeoutMs = 10000) {
    const methods = {
      tabs: ["get", "query", "create", "update", "reload", "remove"],
      windows: ["getAll", "create", "update", "remove"],
      extension: ["isAllowedIncognitoAccess"],
      scripting: ["executeScript"],
      runtime: ["getPlatformInfo"]
    };
    const namespaces = new Map();
    return new Proxy(chromeApi, {
      get(target, namespace) {
        if (!methods[namespace] || !target[namespace]) return target[namespace];
        if (!namespaces.has(namespace)) {
          namespaces.set(namespace, new Proxy(target[namespace], {
            get(api, method) {
              if (!methods[namespace].includes(method) || typeof api[method] !== "function") {
                return api[method];
              }
              return (...args) => new Promise((resolve, reject) => {
                const timer = setTimeout(() => {
                  const error = new Error(`Chrome не завершил ${namespace}.${method} вовремя`);
                  error.code = "browser_api_timeout";
                  error.diagnostics = { stage: `${namespace}.${method}` };
                  reject(error);
                }, timeoutMs);
                Promise.resolve().then(() => api[method].apply(api, args)).then(
                  value => { clearTimeout(timer); resolve(value); },
                  error => { clearTimeout(timer); reject(error); }
                );
              });
            }
          }));
        }
        return namespaces.get(namespace);
      }
    });
  }

  const api = { isTerminalCommandStatus, waitForReply, boundedBrowserApi };
  globalObject.AVITO_CRM_COMMAND_LIFECYCLE = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
