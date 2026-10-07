const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const core = require("../../chrome-extension/command-lifecycle-core.js");
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
(async () => {
  assert.equal(core.isTerminalCommandStatus("inactive"), true);
  assert.equal(core.isTerminalCommandStatus("cancelled"), true);
  assert.equal(core.isTerminalCommandStatus("active"), false);
  assert.equal(core.isTerminalCommandStatus("unknown"), false);
  assert.equal(await core.waitForReply(Promise.resolve("phone"), {timeoutMs: 20}), "phone");
  await assert.rejects(core.waitForReply(new Promise(() => {}), {
    timeoutMs: 15, tickMs: 2
  }), {code: "content_reply_timeout"});
  let manual = true;
  let complete;
  const result = core.waitForReply(new Promise(resolve => {complete = resolve;}), {
    timeoutMs: 15, tickMs: 2, isManual: () => manual
  });
  await sleep(40);
  manual = false;
  await sleep(4);
  complete("after CAPTCHA");
  assert.equal(await result, "after CAPTCHA");
  let waitingForHuman = true;
  const stuckAfterManual = core.waitForReply(new Promise(() => {}), {
    timeoutMs: 15, tickMs: 2, isManual: () => waitingForHuman
  });
  const postManualCheck = assert.rejects(stuckAfterManual, {code: "content_reply_timeout"});
  await sleep(40);
  waitingForHuman = false;
  await postManualCheck;
  const source = fs.readFileSync(path.join(__dirname, "../../chrome-extension/service-worker.js"), "utf8");
  assert.match(source, /isTerminalCommandStatus\(payload.status\)/);
  assert.match(source, /message.commandId !== statusCommandId/);
  assert.match(source, /AbortSignal.timeout\(5000\)/);
  // Execute the real service-worker functions with a replaced Chrome surface.
  // No browser, network, CAPTCHA or personal data is used by this regression.
  let reloads = 0;
  let bridgeStatus = "inactive";
  let reply = () => new Promise(() => {});
  const event = {addListener() {}};
  const context = vm.createContext({
    console, setTimeout, clearTimeout, setInterval, clearInterval, AbortSignal,
    importScripts() {}, crypto: {randomUUID: () => "synthetic-worker"},
    AVITO_CRM_CONFIG: {port: 1, token: "synthetic"},
    AVITO_CRM_COMMAND_LIFECYCLE: core,
    chrome: {
      runtime: {getManifest: () => ({version: "1.0.32"}),
        onInstalled: event, onStartup: event, onMessage: event},
      alarms: {onAlarm: event, create() {}}, action: {onClicked: event},
      tabs: {reload: async () => {reloads++;}, sendMessage: () => reply()}
    },
    fetch: async () => ({ok: true, json: async () => ({status: bridgeStatus})})
  });
  vm.runInContext(source.replace(/^startPolling\(\);$/m, "")
    .replace("setTimeout(resolve, milliseconds)", "setTimeout(resolve, Math.min(milliseconds, 2))"), context);
  vm.runInContext('currentCommandId = "test-command"; managedTabId = 7;', context);
  // AbortController must belong to the worker context, just like in Chrome.
  context.AbortController = AbortController;
  const inactive = await vm.runInContext('waitForCancellation(7, "test-command", new AbortController().signal)', context);
  assert.equal(inactive.status, "cancelled");
  assert.equal(reloads, 1);
  bridgeStatus = "cancelled";
  const stopped = await vm.runInContext('waitForCancellation(7, "test-command", new AbortController().signal)', context);
  assert.equal(stopped.status, "cancelled");
  assert.equal(reloads, 2);
  bridgeStatus = "active";
  await assert.rejects(vm.runInContext('sendContentCommand(7, {id:"test-command", pageTimeoutMs:-4980}, "test")', context),
    {code: "content_reply_timeout"});
  assert.equal(reloads, 3);
  reply = async () => ({status: "phone"});
  const next = await vm.runInContext('sendContentCommand(7, {id:"test-command", pageTimeoutMs:50}, "test")', context);
  assert.equal(next.status, "phone");
  console.log("Command lifecycle: timeout, manual pause, inactive cancellation verified");
})().catch(error => { console.error(error); process.exitCode = 1; });
