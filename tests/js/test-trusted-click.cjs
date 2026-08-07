"use strict";

const assert = require("node:assert/strict");
const trustedClick = require("../../chrome-extension/trusted-click.js");

const listingUrl = "https://www.avito.ru/moskva/usluga_123456789";
const request = { commandId: "command-1" };
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
  trustedClick.validateRequest({ ...request, activation: "tab" }, context).code,
  "invalid_activation"
);
assert.equal(
  trustedClick.validateRequest({ ...request, activation: "native" }, context).code,
  "validated"
);
assert.equal(
  trustedClick.validateTargetMeasurement({
    ok: true,
    x: Number.NaN,
    y: 200,
    width: 180,
    height: 48
  }).code,
  "invalid_coordinates"
);
assert.equal(
  trustedClick.validateTargetMeasurement(
    { ok: true, x: 200, y: 200, width: 180, height: 48, focused: false },
    "enter"
  ).code,
  "click_target_not_focused"
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
          if (failAt === method || failAt === params.type) {
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

function dispatchOptions(calls, overrides = {}) {
  return {
    timeoutMs: 500,
    measureTimeoutMs: 500,
    async afterAttach() {
      calls.push(["focus"]);
    },
    async measureTarget() {
      calls.push(["measure"]);
      return {
        ok: true,
        x: 640.5,
        y: 480.25,
        width: 200,
        height: 52,
        focused: true
      };
    },
    async revalidate() {
      calls.push(["revalidate"]);
      return { ok: true, code: "validated" };
    },
    ...overrides
  };
}

async function main() {
  const success = fakeChrome();
  assert.deepEqual(
    await trustedClick.dispatch(
      success.api,
      // These deliberately stale pre-attach coordinates must never be used.
      { tabId: 42, x: 1, y: 2 },
      dispatchOptions(success.calls)
    ),
    { ok: true, code: "browser_click_dispatched" }
  );
  assert.deepEqual(
    success.calls.map((call) =>
      call[0] === "send" ? (call[3].type || call[2]) : call[0]
    ),
    [
      "attach",
      "focus",
      "Page.bringToFront",
      "Runtime.evaluate",
      "measure",
      "revalidate",
      "mouseMoved",
      "mousePressed",
      "mouseReleased",
      "detach"
    ]
  );
  for (const call of success.calls.filter(
    (item) => item[0] === "send" && item[2] === "Input.dispatchMouseEvent"
  )) {
    assert.deepEqual(call[1], { tabId: 42 });
    assert.equal(call[2], "Input.dispatchMouseEvent");
    assert.equal(call[3].x, 640.5);
    assert.equal(call[3].y, 480.25);
  }
  const pageFocus = success.calls.find(
    (item) => item[0] === "send" && item[2] === "Runtime.evaluate"
  );
  assert.equal(pageFocus[3].expression, "window.focus()");
  assert.equal(pageFocus[3].userGesture, true);

  const inputFailure = fakeChrome({ failAt: "mousePressed" });
  assert.deepEqual(
    await trustedClick.dispatch(
      inputFailure.api,
      { tabId: 42 },
      dispatchOptions(inputFailure.calls)
    ),
    { ok: false, code: "browser_click_unavailable" }
  );
  assert.equal(
    inputFailure.calls.filter(
      (call) => call[0] === "send" && call[3].type === "mouseReleased"
    ).length,
    1
  );
  assert.equal(inputFailure.calls.at(-1)[0], "detach");

  for (const activation of ["enter", "space"]) {
    const keyboard = fakeChrome();
    assert.deepEqual(
      await trustedClick.dispatch(
        keyboard.api,
        { tabId: 42, activation },
        dispatchOptions(keyboard.calls)
      ),
      { ok: true, code: `browser_${activation}_dispatched` }
    );
    assert.deepEqual(
      keyboard.calls
        .filter((call) => call[0] === "send" && call[2] === "Input.dispatchKeyEvent")
        .map((call) => call[3].type),
      ["keyDown", "keyUp"]
    );
    assert.equal(
      keyboard.calls.some(
        (call) => call[0] === "send" && call[2] === "Input.dispatchMouseEvent"
      ),
      false
    );
    assert.equal(keyboard.calls.at(-1)[0], "detach");
  }

  const unfocusedKeyboard = fakeChrome();
  assert.deepEqual(
    await trustedClick.dispatch(
      unfocusedKeyboard.api,
      { tabId: 42, activation: "enter" },
      dispatchOptions(unfocusedKeyboard.calls, {
        async measureTarget() {
          unfocusedKeyboard.calls.push(["measure"]);
          return {
            ok: true,
            x: 640.5,
            y: 480.25,
            width: 200,
            height: 52,
            focused: false
          };
        }
      })
    ),
    { ok: false, code: "click_target_not_focused" }
  );
  assert.equal(
    unfocusedKeyboard.calls.some(
      (call) => call[0] === "send" && call[2] === "Input.dispatchKeyEvent"
    ),
    false
  );
  assert.equal(unfocusedKeyboard.calls.at(-1)[0], "detach");

  const keyFailure = fakeChrome({ failAt: "keyDown" });
  assert.deepEqual(
    await trustedClick.dispatch(
      keyFailure.api,
      { tabId: 42, activation: "enter" },
      dispatchOptions(keyFailure.calls)
    ),
    { ok: false, code: "browser_click_unavailable" }
  );
  assert.equal(
    keyFailure.calls.filter(
      (call) =>
        call[0] === "send" &&
        call[2] === "Input.dispatchKeyEvent" &&
        call[3].type === "keyUp"
    ).length,
    1
  );
  assert.equal(keyFailure.calls.at(-1)[0], "detach");

  const attachFailure = fakeChrome({ failAt: "attach" });
  assert.deepEqual(
    await trustedClick.dispatch(
      attachFailure.api,
      { tabId: 42 },
      dispatchOptions(attachFailure.calls)
    ),
    { ok: false, code: "debugger_busy" }
  );
  assert.equal(attachFailure.calls.some((call) => call[0] === "measure"), false);
  assert.equal(attachFailure.calls.some((call) => call[0] === "send"), false);
  assert.equal(attachFailure.calls.some((call) => call[0] === "detach"), false);

  const pageFocusFailure = fakeChrome({ failAt: "Page.bringToFront" });
  assert.deepEqual(
    await trustedClick.dispatch(
      pageFocusFailure.api,
      { tabId: 42 },
      dispatchOptions(pageFocusFailure.calls)
    ),
    { ok: false, code: "browser_click_unavailable" }
  );
  assert.equal(pageFocusFailure.calls.some((call) => call[0] === "measure"), false);
  assert.equal(
    pageFocusFailure.calls.some(
      (call) => call[0] === "send" && call[2] === "Input.dispatchMouseEvent"
    ),
    false
  );
  assert.equal(pageFocusFailure.calls.at(-1)[0], "detach");

  const missingContent = fakeChrome();
  assert.deepEqual(
    await trustedClick.dispatch(
      missingContent.api,
      { tabId: 42 },
      dispatchOptions(missingContent.calls, {
        async measureTarget() {
          missingContent.calls.push(["measure"]);
          return { ok: false, code: "content_script_unavailable" };
        }
      })
    ),
    { ok: false, code: "content_script_unavailable" }
  );
  assert.equal(
    missingContent.calls.some(
      (call) => call[0] === "send" && call[2] === "Input.dispatchMouseEvent"
    ),
    false
  );
  assert.equal(missingContent.calls.at(-1)[0], "detach");

  const stopped = fakeChrome();
  assert.deepEqual(
    await trustedClick.dispatch(
      stopped.api,
      { tabId: 42 },
      dispatchOptions(stopped.calls, {
        async revalidate() {
          stopped.calls.push(["revalidate"]);
          return { ok: false, code: "command_cancelled" };
        }
      })
    ),
    { ok: false, code: "command_cancelled" }
  );
  assert.equal(
    stopped.calls.some(
      (call) => call[0] === "send" && call[2] === "Input.dispatchMouseEvent"
    ),
    false
  );
  assert.equal(stopped.calls.at(-1)[0], "detach");

  const detachFailure = fakeChrome({ failAt: "detach" });
  assert.deepEqual(
    await trustedClick.dispatch(
      detachFailure.api,
      { tabId: 42 },
      dispatchOptions(detachFailure.calls)
    ),
    { ok: true, code: "browser_click_dispatched_detach_unconfirmed" }
  );
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
