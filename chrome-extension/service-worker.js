importScripts("config.local.js", "readiness-core.js");

const CONFIG = globalThis.AVITO_CRM_CONFIG;
const READINESS = globalThis.AVITO_CRM_READINESS;
const EXTENSION_VERSION = chrome.runtime.getManifest().version;
const BASE_URL = CONFIG ? `http://127.0.0.1:${CONFIG.port}` : "";
const AUTH_HEADERS = CONFIG
  ? {
      Authorization: `Bearer ${CONFIG.token}`,
      "Content-Type": "application/json",
      "X-Avito-CRM-Extension-Version": EXTENSION_VERSION
    }
  : {};

let polling = false;
let managedTabId = null;
let currentCommandId = null;

const NAVIGATION_ATTEMPTS = 2;
const NAVIGATION_RETRY_DELAY_MS = 1500;

class PageNotReadyError extends Error {
  constructor(message, diagnostics = {}) {
    super(message);
    this.diagnostics = diagnostics;
  }
}

class InvalidListingNavigationError extends Error {
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
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
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
  try {
    let tab = await getManagedTab(command.url, command.pageTimeoutMs, false);
    for (let attempt = 1; attempt <= command.maxClicks; attempt += 1) {
      if (attempt > 1) {
        await delay(Math.max(1000, command.retryDelayMs));
        tab = await getManagedTab(command.url, command.pageTimeoutMs, true);
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
          crop: result.crop || null
        });
        return;
      }
      await postEvent({ id: command.id, type: "result", ...result });
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
      error instanceof InvalidListingNavigationError
        ? "invalid_listing"
        : error instanceof PageNotReadyError
          ? "page_not_ready"
          : "error";
    await postEvent({
      id: command.id,
      type: "result",
      status,
      reason: safeMessage(error),
      diagnostics: safeDiagnostics(error?.diagnostics)
    }).catch(() => undefined);
  } finally {
    currentCommandId = null;
  }
}

async function getManagedTab(url, pageTimeoutMs, forceReload) {
  const expectedId = READINESS.listingId(url);
  if (!expectedId) {
    throw new InvalidListingNavigationError(
      "Ссылка Avito не содержит корректный ID объявления",
      { state: "invalid_expected_url" }
    );
  }

  let lastError = null;
  for (let navigationAttempt = 1; navigationAttempt <= NAVIGATION_ATTEMPTS; navigationAttempt += 1) {
    try {
      let tab = null;
      if (managedTabId !== null) {
        tab = await chrome.tabs.get(managedTabId).catch(() => null);
      }
      if (!tab) {
        const created = await chrome.tabs.create({ url: "about:blank", active: true });
        managedTabId = created.id;
        tab = created;
      }
      return await navigateTab(
        tab.id,
        url,
        pageTimeoutMs,
        forceReload,
        navigationAttempt
      );
    } catch (error) {
      if (error instanceof InvalidListingNavigationError) {
        throw error;
      }
      lastError = error;
      await resetManagedTab();
      if (navigationAttempt < NAVIGATION_ATTEMPTS) {
        await delay(NAVIGATION_RETRY_DELAY_MS);
      }
    }
  }
  throw new PageNotReadyError(
    "Страница объявления не отобразила рабочую область после двух ограниченных загрузок; " +
      "строка будет повторена без расходования попытки",
    lastError?.diagnostics || { state: "navigation_attempts_exhausted" }
  );
}

async function resetManagedTab() {
  const staleTabId = managedTabId;
  managedTabId = null;
  if (staleTabId !== null) {
    await chrome.tabs.remove(staleTabId).catch(() => undefined);
  }
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
            expectedUrl: command.url,
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

async function navigateTab(tabId, expectedUrl, timeoutMs, forceReload, navigationAttempt) {
  const startedAt = Date.now();
  const deadline = startedAt + Math.max(3000, Number(timeoutMs) || 10000);
  const current = await chrome.tabs.get(tabId);
  if (forceReload && READINESS.sameListing(current.url || "", expectedUrl)) {
    await chrome.tabs.reload(tabId);
  } else {
    await chrome.tabs.update(tabId, { url: expectedUrl, active: true });
  }

  let actionableSamples = 0;
  let renderedSamples = 0;
  let mismatchSamples = 0;
  let lastDiagnostics = {
    navigationAttempt,
    state: "navigation_started",
    expectedId: READINESS.listingId(expectedUrl)
  };
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId);
    const probe = await chrome.tabs
      .sendMessage(tabId, {
        type: "avito_crm_readiness_probe",
        expectedUrl
      })
      .catch(() => null);
    const state = probe ? READINESS.classifyProbe(probe, expectedUrl) : "loading";
    lastDiagnostics = {
      navigationAttempt,
      state,
      expectedId: READINESS.listingId(expectedUrl),
      actualId: READINESS.listingId(probe?.actualUrl || tab.url || ""),
      tabStatus: tab.status || "unknown",
      readyState: probe?.readyState || "unavailable",
      probeConnected: Boolean(probe),
      elapsedMs: Date.now() - startedAt
    };

    if (state === "manual_required") {
      return tab;
    }
    if (state === "actionable") {
      actionableSamples += 1;
      renderedSamples += 1;
      mismatchSamples = 0;
      if (actionableSamples >= 2) {
        return tab;
      }
    } else if (state === "rendered") {
      actionableSamples = 0;
      renderedSamples += 1;
      mismatchSamples = 0;
    } else if (state === "listing_mismatch") {
      actionableSamples = 0;
      renderedSamples = 0;
      mismatchSamples += 1;
      if (mismatchSamples >= 2 && probe?.rendered && tab.status !== "loading") {
        throw new InvalidListingNavigationError(
          "Avito открыл другое объявление вместо переданного ID",
          lastDiagnostics
        );
      }
    } else {
      actionableSamples = 0;
      renderedSamples = 0;
      mismatchSamples = 0;
    }
    await delay(500);
  }

  const finalTab = await chrome.tabs.get(tabId);
  if (renderedSamples >= 2 && READINESS.sameListing(finalTab.url || "", expectedUrl)) {
    // The listing DOM is stable. Let the unchanged content.js distinguish a
    // missing phone button from an inactive listing instead of misclassifying
    // slow background resources as a browser failure.
    return finalTab;
  }
  if (lastDiagnostics.state === "listing_mismatch") {
    throw new InvalidListingNavigationError(
      "Avito открыл другое объявление вместо переданного ID",
      lastDiagnostics
    );
  }
  throw new PageNotReadyError(
    "Страница объявления не отобразила рабочую область; строка будет повторена без расходования попытки",
    lastDiagnostics
  );
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

function safeDiagnostics(value) {
  if (!value || typeof value !== "object") {
    return {};
  }
  const allowed = [
    "navigationAttempt",
    "state",
    "expectedId",
    "actualId",
    "tabStatus",
    "readyState",
    "probeConnected",
    "elapsedMs"
  ];
  return Object.fromEntries(
    allowed.filter((key) => value[key] !== undefined).map((key) => [key, value[key]])
  );
}
