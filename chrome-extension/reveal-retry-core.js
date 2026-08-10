(function exposeRevealRetryCore(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.AVITO_CRM_REVEAL_RETRY = api;
})(typeof globalThis === "object" ? globalThis : this, function createRevealRetryCore() {
  "use strict";

  const RETRYABLE_STATUSES = new Set(["phone_error", "click_no_effect"]);
  const CAPTURE_STATUSES = new Set(["screenshot", "click_no_effect"]);

  function shouldRetry(status, attempt, maxClicks) {
    return RETRYABLE_STATUSES.has(status) && attempt < maxClicks;
  }

  function shouldCapture(status) {
    return CAPTURE_STATUSES.has(status);
  }

  return Object.freeze({ shouldCapture, shouldRetry });
});
