(function initializeBrowserContextCore(globalObject) {
  "use strict";

  function requiresIncognito(command) {
    return command?.incognitoRequired !== false;
  }

  function tabMatchesCommand(tab, command) {
    return Boolean(
      tab &&
        Number.isInteger(tab.id) &&
        Boolean(tab.incognito) === requiresIncognito(command)
    );
  }

  async function removeWindowSafely(chromeApi, windowId) {
    if (!Number.isInteger(windowId)) {
      return;
    }
    await chromeApi.windows.remove(windowId).catch(() => undefined);
  }

  async function removeTabSafely(chromeApi, tabId) {
    if (!Number.isInteger(tabId)) {
      return;
    }
    await chromeApi.tabs.remove(tabId).catch(() => undefined);
  }

  async function resolveCreatedWindowTab(chromeApi, createdWindow) {
    const embeddedTab = Array.isArray(createdWindow?.tabs)
      ? createdWindow.tabs.find((candidate) => Number.isInteger(candidate?.id))
      : null;
    if (embeddedTab || !Number.isInteger(createdWindow?.id)) {
      return embeddedTab;
    }
    try {
      const tabs = await chromeApi.tabs.query({ windowId: createdWindow.id });
      return Array.isArray(tabs)
        ? tabs.find((candidate) => Number.isInteger(candidate?.id)) || null
        : null;
    } catch (_error) {
      return null;
    }
  }

  async function createManagedTab(chromeApi, command) {
    if (!requiresIncognito(command)) {
      try {
        const tab = await chromeApi.tabs.create({ url: "about:blank", active: true });
        if (tabMatchesCommand(tab, command)) {
          return { ok: true, tab, incognitoAccess: null };
        }
        await removeTabSafely(chromeApi, tab?.id);
        return { ok: false, code: "standard_context_mismatch", incognitoAccess: null };
      } catch (_error) {
        return { ok: false, code: "standard_context_create_failed", incognitoAccess: null };
      }
    }

    let incognitoAccess = false;
    try {
      incognitoAccess = Boolean(await chromeApi.extension.isAllowedIncognitoAccess());
    } catch (_error) {
      return { ok: false, code: "incognito_access_check_failed", incognitoAccess: null };
    }
    if (!incognitoAccess) {
      return { ok: false, code: "incognito_not_allowed", incognitoAccess: false };
    }

    let createdWindow;
    try {
      createdWindow = await chromeApi.windows.create({
        url: "about:blank",
        focused: true,
        incognito: true,
        type: "normal"
      });
    } catch (_error) {
      return { ok: false, code: "incognito_context_create_failed", incognitoAccess: true };
    }
    const tab = await resolveCreatedWindowTab(chromeApi, createdWindow);
    if (!tabMatchesCommand(tab, command)) {
      await removeWindowSafely(chromeApi, createdWindow?.id);
      return { ok: false, code: "incognito_context_mismatch", incognitoAccess: true };
    }
    return { ok: true, tab, incognitoAccess: true };
  }

  function failureMessage(code) {
    if (code === "incognito_not_allowed") {
      return (
        "Для расширения Avito CRM выключено разрешение Chrome «Разрешить использование " +
        "в режиме инкогнито». Включите его на странице chrome://extensions и повторите запуск."
      );
    }
    if (code === "incognito_access_check_failed") {
      return "Chrome не позволил проверить доступ расширения к режиму инкогнито";
    }
    if (code === "incognito_context_mismatch") {
      return "Chrome открыл вкладку не в режиме инкогнито; обычный режим не используется";
    }
    if (code === "incognito_context_create_failed") {
      return "Chrome не смог создать обязательное окно инкогнито";
    }
    return "Chrome не смог создать отдельную управляемую вкладку";
  }

  const api = {
    createManagedTab,
    failureMessage,
    removeTabSafely,
    resolveCreatedWindowTab,
    requiresIncognito,
    tabMatchesCommand
  };
  globalObject.AVITO_CRM_BROWSER_CONTEXT_CORE = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(globalThis);
