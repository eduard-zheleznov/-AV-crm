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
      readyState: "complete",
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
      actualUrl: canonicalRedirect,
      readyState: "interactive",
      rendered: true
    },
    expected,
    "listing"
  ).status,
  "loading"
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

assert.equal(runtime.normalizeRussianPhone("+7\u00a0999\u00a0123–45–67"), "+79991234567");
assert.equal(runtime.normalizeRussianPhone("8 (999) 123-45-67"), "+79991234567");
assert.equal(
  runtime.extractRussianPhone(["Временный номер", "Телефон: +7 999 123 45 67"]),
  "+79991234567"
);
assert.equal(runtime.normalizeRussianPhone("+7 (***) ***-45-67"), "");
assert.equal(runtime.hasMaskedRussianPhone("+7 ••• •••-45-67"), true);
assert.equal(runtime.normalizeRussianPhone("Avito ID 1234567890"), "");
assert.equal(
  runtime.normalizeRussianPhone("https://www.avito.ru/moskva/item_89991234567"),
  ""
);

const unchangedReveal = {
  phone: "",
  buttonPresent: true,
  buttonLooksReveal: true,
  buttonToken: "показать телефон",
  revealedSurface: false,
  stateToken: "0:0:1"
};
assert.equal(
  runtime.classifyRevealTransition(unchangedReveal, { ...unchangedReveal }).status,
  "pending"
);
assert.equal(
  runtime.classifyRevealTransition(unchangedReveal, {
    ...unchangedReveal,
    buttonPresent: false
  }).status,
  "confirmed"
);
assert.equal(
  runtime.classifyRevealTransition(unchangedReveal, {
    ...unchangedReveal,
    buttonLooksReveal: false,
    buttonToken: "временный номер",
    revealedSurface: true,
    stateToken: "1:1:1"
  }).status,
  "confirmed"
);
