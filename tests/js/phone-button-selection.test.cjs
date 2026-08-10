"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const content = fs.readFileSync(
  path.join(__dirname, "..", "..", "chrome-extension", "content.js"),
  "utf8"
);

function control(name, rect, options = {}) {
  return {
    name,
    textContent: options.text || "Показать телефон",
    disabled: Boolean(options.disabled),
    getAttribute(attribute) {
      if (attribute === "aria-label") return options.ariaLabel || "";
      if (attribute === "aria-disabled") return options.ariaDisabled || null;
      return null;
    },
    closest() {
      return this;
    },
    getBoundingClientRect() {
      return rect;
    }
  };
}

function select(candidates) {
  const context = {
    chrome: { runtime: { onMessage: { addListener() {} } } },
    document: { querySelectorAll() { return candidates; } },
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
  return vm.runInContext("findPhoneButton()", context);
}

const offscreen = control("offscreen", {
  left: -500,
  right: -200,
  top: 200,
  bottom: 260,
  width: 300,
  height: 60
});
const onscreen = control("onscreen", {
  left: 700,
  right: 1000,
  top: 250,
  bottom: 310,
  width: 300,
  height: 60
});

assert.equal(select([offscreen, onscreen]).name, "onscreen");
assert.equal(select([offscreen]).name, "offscreen");

const disabledOnscreen = control(
  "disabled",
  { left: 700, right: 1000, top: 250, bottom: 310, width: 300, height: 60 },
  { disabled: true }
);
assert.equal(select([disabledOnscreen, offscreen]).name, "offscreen");

console.log("phone-button-selection: ok");
