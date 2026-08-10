"use strict";

const assert = require("node:assert/strict");
const retry = require("../../chrome-extension/reveal-retry-core.js");

assert.equal(retry.shouldRetry("click_no_effect", 1, 2), true);
assert.equal(retry.shouldRetry("click_no_effect", 2, 2), false);
assert.equal(retry.shouldRetry("phone_error", 1, 2), true);
assert.equal(retry.shouldRetry("screenshot", 1, 2), false);
assert.equal(retry.shouldRetry("phone", 1, 2), false);

assert.equal(retry.shouldCapture("click_no_effect"), true);
assert.equal(retry.shouldCapture("screenshot"), true);
assert.equal(retry.shouldCapture("phone"), false);
assert.equal(retry.shouldCapture("manual_required"), false);

console.log("reveal-retry-core: ok");
