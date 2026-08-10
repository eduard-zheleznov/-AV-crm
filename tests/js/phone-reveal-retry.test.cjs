"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const content = fs.readFileSync(
  path.join(__dirname, "..", "..", "chrome-extension", "content.js"),
  "utf8"
);

const context = {
  chrome: { runtime: { onMessage: { addListener() {} } } },
  document: { querySelectorAll() { return []; } },
  window: { innerWidth: 1200, innerHeight: 700 },
  getComputedStyle() {
    return { display: "block", visibility: "visible", opacity: "1" };
  },
  setTimeout,
  clearTimeout,
  URL,
  AVITO_CRM_READINESS: {}
};
vm.createContext(context);
vm.runInContext(content, context);

function classify({ sameCapture, remainingButton, visible }) {
  context.sameCapture = sameCapture;
  context.remainingButton = remainingButton;
  context.visible = visible;
  return vm.runInContext(
    `(() => {
      const fallback = { region: { kind: "control" } };
      const selected = { region: { kind: "dialog" } };
      captureRegion = () => sameCapture ? fallback : selected;
      findPhoneButton = () => remainingButton ? {} : null;
      isVisibleInViewport = () => visible;
      return postClickCaptureResult({}, fallback);
    })()`,
    context
  );
}

assert.equal(
  classify({ sameCapture: true, remainingButton: true, visible: true }).status,
  "click_no_effect"
);
assert.equal(
  classify({ sameCapture: false, remainingButton: true, visible: true }).status,
  "screenshot"
);
assert.equal(
  classify({ sameCapture: true, remainingButton: false, visible: true }).status,
  "screenshot"
);
assert.equal(
  classify({ sameCapture: true, remainingButton: true, visible: false }).status,
  "screenshot"
);

console.log("phone-reveal-retry: ok");
