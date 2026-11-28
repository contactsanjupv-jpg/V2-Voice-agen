/**
 * Real-browser tests (headless Chromium via Playwright) against a RUNNING Atla stack: the actual Next.js app talking to the actual
 * FastAPI app, Postgres and Redis. No provider is contacted: Paddle, Retell and Google are not exercised here (they need real
 * sandbox credentials; see docs/SECURITY_AUDIT_T12.md for what is and is not covered).
 *
 *   E2E_BASE_URL=http://localhost:3000  E2E_API_URL=http://localhost:8000  npm run e2e
 *
 * Password-reset needs a token the real flow only emails. E2E_TOKEN_CMD is a shell command that prints a fresh reset token for
 * the user id passed as $1 (see docs/E2E_TESTING.md). Without it that one test is skipped, never faked.
 * Use a scratch database. Every test signs up its own throw-away organization.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { chromium } from "playwright";

const WEB = process.env.E2E_BASE_URL;
const API = process.env.E2E_API_URL || "http://localhost:8000";
const TOKEN_CMD = process.env.E2E_TOKEN_CMD;
const skip = WEB ? false : "set E2E_BASE_URL to run the browser tests";
const PW = "correct-horse-battery-staple";
const uniq = () => `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;

let browser;
test.before(async () => { if (!skip) browser = await chromium.launch(); });
test.after(async () => { await browser?.close(); });

async function freshPage() {
  const context = await browser.newContext();
  const page = await context.newPage();
  const problems = [];
  page.on("pageerror", (e) => problems.push(String(e.message)));
  return { context, page, problems };
}

async function signUp(page, email = `e2e-${uniq()}@example.com`, org = `E2E ${uniq()}`) {
  await page.goto(`${WEB}/get-started`);
  await page.getByLabel("Business name").fill(org);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PW);
  await page.getByRole("button", { name: "Create account" }).last().click();
  await page.waitForURL("**/onboarding", { timeout: 20000 });
  return { email, org };
}

async function logIn(page, email, password) {
  await page.goto(`${WEB}/get-started`);
  await page.getByRole("button", { name: "Log in" }).first().click();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Log in" }).last().click();
}

test("signup creates the account and lands in onboarding; the dashboard then shows the honest not-live state", { skip }, async () => {
  const { context, page, problems } = await freshPage();
  const { org } = await signUp(page);
  await page.goto(`${WEB}/dashboard`);
  await page.getByText("Setup needed").first().waitFor();
  await page.getByText(org).first().waitFor();
  assert.ok(await page.getByText("No number yet").isVisible());
  assert.ok(await page.getByText("No plan yet").isVisible());
  assert.deepEqual(problems, []);
  await context.close();
});

test("signup validation and server errors are shown, not swallowed", { skip }, async () => {
  const { context, page } = await freshPage();
  await page.goto(`${WEB}/get-started`);
  await page.getByRole("button", { name: "Create account" }).last().click();
  await page.getByText("Enter your business name.").waitFor();
  const { email } = await signUp(page);
  const second = await context.newPage();  // same email again, different session
  await context.clearCookies();
  await second.goto(`${WEB}/get-started`);
  await second.getByLabel("Business name").fill(`Dup ${uniq()}`);
  await second.getByLabel("Email").fill(email);
  await second.getByLabel("Password").fill(PW);
  await second.getByRole("button", { name: "Create account" }).last().click();
  await second.getByRole("alert").first().waitFor();  // a visible error, and we did not navigate into the product
  assert.ok(!second.url().includes("/onboarding"));
  await context.close();
});

test("login: wrong password shows an error and stays out; right password gets in; sign out ends the session", { skip }, async () => {
  const { context, page } = await freshPage();
  const { email } = await signUp(page);
  await page.goto(`${WEB}/dashboard`);
  await page.getByRole("button", { name: "Sign out" }).first().click();
  await page.waitForURL(/get-started|\/$/, { timeout: 15000 });
  await logIn(page, email, "definitely-the-wrong-password");
  await page.getByRole("alert").first().waitFor();
  assert.ok(!page.url().includes("/dashboard") && !page.url().includes("/onboarding"));
  await logIn(page, email, PW);
  await page.waitForURL(/dashboard|onboarding/, { timeout: 20000 });
  await page.goto(`${WEB}/dashboard`);
  await page.getByText("Overview").first().waitFor();
  await context.close();
});

test("an expired or missing session sends the dashboard back to sign-in instead of showing someone's data or crashing", { skip }, async () => {
  const { context, page, problems } = await freshPage();
  await signUp(page);
  await page.goto(`${WEB}/dashboard`);
  await page.getByText("Overview").first().waitFor();
  await context.clearCookies();  // the session is gone
  await page.reload();
  await page.waitForURL(/get-started|login/, { timeout: 15000 });
  assert.ok(!(await page.content()).includes("E2E "), "no organization data may remain on screen");
  assert.deepEqual(problems, []);
  await context.close();
});

test("notification drawer opens as a dialog, closes with Escape, and preferences live inside it", { skip }, async () => {
  const { context, page, problems } = await freshPage();
  await signUp(page);
  await page.goto(`${WEB}/dashboard`);
  const bell = page.getByRole("button", { name: /^Notifications/ });
  await bell.click();
  const drawer = page.getByRole("dialog", { name: "Notifications" });
  await drawer.waitFor();
  await page.keyboard.press("Escape");
  await drawer.waitFor({ state: "detached" });
  await bell.click();
  await drawer.waitFor();
  await drawer.getByRole("button", { name: "Notification preferences" }).click();
  await page.getByTestId("notification-preferences").waitFor();
  assert.ok(page.url().includes("/dashboard"), "preferences are a drawer view, not a page");
  await drawer.getByRole("button", { name: "Back to notifications" }).click();
  await drawer.getByRole("tablist", { name: "Filter" }).waitFor();
  assert.deepEqual(problems, []);
  await context.close();
});

test("preferences: a switch changes, persists across reload and shows the critical-alert explanation where it applies", { skip }, async () => {
  const { context, page } = await freshPage();
  await signUp(page);
  await page.goto(`${WEB}/dashboard`);
  const open = async () => {
    await page.getByRole("button", { name: /^Notifications/ }).click();
    await page.getByRole("button", { name: "Notification preferences" }).click();
    await page.getByTestId("notification-preferences").getByRole("switch").first().waitFor();
  };
  await open();
  const panel = page.getByTestId("notification-preferences");
  const calls = panel.getByRole("switch", { name: "Calls notifications: on" });
  await calls.click();
  await panel.getByRole("switch", { name: "Calls notifications: off" }).waitFor();
  // a category that can raise critical alerts says so, and its label says it controls only the non-critical ones
  const billing = panel.getByRole("switch", { name: /Billing non-critical notifications: on/ });
  await billing.waitFor();
  assert.match(await panel.getByTestId("critical-note-billing").innerText(), /can't be muted and are always delivered/);
  await page.reload();
  await open();
  await page.getByTestId("notification-preferences").getByRole("switch", { name: "Calls notifications: off" }).waitFor();  // persisted by the server
  await context.close();
});

test("a second person's preferences are independent of the first person's", { skip }, async () => {
  const a = await freshPage();
  await signUp(a.page);
  await a.page.goto(`${WEB}/dashboard`);
  await a.page.getByRole("button", { name: /^Notifications/ }).click();
  await a.page.getByRole("button", { name: "Notification preferences" }).click();
  await a.page.getByRole("switch", { name: "Calls notifications: on" }).click();
  await a.page.getByRole("switch", { name: "Calls notifications: off" }).waitFor();
  const b = await freshPage();
  await signUp(b.page);
  await b.page.goto(`${WEB}/dashboard`);
  await b.page.getByRole("button", { name: /^Notifications/ }).click();
  await b.page.getByRole("button", { name: "Notification preferences" }).click();
  await b.page.getByRole("switch", { name: "Calls notifications: on" }).waitFor();
  await a.context.close();
  await b.context.close();
});

test("preferences show an error state and a retry when the server is unreachable, and a failed save puts the switch back", { skip }, async () => {
  const { context, page } = await freshPage();
  await signUp(page);
  await page.goto(`${WEB}/dashboard`);
  await page.route("**/notifications/preferences", (r) => r.abort());
  await page.getByRole("button", { name: /^Notifications/ }).click();
  await page.getByRole("button", { name: "Notification preferences" }).click();
  await page.getByText("We couldn't load your preferences.").waitFor();
  await page.unroute("**/notifications/preferences");
  await page.getByRole("button", { name: "Try again" }).click();
  await page.getByRole("switch", { name: "Calls notifications: on" }).waitFor();
  await page.route("**/notifications/preferences/calls", (r) => r.abort());
  await page.getByRole("switch", { name: "Calls notifications: on" }).click();
  await page.getByText("Couldn't save. Try again.").waitFor();
  await page.getByRole("switch", { name: "Calls notifications: on" }).waitFor();  // reverted
  await context.close();
});

test("forgot-password answers the same way for an unknown address and never claims success when the request fails", { skip }, async () => {
  const { context, page } = await freshPage();
  await page.goto(`${WEB}/forgot-password`);
  await page.getByLabel(/email/i).fill(`nobody-${uniq()}@example.com`);
  await page.getByRole("button", { name: /send|reset/i }).last().click();
  await page.getByRole("status").getByText(/If there's an Atla account for/).waitFor();
  await page.goto(`${WEB}/forgot-password`);
  await page.route("**/password-reset/request", (r) => r.abort());
  await page.getByLabel(/email/i).fill("someone@example.com");
  await page.getByRole("button", { name: /send|reset/i }).last().click();
  await page.getByRole("alert").first().waitFor();
  assert.equal(await page.getByText(/we've sent|If there's an Atla account/).count(), 0);
  await context.close();
});

test("reset-password: bad or missing token is refused with a clear way forward", { skip }, async () => {
  const { context, page } = await freshPage();
  await page.goto(`${WEB}/reset-password`);
  await page.getByText(/incomplete/).waitFor();
  await page.goto(`${WEB}/reset-password?token=${"x".repeat(43)}`);
  await page.getByLabel("New password", { exact: false }).first().fill("brand-new-passphrase-77");
  await page.getByLabel("Confirm", { exact: false }).first().fill("brand-new-passphrase-77");
  await page.getByRole("button", { name: "Set new password" }).click();
  await page.getByRole("alert").first().waitFor();
  await context.close();
});

test("reset-password end to end: new password works, old one and the old session do not", { skip: skip || (!TOKEN_CMD && "set E2E_TOKEN_CMD (see docs/E2E_TESTING.md)") }, async () => {
  const { context, page } = await freshPage();
  const { email } = await signUp(page);
  const me = await (await page.request.get(`${API}/api/v1/auth/me`)).json();
  const token = execFileSync("sh", ["-c", TOKEN_CMD, "sh", me.id], { encoding: "utf8" }).trim();
  const fresh = await freshPage();
  await fresh.page.goto(`${WEB}/reset-password?token=${encodeURIComponent(token)}`);
  await fresh.page.getByLabel("New password", { exact: false }).first().fill("brand-new-passphrase-77");
  await fresh.page.getByLabel("Confirm", { exact: false }).first().fill("brand-new-passphrase-77");
  await fresh.page.getByRole("button", { name: "Set new password" }).click();
  await fresh.page.getByText(/signed out everywhere/).waitFor();
  assert.equal((await page.request.get(`${API}/api/v1/auth/me`)).status(), 401);  // the first browser's session was revoked
  await logIn(fresh.page, email, PW);
  await fresh.page.getByRole("alert").first().waitFor();  // old password refused
  await logIn(fresh.page, email, "brand-new-passphrase-77");
  await fresh.page.waitForURL(/dashboard|onboarding/, { timeout: 20000 });
  await fresh.page.goto(`${fresh.page.url().startsWith(WEB) ? WEB : WEB}/reset-password?token=${encodeURIComponent(token)}`);  // single use
  await fresh.page.getByLabel("New password", { exact: false }).first().fill("another-long-passphrase-88");
  await fresh.page.getByLabel("Confirm", { exact: false }).first().fill("another-long-passphrase-88");
  await fresh.page.getByRole("button", { name: "Set new password" }).click();
  await fresh.page.getByRole("alert").first().waitFor();
  await context.close();
  await fresh.context.close();
});
