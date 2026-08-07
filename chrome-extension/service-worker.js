importScripts("config.local.js", "runtime-core.js", "trusted-click.js");

const CONFIG = globalThis.AVITO_CRM_CONFIG;
const RUNTIME = globalThis.AVITO_CRM_RUNTIME_CORE;
const TRUSTED_CLICK = globalThis.AVITO_CRM_TRUSTED_CLICK;
const EXTENSION_VERSION = chrome.runtime.getManifest().version;
const EXTENSION_INSTANCE_ID = crypto.randomUUID();
const BROWSER_CLICK_API_TIMEOUT_MS = 1000;
const BASE_URL = CONFIG ? `http://127.0.0.1:${CONFIG.port}` : "";
const AUTH_HEADERS = CONFIG
  ? {
      Authorization: `Bearer ${CONFIG.token}`,
      "Content-Type": "application/json",
      "X-Avito-CRM-Extension-Version": EXTENSION_VERSION,
      "X-Avito-CRM-Extension-Instance": EXTENSION_INSTANCE_ID
    }
  : {};

let polling = false;
let managedTabId = null;
let currentCommandId = null;
let currentCommand = null;

class BrowserInfrastructureError extends Error {
  constructor(message, diagnostics = {}) {
    super(message);
    this.diagnostics = diagnostics;
  }
}

class ListingNavigationError extends Error {
  constructor(message, diagnostics = {}) {
    super(message);
    this.diagnostics = diagnostics;
  }
}

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

chrome.runtime.onInstalled.addListener(() => startPolling());
chrome.runtime.onStartup.addListener(() => startPolling());
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "avito-crm-bridge") {
    startPolling();
  }
});
chrome.action.onClicked.addListener(() => startPolling());
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "avito_crm_browser_click") {
    handleBrowserClick(message, sender)
      .then(sendResponse)
      .catch(() => sendResponse({ ok: false, code: "browser_click_unavailable" }));
    return true;
  }
  if (message?.type !== "avito_crm_status" || !currentCommandId) {
    return false;
  }
  postEvent({
    id: currentCommandId,
    type: "status",
    status: message.status,
    reason: message.reason || ""
  })
    .then(() => sendResponse({ ok: true }))
    .catch(() => sendResponse({ ok: false }));
  return true;
});

startPolling();
chrome.alarms.create("avito-crm-bridge", { periodInMinutes: 0.5 });

function startPolling() {
  if (polling || !validConfig()) {
    return;
  }
  polling = true;
  pollLoop().finally(() => {
    polling = false;
  });
}

function validConfig() {
  return Boolean(
    CONFIG &&
      Number.isInteger(CONFIG.port) &&
      CONFIG.port >= 1024 &&
      CONFIG.port <= 65535 &&
      typeof CONFIG.token === "string" &&
      CONFIG.token.length >= 32
  );
}

async function pollLoop() {
  let retryDelayMs = 1000;
  while (true) {
    try {
      const response = await fetch(`${BASE_URL}/v1/poll`, {
        method: "GET",
        headers: AUTH_HEADERS,
        cache: "no-store"
      });
      if (response.status === 204) {
        retryDelayMs = 1000;
        continue;
      }
      if (!response.ok) {
        await delay(retryDelayMs);
        retryDelayMs = Math.min(15000, retryDelayMs * 2);
        continue;
      }
      retryDelayMs = 1000;
      const command = await response.json();
      await executeCommand(command);
    } catch (_error) {
      await delay(retryDelayMs);
      retryDelayMs = Math.min(15000, retryDelayMs * 2);
    }
  }
}

async function executeCommand(command) {
  currentCommandId = command.id;
  currentCommand = command;
  try {
    const prepared = await getManagedTab(command);
    if (command.type === "health_probe") {
      if (prepared.classification.status === "manual_required") {
        await postEvent({
          id: command.id,
          type: "result",
          status: "manual_required",
          reason: "Avito требует ручной проверки в обычном Chrome",
          diagnostics: prepared.diagnostics
        });
        return;
      }
      await postEvent({
        id: command.id,
        type: "result",
        status: "healthy",
        diagnostics: prepared.diagnostics
      });
      return;
    }
    const tab = prepared.tab;
    for (let attempt = 1; attempt <= command.maxClicks; attempt += 1) {
      if (attempt > 1) {
        await delay(Math.max(1000, command.retryDelayMs));
        const reloaded = await navigateTab(
          tab.id,
          command.url,
          command.pageTimeoutMs,
          true,
          "listing"
        );
        if (
          reloaded.classification.status !== "ready" &&
          reloaded.classification.status !== "manual_required"
        ) {
          throw navigationError(reloaded, command.url);
        }
      }
      await focusTab(tab.id);
      const result = await sendRevealMessage(tab.id, command);
      if (result.status === "phone_error" && attempt < command.maxClicks) {
        continue;
      }
      if (result.status === "screenshot") {
        await focusTab(tab.id);
        await delay(300);
        await postEvent({
          id: command.id,
          type: "result",
          status: "screen_capture",
          crop: result.crop || null,
          diagnostics: prepared.diagnostics
        });
        return;
      }
      await postEvent({
        id: command.id,
        type: "result",
        ...result,
        diagnostics: {
          ...prepared.diagnostics,
          content: result.diagnostics || null
        }
      });
      return;
    }
    await postEvent({
      id: command.id,
      type: "result",
      status: "phone_error",
      reason: "Avito не показал номер после разрешённого числа попыток"
    });
  } catch (error) {
    const status =
      error instanceof ListingNavigationError ? "listing_mismatch" : "browser_infra";
    await postEvent({
      id: command.id,
      type: "result",
      status,
      reason: safeMessage(error),
      diagnostics: error?.diagnostics || null
    }).catch(() => undefined);
  } finally {
    currentCommandId = null;
    currentCommand = null;
  }
}

async function handleBrowserClick(message, sender) {
  const senderTabId = sender?.tab?.id;
  let tab = await browserClickTab(senderTabId);
  let validation = validateBrowserClick(message, senderTabId, tab);
  if (!validation.ok) {
    return validation;
  }
  const focused = await TRUSTED_CLICK.withTimeout(
    focusTab(senderTabId),
    BROWSER_CLICK_API_TIMEOUT_MS,
    "tab_focus_timeout"
  )
    .then(() => true)
    .catch(() => false);
  if (!focused) {
    return { ok: false, code: "tab_focus_timeout" };
  }
  // STOP/cancellation or a redirect may race with focus. Revalidate the live
  // command, exact tab and listing immediately before attaching the debugger.
  tab = await browserClickTab(senderTabId);
  validation = validateBrowserClick(message, senderTabId, tab);
  if (!validation.ok) {
    return validation;
  }
  return await TRUSTED_CLICK.dispatch(
    chrome,
    {
      tabId: senderTabId,
      x: message.x,
      y: message.y
    },
    { timeoutMs: BROWSER_CLICK_API_TIMEOUT_MS }
  );
}

async function browserClickTab(tabId) {
  if (!Number.isInteger(tabId)) {
    return null;
  }
  return await TRUSTED_CLICK.withTimeout(
    chrome.tabs.get(tabId),
    BROWSER_CLICK_API_TIMEOUT_MS,
    "tab_lookup_timeout"
  ).catch(() => null);
}

function validateBrowserClick(message, senderTabId, tab) {
  return TRUSTED_CLICK.validateRequest(message, {
    currentCommandId,
    currentCommandType: currentCommand?.type || "",
    managedTabId,
    senderTabId,
    actualUrl: tab?.url || "",
    expectedUrl: currentCommand?.url || "",
    sameListingIdentity: RUNTIME.sameListingIdentity
  });
}

async function getManagedTab(command) {
  const mode = command.type === "health_probe" ? "health" : "listing";
  const attempts = [];
  for (let recoveryAttempt = 0; recoveryAttempt <= 1; recoveryAttempt += 1) {
    let tab = null;
    if (recoveryAttempt === 0 && managedTabId !== null) {
      tab = await chrome.tabs.get(managedTabId).catch(() => null);
    }
    if (!tab) {
      const created = await chrome.tabs.create({ url: "about:blank", active: true });
      managedTabId = created.id;
      tab = created;
    }
    const navigation = await navigateTab(
      tab.id,
      command.url,
      command.pageTimeoutMs,
      false,
      mode
    );
    attempts.push(navigation.diagnostics);
    if (
      navigation.classification.status === "ready" ||
      navigation.classification.status === "manual_required"
    ) {
      return {
        tab: navigation.tab,
        classification: navigation.classification,
        diagnostics: { attempts, recovered: recoveryAttempt > 0 }
      };
    }
    if (navigation.classification.status === "listing_mismatch") {
      throw new ListingNavigationError(
        "Avito перенаправил строку на другое объявление",
        { attempts, classification: navigation.classification }
      );
    }
    await resetManagedTab(tab.id);
    if (recoveryAttempt === 0) {
      await delay(Math.max(1000, Number(command.recoveryBackoffMs) || 5000));
    }
  }
  throw new BrowserInfrastructureError(
    "Chrome не отобразил Avito после пересоздания управляемой вкладки",
    { attempts }
  );
}

async function resetManagedTab(tabId) {
  if (managedTabId === tabId) {
    managedTabId = null;
  }
  await chrome.tabs.remove(tabId).catch(() => undefined);
}

function navigationError(navigation, expectedUrl) {
  if (navigation.classification.status === "listing_mismatch") {
    return new ListingNavigationError(
      "Avito перенаправил строку на другое объявление",
      { expectedUrl, navigation: navigation.diagnostics }
    );
  }
  return new BrowserInfrastructureError(
    "Chrome не подтвердил готовность страницы Avito",
    { expectedUrl, navigation: navigation.diagnostics }
  );
}

async function sendRevealMessage(tabId, command) {
  let lastError = null;
  const deadline = Date.now() + command.pageTimeoutMs + 5000;
  const cancellationController = new AbortController();
  const cancellation = waitForCancellation(tabId, command.id, cancellationController.signal);
  try {
    while (Date.now() < deadline) {
      try {
        return await Promise.race([
          chrome.tabs.sendMessage(tabId, {
            type: "avito_crm_reveal_once",
            commandId: command.id,
            expectedUrl: command.url,
            expectedContentVersion: EXTENSION_VERSION,
            pageTimeoutMs: command.pageTimeoutMs,
            manualTimeoutMs: command.manualTimeoutMs,
            phoneWaitMs: command.phoneWaitMs
          }),
          cancellation
        ]);
      } catch (error) {
        lastError = error;
        await delay(1000);
      }
    }
    throw new Error(`Расширение не подключилось к странице Avito: ${safeMessage(lastError)}`);
  } finally {
    cancellationController.abort();
  }
}

async function waitForCancellation(tabId, commandId, signal) {
  while (!signal.aborted && currentCommandId === commandId) {
    await delay(1000);
    if (signal.aborted) {
      return { status: "watcher_stopped" };
    }
    try {
      const response = await fetch(
        `${BASE_URL}/v1/command-status?id=${encodeURIComponent(commandId)}`,
        { method: "GET", headers: AUTH_HEADERS, cache: "no-store" }
      );
      if (!response.ok) {
        continue;
      }
      const payload = await response.json();
      if (payload.status === "cancelled") {
        // Destroy the waiting content-script context so the service worker is
        // immediately ready for the next command after STOP/restart.
        await chrome.tabs.reload(tabId).catch(() => undefined);
        return {
          status: "cancelled",
          reason: "Ожидание Chrome остановлено оператором"
        };
      }
    } catch (_error) {
      // The bridge may be restarting; keep the page untouched and retry.
    }
  }
  return { status: "watcher_stopped" };
}

async function focusTab(tabId) {
  const tab = await chrome.tabs.get(tabId);
  await chrome.windows.update(tab.windowId, { focused: true });
  await chrome.tabs.update(tabId, { active: true });
}

async function navigateTab(tabId, expectedUrl, timeoutMs, forceReload, mode) {
  const startedAt = Date.now();
  const deadline = startedAt + Math.max(3000, Number(timeoutMs) || 45000);
  const initial = await chrome.tabs.get(tabId);
  if (forceReload && isExpectedSurface(initial.url || "", expectedUrl, mode)) {
    await chrome.tabs.reload(tabId);
  } else {
    await chrome.tabs.update(tabId, { url: expectedUrl, active: true });
  }
  let tab = await chrome.tabs.get(tabId);
  let probe = null;
  let probeError = "";
  let staleContentReloaded = false;
  let classification = { status: "loading", reason: "navigation_started" };
  while (Date.now() < deadline) {
    tab = await chrome.tabs.get(tabId);
    try {
      probe = await probeTab(tabId, expectedUrl, mode);
      probeError = "";
      if (probe?.contentVersion !== EXTENSION_VERSION) {
        classification = {
          status: "loading",
          reason: "stale_content_script",
          actualUrl: tab?.url || ""
        };
        if (!staleContentReloaded) {
          staleContentReloaded = true;
          await chrome.tabs.reload(tabId);
          await delay(500);
          continue;
        }
      } else {
        classification = RUNTIME.classifyProbe(probe, expectedUrl, mode);
      }
      if (
        classification.status === "listing_mismatch" &&
        (tab?.status !== "complete" || Date.now() - startedAt < 1500)
      ) {
        classification = {
          status: "loading",
          reason: "redirect_still_loading",
          actualUrl: tab?.url || ""
        };
      }
    } catch (error) {
      probeError = safeMessage(error);
      if (!staleContentReloaded && isMissingContentScriptError(probeError)) {
        staleContentReloaded = true;
        classification = {
          status: "loading",
          reason: "content_script_reload",
          actualUrl: tab?.url || ""
        };
        await chrome.tabs.reload(tabId).catch(() => undefined);
        await delay(500);
        continue;
      }
      classification = classifyWithoutContent(tab, expectedUrl, mode);
    }
    if (
      classification.status === "ready" ||
      classification.status === "manual_required" ||
      classification.status === "listing_mismatch"
    ) {
      break;
    }
    await delay(500);
  }
  const diagnostics = {
    stage: "navigation",
    mode,
    elapsedMs: Date.now() - startedAt,
    tabId,
    tabStatus: tab?.status || "unknown",
    discarded: Boolean(tab?.discarded),
    actualUrl: tab?.url || "",
    expectedListingId: RUNTIME.listingId(expectedUrl),
    actualListingId: RUNTIME.listingId(tab?.url || ""),
    classification,
    probe: probe
      ? {
          readyState: probe.readyState,
          rendered: probe.rendered,
          manual: probe.manual,
          auth: probe.auth,
          inactive: probe.inactive,
          bodyLength: probe.bodyLength,
          visibleHeadings: probe.visibleHeadings,
          contentVersion: probe.contentVersion
        }
      : null,
    probeError
  };
  return { tab, classification, diagnostics };
}

function isMissingContentScriptError(message) {
  const normalized = String(message || "").toLowerCase();
  return (
    normalized.includes("receiving end does not exist") ||
    normalized.includes("could not establish connection") ||
    normalized.includes("extension context invalidated")
  );
}

async function probeTab(tabId, expectedUrl, mode) {
  return await chrome.tabs.sendMessage(tabId, {
    type: "avito_crm_probe",
    expectedUrl,
    mode
  });
}

function classifyWithoutContent(tab, expectedUrl, mode) {
  const actualUrl = tab?.url || "";
  if (RUNTIME.isManualSurface(actualUrl)) {
    return { status: "manual_required", reason: "manual_surface", actualUrl };
  }
  if (mode === "listing" && tab?.status === "complete") {
    const actualId = RUNTIME.listingId(actualUrl);
    const expectedId = RUNTIME.listingId(expectedUrl);
    if (actualId && expectedId && actualId !== expectedId) {
      return { status: "listing_mismatch", reason: "different_listing_id", actualUrl };
    }
  }
  return { status: "loading", reason: "content_script_unavailable", actualUrl };
}

function isExpectedSurface(actualUrl, expectedUrl, mode = "listing") {
  if (RUNTIME.isManualSurface(actualUrl)) {
    return true;
  }
  if (mode === "health") {
    return Boolean(RUNTIME.avitoUrl(actualUrl));
  }
  return RUNTIME.sameListingIdentity(actualUrl, expectedUrl);
}

async function postEvent(payload) {
  const response = await fetch(`${BASE_URL}/v1/event`, {
    method: "POST",
    headers: AUTH_HEADERS,
    body: JSON.stringify(payload),
    cache: "no-store"
  });
  if (!response.ok) {
    throw new Error(`Локальный мост отклонил событие: HTTP ${response.status}`);
  }
}

function safeMessage(error) {
  if (error && typeof error.message === "string") {
    return error.message.slice(0, 300);
  }
  return String(error || "неизвестная ошибка").slice(0, 300);
}
