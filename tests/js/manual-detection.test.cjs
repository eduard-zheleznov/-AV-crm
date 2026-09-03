"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const content = fs.readFileSync(
  path.join(__dirname, "..", "..", "chrome-extension", "content.js"),
  "utf8"
);

const context = {
  chrome: { runtime: { onMessage: { addListener() {} } } },
  document: {
    body: { innerText: "" },
    title: "",
    querySelectorAll() {
      return [];
    }
  },
  location: { href: "https://www.avito.ru/moskva/test_123456" },
  window: { innerWidth: 1200, innerHeight: 700 },
  getComputedStyle() {
    return { display: "block", visibility: "visible", opacity: "1" };
  },
  setTimeout,
  clearTimeout,
  URL,
  AVITO_CRM_READINESS: {}
};
vm.createContext(context);
vm.runInContext(content, context);

function classify({ text = "", button = false, phone = false, surface = false, url = "" }) {
  context.testText = text;
  context.testButton = button;
  context.testPhone = phone;
  context.testSurface = surface;
  context.testUrl = url || "https://www.avito.ru/moskva/test_123456";
  return vm.runInContext(
    `(() => {
      pageText = () => testText;
      findPhoneButton = () => testButton ? {} : null;
      findPhone = () => testPhone ? "+79991234567" : "";
      hasVisibleChallengeSurface = () => testSurface;
      hasVisibleAuthDialog = () => false;
      location.href = testUrl;
      return manualReason();
    })()`,
    context
  );
}

assert.equal(
  classify({ text: "необычная активность", button: true }),
  "",
  "a rendered phone control must prevent a page-wide false CAPTCHA"
);
assert.equal(
  classify({ text: "не можем показать телефон", button: true }),
  "",
  "a temporary reveal error is not a CAPTCHA"
);
assert.equal(
  classify({ text: "не можем показать телефон\nнеобычная активность" }),
  "",
  "an explicit reveal error must win over unrelated page-wide challenge text"
);
assert.equal(
  classify({ text: "подтвердите, что вы не робот" }),
  "ручная проверка Avito"
);
assert.equal(
  classify({ text: "обычная страница", button: true, surface: true }),
  "ручная проверка Avito"
);
assert.equal(
  classify({ url: "https://www.avito.ru/challenge?return=/123456" }),
  "ручная проверка Avito"
);

console.log("manual-detection: ok");
