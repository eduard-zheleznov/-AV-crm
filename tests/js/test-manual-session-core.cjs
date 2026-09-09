"use strict";

const assert = require("node:assert/strict");
const manualSession = require("../../chrome-extension/manual-session-core.js");

function prepared(status, tabId = 42) {
  return {
    tab: { id: tabId, incognito: true },
    classification: { status },
    diagnostics: { status }
  };
}

async function main() {
  const calls = [];
  const sequence = [prepared("manual_required", 43), prepared("ready", 43)];
  const recovered = await manualSession.recoverHealthProbe(
    prepared("manual_required"),
    {
      async focus(tabId) {
        calls.push(["focus", tabId]);
      },
      async wait(tabId) {
        calls.push(["wait", tabId]);
        return { status: "manual_cleared" };
      },
      async recheck() {
        calls.push(["recheck"]);
        return sequence.shift();
      }
    }
  );

  assert.equal(recovered.completed, true);
  assert.equal(recovered.manualWaits, 2);
  assert.equal(recovered.prepared.classification.status, "ready");
  assert.deepEqual(calls, [
    ["focus", 42],
    ["wait", 42],
    ["recheck"],
    ["focus", 43],
    ["wait", 43],
    ["recheck"]
  ]);

  const cancelled = await manualSession.recoverHealthProbe(
    prepared("manual_required"),
    {
      async focus() {},
      async wait() {
        return { status: "cancelled", reason: "operator stop" };
      },
      async recheck() {
        throw new Error("recheck must not run after STOP");
      }
    }
  );
  assert.equal(cancelled.completed, false);
  assert.equal(cancelled.result.status, "cancelled");

  const healthy = await manualSession.recoverHealthProbe(prepared("ready"), {
    async focus() {
      throw new Error("focus must not run for a healthy page");
    },
    async wait() {
      throw new Error("wait must not run for a healthy page");
    },
    async recheck() {
      throw new Error("recheck must not run for a healthy page");
    }
  });
  assert.equal(healthy.completed, true);
  assert.equal(healthy.manualWaits, 0);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
