(function initializeRuntimeCore(globalObject) {
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
    const match = parsed.pathname.match(/_(\d+)(?:\/?$)/);
    return match ? match[1] : "";
  }

  function normalizedPath(value) {
    const parsed = avitoUrl(value);
    return parsed ? parsed.pathname.replace(/\/+$/, "") : "";
  }

  function sameListingIdentity(actualUrl, expectedUrl) {
    const actualId = listingId(actualUrl);
    const expectedId = listingId(expectedUrl);
    if (actualId && expectedId) {
      return actualId === expectedId;
    }
    const actualPath = normalizedPath(actualUrl);
    const expectedPath = normalizedPath(expectedUrl);
    return Boolean(actualPath && expectedPath && actualPath === expectedPath);
  }

  function isManualSurface(value) {
    const parsed = avitoUrl(value);
    if (!parsed) {
      return false;
    }
    const route = `${parsed.pathname}${parsed.search}${parsed.hash}`.toLowerCase();
    return (
      route.includes("captcha") ||
      route.includes("/challenge") ||
      /\/(?:auth|login)(?:[/?#]|$)/i.test(parsed.pathname)
    );
  }

  function classifyProbe(probe, expectedUrl, mode) {
    const actualUrl = String(probe?.actualUrl || "");
    if (!avitoUrl(actualUrl)) {
      return { status: "browser_infra", reason: "not_avito", actualUrl };
    }
    if (probe?.manual || probe?.auth || isManualSurface(actualUrl)) {
      return { status: "manual_required", reason: "manual_surface", actualUrl };
    }
    if (mode === "health") {
      // Avito may localize the home page (for example /moskva). Accept that
      // canonical redirect, but never mistake the previously open listing for
      // a successful startup navigation.
      if (listingId(actualUrl)) {
        return { status: "loading", reason: "health_navigation_pending", actualUrl };
      }
      return probe?.rendered
        ? { status: "ready", reason: "avito_rendered", actualUrl }
        : { status: "loading", reason: "avito_not_rendered", actualUrl };
    }
    const actualId = listingId(actualUrl);
    const expectedId = listingId(expectedUrl);
    if (actualId && expectedId && actualId !== expectedId) {
      return {
        status: "listing_mismatch",
        reason: "different_listing_id",
        actualUrl,
        actualId,
        expectedId
      };
    }
    if (!sameListingIdentity(actualUrl, expectedUrl)) {
      if (probe?.rendered && probe?.readyState === "complete") {
        return { status: "listing_mismatch", reason: "unexpected_rendered_surface", actualUrl };
      }
      return { status: "loading", reason: "listing_navigation_pending", actualUrl };
    }
    return probe?.rendered
      ? { status: "ready", reason: probe?.inactive ? "inactive_rendered" : "listing_rendered", actualUrl }
      : { status: "loading", reason: "listing_not_rendered", actualUrl };
  }

  const api = {
    avitoUrl,
    classifyProbe,
    isManualSurface,
    listingId,
    normalizedPath,
    sameListingIdentity
  };

  globalObject.AVITO_CRM_RUNTIME_CORE = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(globalThis);
