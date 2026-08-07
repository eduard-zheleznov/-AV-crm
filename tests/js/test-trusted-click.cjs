"use strict";

const assert = require("node:assert/strict");
const vm = require("node:vm");
const trustedClick = require("../../chrome-extension/trusted-click.js");

const listingUrl = "https://www.avito.ru/moskva/usluga_123456789";
const request = { commandId: "command-1", activation: "ui_eval" };
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

  let clickListener = null;
  let clickCount = 0;
  const button = {
    ariaDisabled: "false",
    disabled: false,
    isConnected: true,
    tagName: "BUTTON",
    textContent: "Показать телефон",
    closest: () => button,
    contains: (target) => target === button,
    focus: () => undefined,
    getAttribute(name) {
      return name === "aria-disabled" ? this.ariaDisabled : "";
    },
    getBoundingClientRect: () => ({
      left: 100,
      top: 200,
      right: 280,
      bottom: 248,
      width: 180,
      height: 48
    }),
    scrollIntoView: () => undefined,
    click() {
      clickCount += 1;
      clickListener?.({ target: button, isTrusted: false });
    }
  };
  const expressionResult = vm.runInNewContext(
    trustedClick.mainWorldRevealExpression("123456789"),
    {
      document: {
        readyState: "interactive",
        querySelectorAll: () => [button],
        elementFromPoint: () => button,
        addEventListener: (_name, listener) => {
          clickListener = listener;
        },
        removeEventListener: () => {
          clickListener = null;
        }
      },
      getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
      innerHeight: 900,
      innerWidth: 1400,
      location: { pathname: "/moskva/usluga_123456789" },
      navigator: { userActivation: { isActive: true } }
    }
  );
  assert.equal(clickCount, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(expressionResult)), {
    ok: true,
    code: "ui_gesture_click_dispatched",
    eventTrusted: false,
    userActivation: true,
    targetKind: "button"
  });

  const calls = [];
  const chromeApi = {
    debugger: {
      async attach(target, protocol) {
        calls.push(["attach", target, protocol]);
      },
      async sendCommand(target, method, params) {
        calls.push(["send", target, method, params]);
        return {
          result: {
            value: {
              ok: true,
              eventTrusted: false,
              userActivation: true,
              targetKind: "button"
            }
          }
        };
      },
      async detach(target) {
        calls.push(["detach", target]);
      }
    }
  };
  const dispatched = await trustedClick.dispatchUserGestureClick(
    chromeApi,
    { tabId: 42, listingId: "123456789" },
    { revalidate: async () => ({ ok: true, code: "validated" }) }
  );
  assert.deepEqual(dispatched, {
    ok: true,
    code: "ui_gesture_click_dispatched",
    eventTrusted: false,
    userActivation: true,
    targetKind: "button"
  });
  assert.equal(calls[0][0], "attach");
  assert.equal(calls[1][2], "Runtime.evaluate");
  assert.equal(calls[1][3].userGesture, true);
  assert.match(calls[1][3].expression, /button\.click\(\)/);
  assert.equal(calls.at(-1)[0], "detach");

  const blockedCalls = [];
  const blockedChrome = {
    debugger: {
      async attach() {
        blockedCalls.push("attach");
      },
      async sendCommand() {
        blockedCalls.push("send");
        return {};
      },
      async detach() {
        blockedCalls.push("detach");
      }
    }
  };
  assert.deepEqual(
    await trustedClick.dispatchUserGestureClick(
      blockedChrome,
      { tabId: 42, listingId: "123456789" },
      { revalidate: async () => ({ ok: false, code: "command_cancelled" }) }
    ),
    { ok: false, code: "command_cancelled" }
  );
  assert.deepEqual(blockedCalls, ["attach", "detach"]);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
