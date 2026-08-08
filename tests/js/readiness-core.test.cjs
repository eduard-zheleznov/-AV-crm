"use strict";

const assert = require("node:assert/strict");
const readiness = require("../../chrome-extension/readiness-core.js");

const expected =
  "https://www.avito.ru/novosibirsk/predlozheniya_uslug/remont_kvartir_8256014724";

assert.equal(readiness.listingId(expected), "8256014724");
assert.equal(
  readiness.sameListing(
    "https://www.avito.ru/novosibirsk/drugoy_slug_8256014724?context=H4sIA",
    expected
  ),
  true
);
assert.equal(
  readiness.classifyProbe(
    {
      actualUrl: expected,
      readyState: "interactive",
      actionable: true,
      rendered: true
    },
    expected
  ),
  "actionable"
);
assert.equal(
  readiness.classifyProbe(
    {
      actualUrl: expected,
      readyState: "loading",
      actionable: false,
      rendered: true
    },
    expected
  ),
  "rendered"
);
assert.equal(
  readiness.classifyProbe(
    {
      actualUrl: "https://www.avito.ru/novosibirsk/drugoe_8256014999",
      actionable: true,
      rendered: true
    },
    expected
  ),
  "listing_mismatch"
);
assert.equal(
  readiness.classifyProbe(
    { actualUrl: "https://www.avito.ru/novosibirsk/predlozheniya_uslug/bez_id" },
    "https://www.avito.ru/novosibirsk/bez_id"
  ),
  "invalid_listing"
);
assert.equal(
  readiness.classifyProbe(
    { actualUrl: "https://www.avito.ru/challenge", manual: true },
    expected
  ),
  "manual_required"
);

console.log("readiness-core: ok");
