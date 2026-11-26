import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { criticalExplanation, initialPrefsFor, prefsReducer, PREFERENCES_NOTE, switchLabel, type NotificationPreference, type PrefsState } from "../src/lib/notificationPreferences";

const item = (category: NotificationPreference["category"], enabled = true): NotificationPreference => ({ category, enabled, critical_always_delivered: false });
const ready = (org = "o1"): PrefsState => prefsReducer(initialPrefsFor(org), { type: "loaded", org, items: [item("calls"), item("leads")] });

test("loading fills the list", () => {
  const s = ready();
  assert.equal(s.load, "ready");
  assert.equal(s.items.length, 2);
});

test("toggle is optimistic and a save confirms it", () => {
  let s = prefsReducer(ready(), { type: "toggle", org: "o1", category: "calls", enabled: false });
  assert.equal(s.items[0].enabled, false);
  assert.equal(s.saving.calls, true);
  s = prefsReducer(s, { type: "saved", org: "o1", item: item("calls", false) });
  assert.equal(s.saving.calls, false);
  assert.equal(s.items[0].enabled, false);
});

test("a failed save puts the switch back and flags the category", () => {
  let s = prefsReducer(ready(), { type: "toggle", org: "o1", category: "calls", enabled: false });
  s = prefsReducer(s, { type: "save-failed", org: "o1", category: "calls", previous: true });
  assert.equal(s.items[0].enabled, true);
  assert.equal(s.saveError, "calls");
  assert.equal(s.saving.calls, false);
});

test("switching organization drops everything, and late results from the old one are ignored", () => {
  let s = prefsReducer(ready("o1"), { type: "switch-org", orgId: "o2" });
  assert.deepEqual(s, initialPrefsFor("o2"));
  s = prefsReducer(s, { type: "loaded", org: "o1", items: [item("calls", false)] });
  assert.equal(s.items.length, 0);
  s = prefsReducer(s, { type: "toggle", org: "o1", category: "calls", enabled: false });
  assert.equal(s.items.length, 0);
  s = prefsReducer(s, { type: "save-failed", org: "o1", category: "calls", previous: true });
  assert.equal(s.saveError, null);
});

test("a failed load shows an error only when nothing is loaded", () => {
  assert.equal(prefsReducer(initialPrefsFor("o1"), { type: "load-failed", org: "o1" }).load, "error");
  assert.equal(prefsReducer(ready(), { type: "load-failed", org: "o1" }).load, "ready");
});

test("labels and the critical note", () => {
  assert.equal(switchLabel("Calls", false), "Calls notifications: off");
  assert.match(PREFERENCES_NOTE, /always delivered/);
});

test("a category with critical alerts: the switch name says it controls non-critical notifications only", () => {
  assert.equal(switchLabel("Billing", true, true), "Billing non-critical notifications: on");
  assert.equal(switchLabel("Billing", false, true), "Billing non-critical notifications: off");
  assert.doesNotMatch(switchLabel("Billing", true, true), /^Billing notifications/);
});

test("the critical explanation says critical alerts in that category cannot be muted", () => {
  const t = criticalExplanation("Billing");
  assert.match(t, /Critical billing alerts can't be muted/);
  assert.match(t, /always delivered/);
  assert.match(t, /controls the other billing notifications/);
});

test("muting a critical category only changes its switch; the critical flag is preserved and no other category moves", () => {
  const crit: NotificationPreference = { category: "billing", enabled: true, critical_always_delivered: true };
  let s = prefsReducer(initialPrefsFor("o1"), { type: "loaded", org: "o1", items: [crit, item("calls")] });
  s = prefsReducer(s, { type: "toggle", org: "o1", category: "billing", enabled: false });
  assert.deepEqual(s.items.map((p) => [p.category, p.enabled, p.critical_always_delivered]), [["billing", false, true], ["calls", true, false]]);
  s = prefsReducer(s, { type: "saved", org: "o1", item: { ...crit, enabled: false } });
  assert.equal(s.items[0].critical_always_delivered, true);
});

test("the preferences view renders the critical note and the explicit label from critical_always_delivered, and never offers a way to mute critical alerts", () => {
  const dir = dirname(fileURLToPath(import.meta.url));
  const ui = readFileSync(join(resolve(dir, "..", "src", "components", "dashboard"), "NotificationPreferences.tsx"), "utf8");
  assert.match(ui, /p\.critical_always_delivered &&[^]*criticalExplanation\(/);
  assert.match(ui, /switchLabel\([^)]*p\.critical_always_delivered\)/);
  assert.match(ui, /aria-describedby=\{p\.critical_always_delivered/);
  assert.match(ui, /PREFERENCES_NOTE/);
  assert.doesNotMatch(ui, /critical_always_delivered:\s*false|setEnabled\([^)]*critical/);
});

test("api calls are the caller's own, org-scoped, with a JSON body and no email preference", () => {
  const dir = dirname(fileURLToPath(import.meta.url));
  const api = readFileSync(join(resolve(dir, "..", "src", "lib"), "api.ts"), "utf8");
  assert.match(api, /orgs\/\$\{orgId\}\/notifications\/preferences`/);
  assert.match(api, /notifications\/preferences\/\$\{encodeURIComponent\(category\)\}`[^)]*method: "PUT"[^)]*JSON\.stringify\(\{ enabled \}\)/);
  const ui = readFileSync(join(resolve(dir, "..", "src", "components", "dashboard"), "NotificationPreferences.tsx"), "utf8");
  assert.match(ui, /role="switch"/);
  assert.doesNotMatch(ui, /email/i);
});
