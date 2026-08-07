"use strict";

const assert = require("node:assert/strict");
const trustedClick = require("../../chrome-extension/trusted-click.js");

const listingUrl = "https://www.avito.ru/moskva/usluga_123456789";
const request = { commandId: "command-1", activation: "dom" };
const context = {
  currentCommandId: "command-1",
  currentCommandType: "reveal_phone",
  managedTabId: 42,
  senderTabId: 42,
  actualUrl: `${listingUrl}?context=test`,
  expectedUrl: listingUrl,
  sameListingIdentity: (actual, expected) =>
    actual.includes("_123456789") && expected.includes("_123456789")
};

assert.deepEqual(trustedClick.validateRequest(request, context), {
  ok: true,
  code: "validated"
});
assert.equal(
  trustedClick.validateRequest(request, { ...context, currentCommandId: "other" }).code,
  "command_mismatch"
);
assert.equal(
  trustedClick.validateRequest(request, { ...context, currentCommandType: "health_probe" })
    .code,
  "command_type_mismatch"
);
assert.equal(
  trustedClick.validateRequest(request, { ...context, senderTabId: 41 }).code,
  "tab_mismatch"
);
assert.equal(
  trustedClick.validateRequest(request, {
    ...context,
    actualUrl: "https://www.avito.ru/moskva/drugoe_987654321"
  }).code,
  "listing_mismatch"
);
assert.equal(
  trustedClick.validateRequest({ ...request, activation: "mouse" }, context).code,
  "invalid_activation"
);

async function main() {
  assert.equal(
    await trustedClick.withTimeout(Promise.resolve("ok"), 50, "timeout"),
    "ok"
  );
  await assert.rejects(
    trustedClick.withTimeout(new Promise(() => undefined), 10, "gate_timeout"),
    /gate_timeout/
  );
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
