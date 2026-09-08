"use strict";

const assert = require("node:assert/strict");
const runtime = require("../../chrome-extension/runtime-core.js");
const navigation = require("../../chrome-extension/navigation-core.js");

const expected = "https://www.avito.ru/moskva/usluga_123456789";
const committedListing = {
  id: 42,
  status: "complete",
  url: "https://www.avito.ru/moskva/novyy_slug_123456789",
  pendingUrl: ""
};

assert.equal(navigation.surfaceClass("about:blank", runtime), "about:blank");
assert.equal(navigation.surfaceClass("https://www.avito.ru/", runtime), "avito:root");
assert.equal(
  navigation.surfaceClass("https://www.avito.ru/moskva", runtime),
  "avito:path"
);
assert.equal(navigation.surfaceClass(expected, runtime), "avito:listing");
assert.equal(
  navigation.surfaceClass("https://www.avito.ru/challenge", runtime),
  "avito:manual"
);
assert.equal(
  navigation.surfaceClass("https://private.example/secret/path", runtime),
  "https:other"
);

assert.deepEqual(
  navigation.contentGate(committedListing, expected, "listing", runtime),
  { ok: true, code: "committed_expected_surface" }
);
assert.equal(
  navigation.contentGate(
    { ...committedListing, url: "about:blank" },
    expected,
    "listing",
    runtime
  ).code,
  "unexpected_surface"
);
assert.equal(
  navigation.contentGate(
    { ...committedListing, url: "about:blank", pendingUrl: expected },
    expected,
    "listing",
    runtime
  ).code,
  "navigation_pending"
);
assert.equal(
  navigation.contentGate(
    {
      ...committedListing,
      url: "https://www.avito.ru/moskva/drugoe_987654321"
    },
    expected,
    "listing",
    runtime
  ).code,
  "unexpected_surface"
);
assert.equal(
  navigation.contentGate(
    { ...committedListing, url: expected, status: "loading" },
    expected,
    "listing",
    runtime
  ).ok,
  true
);
assert.equal(
  navigation.contentGate(
    { ...committedListing, url: expected, discarded: true },
    expected,
    "listing",
    runtime
  ).code,
  "document_unavailable"
);
assert.equal(
  navigation.contentGate(
    { ...committedListing, url: "https://www.avito.ru/moskva" },
    "https://www.avito.ru/",
    "health",
    runtime
  ).ok,
  true
);
assert.equal(
  navigation.contentGate(
    committedListing,
    "https://www.avito.ru/",
    "health",
    runtime
  ).code,
  "unexpected_surface"
);

assert.equal(
  navigation.shouldInject(
    {
      tab: committedListing,
      expectedUrl: expected,
      mode: "listing",
      missingReceiver: true,
      attempted: false,
      committedForMs: 1000,
      graceMs: 1000
    },
    runtime
  ).ok,
  true
);
assert.equal(
  navigation.shouldInject(
    {
      tab: committedListing,
      expectedUrl: expected,
      mode: "listing",
      missingReceiver: true,
      attempted: false,
      committedForMs: 999,
      graceMs: 1000
    },
    runtime
  ).code,
  "content_script_grace"
);
assert.equal(
  navigation.shouldInject(
    {
      tab: committedListing,
      expectedUrl: expected,
      mode: "listing",
      missingReceiver: true,
      attempted: true,
      committedForMs: 2000,
      graceMs: 1000
    },
    runtime
  ).code,
  "injection_already_attempted"
);

const interactiveRendered = {
  readyState: "interactive",
  rendered: true,
  bodyLength: 2609,
  visibleHeadings: 1,
  hasPhone: false,
  hasPhoneButton: true,
  contentVersion: "1.0.19"
};
let stable = null;
for (const now of [0, 500, 1000]) {
  stable = navigation.advanceRenderedStability(stable, interactiveRendered, now, {
    minSamples: 4,
    minStableMs: 1500
  });
  assert.equal(stable.ready, false);
}
stable = navigation.advanceRenderedStability(stable, interactiveRendered, 1500, {
  minSamples: 4,
  minStableMs: 1500
});
assert.equal(stable.ready, true);

let changing = navigation.advanceRenderedStability(null, interactiveRendered, 0);
changing = navigation.advanceRenderedStability(
  changing,
  { ...interactiveRendered, bodyLength: 2810 },
  500
);
assert.equal(changing.samples, 1);
assert.equal(changing.ready, false);
changing = navigation.advanceRenderedStability(
  changing,
  { ...interactiveRendered, bodyLength: 2810, hasPhoneButton: false },
  1000
);
assert.equal(changing.samples, 1);
assert.equal(changing.ready, false);
assert.equal(
  navigation.advanceRenderedStability(changing, { ...interactiveRendered, rendered: false }, 2000)
    .ready,
  false
);

function fakeChrome(tab, { executeError = null } = {}) {
  const calls = [];
  return {
    calls,
    api: {
      tabs: {
        async get(tabId) {
          calls.push(["get", tabId]);
          return { ...tab };
        }
      },
      scripting: {
        async executeScript(details) {
          calls.push(["execute", details]);
          if (executeError) {
            throw executeError;
          }
        }
      }
    }
  };
}

async function main() {
  const exact = fakeChrome(committedListing);
  assert.deepEqual(
    await navigation.injectContentFiles(
      exact.api,
      42,
      expected,
      "listing",
      runtime
    ),
    { attempted: true, status: "injected" }
  );
  assert.deepEqual(exact.calls[1], [
    "execute",
    {
      target: { tabId: 42, frameIds: [0] },
      files: ["runtime-core.js", "content.js"]
    }
  ]);

  for (const unsafeTab of [
    { ...committedListing, url: "about:blank" },
    { ...committedListing, url: "https://example.com/other" },
    { ...committedListing, pendingUrl: expected }
  ]) {
    const unsafe = fakeChrome(unsafeTab);
    const result = await navigation.injectContentFiles(
      unsafe.api,
      42,
      expected,
      "listing",
      runtime
    );
    assert.equal(result.attempted, false);
    assert.equal(unsafe.calls.some((call) => call[0] === "execute"), false);
  }

  const failed = fakeChrome(committedListing, {
    executeError: new Error("injection unavailable")
  });
  const failedResult = await navigation.injectContentFiles(
    failed.api,
    42,
    expected,
    "listing",
    runtime
  );
  assert.equal(failedResult.attempted, true);
  assert.equal(failedResult.status, "execute_failed");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
