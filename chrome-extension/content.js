(function initializeAvitoContentScript(globalObject) {
"use strict";

const INSTALLED_CONTENT_VERSION = chrome.runtime.getManifest().version;
if (globalObject.AVITO_CRM_CONTENT_SCRIPT_VERSION === INSTALLED_CONTENT_VERSION) {
  return;
}

const STRONG_CHALLENGE_PATTERNS = [
  "доступ временно ограничен",
  "проблема с ip",
  "подтвердите, что вы не робот",
  "необычная активность"
];
const SURFACE_CHALLENGE_PATTERNS = [
  ...STRONG_CHALLENGE_PATTERNS,
  "доступ ограничен",
  "пройдите проверку",
  "captcha",
  "слишком много запросов"
];

const AUTH_PATTERNS = ["телефон или почта", "нет аккаунта на авито?"];
const TEMP_ERROR_PATTERNS = [
  "произошла ошибка, поэтому мы не можем показать телефон",
  "не можем показать телефон",
  "попробуйте перезагрузить страницу"
];
const INACTIVE_PATTERNS = [
  ["снято с публикации", "объявление снято с публикации"],
  ["объявление закрыто", "объявление закрыто"],
  ["объявление заблокировано", "объявление заблокировано"],
  ["объявление удалено", "объявление удалено"],
  ["такого объявления нет", "объявление не найдено"],
  ["страница не найдена", "страница объявления не найдена"],
  ["объявление больше не актуально", "объявление больше не актуально"]
];
const CONTENT_SCRIPT_VERSION = INSTALLED_CONTENT_VERSION;
const PHONE_BUTTON_RE = /(?:показать\s+(?:номер(?:\s+телефона)?|телефон)|позвонить)/i;
const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
let activeRevealCommandId = null;

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "avito_crm_probe") {
    sendResponse(probePage(message.expectedUrl, message.mode));
    return false;
  }
  if (message?.type === "avito_crm_measure_click_target") {
    measureAttachedClickTarget(message)
      .then(sendResponse)
      .catch(() => sendResponse({ ok: false, code: "target_measurement_unavailable" }));
    return true;
  }
  if (message?.type !== "avito_crm_reveal_once") {
    return false;
  }
  if (activeRevealCommandId && activeRevealCommandId !== message.commandId) {
    sendResponse({ status: "error", reason: "Другая команда уже активна" });
    return false;
  }
  activeRevealCommandId = message.commandId;
  revealOnce(message)
    .then((result) => sendResponse(result))
    .catch((error) =>
      sendResponse({
        status: "error",
        reason: error?.message ? error.message.slice(0, 300) : String(error).slice(0, 300)
      })
    )
    .finally(() => {
      if (activeRevealCommandId === message.commandId) {
        activeRevealCommandId = null;
      }
    });
  return true;
});

async function revealOnce(command) {
  if (
    command.expectedContentVersion &&
    command.expectedContentVersion !== CONTENT_SCRIPT_VERSION
  ) {
    return {
      status: "stale_content_script",
      reason: "Content script Chrome не совпадает с версией расширения"
    };
  }
  const manualResult = await waitForManualAction(command.manualTimeoutMs);
  if (manualResult) {
    return manualResult;
  }

  const pageReady = await waitForTargetReady(command.expectedUrl, command.pageTimeoutMs);
  if (!pageReady) {
    return {
      status: "page_not_ready",
      reason: "Страница объявления не успела полностью отобразиться"
    };
  }

  const lateManualResult = await waitForManualAction(command.manualTimeoutMs);
  if (lateManualResult) {
    return lateManualResult;
  }

  const inactive = inactiveReason();
  if (inactive) {
    return { status: "inactive", reason: inactive };
  }

  const existing = findPhone();
  if (existing) {
    return { status: "phone", phone: existing, source: "chrome-extension-dom" };
  }

  const button = await waitForPhoneButton(Math.min(10000, command.phoneWaitMs));
  if (!button) {
    return { status: "button_missing", reason: "Кнопка показа телефона не найдена" };
  }

  return await revealWithVerifiedClick(button, command);
}

async function revealWithVerifiedClick(initialButton, command) {
  const overallDeadline = Date.now() + Math.max(3000, Number(command.phoneWaitMs) || 10000);
  let button = initialButton;
  let phoneRegion = null;

  for (let actionAttempt = 1; actionAttempt <= 2; actionAttempt += 1) {
    button = await prepareClickTarget(button);
    if (!button) {
      if (actionAttempt === 1) {
        notifyStatus("click_recovery", "кнопка будет найдена повторно");
        await delay(500);
        continue;
      }
      return clickNotEffective();
    }
    if (manualReason()) {
      await waitForManualAction(command.manualTimeoutMs);
      button = findPhoneButton();
      actionAttempt -= 1;
      continue;
    }

    const before = revealState(button);
    phoneRegion = screenRegion(button);
    notifyStatus("clicking", "кнопка показа телефона готова");
    const dispatched = await requestBrowserClick(button, command);
    if (!dispatched.ok) {
      if (actionAttempt === 1) {
        notifyStatus("click_recovery", "browser-level клик недоступен; повторяем один раз");
        button = findPhoneButton();
        await delay(500);
        continue;
      }
      return browserClickUnavailable(dispatched.code);
    }
    notifyStatus("click_dispatched", "browser-level клик отправлен; ожидаем изменение DOM");

    const verificationDeadline =
      actionAttempt === 1
        ? Math.min(overallDeadline, Date.now() + 3000)
        : overallDeadline;
    const transition = await waitForRevealTransition(
      before,
      verificationDeadline,
      command.manualTimeoutMs
    );
    if (transition.result) {
      return transition.result;
    }
    if (transition.confirmed) {
      notifyStatus("reveal_confirmed", "Avito подтвердил раскрытие номера");
      return await waitForPhoneOrOcrFallback(
        overallDeadline,
        command.manualTimeoutMs,
        button,
        phoneRegion
      );
    }
    if (actionAttempt === 1) {
      notifyStatus("click_recovery", "первый клик не изменил DOM; повторяем один раз");
      button = findPhoneButton();
      await delay(500);
    }
  }
  return clickNotEffective();
}

async function prepareClickTarget(candidate) {
  let button = candidate?.isConnected ? candidate : findPhoneButton();
  if (!button) {
    return null;
  }
  button.scrollIntoView({ behavior: "auto", block: "center", inline: "center" });
  await delay(350);
  button = findPhoneButton();
  if (!button || !isActionableClickTarget(button)) {
    return null;
  }
  button.focus({ preventScroll: true });
  await delay(100);
  return isActionableClickTarget(button) ? button : null;
}

function isActionableClickTarget(button) {
  if (
    !button?.isConnected ||
    !isVisibleInViewport(button) ||
    button.disabled ||
    button.getAttribute("aria-disabled") === "true"
  ) {
    return false;
  }
  const rect = button.getBoundingClientRect();
  const centerX = Math.min(window.innerWidth - 1, Math.max(0, rect.left + rect.width / 2));
  const centerY = Math.min(window.innerHeight - 1, Math.max(0, rect.top + rect.height / 2));
  const topElement = document.elementFromPoint(centerX, centerY);
  return Boolean(
    topElement &&
      (topElement === button || button.contains(topElement))
  );
}

async function requestBrowserClick(button, command) {
  // Do not send coordinates measured before chrome.debugger.attach: Chrome may
  // resize the viewport when attaching. The service worker asks this content
  // script for a fresh target while the debugger is already attached.
  const request = chrome.runtime.sendMessage({
    type: "avito_crm_browser_click",
    commandId: command.commandId
  });
  let timer = null;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(
      () => resolve({ ok: false, code: "browser_click_rpc_timeout" }),
      10000
    );
  });
  try {
    return await Promise.race([request, timeout]);
  } catch (_error) {
    return { ok: false, code: "browser_click_rpc_failed" };
  } finally {
    clearTimeout(timer);
  }
}

async function measureAttachedClickTarget(message) {
  if (message.commandId !== activeRevealCommandId) {
    return { ok: false, code: "command_mismatch" };
  }
  if (
    message.expectedContentVersion &&
    message.expectedContentVersion !== CONTENT_SCRIPT_VERSION
  ) {
    return { ok: false, code: "stale_content_script" };
  }
  if (
    !globalThis.AVITO_CRM_RUNTIME_CORE.sameListingIdentity(
      window.location.href,
      message.expectedUrl
    )
  ) {
    return { ok: false, code: "listing_mismatch" };
  }
  if (manualReason()) {
    return { ok: false, code: "manual_required" };
  }
  let button = await prepareClickTarget(findPhoneButton());
  if (!button) {
    return { ok: false, code: "click_target_unavailable" };
  }
  // Re-query once after focus/scroll settles. If the viewport changed, only
  // this final DOMRect is used; any earlier rectangle is discarded.
  await delay(50);
  button = findPhoneButton();
  if (!button || !isActionableClickTarget(button)) {
    return { ok: false, code: "click_target_unavailable" };
  }
  const rect = button.getBoundingClientRect();
  return {
    ok: true,
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
    width: rect.width,
    height: rect.height
  };
}

async function waitForRevealTransition(before, deadline, manualTimeoutMs) {
  while (Date.now() < deadline) {
    const manualResult = await waitForManualAction(manualTimeoutMs);
    if (manualResult) {
      return { confirmed: false, result: manualResult };
    }
    if (hasAnyText(TEMP_ERROR_PATTERNS)) {
      return {
        confirmed: false,
        result: {
          status: "phone_error",
          reason: "Avito сообщил, что временный номер сейчас показать не удалось"
        }
      };
    }
    const after = revealState(findPhoneButton());
    const transition = globalThis.AVITO_CRM_RUNTIME_CORE.classifyRevealTransition(before, after);
    if (transition.status === "confirmed") {
      if (after.phone) {
        notifyStatus("reveal_confirmed", "Avito подтвердил DOM-номер");
        return {
          confirmed: true,
          result: { status: "phone", phone: after.phone, source: "chrome-extension-dom" }
        };
      }
      return { confirmed: true, result: null };
    }
    await delay(250);
  }
  return { confirmed: false, result: null };
}

async function waitForPhoneOrOcrFallback(deadline, manualTimeoutMs, button, phoneRegion) {
  while (Date.now() < deadline) {
    const manualResult = await waitForManualAction(manualTimeoutMs);
    if (manualResult) {
      return manualResult;
    }
    const phone = findPhone();
    if (phone) {
      return { status: "phone", phone, source: "chrome-extension-dom" };
    }
    if (hasAnyText(TEMP_ERROR_PATTERNS)) {
      return {
        status: "phone_error",
        reason: "Avito сообщил, что временный номер сейчас показать не удалось"
      };
    }
    await delay(250);
  }
  return { status: "screenshot", crop: captureRegion(button, phoneRegion) };
}

function clickNotEffective(code = "dispatch_without_effect") {
  return {
    status: "click_not_effective",
    reason:
      "Avito не подтвердил раскрытие номера после двух ограниченных browser-level кликов " +
      `(${String(code).slice(0, 80)})`
  };
}

function browserClickUnavailable(code) {
  const safeCode = String(code || "browser_click_unavailable").slice(0, 80);
  if (safeCode === "listing_mismatch") {
    return {
      status: "listing_mismatch",
      reason: "Managed tab ушла с ожидаемого объявления до browser-level клика"
    };
  }
  return {
    status: "click_not_effective",
    reason:
      "Browser-level ввод недоступен после одного ограниченного повтора; " +
      `неподтверждённый клик не засчитан (${safeCode})`
  };
}

async function waitForManualAction(_timeoutMs) {
  let initialReason = manualReason();
  if (!initialReason) {
    return null;
  }
  // Do not stop the queue because of a short-lived page fragment. A real
  // challenge remains visible after the page has settled.
  await delay(700);
  initialReason = manualReason();
  if (!initialReason) {
    return null;
  }
  notifyStatus("manual_required", initialReason);
  while (true) {
    await delay(1000);
    if (!manualReason()) {
      notifyStatus("manual_cleared", initialReason);
      await delay(500);
      return null;
    }
  }
}

function notifyStatus(status, reason) {
  chrome.runtime.sendMessage({ type: "avito_crm_status", status, reason }).catch(() => undefined);
}

function pageText() {
  return `${document.title}\n${document.body?.innerText || ""}`.toLowerCase();
}

function manualReason() {
  const content = pageText();
  const currentUrl = location.href.toLowerCase();
  if (
    currentUrl.includes("captcha") ||
    currentUrl.includes("/challenge") ||
    STRONG_CHALLENGE_PATTERNS.some((pattern) => content.includes(pattern)) ||
    hasVisibleChallengeSurface()
  ) {
    return "ручная проверка Avito";
  }
  if (isAuthPage(currentUrl) || hasVisibleAuthDialog()) {
    return "авторизация Avito";
  }
  return "";
}

function hasVisibleChallengeSurface() {
  const selectors = [
    '[role="dialog"]',
    '[aria-modal="true"]',
    '[data-marker*="captcha" i]',
    '[class*="captcha" i]',
    'iframe[src*="captcha" i]',
    'iframe[title*="captcha" i]',
    'form[action*="captcha" i]'
  ];
  for (const element of document.querySelectorAll(selectors.join(","))) {
    if (!isVisibleInViewport(element)) {
      continue;
    }
    if (element.matches('iframe[src*="captcha" i], iframe[title*="captcha" i]')) {
      return true;
    }
    const text = (element.innerText || element.textContent || "").toLowerCase();
    if (SURFACE_CHALLENGE_PATTERNS.some((pattern) => text.includes(pattern))) {
      return true;
    }
  }
  return false;
}

function isAuthPage(currentUrl) {
  return /\/(?:auth|login)(?:[/?#]|$)/i.test(currentUrl);
}

function hasVisibleAuthDialog() {
  const dialogs = document.querySelectorAll('[role="dialog"], [aria-modal="true"]');
  for (const dialog of dialogs) {
    if (!isVisible(dialog)) {
      continue;
    }
    const text = (dialog.innerText || dialog.textContent || "").toLowerCase();
    if (AUTH_PATTERNS.some((pattern) => text.includes(pattern))) {
      return true;
    }
  }
  return false;
}

function inactiveReason() {
  const content = pageText();
  for (const [pattern, reason] of INACTIVE_PATTERNS) {
    if (content.includes(pattern)) {
      return reason;
    }
  }
  return "";
}

function hasAnyText(patterns) {
  const content = pageText();
  return patterns.some((pattern) => content.includes(pattern));
}

function hasTemporaryNumberLabel() {
  return pageText().includes("временный номер");
}

function findPhone() {
  const runtime = globalThis.AVITO_CRM_RUNTIME_CORE;
  const attributeCandidates = document.querySelectorAll(
    'a[href^="tel:"], [aria-label], [title], [data-phone], [data-marker*="phone" i]'
  );
  for (const element of attributeCandidates) {
    if (!isVisible(element)) {
      continue;
    }
    const phone = runtime.extractRussianPhone([
      element.getAttribute("href"),
      element.getAttribute("aria-label"),
      element.getAttribute("title"),
      element.getAttribute("data-phone"),
      element.getAttribute("value")
    ]);
    if (phone) {
      return phone;
    }
  }

  const textSelectors = [
    '[data-marker*="phone" i]',
    '[class*="phone" i]',
    '[class*="contact" i]',
    '[role="dialog"]',
    '[aria-modal="true"]',
    'button, a, [role="button"]'
  ];
  for (const element of document.querySelectorAll(textSelectors.join(","))) {
    if (!isVisible(element)) {
      continue;
    }
    const phone = runtime.extractRussianPhone([element.innerText, element.textContent]);
    if (phone) {
      return phone;
    }
  }

  const bodyText = document.body?.innerText || "";
  for (const pattern of ["временный номер", "телефон", "позвонить"]) {
    let offset = bodyText.toLowerCase().indexOf(pattern);
    while (offset >= 0) {
      const context = bodyText.slice(Math.max(0, offset - 80), offset + pattern.length + 160);
      const phone = normalizePhone(context);
      if (phone) {
        return phone;
      }
      offset = bodyText.toLowerCase().indexOf(pattern, offset + pattern.length);
    }
  }
  return "";
}

function revealState(button) {
  const currentButton = button?.isConnected ? button : findPhoneButton();
  const metrics = phoneSurfaceMetrics();
  const buttonLabel = currentButton
    ? `${currentButton.innerText || currentButton.textContent || ""} ${currentButton.getAttribute("aria-label") || ""}`
        .trim()
        .toLowerCase()
    : "";
  return {
    phone: findPhone(),
    buttonPresent: Boolean(currentButton && isVisible(currentButton)),
    buttonLooksReveal: PHONE_BUTTON_RE.test(buttonLabel),
    buttonToken: buttonLabel,
    revealedSurface: metrics.revealedSurface,
    stateToken: `${metrics.surfaceCount}:${metrics.maskedCount}:${Number(metrics.temporaryLabel)}`
  };
}

function phoneSurfaceMetrics() {
  const runtime = globalThis.AVITO_CRM_RUNTIME_CORE;
  let surfaceCount = 0;
  let maskedCount = 0;
  const selectors = [
    'a[href^="tel:"]',
    '[data-marker*="phone" i]',
    '[class*="phone" i]',
    '[class*="contact" i]',
    '[role="dialog"]',
    '[aria-modal="true"]'
  ];
  for (const element of document.querySelectorAll(selectors.join(","))) {
    if (!isVisible(element)) {
      continue;
    }
    const label = `${element.innerText || element.textContent || ""} ${element.getAttribute("aria-label") || ""}`;
    if (PHONE_BUTTON_RE.test(label)) {
      continue;
    }
    if (
      normalizePhone(label) ||
      runtime.hasMaskedRussianPhone(label) ||
      label.toLowerCase().includes("временный номер") ||
      label.toLowerCase().includes("звонок через авито")
    ) {
      surfaceCount += 1;
    }
    if (runtime.hasMaskedRussianPhone(label)) {
      maskedCount += 1;
    }
  }
  const temporaryLabel = hasTemporaryNumberLabel();
  return {
    surfaceCount,
    maskedCount,
    temporaryLabel,
    revealedSurface: surfaceCount > 0 || maskedCount > 0
  };
}

function findPhoneButton() {
  const candidates = document.querySelectorAll(
    '[data-marker*="phone" i], button, a, [role="button"]'
  );
  for (const element of candidates) {
    const label = `${element.textContent || ""} ${element.getAttribute("aria-label") || ""}`;
    if (
      isVisible(element) &&
      PHONE_BUTTON_RE.test(label) &&
      !element.disabled &&
      element.getAttribute("aria-disabled") !== "true"
    ) {
      return element.closest('button, a, [role="button"]') || element;
    }
  }
  return null;
}

async function waitForPhoneButton(timeoutMs) {
  const deadline = Date.now() + Math.max(1000, timeoutMs);
  while (Date.now() < deadline) {
    const button = findPhoneButton();
    if (button) {
      return button;
    }
    await delay(250);
  }
  return null;
}

async function waitForTargetReady(expectedUrl, timeoutMs) {
  const waitMs = Math.max(3000, Math.min(15000, Number(timeoutMs) || 10000));
  const deadline = Date.now() + waitMs;
  while (Date.now() < deadline) {
    if (manualReason()) {
      return true;
    }
    const probe = probePage(expectedUrl, "listing");
    if (probe.rendered && sameListingPath(location.href, expectedUrl)) {
      return true;
    }
    await delay(250);
  }
  return false;
}

function hasRenderedListingSurface() {
  if (inactiveReason() || findPhone() || findPhoneButton()) {
    return true;
  }
  const bodyText = (document.body?.innerText || "").trim();
  return (
    bodyText.length > 200 &&
    Array.from(document.querySelectorAll("h1")).some((heading) => isVisible(heading))
  );
}

function sameListingPath(actualUrl, expectedUrl) {
  return globalThis.AVITO_CRM_RUNTIME_CORE.sameListingIdentity(actualUrl, expectedUrl);
}

function probePage(expectedUrl, mode) {
  const manual = Boolean(manualReason());
  const auth = isAuthPage(location.href) || hasVisibleAuthDialog();
  const inactive = Boolean(inactiveReason());
  const visibleHeadings = Array.from(document.querySelectorAll("h1")).filter((heading) =>
    isVisible(heading)
  ).length;
  const bodyLength = (document.body?.innerText || "").trim().length;
  const rendered =
    manual ||
    auth ||
    inactive ||
    Boolean(findPhone()) ||
    Boolean(findPhoneButton()) ||
    (bodyLength > 200 && visibleHeadings > 0) ||
    (mode === "health" && bodyLength > 200);
  return {
    contentVersion: CONTENT_SCRIPT_VERSION,
    actualUrl: location.href,
    expectedUrl,
    readyState: document.readyState,
    rendered,
    manual,
    auth,
    inactive,
    bodyLength,
    visibleHeadings
  };
}

function normalizePhone(value) {
  return globalThis.AVITO_CRM_RUNTIME_CORE.normalizeRussianPhone(value);
}

function screenRegion(element) {
  const rect = element.getBoundingClientRect();
  const frameX = Math.max(0, (window.outerWidth - window.innerWidth) / 2);
  const frameY = Math.max(0, window.outerHeight - window.innerHeight - frameX);
  return {
    left: window.screenX + frameX + rect.left,
    top: window.screenY + frameY + rect.top,
    width: rect.width,
    height: rect.height,
    screenWidth: window.screen.width,
    screenHeight: window.screen.height,
    kind: "control"
  };
}

function captureRegion(fallbackElement, fallbackRegion) {
  const selectors = [
    '[role="dialog"]',
    '[aria-modal="true"]',
    '[data-marker*="modal" i]',
    '[data-marker*="popup" i]',
    '[data-marker*="phone" i]',
    '[class*="modal" i]',
    '[class*="popup" i]',
    '[class*="popover" i]'
  ];
  let selected = null;
  let selectedScore = 0;
  const seen = new Set();
  for (const element of document.querySelectorAll(selectors.join(","))) {
    if (seen.has(element) || !isVisible(element)) {
      continue;
    }
    seen.add(element);
    const rect = element.getBoundingClientRect();
    if (rect.width < 120 || rect.height < 40 || rect.width > window.innerWidth * 0.95) {
      continue;
    }
    const text = (element.innerText || element.textContent || "").toLowerCase();
    let score = 0;
    if (text.includes("временный номер")) score += 120;
    if (text.includes("звонок через авито")) score += 100;
    if (normalizePhone(text)) score += 140;
    if (element.matches('[role="dialog"], [aria-modal="true"]')) score += 60;
    if (fallbackElement && element.contains(fallbackElement)) score += 20;
    if (score > selectedScore || (score === selectedScore && selected && rect.width < selected.rect.width)) {
      selected = { element, rect };
      selectedScore = score;
    }
  }
  if (!selected || selectedScore < 60) {
    return fallbackRegion;
  }
  const region = screenRegion(selected.element);
  region.kind = selected.element.matches('[role="dialog"], [aria-modal="true"]')
    ? "dialog"
    : selected.rect.height > 180
      ? "panel"
      : "control";
  return region;
}

function isVisible(element) {
  const style = getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return (
    style.display !== "none" &&
    style.visibility !== "hidden" &&
    Number(style.opacity || "1") > 0 &&
    rect.width > 1 &&
    rect.height > 1
  );
}

function isVisibleInViewport(element) {
  if (!isVisible(element)) {
    return false;
  }
  const rect = element.getBoundingClientRect();
  return (
    rect.bottom > 0 &&
    rect.right > 0 &&
    rect.top < window.innerHeight &&
    rect.left < window.innerWidth
  );
}

globalObject.AVITO_CRM_CONTENT_SCRIPT_VERSION = CONTENT_SCRIPT_VERSION;
})(globalThis);
