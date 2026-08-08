(function exposeTabCaptureCore(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.AVITO_CRM_TAB_CAPTURE = api;
})(typeof globalThis === "object" ? globalThis : this, function createTabCaptureCore() {
  "use strict";

  const SCHEMA_VERSION = 1;
  const MAX_DATA_URL_CHARS = 10 * 1024 * 1024;
  const ALLOWED_KINDS = new Set(["control", "dialog", "panel"]);

  function finiteNumber(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function normalizeMetadata(crop) {
    const viewport = crop && typeof crop.viewport === "object" ? crop.viewport : null;
    const region = crop && typeof crop.region === "object" ? crop.region : null;
    if (!viewport || !region) {
      throw new Error("Расширение не получило координаты номера внутри вкладки");
    }

    const cssWidth = finiteNumber(viewport.cssWidth);
    const cssHeight = finiteNumber(viewport.cssHeight);
    const devicePixelRatio = finiteNumber(viewport.devicePixelRatio);
    const left = finiteNumber(region.left);
    const top = finiteNumber(region.top);
    const width = finiteNumber(region.width);
    const height = finiteNumber(region.height);
    const kind = ALLOWED_KINDS.has(region.kind) ? region.kind : "control";

    if (
      cssWidth === null ||
      cssHeight === null ||
      devicePixelRatio === null ||
      left === null ||
      top === null ||
      width === null ||
      height === null ||
      cssWidth < 200 ||
      cssWidth > 10000 ||
      cssHeight < 200 ||
      cssHeight > 10000 ||
      devicePixelRatio < 0.5 ||
      devicePixelRatio > 8 ||
      width <= 10 ||
      height <= 10 ||
      width > cssWidth * 1.1 ||
      height > cssHeight * 1.1 ||
      left >= cssWidth ||
      top >= cssHeight ||
      left + width <= 0 ||
      top + height <= 0
    ) {
      throw new Error("Расширение отклонило некорректную область номера");
    }

    return {
      schemaVersion: SCHEMA_VERSION,
      viewport: { cssWidth, cssHeight, devicePixelRatio },
      region: { left, top, width, height, kind }
    };
  }

  function validatePngDataUrl(dataUrl) {
    if (typeof dataUrl !== "string" || !dataUrl.startsWith("data:image/png;base64,")) {
      throw new Error("Chrome вернул снимок вкладки в неизвестном формате");
    }
    if (dataUrl.length > MAX_DATA_URL_CHARS) {
      throw new Error("Снимок вкладки превысил безопасный размер");
    }
    return dataUrl;
  }

  return Object.freeze({
    MAX_DATA_URL_CHARS,
    SCHEMA_VERSION,
    normalizeMetadata,
    validatePngDataUrl
  });
});
