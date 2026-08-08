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
assert.equal(capture.validatePngDataUrl("data:image/png;base64,iVBORw0KGgo="), "data:image/png;base64,iVBORw0KGgo=");
assert.throws(
  () => capture.normalizeMetadata({ viewport: { cssWidth: 1200, cssHeight: 675, devicePixelRatio: 2 } }),
  /координаты/
);
assert.throws(
  () => capture.normalizeMetadata({
    viewport: { cssWidth: 1200, cssHeight: 675, devicePixelRatio: 2 },
    region: { left: 1300, top: 250, width: 300, height: 60, kind: "control" }
  }),
  /некорректную область/
);
assert.throws(() => capture.validatePngDataUrl("data:image/jpeg;base64,AAAA"), /формате/);

console.log("tab-capture-core: ok");
