"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.resolve(__dirname, "../../chrome-extension/content.js"),
  "utf8"
);
const listeners = [];
const context = vm.createContext({
  URL,
  clearTimeout,
  console,
  setTimeout,
  chrome: {
    runtime: {
      getManifest: () => ({ version: "1.0.15" }),
      onMessage: {
        addListener(listener) {
          listeners.push(listener);
        }
      },
      sendMessage: async () => ({ ok: true })
    }
  }
});

vm.runInContext(source, context, { filename: "content.js" });
vm.runInContext(source, context, { filename: "content.js" });

assert.equal(context.AVITO_CRM_CONTENT_SCRIPT_VERSION, "1.0.15");
assert.equal(listeners.length, 1);
