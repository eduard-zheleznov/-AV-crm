"use strict";

const assert = require("node:assert/strict");
const runtime = require("../../chrome-extension/runtime-core.js");

const expected = "https://www.avito.ru/moskva/remont_kvartir_123456789";
const canonicalRedirect = "https://www.avito.ru/moskva/novyy_slug_123456789?context=H4sIA";

assert.equal(runtime.sameListingIdentity(canonicalRedirect, expected), true);
assert.deepEqual(
  runtime.classifyProbe(
    {
      actualUrl: canonicalRedirect,
      readyState: "interactive",
      rendered: true
    },
    expected,
    "listing"
  ).status,
  "ready"
);

assert.equal(
  runtime.classifyProbe(
    {
      actualUrl: "https://www.avito.ru/moskva/drugoe_987654321",
      readyState: "complete",
      rendered: true
    },
    expected,
    "listing"
  ).status,
  "listing_mismatch"
);

assert.equal(
  runtime.classifyProbe(
    {
      actualUrl: "https://www.avito.ru/moskva",
      readyState: "complete",
      rendered: true
    },
    "https://www.avito.ru/",
    "health"
  ).status,
  "ready"
);

assert.equal(
  runtime.classifyProbe(
    {
      actualUrl: expected,
      readyState: "complete",
      rendered: true
    },
    "https://www.avito.ru/",
    "health"
  ).status,
  "loading"
);

assert.equal(
  runtime.classifyProbe(
    {
      actualUrl: "https://www.avito.ru/challenge",
      readyState: "complete",
      rendered: true,
      manual: true
    },
    expected,
    "listing"
  ).status,
  "manual_required"
);

