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
  const syntheticChrome = {
    tabs: { update: () => new Promise(() => {}), sendMessage: () => new Promise(() => {}) },
    runtime: { getPlatformInfo: async () => ({os: "test"}) }
  };
  const bounded = core.boundedBrowserApi(syntheticChrome, 15);
  await assert.rejects(bounded.tabs.update(7, {url: "https://www.avito.ru/"}),
    {code: "browser_api_timeout"});
  assert.deepEqual(await bounded.runtime.getPlatformInfo(), {os: "test"});
  // Manual content messaging remains unbounded at this layer.
  assert.equal(bounded.tabs.sendMessage, syntheticChrome.tabs.sendMessage);
  const receiverApi = { get() { assert.equal(this, receiverApi); return Promise.resolve(7); } };
  assert.equal(await core.boundedBrowserApi({tabs: receiverApi}, 15).tabs.get(), 7);
  await assert.rejects(core.boundedBrowserApi({tabs: {get() {throw new Error("native error");}}}, 15)
    .tabs.get(), /native error/);
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
  const activePulses = new Set();
  const context = vm.createContext({
    console, setTimeout, clearTimeout, AbortSignal,
    setInterval(callback, milliseconds) {
      const timer = setInterval(callback, milliseconds);
      activePulses.add(timer);
      return timer;
    },
    clearInterval(timer) { activePulses.delete(timer); clearInterval(timer); },
    importScripts() {}, crypto: {randomUUID: () => "synthetic-worker"},
    AVITO_CRM_CONFIG: {port: 1, token: "synthetic"},
    AVITO_CRM_COMMAND_LIFECYCLE: {...core,
      boundedBrowserApi: api => core.boundedBrowserApi(api, 15)},
    AVITO_CRM_NAVIGATION_CORE: {shouldPreserveManagedTab: () => false},
    chrome: {
      runtime: {getManifest: () => ({version: "1.0.33"}),
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
  // A native Chrome navigation promise stalls BEFORE sendContentCommand.
  // The real executeCommand must report a bounded infrastructure failure and
  // then accept a following command rather than strand its poll loop.
  const results = [];
  context.chrome.tabs.update = () => new Promise(() => {});
  context.chrome.tabs.get = async () => ({id:7, windowId:2});
  context.chrome.runtime.getPlatformInfo = async () => ({os: "test"});
  context.fetch = async (_url, options) => {
    if (options?.body) results.push(JSON.parse(options.body));
    return {ok: true, json: async () => ({status: "active"})};
  };
  vm.runInContext(`getManagedTab = async () => {
    await navigateTab(7, "https://www.avito.ru/", 1000, false, "listing");
    return {tab: {id:7}, classification: {status:"ready"}, diagnostics:{}};
  };`, context);
  await vm.runInContext('executeCommand({id:"hung-native", type:"reveal_phone", maxClicks:1})', context);
  const failure = results.find(item => item.type === "result");
  assert.equal(failure.status, "browser_infra");
  assert.equal(failure.diagnostics.stage, "tabs.update");
  assert.equal(vm.runInContext("currentCommandId", context), null);
  assert.equal(activePulses.size, 0);
  context.chrome.tabs.update = async () => ({id:7, windowId:2});
  context.chrome.tabs.get = async () => ({id:7, windowId:2});
  context.chrome.windows = {update: async () => ({id:2})};
  vm.runInContext(`getManagedTab = async () => ({
    tab: {id:7}, classification: {status:"ready"}, diagnostics:{}
  });`, context);
  // A new namespace must be present before it is first accessed by the proxy.
  vm.runInContext('sendRevealMessage = async () => ({status:"phone"});', context);
  await vm.runInContext('executeCommand({id:"following-native", type:"reveal_phone", maxClicks:1})', context);
  assert.equal(results.find(item => item.id === "following-native" && item.type === "result").status, "phone");
  assert.equal(activePulses.size, 0);
  console.log("Command lifecycle: timeout, manual pause, inactive cancellation verified");
})().catch(error => { console.error(error); process.exitCode = 1; });
