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
      : {
          status: "loading",
          reason: "listing_not_rendered",
          actualUrl
        };
  }

  const PHONE_SEPARATORS = "[\\s\\u00a0()\\-\u2013\u2014.]*";
  const RUSSIAN_PHONE_RE = new RegExp(
    `(?:^|[^\\d])((?:\\+?7|8)${PHONE_SEPARATORS}\\d{3}${PHONE_SEPARATORS}` +
      `\\d{3}${PHONE_SEPARATORS}\\d{2}${PHONE_SEPARATORS}\\d{2})(?!\\d)`,
    "g"
  );

  function normalizeRussianPhone(value) {
    const source = String(value || "");
    if (/avito\.ru\/\S*_\d{8,}(?:[/?#]|$)/i.test(source)) {
      return "";
    }
    RUSSIAN_PHONE_RE.lastIndex = 0;
    const match = RUSSIAN_PHONE_RE.exec(source);
    if (!match) {
      return "";
    }
    const digits = match[1].replace(/\D/g, "");
    if (digits.length !== 11 || (digits[0] !== "7" && digits[0] !== "8")) {
      return "";
    }
    return `+7${digits.slice(1)}`;
  }

  function extractRussianPhone(values) {
    for (const value of values || []) {
      const phone = normalizeRussianPhone(value);
      if (phone) {
        return phone;
      }
    }
    return "";
  }

  function hasMaskedRussianPhone(value) {
    const source = String(value || "").toLowerCase();
    return /(?:\+?7|8)[\s\u00a0()\-\u2013\u2014.]{0,8}(?:[*\u2022\u00b7x\u0445])/.test(source);
  }

  function classifyRevealTransition(before, after) {
    if (after?.phone) {
      return { status: "confirmed", reason: "phone_dom" };
    }
    if (before?.buttonPresent && !after?.buttonPresent) {
      return { status: "confirmed", reason: "button_disappeared" };
    }
    if (
      after?.revealedSurface &&
      (!before?.revealedSurface || before?.stateToken !== after?.stateToken)
    ) {
      return { status: "confirmed", reason: "revealed_surface_changed" };
    }
    if (
      before?.buttonPresent &&
      after?.buttonPresent &&
      before?.buttonToken !== after?.buttonToken &&
      !after?.buttonLooksReveal
    ) {
      return { status: "confirmed", reason: "button_state_changed" };
    }
    return { status: "pending", reason: "dispatch_without_effect" };
  }

  const api = {
    avitoUrl,
    classifyProbe,
    classifyRevealTransition,
    extractRussianPhone,
    hasMaskedRussianPhone,
    isManualSurface,
    listingId,
    normalizeRussianPhone,
    normalizedPath,
    sameListingIdentity
  };

  globalObject.AVITO_CRM_RUNTIME_CORE = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(globalThis);
