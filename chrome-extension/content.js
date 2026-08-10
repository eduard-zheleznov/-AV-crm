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
const PHONE_BUTTON_RE = /(?:показать\s+(?:номер(?:\s+телефона)?|телефон)|позвонить)/i;
const PHONE_RE = /(?:\+7|8)[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}/g;
const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const READINESS = globalThis.AVITO_CRM_READINESS;

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "avito_crm_readiness_probe") {
    sendResponse(readinessProbe(message.expectedUrl));
    return false;
  }
  if (message?.type !== "avito_crm_reveal_once") {
    return false;
  }
  revealOnce(message)
    .then((result) => sendResponse(result))
    .catch((error) =>
      sendResponse({
        status: "error",
        reason: error?.message ? error.message.slice(0, 300) : String(error).slice(0, 300)
      })
    );
  return true;
});

async function revealOnce(command) {
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
    if (hasTemporaryNumberLabel()) {
      return { status: "screenshot", crop: captureRegion(null, null) };
    }
    return { status: "button_missing", reason: "Кнопка показа телефона не найдена" };
  }

  button.scrollIntoView({ behavior: "auto", block: "center", inline: "center" });
  await delay(750);
  const phoneRegion = screenRegion(button);
  button.focus({ preventScroll: true });
  notifyStatus("clicking", "кнопка показа телефона найдена");
  button.click();
  notifyStatus("clicked", "команда клика отправлена");

  const deadline = Date.now() + Math.max(3000, command.phoneWaitMs);
  while (Date.now() < deadline) {
    const afterClickManual = await waitForManualAction(command.manualTimeoutMs);
    if (afterClickManual) {
      return afterClickManual;
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
    if (hasTemporaryNumberLabel()) {
      await delay(500);
      const finalPhone = findPhone();
      if (finalPhone) {
        return { status: "phone", phone: finalPhone, source: "chrome-extension-dom" };
      }
      return postClickCaptureResult(button, phoneRegion);
    }
    await delay(500);
  }
  return postClickCaptureResult(button, phoneRegion);
}

function postClickCaptureResult(button, phoneRegion) {
  const crop = captureRegion(button, phoneRegion);
  const remainingButton = findPhoneButton();
  if (crop === phoneRegion && remainingButton && isVisibleInViewport(remainingButton)) {
    return {
      status: "click_no_effect",
      reason: "Кнопка показа телефона осталась закрытой после клика",
      crop
    };
  }
  return { status: "screenshot", crop };
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
  for (const link of document.querySelectorAll('a[href^="tel:"]')) {
    if (!isVisible(link)) {
      continue;
    }
    const fromHref = normalizePhone(link.getAttribute("href") || "");
    if (fromHref) {
      return fromHref;
    }
    const fromText = normalizePhone(link.textContent || "");
    if (fromText) {
      return fromText;
    }
  }

  const selectors = [
    '[data-marker*="phone" i]',
    '[aria-label*="телефон" i]',
    '[class*="phone" i]'
  ];
  for (const element of document.querySelectorAll(selectors.join(","))) {
    if (!isVisible(element)) {
      continue;
    }
    const phone = normalizePhone(element.textContent || element.getAttribute("aria-label") || "");
    if (phone) {
      return phone;
    }
  }
  const fromVisiblePage = normalizePhone(document.body?.innerText || "");
  if (fromVisiblePage) {
    return fromVisiblePage;
  }
  return "";
}

function findPhoneButton() {
  const candidates = document.querySelectorAll(
    '[data-marker*="phone" i], button, a, [role="button"]'
  );
  let firstVisibleControl = null;
  const seenControls = new Set();
  for (const element of candidates) {
    const label = `${element.textContent || ""} ${element.getAttribute("aria-label") || ""}`;
    if (
      isVisible(element) &&
      PHONE_BUTTON_RE.test(label) &&
      !element.disabled &&
      element.getAttribute("aria-disabled") !== "true"
    ) {
      const control = element.closest('button, a, [role="button"]') || element;
      if (seenControls.has(control)) {
        continue;
      }
      seenControls.add(control);
      if (!firstVisibleControl) {
        firstVisibleControl = control;
      }
      if (isVisibleInViewport(control)) {
        return control;
      }
    }
  }
  // Preserve the established scroll-to-control behavior when the page has no
  // matching phone control inside the current viewport.
  return firstVisibleControl;
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
  const deadline = Date.now() + Math.max(3000, Number(timeoutMs) || 10000);
  let stableSamples = 0;
  while (Date.now() < deadline) {
    const state = READINESS.classifyProbe(readinessProbe(expectedUrl), expectedUrl);
    if (state === "manual_required") {
      return true;
    }
    if (state === "actionable" || state === "rendered") {
      stableSamples += 1;
      if (stableSamples >= 2) {
        return true;
      }
    } else {
      stableSamples = 0;
    }
    await delay(250);
  }
  return false;
}

function readinessProbe(expectedUrl) {
  const manual = Boolean(manualReason());
  const inactive = Boolean(inactiveReason());
  const phone = Boolean(findPhone());
  const phoneButton = Boolean(findPhoneButton());
  const bodyText = (document.body?.innerText || "").trim();
  const listingShell =
    bodyText.length > 200 &&
    Array.from(document.querySelectorAll("h1")).some((heading) => isVisible(heading));
  return {
    actualUrl: location.href,
    expectedId: READINESS.listingId(expectedUrl),
    actualId: READINESS.listingId(location.href),
    readyState: document.readyState,
    manual,
    inactive,
    phone,
    phoneButton,
    actionable: inactive || phone || phoneButton,
    rendered: inactive || phone || phoneButton || listingShell
  };
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
  try {
    const actual = new URL(actualUrl);
    const expected = new URL(expectedUrl);
    const normalizePath = (value) => value.replace(/\/+$/, "");
    return (
      /(^|\.)avito\.ru$/i.test(actual.hostname) &&
      normalizePath(actual.pathname) === normalizePath(expected.pathname)
    );
  } catch (_error) {
    return false;
  }
}

function normalizePhone(value) {
  const match = String(value).match(PHONE_RE)?.[0];
  if (!match) {
    return "";
  }
  const digits = match.replace(/\D/g, "");
  if (digits.length !== 11 || (digits[0] !== "7" && digits[0] !== "8")) {
    return "";
  }
  return `+7${digits.slice(1)}`;
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
    kind: "control",
    viewport: {
      cssWidth: window.innerWidth,
      cssHeight: window.innerHeight,
      devicePixelRatio: window.devicePixelRatio || 1
    },
    region: {
      left: rect.left,
      top: rect.top,
      width: rect.width,
      height: rect.height,
      kind: "control"
    }
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
    if (seen.has(element) || !isVisibleInViewport(element)) {
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
  region.region.kind = region.kind;
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
