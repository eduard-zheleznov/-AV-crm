importScripts("config.local.js");

const CONFIG = globalThis.AVITO_CRM_CONFIG;
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

class PageNotReadyError extends Error {}

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
    const tab = await getManagedTab(command.url, command.pageTimeoutMs);
    for (let attempt = 1; attempt <= command.maxClicks; attempt += 1) {
      if (attempt > 1) {
        await delay(Math.max(1000, command.retryDelayMs));
        await navigateTab(tab.id, command.url, command.pageTimeoutMs, true);
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
    await postEvent({
      id: command.id,
      type: "result",
      status: error instanceof PageNotReadyError ? "page_not_ready" : "error",
      reason: safeMessage(error)
    }).catch(() => undefined);
  } finally {
    currentCommandId = null;
  }
}

async function getManagedTab(url, pageTimeoutMs) {
  if (managedTabId !== null) {
    try {
      const existing = await chrome.tabs.get(managedTabId);
      return await navigateTab(existing.id, url, pageTimeoutMs, false);
    } catch (_error) {
      const staleTabId = managedTabId;
      managedTabId = null;
      if (staleTabId !== null) {
        await chrome.tabs.remove(staleTabId).catch(() => undefined);
      }
    }
  }
  // Create an empty managed tab first. The navigation observer must be attached
  // before the real Avito navigation starts; otherwise Chrome can report the
  // previous page as complete and the extension captures a white/stale frame.
  const created = await chrome.tabs.create({ url: "about:blank", active: true });
  managedTabId = created.id;
  return await navigateTab(created.id, url, pageTimeoutMs, false);
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

function navigateTab(tabId, expectedUrl, timeoutMs, forceReload) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const cleanup = () => {
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
    };
    const complete = (tab) => {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      resolve(tab);
    };
    const fail = (error) => {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      reject(error);
    };
    const listener = (updatedTabId, changeInfo, tab) => {
      if (
        updatedTabId === tabId &&
        changeInfo.status === "complete" &&
        isExpectedSurface(tab?.url || changeInfo.url || "", expectedUrl)
      ) {
        complete(tab);
      }
    };
    const timer = setTimeout(() => {
      fail(
        new PageNotReadyError(
          "Страница объявления не успела полностью загрузиться; строка будет повторена без расходования попытки"
        )
      );
    }, Math.max(1000, timeoutMs));
    // The listener is intentionally installed before update/reload.
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs
      .get(tabId)
      .then((current) => {
        if (forceReload && isExpectedSurface(current.url || "", expectedUrl)) {
          return chrome.tabs.reload(tabId);
        }
        return chrome.tabs.update(tabId, { url: expectedUrl, active: true });
      })
      .then(() => chrome.tabs.get(tabId))
      .then((tab) => {
        if (tab.status === "complete" && isExpectedSurface(tab.url || "", expectedUrl)) {
          complete(tab);
        }
      })
      .catch(fail);
  });
}

function isExpectedSurface(actualUrl, expectedUrl) {
  try {
    const actual = new URL(actualUrl);
    const expected = new URL(expectedUrl);
    if (!/(^|\.)avito\.ru$/i.test(actual.hostname)) {
      return false;
    }
    const actualValue = `${actual.pathname}${actual.search}${actual.hash}`.toLowerCase();
    if (
      actualValue.includes("captcha") ||
      actualValue.includes("/challenge") ||
      /\/(?:auth|login)(?:[/?#]|$)/i.test(actual.pathname)
    ) {
      return true;
    }
    const normalizePath = (value) => value.replace(/\/+$/, "");
    return normalizePath(actual.pathname) === normalizePath(expected.pathname);
  } catch (_error) {
    return false;
  }
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
