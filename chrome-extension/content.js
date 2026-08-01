const CHALLENGE_PATTERNS = [
  "доступ временно ограничен",
  "доступ ограничен",
  "проблема с ip",
  "подтвердите, что вы не робот",
  "пройдите проверку",
  "captcha",
  "слишком много запросов",
  "необычная активность"
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

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
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
      return { status: "screenshot" };
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
      return { status: "screenshot", crop: phoneRegion };
    }
    await delay(500);
  }
  return { status: "screenshot", crop: phoneRegion };
}

async function waitForManualAction(timeoutMs) {
  const initialReason = manualReason();
  if (!initialReason) {
    return null;
  }
  notifyStatus("manual_required", initialReason);
  const deadline = Date.now() + Math.max(1000, timeoutMs);
  while (Date.now() < deadline) {
    await delay(1000);
    if (!manualReason()) {
      notifyStatus("manual_cleared", initialReason);
      await delay(500);
      return null;
    }
  }
  return {
    status: "manual_timeout",
    reason: `${initialReason} не завершена за отведённое время`
  };
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
    CHALLENGE_PATTERNS.some((pattern) => content.includes(pattern))
  ) {
    return "ручная проверка Avito";
  }
  if (isAuthPage(currentUrl) || hasVisibleAuthDialog()) {
    return "авторизация Avito";
  }
  return "";
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
    screenHeight: window.screen.height
  };
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
