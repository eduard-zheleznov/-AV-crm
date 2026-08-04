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
        await chrome.tabs.update(tab.id, { url: command.url, active: true });
        await waitForLoad(tab.id, command.pageTimeoutMs);
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
      status: "error",
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
      await chrome.tabs.update(existing.id, { url, active: true });
      await waitForLoad(existing.id, pageTimeoutMs);
      return await chrome.tabs.get(existing.id);
    } catch (_error) {
      managedTabId = null;
    }
  }
  const created = await chrome.tabs.create({ url, active: true });
  managedTabId = created.id;
  await waitForLoad(created.id, pageTimeoutMs);
  return await chrome.tabs.get(created.id);
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

function waitForLoad(tabId, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    const cleanup = () => {
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
    };
    const complete = () => {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      resolve();
    };
    const listener = (updatedTabId, changeInfo) => {
      if (updatedTabId === tabId && changeInfo.status === "complete") {
        complete();
      }
    };
    const timer = setTimeout(() => {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      resolve();
    }, Math.max(1000, timeoutMs));
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((tab) => {
      if (tab.status === "complete") {
        complete();
      }
    });
  });
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
