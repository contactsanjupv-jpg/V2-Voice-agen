import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ApiError } from "../src/lib/api.ts";
import {
  GOOGLE_ERROR_CODES, classifyCompleteError, googleErrorMessage, readEmailLinkToken, googleStartUrl, validateBusinessName,
} from "../src/lib/googleAuth.ts";

// Whitespace-insensitive, so reformatting a chained call does not break these source checks.
const read = (p: string) => readFileSync(new URL(p, import.meta.url), "utf8").replace(/\s+/g, "");

test("every backend error code has a distinct, human message; no error means no banner", () => {
  assert.equal(googleErrorMessage(undefined), null);
  assert.equal(googleErrorMessage(""), null);
  const messages = GOOGLE_ERROR_CODES.map((c) => googleErrorMessage(c));
  assert.ok(messages.every((m) => typeof m === "string" && m.length > 20));
  assert.equal(new Set(messages).size, GOOGLE_ERROR_CODES.length);
  assert.match(googleErrorMessage("cancelled")!, /cancel/i);
  assert.match(googleErrorMessage("unverified_email")!, /verif/i);
  assert.match(googleErrorMessage("not_configured")!, /isn't set up/i);
});

test("an unknown or hostile google_error value never reaches the page", () => {
  const hostile = "<img src=x onerror=alert(1)>";
  const msg = googleErrorMessage(hostile)!;
  assert.ok(!msg.includes("<") && !msg.includes("onerror"));
  assert.equal(msg, googleErrorMessage("failed"));
  assert.equal(googleErrorMessage(["cancelled", "x"]), googleErrorMessage("cancelled")); // repeated ?google_error=
});

test("start URL points at the backend and tolerates a trailing slash", () => {
  assert.equal(googleStartUrl("http://localhost:8000"), "http://localhost:8000/api/v1/auth/google/start");
  assert.equal(googleStartUrl("https://api.example.com//"), "https://api.example.com/api/v1/auth/google/start");
});

test("business name validation", () => {
  assert.match(validateBusinessName("")!, /enter your business name/i);
  assert.match(validateBusinessName("   ")!, /enter your business name/i);
  assert.match(validateBusinessName("x".repeat(256))!, /255/);
  assert.equal(validateBusinessName("  ABC Dental "), null);
});

test("completing sign-up: only an expired session sends them back; other failures keep the form and say nothing was created", () => {
  assert.equal(classifyCompleteError(new ApiError(400, "x")).kind, "expired");
  assert.equal(classifyCompleteError(new ApiError(404, "x")).kind, "expired");
  assert.equal(classifyCompleteError(new ApiError(422, "x")).kind, "invalid");
  assert.equal(classifyCompleteError(new ApiError(429, "x")).kind, "rate_limited");
  for (const err of [new ApiError(500, "boom"), new TypeError("Failed to fetch")]) {
    const c = classifyCompleteError(err);
    assert.equal(c.kind, "network");
    assert.match(c.message, /not created/i);
  }
});

test("the button only exists when the backend says Google is configured, and goes to the backend start URL", () => {
  const button = read("../src/components/GoogleButton.tsx");
  assert.ok(button.includes("api.authProviders()"));
  assert.ok(button.includes("available===false)returnnull"));
  assert.ok(button.includes("googleStartUrl(API_BASE)"));
  assert.ok(button.includes("ContinuewithGoogle"));
  assert.ok(!/client_secret|GOOGLE_CLIENT/i.test(button)); // no credentials in the browser bundle
});

test("login/signup shows the button and the Google error; the business step handles expiry", () => {
  const form = read("../src/components/AuthForm.tsx");
  assert.ok(form.includes("<GoogleButton/>") && form.includes("googleNotice"));
  const page = read("../src/app/get-started/page.tsx");
  assert.ok(page.includes("googleErrorMessage(") && page.includes("google_error"));
  const step = read("../src/app/get-started/google/page.tsx");
  for (const needle of ["api.googlePending()", "api.googleComplete(", "validateBusinessName", "classifyCompleteError", "Backtologin"]) {
    assert.ok(step.includes(needle), `business step is missing ${needle}`);
  }
});

// ---------------- account linking: proof of ownership, never an email match ----------------
import { classifyLinkError, googleLinkResult } from "../src/lib/googleAuth.ts";

test("link errors: a wrong password is retryable in place; only expiry restarts; nothing claims success", () => {
  assert.equal(classifyLinkError(new ApiError(401, "x")).kind, "wrong_password");
  assert.equal(classifyLinkError(new ApiError(401, "x")).message, "That password isn't correct.");
  assert.equal(classifyLinkError(new ApiError(429, "x")).kind, "rate_limited");
  assert.match(classifyLinkError(new ApiError(429, "x")).message, /Forgot your password/);
  assert.equal(classifyLinkError(new ApiError(400, "x")).kind, "expired");
  assert.match(classifyLinkError(new ApiError(400, "x")).message, /different browser/);
  assert.equal(classifyLinkError(new ApiError(404, "x")).kind, "expired");
  assert.equal(classifyLinkError(new ApiError(409, "Already linked")).message, "Already linked");
  for (const err of [new ApiError(500, "boom"), new TypeError("Failed to fetch")]) {
    const c = classifyLinkError(err);
    assert.equal(c.kind, "network");
    assert.match(c.message, /Nothing was connected/);
  }
});

test("Settings link results: success is only 'ok'; unknown values never echo", () => {
  assert.equal(googleLinkResult(null), null);
  assert.equal(googleLinkResult("ok")!.ok, true);
  for (const code of ["cancelled", "failed", "expired", "session_changed", "already_linked", "already_connected", "unverified_email", "rate_limited", "not_configured"]) {
    const r = googleLinkResult(code)!;
    assert.equal(r.ok, false, code);
    assert.ok(r.message.length > 20, code);
  }
  const hostile = googleLinkResult("<script>alert(1)</script>")!;
  assert.equal(hostile.ok, false);
  assert.ok(!hostile.message.includes("<"));
});

test("the Google step asks for a password or a mailbox, and the emailed token is used exactly once", () => {
  const page = read("../src/app/get-started/google/page.tsx");
  for (const needle of ["api.googleLinkConfirm(", "api.googleLinkEmail(", "api.googleLinkVerify(", "case\"link_password\"", "case\"link_email\"",
    "Forgotyourpassword?", "started.current", "inthissamebrowser"]) {
    assert.ok(page.includes(needle), `google step is missing ${needle}`);
  }
  // a wrong password keeps the form (no navigation, no reset of the field) -> the only navigation is after success
  assert.ok(page.indexOf("awaitapi.googleLinkConfirm(password)") < page.indexOf('router.push("/onboarding")', page.indexOf("awaitapi.googleLinkConfirm")));
});

test("Settings connect control hides itself unless the server says Google is configured, and re-asks the password", () => {
  const c = read("../src/components/settings/views/GoogleConnect.tsx");
  assert.ok(c.includes("!status.configured)returnnull"));
  assert.ok(c.includes("api.googleStatus()") && c.includes("api.googleLinkStart(status!.has_password?password:null)"));
  assert.ok(!/client_secret|GOOGLE_CLIENT/i.test(c));
  assert.ok(read("../src/components/settings/views/SecurityView.tsx").includes("<GoogleConnect/>"));
});

// ---------------- signed-in passwordless linking: email proof, never a session alone ----------------
import { classifyVerifyStartError, linkStartOutcome, readLinkVerifyToken } from "../src/lib/googleAuth.ts";

test("start outcome: only a Google URL redirects; the email path never navigates; anything odd is refused", () => {
  assert.deepEqual(linkStartOutcome({ url: "https://accounts.google.com/o/oauth2/v2/auth?x=1" }), { kind: "redirect", url: "https://accounts.google.com/o/oauth2/v2/auth?x=1" });
  assert.deepEqual(linkStartOutcome({ verification: "email_sent" }), { kind: "email_sent" });
  for (const bad of [{}, { url: "https://evil.example/phish" }, { url: "javascript:alert(1)" }, { url: "http://accounts.google.com/x" }, { verification: "done" }]) {
    assert.deepEqual(linkStartOutcome(bad), { kind: "unexpected" }, JSON.stringify(bad));
  }
});

test("only a well-formed token is read from the emailed link", () => {
  assert.equal(readLinkVerifyToken("?google_link_verify=" + "A".repeat(43)), "A".repeat(43));
  assert.equal(readLinkVerifyToken("?google_link_verify=abc_DEF-123456"), "abc_DEF-123456");
  for (const bad of ["", "?x=1", "?google_link_verify=", "?google_link_verify=short", "?google_link_verify=<script>alert(1)</script>", "?google_link_verify=" + "A".repeat(201)]) {
    assert.equal(readLinkVerifyToken(bad), null, bad);
  }
});

test("verify-start errors are specific and never claim success", () => {
  assert.match(classifyVerifyStartError(new ApiError(400, "x")), /different browser or session/);
  assert.match(classifyVerifyStartError(new ApiError(401, "x")), /log in/i);
  assert.equal(classifyVerifyStartError(new ApiError(409, "Has a password now")), "Has a password now");
  assert.match(classifyVerifyStartError(new ApiError(429, "x")), /Too many/);
  for (const err of [new ApiError(500, "boom"), new TypeError("Failed to fetch")]) assert.match(classifyVerifyStartError(err), /Nothing was connected/);
});

test("Settings finishes the emailed link exactly once, strips the token, and only navigates to what the server says is Google", () => {
  const c = read("../src/components/settings/views/GoogleConnect.tsx");
  for (const needle of ["started.current", "readLinkVerifyToken(window.location.search)", "api.googleLinkVerifyStart(token)", "linkStartOutcome(",
    'outcome.kind==="redirect"', "history.replaceState", "Emailmeaconfirmationlink", "inthissamebrowser"]) {
    assert.ok(c.includes(needle), `GoogleConnect is missing ${needle}`);
  }
  // the token is removed from the address bar BEFORE the request is sent
  assert.ok(c.indexOf("started.current=true;window.history.replaceState") !== -1);
  assert.ok(c.indexOf("window.history.replaceState(null") < c.indexOf("api.googleLinkVerifyStart(token)"));
});

test("the emailed ?verify= token is read only when token-shaped", () => {
  const t = "abcDEF123_-abcDEF123_-abcDEF123_-abcDEF123_-xyz";
  assert.equal(readEmailLinkToken(`?verify=${t}`), t);
  assert.equal(readEmailLinkToken(`?a=1&verify=${t}`), t);
  for (const bad of ["", "?", "?verify=", "?verify=short", "?verify=<script>alert(1)</script>", `?verify=${"x".repeat(201)}`, `?google_link_verify=${t}`])
    assert.equal(readEmailLinkToken(bad), null, bad);
});

test("the emailed token is removed from the address bar before it is sent, and sent exactly once", () => {
  const page = read("../src/app/get-started/google/page.tsx");
  const strip = 'window.history.replaceState(null,"",window.location.pathname+window.location.hash)';
  const start = page.indexOf("functionVerifyEmailLink");
  const end = page.indexOf("exportdefaultfunctionGoogleStep");
  const link = page.slice(start, end);
  // once-only guard first, then the strip, then the request
  const guard = link.indexOf("if(started.current)return;started.current=true;");
  const stripAt = link.indexOf(strip);
  const send = link.indexOf("api.googleLinkVerify(token)");
  assert.ok(guard !== -1 && stripAt > guard && send > stripAt, "guard -> strip -> request");
  assert.equal(link.split("api.googleLinkVerify(").length - 1, 1, "a single call site");
  // the token comes from the validated pure reader into state, not from the effect (which StrictMode runs twice)
  assert.ok(page.includes("useState<string|null>(()=>(typeofwindow===\"undefined\"?null:readEmailLinkToken(window.location.search)))"));
  assert.ok(!page.includes('.get("verify")'), "no raw token read left in the page");
});
