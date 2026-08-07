"use strict";

const assert = require("node:assert/strict");
const trustedClick = require("../../chrome-extension/trusted-click.js");

const listingUrl = "https://www.avito.ru/moskva/usluga_123456789";
const request = {
  commandId: "command-1",
  x: 320.5,
  y: 440.25,
  width: 180,
  height: 48
};
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
  trustedClick.validateRequest(request, { ...context, currentCommandType: "health_probe" }).code,
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
  trustedClick.validateRequest({ ...request, x: Number.NaN }, context).code,
  "invalid_coordinates"
);

function fakeChrome({ failAt = "" } = {}) {
  const calls = [];
  return {
    calls,
    api: {
      debugger: {
        async attach(target, protocol) {
          calls.push(["attach", target, protocol]);
          if (failAt === "attach") {
            throw new Error("Another debugger is already attached to the tab");
          }
        },
        async sendCommand(target, method, params) {
          calls.push(["send", target, method, params]);
          if (failAt === params.type) {
            throw new Error("CDP input failed");
          }
        },
        async detach(target) {
          calls.push(["detach", target]);
          if (failAt === "detach") {
            throw new Error("detach failed");
          }
        }
      }
    }
  };
}

async function main() {
  const success = fakeChrome();
  assert.deepEqual(
    await trustedClick.dispatch(
      success.api,
      { tabId: 42, x: request.x, y: request.y },
      { timeoutMs: 500 }
    ),
    { ok: true, code: "browser_click_dispatched" }
  );
  assert.deepEqual(
    success.calls.map((call) => call[0] === "send" ? call[3].type : call[0]),
    ["attach", "mouseMoved", "mousePressed", "mouseReleased", "detach"]
  );
  for (const call of success.calls.filter((item) => item[0] === "send")) {
    assert.deepEqual(call[1], { tabId: 42 });
    assert.equal(call[2], "Input.dispatchMouseEvent");
  }

  const inputFailure = fakeChrome({ failAt: "mousePressed" });
  assert.deepEqual(
    await trustedClick.dispatch(
      inputFailure.api,
      { tabId: 42, x: request.x, y: request.y },
      { timeoutMs: 500 }
    ),
    { ok: false, code: "browser_click_unavailable" }
  );
  assert.equal(inputFailure.calls.at(-1)[0], "detach");

  const attachFailure = fakeChrome({ failAt: "attach" });
  assert.deepEqual(
    await trustedClick.dispatch(
      attachFailure.api,
      { tabId: 42, x: request.x, y: request.y },
      { timeoutMs: 500 }
    ),
    { ok: false, code: "debugger_busy" }
  );
  assert.equal(attachFailure.calls.some((call) => call[0] === "send"), false);
  assert.equal(attachFailure.calls.some((call) => call[0] === "detach"), false);

  const detachFailure = fakeChrome({ failAt: "detach" });
  assert.deepEqual(
    await trustedClick.dispatch(
      detachFailure.api,
      { tabId: 42, x: request.x, y: request.y },
      { timeoutMs: 500 }
    ),
    { ok: true, code: "browser_click_dispatched_detach_unconfirmed" }
  );
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
