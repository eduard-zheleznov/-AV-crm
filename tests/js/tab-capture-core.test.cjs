"use strict";

const assert = require("node:assert/strict");
const capture = require("../../chrome-extension/tab-capture-core.js");

const metadata = capture.normalizeMetadata({
  viewport: { cssWidth: 1200, cssHeight: 675, devicePixelRatio: 2 },
  region: { left: 600, top: 250, width: 300, height: 60, kind: "control" }
});
assert.deepEqual(metadata, {
  schemaVersion: 1,
  viewport: { cssWidth: 1200, cssHeight: 675, devicePixelRatio: 2 },
  region: { left: 600, top: 250, width: 300, height: 60, kind: "control" }
});

const clippedPanel = capture.normalizeMetadata({
  viewport: { cssWidth: 1200, cssHeight: 675, devicePixelRatio: 1 },
  region: { left: 900, top: -80, width: 500, height: 900, kind: "panel" }
});
assert.deepEqual(clippedPanel.region, {
  left: 900,
  top: 0,
  width: 300,
  height: 675,
  kind: "panel"
});
assert.equal(capture.validatePngDataUrl("data:image/png;base64,iVBORw0KGgo="), "data:image/png;base64,iVBORw0KGgo=");
assert.throws(
  () => capture.normalizeMetadata({ viewport: { cssWidth: 1200, cssHeight: 675, devicePixelRatio: 2 } }),
  /координаты/
);
const leftOffscreen = capture.normalizeMetadata({
  viewport: { cssWidth: 1200, cssHeight: 675, devicePixelRatio: 2 },
  region: { left: -500, top: 250, width: 300, height: 60, kind: "control" }
});
assert.deepEqual(leftOffscreen.region, {
  left: 0,
  top: 0,
  width: 1200,
  height: 675,
  kind: "control"
});
assert.equal(leftOffscreen.fallback, "offscreen_region");

const rightOffscreen = capture.normalizeMetadata({
  viewport: { cssWidth: 1200, cssHeight: 675, devicePixelRatio: 2 },
  region: { left: 1300, top: 250, width: 300, height: 60, kind: "control" }
});
assert.equal(rightOffscreen.fallback, "offscreen_region");

const invalidRegion = capture.normalizeMetadata({
  viewport: { cssWidth: 1200, cssHeight: 675, devicePixelRatio: 2 },
  region: { left: null, top: 250, width: 300, height: 60, kind: "control" }
});
assert.equal(invalidRegion.fallback, "invalid_region");
assert.throws(() => capture.validatePngDataUrl("data:image/jpeg;base64,AAAA"), /формате/);

console.log("tab-capture-core: ok");
