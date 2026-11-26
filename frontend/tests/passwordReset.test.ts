import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ApiError } from "../src/lib/api.ts";
import {
  RESEND_COOLDOWN_SECONDS, classifyConfirmError, classifyRequestError, cooldownLabel, passwordChecks,
  validateNewPassword, validateResetEmail,
} from "../src/lib/passwordReset.ts";

test("email validation", () => {
  assert.match(validateResetEmail("")!, /enter your email/i);
  assert.match(validateResetEmail("   ")!, /enter your email/i);
  assert.match(validateResetEmail("nope")!, /valid email/i);
  assert.match(validateResetEmail("a@b")!, /valid email/i);
  assert.equal(validateResetEmail("  owner@salon.com "), null);
});

test("new password: length, max, and confirmation must match", () => {
  assert.match(validateNewPassword("", "")!, /enter a new password/i);
  assert.match(validateNewPassword("short", "short")!, /at least 10/);
  assert.match(validateNewPassword("x".repeat(129), "x".repeat(129))!, /at most 128/);
  assert.match(validateNewPassword("long-enough-pw", "long-enough-px")!, /don't match/);
  assert.equal(validateNewPassword("long-enough-pw", "long-enough-pw"), null);
});

test("password checklist reflects length and match live", () => {
  const byId = (pw: string, c: string) => Object.fromEntries(passwordChecks(pw, c).map((x) => [x.id, x.ok]));
  assert.deepEqual(byId("", ""), { length: false, match: false }); // empty == empty is not 'matching'
  assert.deepEqual(byId("longenough1", ""), { length: true, match: false });
  assert.deepEqual(byId("longenough1", "longenough1"), { length: true, match: true });
  assert.deepEqual(byId("short", "short"), { length: false, match: true });
});

test("confirm errors: only 400 means 'request a new link'; others keep the form and say the password was not changed", () => {
  assert.equal(classifyConfirmError(new ApiError(400, "x")).kind, "invalid_link");
  assert.equal(classifyConfirmError(new ApiError(429, "x")).kind, "rate_limited");
  assert.equal(classifyConfirmError(new ApiError(422, "Password too weak")).message, "Password too weak");
  const server = classifyConfirmError(new ApiError(500, "boom"));
  assert.equal(server.kind, "network");
  assert.match(server.message, /not changed/i);
  const offline = classifyConfirmError(new TypeError("Failed to fetch"));
  assert.equal(offline.kind, "network");
  assert.match(offline.message, /not changed/i);
});

test("request errors never imply an email was sent", () => {
  assert.match(classifyRequestError(new TypeError("Failed to fetch")), /No email was sent/);
  assert.match(classifyRequestError(new ApiError(500, "x")), /No email was sent/);
  assert.match(classifyRequestError(new ApiError(422, "x")), /valid email/i);
});

test("resend cooldown label", () => {
  assert.equal(RESEND_COOLDOWN_SECONDS, 60);
  assert.equal(cooldownLabel(42), "Resend in 42s");
  assert.equal(cooldownLabel(0), "Resend email");
});

test("pages never swallow a failed request into a 'sent' state, and the reset page has the required controls", () => {
  const forgot = readFileSync(new URL("../src/app/forgot-password/page.tsx", import.meta.url), "utf8");
  // setSentTo only runs after the awaited request succeeded (inside try, after the await)
  assert.ok(forgot.indexOf("await api.requestPasswordReset") < forgot.indexOf("setSentTo(address)"));
  assert.ok(forgot.indexOf("setSentTo(address)") < forgot.indexOf("catch (err)"));
  const reset = readFileSync(new URL("../src/app/reset-password/page.tsx", import.meta.url), "utf8");
  for (const needle of ["confirm-password", "Show passwords", "Request a new link", "validateNewPassword"]) {
    assert.ok(reset.includes(needle), `reset page is missing ${needle}`);
  }
});
