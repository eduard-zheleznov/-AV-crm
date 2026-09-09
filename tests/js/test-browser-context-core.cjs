"use strict";

const assert = require("node:assert/strict");
const context = require("../../chrome-extension/browser-context-core.js");

function fakeChrome({
  allowed = true,
  includeWindowTabs = true,
  standardTab = null,
  windowTab = null,
  existingWindows = []
} = {}) {
  const calls = [];
  return {
    calls,
    api: {
      extension: {
        async isAllowedIncognitoAccess() {
          calls.push(["incognito-access"]);
          return allowed;
        }
      },
      tabs: {
        async create(details) {
          calls.push(["tabs-create", details]);
          return standardTab || { id: 7, incognito: false, ...details };
        },
        async query(details) {
          calls.push(["tabs-query", details]);
          return [windowTab || { id: 42, incognito: true }];
        },
        async remove(tabId) {
          calls.push(["tabs-remove", tabId]);
        }
      },
      windows: {
        async getAll(details) {
          calls.push(["windows-get-all", details]);
          return existingWindows;
        },
        async create(details) {
          calls.push(["windows-create", details]);
          return {
            id: 11,
            ...(includeWindowTabs
              ? { tabs: [windowTab || { id: 42, incognito: true }] }
              : {})
          };
        },
        async remove(windowId) {
          calls.push(["windows-remove", windowId]);
        }
      }
    }
  };
}

async function main() {
  assert.equal(context.requiresIncognito({}), true);
  assert.equal(context.requiresIncognito({ incognitoRequired: true }), true);
  assert.equal(context.requiresIncognito({ incognitoRequired: false }), false);
  assert.equal(context.tabMatchesCommand({ id: 42, incognito: true }, {}), true);
  assert.equal(
    context.tabMatchesCommand({ id: 42, incognito: false }, { incognitoRequired: true }),
    false
  );

  const denied = fakeChrome({ allowed: false });
  assert.deepEqual(await context.createManagedTab(denied.api, {}), {
    ok: false,
    code: "incognito_not_allowed",
    incognitoAccess: false
  });
  assert.equal(denied.calls.some(([name]) => name === "windows-create"), false);
  assert.match(context.failureMessage("incognito_not_allowed"), /chrome:\/\/extensions/);

  const incognito = fakeChrome();
  const incognitoResult = await context.createManagedTab(incognito.api, {});
  assert.equal(incognitoResult.ok, true);
  assert.equal(incognitoResult.tab.incognito, true);
  assert.deepEqual(incognito.calls[2], [
    "windows-create",
    { url: "about:blank", focused: true, incognito: true, type: "normal" }
  ]);
  assert.equal(incognito.calls.some(([name]) => name === "tabs-create"), false);

  const bootstrap = fakeChrome({
    existingWindows: [
      {
        id: 10,
        type: "normal",
        incognito: true,
        tabs: [{ id: 55, incognito: true, url: "chrome://newtab/" }]
      }
    ]
  });
  const bootstrapResult = await context.createManagedTab(bootstrap.api, {});
  assert.equal(bootstrapResult.ok, true);
  assert.equal(bootstrapResult.tab.id, 55);
  assert.equal(bootstrapResult.reusedBootstrap, true);
  assert.equal(bootstrap.calls.some(([name]) => name === "windows-create"), false);

  const ambiguous = fakeChrome({
    existingWindows: [
      {
        id: 10,
        type: "normal",
        incognito: true,
        tabs: [{ id: 55, incognito: true, url: "chrome://newtab/" }]
      },
      {
        id: 11,
        type: "normal",
        incognito: true,
        tabs: [{ id: 56, incognito: true, url: "about:blank" }]
      }
    ]
  });
  const ambiguousResult = await context.createManagedTab(ambiguous.api, {});
  assert.equal(ambiguousResult.ok, true);
  assert.equal(ambiguousResult.reusedBootstrap, false);
  assert.equal(ambiguous.calls.some(([name]) => name === "windows-create"), true);

  const queried = fakeChrome({ includeWindowTabs: false });
  const queriedResult = await context.createManagedTab(queried.api, {});
  assert.equal(queriedResult.ok, true);
  assert.deepEqual(queried.calls.at(-1), ["tabs-query", { windowId: 11 }]);

  const mismatch = fakeChrome({ windowTab: { id: 43, incognito: false } });
  const mismatchResult = await context.createManagedTab(mismatch.api, {});
  assert.equal(mismatchResult.code, "incognito_context_mismatch");
  assert.deepEqual(mismatch.calls.at(-1), ["windows-remove", 11]);

  const standard = fakeChrome();
  const standardResult = await context.createManagedTab(standard.api, {
    incognitoRequired: false
  });
  assert.equal(standardResult.ok, true);
  assert.equal(standardResult.tab.incognito, false);
  assert.equal(standard.calls.some(([name]) => name === "windows-create"), false);

  const standardMismatch = fakeChrome({ standardTab: { id: 8, incognito: true } });
  const standardMismatchResult = await context.createManagedTab(standardMismatch.api, {
    incognitoRequired: false
  });
  assert.equal(standardMismatchResult.code, "standard_context_mismatch");
  assert.deepEqual(standardMismatch.calls.at(-1), ["tabs-remove", 8]);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
