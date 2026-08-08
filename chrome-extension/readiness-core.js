(function exposeReadinessCore(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.AVITO_CRM_READINESS = api;
})(typeof globalThis === "object" ? globalThis : this, function buildReadinessCore() {
  "use strict";

  function avitoUrl(value) {
    try {
      const parsed = new URL(String(value || ""));
      return /(^|\.)avito\.ru$/i.test(parsed.hostname) ? parsed : null;
    } catch (_error) {
      return null;
    }
  }

  function listingId(value) {
    const parsed = avitoUrl(value);
    if (!parsed) {
      return "";
    }
    const tail = parsed.pathname.replace(/\/+$/, "").split("/").pop() || "";
    return tail.match(/(?:^|_)(\d{6,})$/)?.[1] || "";
  }

  function sameListing(actualUrl, expectedUrl) {
    const actualId = listingId(actualUrl);
    const expectedId = listingId(expectedUrl);
    return Boolean(actualId && expectedId && actualId === expectedId);
  }

  function isManualUrl(value) {
    const parsed = avitoUrl(value);
    if (!parsed) {
      return false;
    }
    const path = `${parsed.pathname}${parsed.search}${parsed.hash}`.toLowerCase();
    return (
      path.includes("captcha") ||
      path.includes("/challenge") ||
      /\/(?:auth|login)(?:[/?#]|$)/i.test(parsed.pathname)
    );
  }

  function classifyProbe(probe, expectedUrl) {
    const expectedId = listingId(expectedUrl);
    if (!expectedId) {
      return "invalid_listing";
    }
    if (probe?.manual || isManualUrl(probe?.actualUrl)) {
      return "manual_required";
    }
    const actualId = listingId(probe?.actualUrl);
    if (actualId && actualId !== expectedId) {
      return "listing_mismatch";
    }
    if (actualId !== expectedId) {
      return "loading";
    }
    if (probe?.actionable) {
      return "actionable";
    }
    return probe?.rendered ? "rendered" : "loading";
  }

  return Object.freeze({
    avitoUrl,
    classifyProbe,
    isManualUrl,
    listingId,
    sameListing
  });
});
